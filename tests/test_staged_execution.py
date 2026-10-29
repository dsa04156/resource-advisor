"""End-to-end on this host: real staging bytes, real subprocess, frozen prediction → JCT.

The backend double runs the actual staged workload as a local subprocess with the
real adapter's identity environment, plus Downward-API-shaped Pod identity that it
also reports as its own Pod observation. It is local execution, not Kubernetes or
Slurm hardware evidence; the Kubernetes manifest itself is tested in test_backends.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from resource_advisor.backends import Observation, identity_environment, result_from_log
from resource_advisor.contracts import JobRequest, State, now
from resource_advisor.network_profile import CacheBinding, storage_identity
from resource_advisor.recommendation import (
    DataLocation,
    HardwareFingerprint,
    NodeAddress,
    RouteQueueSnapshot,
    decision_linkage,
    jct_profile_from_jobs,
    network_profile_from_receipts,
)
from resource_advisor.worker import Worker

HOST = socket.gethostname()
PAYLOAD = os.urandom(2 * 1024 * 1024)
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *args):
        pass


@pytest.fixture
def source():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/dataset.bin"
    httpd.shutdown()
    httpd.server_close()


class LocalProcessBackend:
    """Runs the real workload here as a 'Pod' it names; timestamps are its own."""

    def __init__(self, env, *, claimed_uid=None):
        self.env, self.runs, self.claimed_uid = env, {}, claimed_uid

    def validate(self, job):
        pass

    def submit(self, job):
        submitted = now()
        uid = str(uuid.uuid4())
        env = {
            **os.environ,
            **self.env,
            **identity_environment(job),  # the real adapter's env, incl. cache binding
            "RA_NODE_NAME": HOST,
            "RA_POD_NAME": "pod-" + job["id"],
            "RA_POD_UID": self.claimed_uid or uid,
        }
        started = now()
        done = subprocess.run(
            [sys.executable, "-m", "resource_advisor.staged_workload"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.runs[job["id"]] = (submitted, started, now(), done.stdout, uid)
        return "local-" + job["id"]

    def reconcile(self, job):
        return "local-" + job["id"] if job["id"] in self.runs else None

    def status(self, job):
        submitted, started, finished, _, uid = self.runs[job["id"]]
        return Observation(
            State.COLLECTING,
            submitted_at=submitted.isoformat(),
            started_at=started.isoformat(),
            execution_started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            placement={
                "source": "kubernetes:Pod",
                "node_name": HOST,
                "pod_name": "pod-" + job["id"],
                "pod_uid": uid,
            },
        )

    def result(self, job):
        return result_from_log(self.runs[job["id"]][3])

    def cancel(self, job):
        pass


@pytest.fixture
def local(service, bundle):
    spec, candidate, variant, cap = bundle
    cap = cap.model_copy(update={"ref": "local-cap", "node_ref": HOST})
    candidate = candidate.model_copy(update={"ref": "local", "capability_ref": cap.ref})
    variant = variant.model_copy(update={"ref": "staged-variant", "workload_ref": "staged-work"})
    spec = spec.model_copy(
        update={
            "ref": "staged-work",
            "candidates": (candidate.model_copy(update={"variant_ref": variant.ref}),),
            "baseline_candidate_ref": "local",
        }
    )
    service.operational_mode = True
    for kind, model in [("capability", cap), ("variant", variant), ("workload", spec)]:
        service.register(kind, model, "team-a")
    return spec, cap


def rows(service, job_ids):
    with service.store.transaction() as conn:
        out = []
        for job_id in job_ids:
            row = dict(service.store.job(conn, job_id))
            attempt = row["body"]["attempt_id"]
            row["result"] = service.store.get(conn, "result", attempt)["body"]
            staging = service.store.get(conn, "staging_receipt", attempt)
            row["staging"] = staging["body"] if staging else None
            out.append(row)
        return out


def dataset_env(source, cache):
    return {
        "RA_DATASET_SOURCE": source,
        "RA_DATASET_SHA256": DIGEST,
        "RA_DATASET_SIZE_BYTES": str(len(PAYLOAD)),
        "RA_DATASET_CACHE_DIR": str(cache),
        "RA_DATASET_SOURCE_ENDPOINT": "loopback-http",
    }


def run_fixed(service, worker, spec, key):
    job = service.submit("team-a", JobRequest(workload_ref=spec.ref, candidate_ref="local"), key)
    assert worker.submit_one()
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    return job["job_id"]


def hardware(cap):
    return HardwareFingerprint(
        accelerator_vendor=cap.accelerator_vendor,
        accelerator_model=cap.accelerator_model,
        device_class=cap.device_class,
        memory_model=cap.memory_model,
        allocation_mode=cap.allocation_mode,
        logical_slot=False,
    )


def test_staged_job_feeds_a_separate_staging_profile_and_a_measured_recommendation(
    service, local, source, tmp_path
):
    from test_heterogeneous_recommendation import scheduling_profile

    spec, cap = local
    cache = (tmp_path / "dest-cache").resolve()
    worker = Worker(service, {("team-a", "lab"): LocalProcessBackend(dataset_env(source, cache))})

    # 1. Profiling job: cold staging (actual bytes copied), then compute over them.
    [cold] = rows(service, [run_fixed(service, worker, spec, "p-1")])
    assert cold["staging"]["cache_state"] == "cold"
    assert cold["staging"]["sample"]["bytes"] == len(PAYLOAD)
    assert cold["staging"]["pod_uid"] == cold["body"]["placement"]["pod_uid"]

    # 2. Collector: staging is its own phase, subtracted from the residual.
    profile = jct_profile_from_jobs(
        [cold], ref="staged-profile", valid_for_seconds=3600, hardware=hardware(cap)
    )
    assert profile.staging_boundary == "separate"
    assert profile.phases["data_staging"] == cold["staging"]["sample"]["elapsed_seconds"]
    assert profile.phases["compute"] == cold["result"]["measurements"]["elapsed_seconds"]
    assert profile.phases["unattributed_in_container"] >= 0
    service.register("jct_profile", profile, "team-a")

    # 3. A second job reads the now-populated cache: hot evidence from its own
    #    receipt, identity-checked against the observed Pod, plus the cache binding.
    [hot] = rows(service, [run_fixed(service, worker, spec, "p-2")])
    measured = network_profile_from_receipts([hot], ref="net-local-hot", valid_for_seconds=3600)
    assert measured.cache_state == "hot" and measured.node_ref == HOST
    assert measured.identity_source == "ra_job_receipts" and measured.native_job_ids == (hot["id"],)
    service.register("network_profile", measured, "team-a")
    service.register(
        "cache_binding",
        CacheBinding(
            ref="local-cache",
            project_ref="team-a",
            backend="kubernetes",
            cluster_ref="lab",
            node_ref=HOST,
            cache_path=str(cache),
            storage_mode="persistent",
            storage_identity=storage_identity(cache),
            evidence_ref="local-stat",
        ),
        "team-a",
    )
    service.register(
        "data_location",
        DataLocation(
            ref="staged-data",
            project_ref="team-a",
            workload_ref=spec.ref,
            dataset_version=spec.identity.dataset_version,
            size_bytes=len(PAYLOAD),
            source_endpoint_ref="loopback-http",
            storage_kind="http",
            accessible_nodes=(NodeAddress(backend="kubernetes", cluster_ref="lab", node_ref=HOST),),
            provenance_ref="loopback-fixture",
            observed_at=now() - timedelta(seconds=1),
            content_digest="sha256:" + DIGEST,
        ),
        "team-a",
    )
    service.register(
        "route_queue_snapshot",
        RouteQueueSnapshot(
            ref="q-local",
            project_ref="team-a",
            backend="kubernetes",
            cluster_ref="lab",
            route_ref=HOST,
            resource_key=cap.resource_key,
            observed_at=now(),
            complete=True,
            capacity=1,
            source="local-process",
            evidence_ref="local-process",
        ),
        "team-a",
    )
    policy = scheduling_profile(
        "staged-measured", {"version": "hetero-rec-v2", "policy": "profile_queue_network"}
    )
    service.register("scheduling_profile", policy, "team-a")

    # 4. Automatic job: prediction frozen at acceptance; execution stages into the
    #    plan's bound cache (not the variant default) and ingest confirms it.
    request = JobRequest(workload_ref=spec.ref, scheduling_profile_ref=policy.ref)
    worker.backends[("team-a", "lab")].env["RA_DATASET_CACHE_DIR"] = str(tmp_path / "unbound")
    second = service.submit("team-a", request, "e-1")
    assert worker.submit_one()
    worker.reconcile_all()
    link = decision_linkage(service, "team-a", second["job_id"])
    assert link["state"] == "SUCCEEDED"
    [chosen] = link["recommendation_evidence"]["predictions"]
    staging = chosen["breakdown"]["staging"]
    assert staging["source"] == "measured_network" and staging["cache_state"] == "hot"
    assert staging["profile_refs"] == ["net-local-hot"]
    outcome = link["outcome"]
    assert outcome["staging_binding_check"]["status"] == "MATCHED"
    assert outcome["actual"]["status"] == "OBSERVED" and outcome["actual"]["jct_seconds"] > 0
    assert outcome["prediction"]["predicted_jct_seconds"] == chosen["predicted_jct_seconds"]
    assert outcome["error"]["signed_seconds"] == pytest.approx(
        outcome["actual"]["jct_seconds"] - chosen["predicted_jct_seconds"]
    )
    [executed] = rows(service, [second["job_id"]])
    assert executed["staging"]["cache_state"] == "hot"  # the job really read the bound cache
    assert executed["staging"]["cache_binding_ref"] == "local-cache"
    assert not (tmp_path / "unbound").exists()


def test_claimed_pod_identity_must_match_the_observed_pod(service, local, source, tmp_path):
    spec, cap = local
    backend = LocalProcessBackend(dataset_env(source, tmp_path / "c"), claimed_uid="forged-uid")
    worker = Worker(service, {("team-a", "lab"): backend})
    [row] = rows(service, [run_fixed(service, worker, spec, "f-1")])
    with pytest.raises(ValueError, match="remote"):
        network_profile_from_receipts([row], ref="x", valid_for_seconds=60)
    with pytest.raises(ValueError, match="remote"):
        jct_profile_from_jobs([row], ref="x", valid_for_seconds=60, hardware=hardware(cap))


def test_mixed_staged_and_unstaged_jobs_cannot_form_a_profile(service, local, source, tmp_path):
    spec, cap = local
    worker = Worker(
        service, {("team-a", "lab"): LocalProcessBackend(dataset_env(source, tmp_path))}
    )
    first, second = rows(
        service, [run_fixed(service, worker, spec, "m-1"), run_fixed(service, worker, spec, "m-2")]
    )
    with pytest.raises(ValueError, match="mixed staged"):
        jct_profile_from_jobs(
            [first, {**second, "staging": None}],
            ref="x",
            valid_for_seconds=60,
            hardware=hardware(cap),
        )


def test_kubernetes_adapter_supplies_scheduler_owned_identity_and_plan_binding(service):
    from test_backends import row as job_row

    from resource_advisor.backends import KubernetesBackend

    job = job_row(service)
    pod = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}
    ).manifest(job)
    env = pod["spec"]["template"]["spec"]["containers"][0]["env"]
    fields = {e["name"]: e["valueFrom"]["fieldRef"]["fieldPath"] for e in env if "valueFrom" in e}
    assert fields == {
        "RA_NODE_NAME": "spec.nodeName",
        "RA_POD_NAME": "metadata.name",
        "RA_POD_UID": "metadata.uid",
    }
    assert "RA_STAGING_BINDINGS_JSON" not in {e["name"] for e in env}  # legacy plan: none
    job["body"]["scheduling_plan"] = {
        "staging_bindings": {"test-worker": {"ref": "c", "cache_path": "/var/cache/ra"}}
    }
    bound = identity_environment(job)
    assert json.loads(bound["RA_STAGING_BINDINGS_JSON"])["test-worker"]["ref"] == "c"
