"""TEST DATA ONLY: Console API for the heterogeneous placement preview (#placement).

Reuses workload_console_fixture registrations and adds a second GPU fixture, a GPU
head workload and operator-imported topology snapshots. Every accelerator name,
node and link below is a labelled test fixture: link bandwidth/overhead values are
synthetic inputs chosen so the planner's ranking can be exercised. They are not
hardware measurements, interview evidence or training results. No worker or native
backend is configured; POST /placement-plans is read-only and nothing is reserved.

Preview (auto-connects to the fixture project; production API/DB untouched):
  rtk proxy uv run python examples/placement_console_fixture.py --host 127.0.0.1 --port 18433
"""

import argparse
import hashlib
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workload_console_fixture as base  # noqa: E402

from resource_advisor.api import Principal, create_app  # noqa: E402
from resource_advisor.contracts import now  # noqa: E402
from resource_advisor.placement import TopologySnapshot, import_snapshot  # noqa: E402
from resource_advisor.service import Service  # noqa: E402
from resource_advisor.store import Store  # noqa: E402

PROJECT = base.PROJECT
TOKEN = base.TOKEN
OTHER_PROJECT = "TEST-DATA-other-project"
FRESH = "TEST-DATA-topology-fresh"
EXPIRED = "TEST-DATA-topology-expired"
OTHER = "TEST-DATA-topology-other-project"
# A second discrete GPU node so the stage-to-stage link actually changes the choice.
base.DEVICES = {
    **base.DEVICES,
    "cuda-b": ("kubernetes", "gpu", "nvidia", "TEST RTX 4090 (fixture)", "cuda", 1),
}
WORKLOADS = {
    "fixture-image-classify": ("inference", ["cuda", "hailo", "mobilint"]),
    "fixture-gpu-head": ("inference", ["cuda", "cuda-b"]),
    # No training isolation binding is registered: the planner must show why it is blocked.
    "fixture-cnn-training": ("training", ["cuda"]),
    "fixture-cpu-benchmark": ("benchmark", ["slurm"]),
}
GBIT = 1e9 / 8  # bytes per second in one Gbit/s
# (source, target, bandwidth bytes/s, request overhead ms) -- TEST DATA, directed.
# Fast link with high per-request overhead vs slow link with low overhead: large
# transfers favour the first, many tiny transfers the second. Hailo has no link.
LINKS = (
    ("mobilint", "cuda", 10 * GBIT, 2.0),
    ("mobilint", "cuda-b", 1 * GBIT, 0.1),
)


def endpoint(key):
    backend, device, vendor, _, _, _ = base.DEVICES[key]
    return {
        "endpoint_ref": f"TEST-ep-{key}",
        "backend": backend,
        "cluster_ref": "fixture-slurm" if backend == "slurm" else "fixture-k8s",
        "node_ref": f"fixture-{key}-node",
        "physical_host_ref": f"TEST-host-{key}",
        "devices": []
        if device == "cpu"
        else [{"device_ref": f"TEST-{key}-device-0", "resource_key": f"{vendor}.example/{device}"}],
    }


def snapshot(ref, project, observed, expires):
    return TopologySnapshot.model_validate(
        {
            "ref": ref,
            "project_ref": project,
            "evidence_ref": ref + "-evidence",
            "measurement_method": "TEST DATA synthetic link table (not a hardware measurement)",
            "observed_at": observed,
            "expires_at": expires,
            "link_max_age_seconds": 86400,
            "endpoints": [endpoint(k) for k in ("cuda", "cuda-b", "hailo", "mobilint", "slurm")],
            "links": [
                {
                    "source_endpoint_ref": f"TEST-ep-{a}",
                    "target_endpoint_ref": f"TEST-ep-{b}",
                    "bandwidth_bytes_per_second": bandwidth,
                    "request_overhead_ms": overhead,
                    "measured_at": observed - timedelta(seconds=30),
                    "evidence_ref": f"{ref}-{a}-to-{b}",
                }
                for a, b, bandwidth, overhead in LINKS
            ],
        }
    )


def build(url="sqlite://", *, snapshots=True):
    store = Store(url)
    store.initialize()
    service = Service(
        store, accept_synthetic=True, operational_mode=True, console_workloads=tuple(WORKLOADS)
    )
    for ref, (task_type, devices) in WORKLOADS.items():
        base.register_workload(service, ref, task_type, devices)
    service.register(
        "scheduling_profile",
        base.profile(
            "fixture-default",
            "기본 정책 (TEST DATA)",
            allocation_modes=["physical_device", "virtual_slot", "cpu_only"],
        ),
        PROJECT,
    )
    service.register(
        "scheduling_profile",
        base.profile(
            "fixture-cpu-only",
            "CPU 전용 정책 (TEST DATA)",
            allocation_modes=["cpu_only"],
            backend_order=["slurm", "kubernetes"],
        ),
        PROJECT,
    )
    if snapshots:
        t = now()
        # Same operator import path as POST /topology-snapshots.
        import_snapshot(
            service, PROJECT, snapshot(FRESH, PROJECT, t - timedelta(minutes=1), t + timedelta(days=2))
        )
        import_snapshot(
            service, PROJECT, snapshot(EXPIRED, PROJECT, t - timedelta(hours=3), t - timedelta(hours=1))
        )
        # Another project's snapshot must never be listed for this project.
        import_snapshot(
            service,
            OTHER_PROJECT,
            snapshot(OTHER, OTHER_PROJECT, t - timedelta(minutes=1), t + timedelta(days=2)),
        )
    return service


def app(url="sqlite://", *, snapshots=True, preview=False, placement_api=True):
    service = build(url, snapshots=snapshots)
    application = create_app(
        service,
        {hashlib.sha256(TOKEN.encode()).hexdigest(): Principal(PROJECT)},
        anonymous_project=PROJECT if preview else None,
    )
    if not placement_api:
        # An older server without the placement routes: the real app answers 404.
        application.router.routes = [
            r
            for r in application.router.routes
            if not any(s in getattr(r, "path", "") for s in ("/topology-snapshots", "/placement-plans"))
        ]
    return application


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TEST DATA placement preview")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18433)
    args = parser.parse_args()
    database = tempfile.mkdtemp(prefix="placement-console-preview-") + "/fixture.db"
    print(f"TEST DATA preview: http://{args.host}:{args.port}/console#placement (db {database})")
    uvicorn.run(app("sqlite:///" + database, preview=True), host=args.host, port=args.port)
