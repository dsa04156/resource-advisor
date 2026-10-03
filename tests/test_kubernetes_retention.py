"""Scheduler doubles: durable terminal evidence, not hardware results."""

import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_backends import row
from test_worker import expire_retry

from resource_advisor.backends import BackendError, KubernetesBackend
from resource_advisor.contracts import State, now
from resource_advisor.kubernetes_retention import FINALIZER
from resource_advisor.store import outbox, usage
from resource_advisor.worker import Worker


class Scheduler:
    def __init__(self, job):
        self.obj = {
            "metadata": {
                "name": job["body"]["attempt_id"],
                "namespace": "research-a",
                "uid": "job-uid",
                "annotations": {
                    "resource-advisor/job-id": job["id"],
                    "resource-advisor/context-signature": job["body"]["context_signature"],
                },
            },
            "status": {},
        }
        self.pod = {
            "metadata": {
                "name": "attempt-pod",
                "namespace": "research-a",
                "uid": "pod-uid",
                "resourceVersion": "12",
                "finalizers": [FINALIZER, "example.test/other"],
                "ownerReferences": [{"kind": "Job", "uid": "job-uid"}],
            },
            "spec": {
                "containers": [
                    {
                        "name": "workload",
                        "resources": {
                            "requests": {"cpu": "500m", "memory": "1Gi", "nvidia.com/gpu": "1"}
                        },
                    }
                ]
            },
            "status": {
                "phase": "Running",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "True",
                        "lastTransitionTime": "2026-10-03T00:00:01Z",
                    }
                ],
                "containerStatuses": [
                    {
                        "name": "workload",
                        "state": {"running": {"startedAt": "2026-10-03T00:00:03Z"}},
                    }
                ],
            },
        }
        self.commands = []
        self.lose_patch_response = False

    def terminate(self, code=143):
        self.pod["status"].update(
            phase="Failed" if code else "Succeeded",
            containerStatuses=[
                {
                    "name": "workload",
                    "state": {
                        "terminated": {
                            "startedAt": "2026-10-03T00:00:03Z",
                            "finishedAt": "2026-10-03T00:00:08Z",
                            "exitCode": code,
                        }
                    },
                }
            ],
        )

    def execute(self, args, **kwargs):
        self.commands.append(args)
        if "create" in args:
            manifest = json.loads(kwargs["stdin"])
            assert manifest["spec"]["template"]["metadata"]["finalizers"] == [FINALIZER]
            return json.dumps(self.obj)
        if "delete" in args:
            assert "--cascade=foreground" in args
            self.obj["metadata"]["deletionTimestamp"] = "2026-10-03T00:00:06Z"
            return "deleted"
        if "patch" in args:
            patch = json.loads(args[-1])
            assert patch[:2] == [
                {"op": "test", "path": "/metadata/uid", "value": "pod-uid"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
            ]
            self.pod["metadata"]["finalizers"] = patch[-1]["value"]
            if self.lose_patch_response:
                raise BackendError("patch accepted; response lost")
            return "patched"
        if "pods" in args:
            return json.dumps({"items": [self.pod] if self.pod else []})
        if "pod" in args:
            return json.dumps(self.pod) if self.pod else ""
        return json.dumps(self.obj) if self.obj else ""


def setup(service):
    job = row(service)
    scheduler = Scheduler(job)
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        retain_termination_evidence=True,
        execute=scheduler.execute,
    )
    worker = Worker(service, {("team-a", job["body"]["backend_cluster_id"]): backend})
    worker.submit_one()
    worker.reconcile_all()
    return job, scheduler, backend, worker


@pytest.mark.parametrize("exit_code", [0, 1])
def test_delayed_job_condition_does_not_trigger_queue_timeout_or_early_result(service, exit_code):
    from test_worker import SchedulerDouble

    job, scheduler, backend, worker = setup(service)
    with service.store.transaction() as conn:
        latest = service.store.job(conn, job["id"])
        body = dict(latest["body"])
        body["queued_at"] = (
            now() - timedelta(seconds=body["spec"]["execution"]["max_queue_seconds"] + 1)
        ).isoformat()
        service.store.change_job(conn, latest, State.RUNNING, body)
    scheduler.terminate(exit_code)
    worker.reconcile_all()
    with service.store.transaction() as conn:
        current = service.store.job(conn, job["id"])
        assert current["state"] == State.RUNNING
        assert "collecting_since" not in current["body"]
        assert service.store.get(conn, "result", job["body"]["attempt_id"]) is None
        assert not conn.execute(select(usage)).first()
        assert not conn.execute(select(outbox).where(outbox.c.kind == "cancel")).first()
    assert not any("logs" in command for command in scheduler.commands)
    backend.result = SchedulerDouble().result
    scheduler.obj["status"]["conditions"] = [
        {"type": "Failed" if exit_code else "Complete", "status": "True"}
    ]
    worker.reconcile_all()
    with service.store.transaction() as conn:
        current = service.store.job(conn, job["id"])
        assert current["state"] == (State.FAILED if exit_code else State.SUCCEEDED)
        ledger = conn.execute(select(usage)).mappings().one()
        assert ledger["allocated_device_seconds"] == 7


def test_cancel_commits_exact_evidence_before_retryable_release(service):
    job, scheduler, backend, worker = setup(service)
    service.cancel("team-a", job["id"])
    worker.cancel_one()
    assert not worker.release_one()
    scheduler.terminate()
    worker.reconcile_all()
    with service.store.transaction() as conn:
        receipt = service.store.get(conn, "termination", job["body"]["attempt_id"])
        ledger = conn.execute(select(usage)).mappings().one()
        assert ledger["allocated_device_seconds"] == 7
        assert ledger["body"]["allocated_cpu_seconds"] == 3.5
        assert ledger["body"]["termination_receipt_ref"] == receipt["ref"]
        assert ledger["body"]["outcome"] == "CANCELED"
        assert receipt["body"]["container"]["exitCode"] == 143
    assert FINALIZER in scheduler.pod["metadata"]["finalizers"]
    restarted = Worker(service, worker.backends)
    scheduler.lose_patch_response = True
    restarted.release_one()
    assert scheduler.pod["metadata"]["finalizers"] == ["example.test/other"]
    expire_retry(service)
    restarted.release_one()
    with service.store.transaction() as conn:
        events = (
            conn.execute(select(outbox).where(outbox.c.kind == "release_termination"))
            .mappings()
            .all()
        )
        assert len(events) == 1 and events[0]["status"] == "DONE"
        assert len(conn.execute(select(usage)).all()) == 1
    assert sum("patch" in c for c in scheduler.commands) == 1


@pytest.mark.parametrize("mutation", ["job_uid", "annotation", "pod_owner", "namespace"])
def test_foreign_records_are_not_attached_or_released(service, mutation):
    job, scheduler, backend, worker = setup(service)
    if mutation == "job_uid":
        scheduler.obj["metadata"]["uid"] = "other"
    if mutation == "annotation":
        scheduler.obj["metadata"]["annotations"]["resource-advisor/job-id"] = "other"
    if mutation == "pod_owner":
        scheduler.pod["metadata"]["ownerReferences"][0]["uid"] = "other"
    if mutation == "namespace":
        scheduler.pod["metadata"]["namespace"] = "other"
    with pytest.raises(BackendError, match="ownership mismatch"):
        backend.status(worker._row(job["id"]))
    assert not worker.release_one()


def test_completed_during_cancel_is_collected_from_retained_pod(service):
    job, scheduler, backend, worker = setup(service)
    service.cancel("team-a", job["id"])
    worker.cancel_one()
    scheduler.terminate(code=0)
    observation = backend.status(worker._row(job["id"]))
    assert observation.state == State.COLLECTING
    assert observation.termination["container"]["exitCode"] == 0
    # The deleted Job may no longer be a log target; the retained Pod is.
    current = worker._row(job["id"])
    current = dict(current, body=Worker.observed_body(current, observation))
    calls = []
    backend.execute = lambda args, **kwargs: (
        calls.append(args) or 'RESOURCE_ADVISOR_RESULT {"result": "fixture"}'
    )
    assert backend.result(current)["result"] == "fixture"
    assert "pod/attempt-pod" in calls[0]


def test_terminal_job_waits_for_container_shutdown(service):
    job, scheduler, backend, worker = setup(service)
    scheduler.obj["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    worker.reconcile_all()
    assert worker._row(job["id"])["state"] == "RUNNING"
    scheduler.terminate()
    worker.reconcile_all()
    assert worker._row(job["id"])["state"] == "FAILED"
    assert worker.release_one()


def test_release_does_not_touch_recreated_or_live_pod(service):
    job, scheduler, backend, worker = setup(service)
    scheduler.terminate()
    receipt = backend.status(worker._row(job["id"])).termination
    original = copy.deepcopy(scheduler.pod)
    scheduler.pod["metadata"]["uid"] = "recreated"
    with pytest.raises(BackendError):
        backend.release_termination(receipt)
    scheduler.pod = original
    scheduler.pod["status"]["phase"] = "Running"
    with pytest.raises(BackendError):
        backend.release_termination(receipt)
    assert not any("patch" in c for c in scheduler.commands)


def test_retention_is_opt_in(service):
    job = row(service)
    backend = KubernetesBackend(namespace="a", local_queue="q", node_selector={"pool": "a"})
    assert "finalizers" not in backend.manifest(job)["spec"]["template"]["metadata"]


def test_failed_database_commit_cannot_release_terminal_evidence(service, monkeypatch):
    job, scheduler, backend, worker = setup(service)
    service.cancel("team-a", job["id"])
    worker.cancel_one()
    scheduler.terminate()
    original = service.store.put

    def unavailable(conn, kind, *args):
        if kind == "termination":
            raise BackendError("injected database receipt failure")
        return original(conn, kind, *args)

    monkeypatch.setattr(service.store, "put", unavailable)
    worker.reconcile_all()
    assert worker._row(job["id"])["state"] == "CANCEL_REQUESTED"
    assert not worker.release_one()
    assert FINALIZER in scheduler.pod["metadata"]["finalizers"]
    with service.store.transaction() as conn:
        assert not conn.execute(select(usage)).first()
    monkeypatch.setattr(service.store, "put", original)
    worker.reconcile_all()
    assert worker._row(job["id"])["state"] == "CANCELED"
    assert worker.release_one()
