import json
from datetime import timedelta

import pytest
from test_backends import row
from test_service import complete

from resource_advisor.backends import KubernetesBackend, Observation, SlurmBackend
from resource_advisor.console import overview
from resource_advisor.contracts import JobRequest, State, now
from resource_advisor.operations import job_attention
from resource_advisor.worker import Worker


def test_operations_are_project_scoped_and_independent_of_table_filters(service):
    complete(service)
    for i in range(27):
        service.submit(
            "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), str(i)
        )
    view = overview(service, "team-a", jobs_status="failed")
    op = view["operations"]
    assert view["jobs"]["total"] == 0
    assert op["active_total"] == op["waiting_total"] == 27
    assert len(op["active_jobs"]) == 27
    assert op["usage"]["attempts"] == 1
    assert op["usage"]["queue_mean_seconds"] is None
    assert op["usage"]["queue_unknown_attempts"] == 1
    assert op["usage"]["groups"][0]["unknown_allocation_attempts"] == 1
    other = overview(service, "team-b")["operations"]
    assert other["active_jobs"] == [] and other["usage"]["groups"] == []


def test_recovered_observation_does_not_keep_old_alarm():
    earlier = now() - timedelta(minutes=1)
    job = {
        "state": "QUEUED",
        "scheduler_reason": "Priority",
        "last_observation_error_at": earlier.isoformat(),
        "backend_observed_at": now().isoformat(),
    }
    assert job_attention(job)["title"] == "앞선 우선순위 작업 대기"
    old = {"body": {"scheduler_reason": "Resources"}}
    assert Worker.observed_body(old, Observation(State.RUNNING))["scheduler_reason"] is None
    assert "중복 제출" in job_attention({"state": "SUBMISSION_UNKNOWN"})["action"]


@pytest.mark.parametrize("reason", ["Resources", "Priority", "QOSGrpGRES"])
def test_slurm_reports_owned_pending_reason(service, reason):
    job = row(service)
    job["body"]["external_id"] = "42"

    def execute(args, **kwargs):
        assert args[0] == "squeue" and args[-1].endswith("|%r")
        return f"42|{job['body']['attempt_id']}|team-a|gpu|PENDING|{reason}\n"

    backend = SlurmBackend(
        partition="gpu", account="team-a", qos="normal", output_dir="/tmp/ra", execute=execute
    )
    result = backend.status(job)
    assert result.state == State.QUEUED and result.scheduler_reason == reason


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "Unschedulable", "AdmissionPending"])
def test_kubernetes_pending_reason_is_observed_not_guessed(service, reason):
    job = row(service)
    job["body"]["external_id"] = "external-job"
    status = (
        {"containerStatuses": [{"name": "workload", "state": {"waiting": {"reason": reason}}}]}
        if reason == "ImagePullBackOff"
        else {
            "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]
        }
        if reason == "Unschedulable"
        else {}
    )
    obj = {"metadata": {"uid": "owned"}, "spec": {"suspend": reason == "AdmissionPending"}}
    pods = {"items": [{"metadata": {"ownerReferences": [{"uid": "owned"}]}, "status": status}]}
    backend = KubernetesBackend(
        namespace="team-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        execute=lambda args, **kwargs: json.dumps(pods if "pods" in args else obj),
    )
    result = backend.status(job)
    assert result.state == State.QUEUED and result.scheduler_reason == reason
