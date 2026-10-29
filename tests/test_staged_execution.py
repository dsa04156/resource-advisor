"""End-to-end on this host: real staging bytes, real subprocess, frozen prediction → JCT.

The backend here runs the actual staged workload as a local subprocess and reports
its own process timestamps. It is local execution, not Kubernetes/Slurm hardware
evidence; the adapters' contracts are unchanged and tested elsewhere.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from resource_advisor.backends import Observation, result_from_log
from resource_advisor.contracts import JobRequest, State, now
from resource_advisor.network_profile import main as network_cli
from resource_advisor.recommendation import (
    DataLocation,
    HardwareFingerprint,
    NodeAddress,
    RouteQueueSnapshot,
    decision_linkage,
    jct_profile_from_jobs,
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
    """Runs the real workload process here; timestamps are this process's own."""

    def __init__(self, env):
        self.env, self.runs = env, {}

    def validate(self, job):
        pass

    def submit(self, job):
        b = job["body"]
        submitted = now()
        env = {
            **os.environ,
            **self.env,
            "RA_JOB_ID": job["id"],
            "RA_ATTEMPT_ID": b["attempt_id"],
            "RA_EPOCH": str(b["epoch"]),
            "RA_WORKLOAD_SIGNATURE": b["workload_signature"],
            "RA_CONTEXT_SIGNATURE": b["context_signature"],
            "RA_WORK_UNITS": str(b["spec"]["identity"]["work_units"]),
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
        self.runs[job["id"]] = (submitted, started, now(), done.stdout)
        return "local-" + job["id"]

    def reconcile(self, job):
        return "local-" + job["id"] if job["id"] in self.runs else None

    def status(self, job):
        submitted, started, finished, _ = self.runs[job["id"]]
        return Observation(
            State.COLLECTING,
            submitted_at=submitted.isoformat(),
            started_at=started.isoformat(),
            execution_started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            placement={"node_name": HOST, "source": "local-process"},
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


def test_staged_job_feeds_a_separate_staging_profile_and_a_measured_recommendation(
    service, local, source, tmp_path, capsys
):
    from test_heterogeneous_recommendation import scheduling_profile

    spec, cap = local
    cache = tmp_path / "dest-cache"
    backend = LocalProcessBackend(
        {
            "RA_DATASET_SOURCE": source,
            "RA_DATASET_SHA256": DIGEST,
            "RA_DATASET_SIZE_BYTES": str(len(PAYLOAD)),
            "RA_DATASET_CACHE_DIR": str(cache),
            "RA_DATASET_SOURCE_ENDPOINT": "loopback-http",
        }
    )
    worker = Worker(service, {("team-a", "lab"): backend})

    # 1. Profiling job: cold staging (actual bytes copied), then compute over them.
    job = service.submit("team-a", JobRequest(workload_ref=spec.ref, candidate_ref="local"), "p-1")
    assert worker.submit_one()
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    [row] = rows(service, [job["job_id"]])
    assert row["staging"]["cache_state"] == "cold"
    assert row["staging"]["sample"]["bytes"] == len(PAYLOAD)

    # 2. Collector: staging is its own phase, subtracted from the residual.
    profile = jct_profile_from_jobs(
        [row],
        ref="staged-profile",
        valid_for_seconds=3600,
        hardware=HardwareFingerprint(
            accelerator_vendor=cap.accelerator_vendor,
            accelerator_model=cap.accelerator_model,
            device_class=cap.device_class,
            memory_model=cap.memory_model,
            allocation_mode=cap.allocation_mode,
            logical_slot=False,
        ),
    )
    assert profile.staging_boundary == "separate"
    assert profile.phases["data_staging"] == row["staging"]["sample"]["elapsed_seconds"]
    assert profile.phases["compute"] == row["result"]["measurements"]["elapsed_seconds"]
    assert profile.phases["unattributed_in_container"] >= 0
    service.register("jct_profile", profile, "team-a")

    # 3. Destination measurement of the same cache the next job will read (hot).
    code = network_cli(
        [
            "measure",
            "--source",
            source,
            "--sha256",
            DIGEST,
            "--size-bytes",
            str(len(PAYLOAD)),
            "--cache-dir",
            str(cache),
            "--mode",
            "hot",
            "--ref",
            "net-local-hot",
            "--project",
            "team-a",
            "--workload-ref",
            spec.ref,
            "--dataset-version",
            spec.identity.dataset_version,
            "--source-endpoint-ref",
            "loopback-http",
            "--backend",
            "kubernetes",
            "--cluster-ref",
            "lab",
            "--repeats",
            "3",
            "--max-bytes",
            str(4 * 1024 * 1024),
        ]
    )
    measured = json.loads(capsys.readouterr().out)
    assert code == 0, measured
    from resource_advisor.network_profile import NetworkProfile

    service.register("network_profile", NetworkProfile.model_validate(measured), "team-a")
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

    # 4. Automatic job: prediction frozen at acceptance, then actually executed.
    request = JobRequest(workload_ref=spec.ref, scheduling_profile_ref=policy.ref)
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
    assert outcome["actual"]["status"] == "OBSERVED" and outcome["actual"]["jct_seconds"] > 0
    assert outcome["prediction"]["predicted_jct_seconds"] == chosen["predicted_jct_seconds"]
    assert outcome["error"]["signed_seconds"] == pytest.approx(
        outcome["actual"]["jct_seconds"] - chosen["predicted_jct_seconds"]
    )
    [executed] = rows(service, [second["job_id"]])
    assert executed["staging"]["cache_state"] == "hot"  # the job really read the cache


def test_mixed_or_remote_staging_receipts_cannot_form_a_profile(service, local, source, tmp_path):
    spec, cap = local
    backend = LocalProcessBackend(
        {
            "RA_DATASET_SOURCE": source,
            "RA_DATASET_SHA256": DIGEST,
            "RA_DATASET_SIZE_BYTES": str(len(PAYLOAD)),
            "RA_DATASET_CACHE_DIR": str(tmp_path / "c"),
            "RA_DATASET_SOURCE_ENDPOINT": "loopback-http",
        }
    )
    worker = Worker(service, {("team-a", "lab"): backend})
    ids = []
    for key in ("m-1", "m-2"):
        ids.append(
            service.submit("team-a", JobRequest(workload_ref=spec.ref, candidate_ref="local"), key)[
                "job_id"
            ]
        )
        assert worker.submit_one()
        worker.reconcile_all()
    first, second = rows(service, ids)
    hardware = HardwareFingerprint(
        accelerator_vendor="nvidia",
        accelerator_model=cap.accelerator_model,
        device_class="gpu",
        memory_model="unified",
        allocation_mode="physical_device",
        logical_slot=False,
    )
    with pytest.raises(ValueError, match="mixed staged"):
        jct_profile_from_jobs(
            [first, {**second, "staging": None}], ref="x", valid_for_seconds=60, hardware=hardware
        )
    remote = {**second, "staging": {**second["staging"], "observer_host": "other-node"}}
    with pytest.raises(ValueError, match="remote"):
        jct_profile_from_jobs([first, remote], ref="x", valid_for_seconds=60, hardware=hardware)
