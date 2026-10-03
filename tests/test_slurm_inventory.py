import copy
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import BackendError
from resource_advisor.contracts import now
from resource_advisor.inventory import fresh_view, save_inventory
from resource_advisor.slurm_inventory import SlurmInventoryCollector, SlurmInventoryConfig


def config(*, controller=True, **overrides):
    values = {
        "project_ref": "team-a",
        "cluster_ref": "slurm-lab",
        "node_refs": ["host-a"],
        "node_names": {"host-a": "scheduler-a"},
        "gres_types": {
            "gres/gpu": {"device_class": "gpu", "allocation_mode": "physical_device"},
            "gres/npu": {"device_class": "npu", "allocation_mode": "physical_device"},
        },
    }
    if controller:
        values["controller"] = (
            controller
            if isinstance(controller, dict)
            else {"transport": "local", "account": "team-a", "partition": "gpu"}
        )
    values.update(overrides)
    return SlurmInventoryConfig.model_validate(values)


def node():
    # Source-schema fixture, not physical scheduler evidence.
    return {
        "name": "scheduler-a",
        "architecture": "aarch64",
        "version": "24.11.5",
        "cpus": 8,
        "effective_cpus": 6,
        "alloc_cpus": 2,
        "real_memory": 16384,
        "specialized_memory": 512,
        "alloc_memory": 4096,
        "state": ["MIXED"],
        "gres": "gpu:model:2,npu:1",
        "gres_drained": "",
        "tres": "cpu=8,mem=16384M,gres/gpu=2,gres/npu=1",
        "tres_used": "cpu=2,mem=4096M,gres/gpu=1",
        "address": "private-address",
        "reason": "another-user-private-reason",
    }


def envelope(key, rows):
    return {
        "meta": {"plugin": {"data_parser": "data_parser/v0.0.42"}},
        "errors": [],
        "warnings": [],
        key: rows,
    }


def source(*, nodes=None, jobs=None, fail=None, mutate=None):
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        key = "nodes" if argv[0] == "scontrol" else "jobs"
        assert argv[0] in {"scontrol", "squeue"} and "--json=v0.0.42" in argv
        assert kwargs["timeout"] == 12
        if key == "jobs":
            assert "--account=team-a" in argv and "--partition=gpu" in argv
        if key == fail:
            raise BackendError("deliberate source outage")
        payload = envelope(
            key,
            ([node()] if nodes is None else nodes)
            if key == "nodes"
            else ([] if jobs is None else jobs),
        )
        if mutate:
            mutate(payload)
        return json.dumps(payload)

    return execute, calls


def telemetry():
    return {
        "prometheus_url": "http://metrics.invalid",
        "metrics": [
            {
                "node_ref": "host-a",
                "name": "memory_available_bytes",
                "unit": "bytes",
                "query": "available",
                "timestamp_query": "stamp",
                "health_query": "health",
            }
        ],
    }


def metric_client(*, age=0, health=1):
    def request(req):
        value = {"available": 12345, "stamp": now().timestamp() - age, "health": health}[
            req.url.params["query"]
        ]
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"value": [now().timestamp(), str(value)]}],
                },
            },
        )

    return httpx.Client(base_url="http://metrics.invalid", transport=httpx.MockTransport(request))


def test_reported_allocations_remain_distinct_from_physical_telemetry_and_admission():
    execute, calls = source()
    snapshot = SlurmInventoryCollector(config(), execute=execute).collect()
    n = snapshot["nodes"][0]
    assert n["resources"]["cpu"]["capacity"]["value"] == 8
    assert n["resources"]["cpu"]["allocatable"]["value"] == 6
    assert n["resources"]["cpu"]["request_headroom"]["value"] == 4
    assert n["resources"]["memory"]["request_headroom"]["value"] == (16384 - 512 - 4096) * 1024**2
    assert n["resources"]["gres/gpu"]["requested"]["value"] == 1
    assert n["resources"]["gres/npu"]["requested"]["value"] is None
    assert n["resources"]["gres/npu"]["request_headroom"]["value"] is None
    assert "ready" not in n and n["scheduler_state"]["value"] == ["MIXED"]
    assert n["telemetry"] == {} and len(calls) == 2
    assert "private-address" not in json.dumps(snapshot)
    assert "another-user" not in json.dumps(snapshot)


@pytest.mark.parametrize("allocated", [None, -1, True, 1.5, "0", 2**64 - 1])
def test_missing_or_invalid_allocation_never_becomes_free_capacity(allocated):
    n = node()
    n["alloc_cpus"] = allocated
    execute, _ = source(nodes=[n])
    view = SlurmInventoryCollector(config(), execute=execute).collect()["nodes"][0]
    assert view["resources"]["cpu"]["capacity"]["value"] == 8
    assert view["resources"]["cpu"]["requested"]["value"] is None
    assert view["resources"]["cpu"]["request_headroom"]["value"] is None


def test_explicit_zero_allocation_and_empty_queue_are_observations():
    n = node()
    n["alloc_cpus"] = 0
    execute, _ = source(nodes=[n], jobs=[])
    s = SlurmInventoryCollector(config(), execute=execute).collect()
    assert s["nodes"][0]["resources"]["cpu"]["requested"]["value"] == 0
    assert s["slurm_queue"]["status"] == "ok" and s["slurm_queue"]["value"]["record_count"] == 0


@pytest.mark.parametrize("failure", ["nodes", "jobs"])
def test_scheduler_failures_do_not_erase_actual_host_telemetry(failure):
    execute, _ = source(fail=failure)
    with metric_client() as client:
        s = SlurmInventoryCollector(config(**telemetry()), execute=execute, client=client).collect()
    assert s["nodes"][0]["telemetry"]["node:memory_available_bytes"]["value"] == 12345
    assert s["sources"]["slurm_nodes" if failure == "nodes" else "slurm_queue"] == "unavailable"
    assert s["sources"]["slurm_queue" if failure == "nodes" else "slurm_nodes"] == "ok"
    if failure == "jobs":
        assert s["slurm_queue"]["value"] is None


def test_explicitly_unconfigured_controller_makes_no_scheduler_calls():
    def forbidden(*args, **kwargs):
        raise AssertionError("must not call a scheduler")

    with metric_client() as client:
        s = SlurmInventoryCollector(
            config(controller=False, **telemetry()), execute=forbidden, client=client
        ).collect()
    assert s["backend"] == "slurm" and s["slurm_queue"]["status"] == "not_configured"
    assert s["nodes"][0]["resources"] == {}
    assert s["nodes"][0]["scheduler_state"]["value"] is None
    assert s["nodes"][0]["telemetry"]["node:memory_available_bytes"]["status"] == "ok"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(warnings=["partial"]),
        lambda p: p.update(errors=[{"error": "failure"}]),
        lambda p: p["meta"]["plugin"].update(data_parser="data_parser/v0.0.99"),
    ],
)
def test_partial_or_unqualified_parser_cannot_report_zero_queue(mutation):
    execute, _ = source(mutate=mutation)
    s = SlurmInventoryCollector(config(), execute=execute).collect()
    assert s["slurm_queue"]["value"] is None and s["nodes"][0]["resources"] == {}


def test_duplicate_nodes_are_ambiguous_even_when_one_is_authorized():
    execute, _ = source(nodes=[node(), node()])
    s = SlurmInventoryCollector(config(), execute=execute).collect()
    assert s["sources"]["slurm_nodes"] == "unavailable"
    assert s["nodes"][0]["resources"] == {}


def job(identity, state, **changes):
    return {
        "job_id": identity,
        "account": "team-a",
        "partition": "gpu",
        "job_state": state,
        "name": "private-job-command",
        **changes,
    }


def test_queue_account_scope_and_base_flags_without_job_identity_leak():
    execute, _ = source(jobs=[job(1, ["PENDING"]), job(2, ["COMPLETING", "RUNNING"])])
    s = SlurmInventoryCollector(config(), execute=execute).collect()
    q = s["slurm_queue"]["value"]
    assert q["pending_records"] == 1 and q["running_records"] == 1
    assert q["array_tasks_expanded"] is False
    assert "private-job-command" not in json.dumps(s)


@pytest.mark.parametrize(
    "jobs",
    [
        [job(1, ["PENDING"], account="team-b")],
        [job(1, ["RUNNING"], partition="other")],
        [job(1, ["PENDING"]), job(1, ["RUNNING"])],
        [job(1, ["PENDING", "RUNNING"])],
        [job(1, ["UNKNOWN_FUTURE_STATE"])],
        [job(-1, ["RUNNING"])],
    ],
)
def test_ambiguous_or_foreign_queue_is_unknown(jobs):
    execute, _ = source(jobs=jobs)
    q = SlurmInventoryCollector(config(), execute=execute).collect()["slurm_queue"]
    assert q["status"] == "unavailable" and q["value"] is None


def test_missing_drain_information_cannot_become_allocatable_gres():
    n = node()
    del n["gres_drained"]
    execute, _ = source(nodes=[n])
    r = SlurmInventoryCollector(config(), execute=execute).collect()["nodes"][0]["resources"][
        "gres/gpu"
    ]
    assert r["capacity"]["value"] == 2 and r["allocatable"]["value"] is None
    assert r["request_headroom"]["value"] is None


def test_expiry_api_project_isolation_and_no_execution_side_effects(service):
    execute, _ = source()
    with metric_client(age=600) as client:
        s = SlurmInventoryCollector(config(**telemetry()), execute=execute, client=client).collect()
    original = copy.deepcopy(s)
    assert (
        fresh_view(s)["nodes"][0]["telemetry"]["node:memory_available_bytes"]["status"] == "stale"
    )
    expired = fresh_view(s, at=now() + timedelta(minutes=5))
    assert expired["slurm_queue"]["value"] is None
    assert expired["nodes"][0]["scheduler_state"]["value"] is None
    assert s == original
    with service.store.transaction() as conn:
        before = service.store.list(conn, "capability", "team-a")
    save_inventory(service.store, "team-a", s)
    credentials = {
        hashlib.sha256(token.encode()).hexdigest(): Principal(project)
        for token, project in [("a", "team-a"), ("b", "team-b")]
    }
    with TestClient(create_app(service, credentials)) as client:
        assert client.get("/api/v1/compute/inventory/slurm-lab").status_code == 401
        assert (
            client.get(
                "/api/v1/compute/inventory/slurm-lab", headers={"Authorization": "Bearer b"}
            ).status_code
            == 404
        )
        response = client.get("/api/v1/compute/overview", headers={"Authorization": "Bearer a"})
        assert response.status_code == 200 and response.json()["inventory"][0]["backend"] == "slurm"
    with service.store.transaction() as conn:
        assert service.store.list(conn, "capability", "team-a") == before


def test_ssh_route_is_pinned_and_cannot_include_shell_options():
    with pytest.raises(ValueError):
        config(
            controller={
                "transport": "ssh",
                "ssh_target": "-oProxyCommand=bad",
                "account": "team-a",
                "partition": "gpu",
            }
        )
    c = config(
        controller={
            "transport": "ssh",
            "ssh_target": "operator@slurm.example.invalid",
            "account": "team-a",
            "partition": "gpu",
        }
    )
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        raise BackendError("unreachable")

    SlurmInventoryCollector(c, execute=execute).collect()
    assert all(
        a[:3] == ["ssh", "-o", "BatchMode=yes"] and "StrictHostKeyChecking=yes" in a for a in calls
    )
    assert len(calls) == 2
