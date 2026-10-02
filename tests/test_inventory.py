import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import BackendError
from resource_advisor.contracts import now
from resource_advisor.inventory import (
    InventoryCollector,
    InventoryConfig,
    MetricBinding,
    fresh_view,
    latest_inventory,
    pod_request,
    quantity,
    save_inventory,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1500m", 1.5),
        ("25n", 25e-9),
        ("2Gi", 2 * 1024**3),
        ("250M", 250e6),
        ("2e3", 2000),
        ("0", 0),
    ],
)
def test_resource_units(raw, expected):
    assert float(quantity(raw)) == expected


def test_init_sidecars_and_overhead_are_reserved_once():
    def c(cpu, *, sidecar=False):
        return {
            "resources": {"requests": {"cpu": cpu}},
            **({"restartPolicy": "Always"} if sidecar else {}),
        }

    pod = {
        "spec": {
            "containers": [c("1"), c("500m")],
            "initContainers": [c("250m", sidecar=True), c("3"), c("500m", sidecar=True)],
            "overhead": {"cpu": "100m"},
        }
    }
    assert float(pod_request(pod, "cpu")) == 3.35  # max(2.25, 3.25) + overhead
    pod["status"] = {"resize": "Proposed"}
    with pytest.raises(ValueError, match="resize"):
        pod_request(pod, "cpu")


def source(*, pods_fail=False, metrics_time=None):
    timestamp = metrics_time or now().isoformat()

    def execute(args, **_):
        if "--raw" in args:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "node-a"},
                            "timestamp": timestamp,
                            "usage": {"cpu": "0", "memory": "1Gi"},
                        }
                    ]
                }
            )
        if "pods" in args:
            if pods_fail:
                raise BackendError("forbidden")
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"namespace": "another-team", "name": "private-job"},
                            "spec": {
                                "nodeName": "node-a",
                                "containers": [
                                    {
                                        "resources": {
                                            "requests": {
                                                "cpu": "1",
                                                "memory": "1Gi",
                                                "vendor/gpu.shared": "1",
                                            }
                                        }
                                    }
                                ],
                            },
                            "status": {"phase": "Running"},
                        },
                        {
                            "spec": {
                                "nodeName": "node-a",
                                "containers": [{"resources": {"requests": {"cpu": "99"}}}],
                            },
                            "status": {"phase": "Succeeded"},
                        },
                    ]
                }
            )
        if "nodes" in args:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "node-a"},
                            "spec": {},
                            "status": {
                                "capacity": {"cpu": "4", "memory": "8Gi", "vendor/gpu.shared": "2"},
                                "allocatable": {
                                    "cpu": "3",
                                    "memory": "7Gi",
                                    "vendor/gpu.shared": "2",
                                },
                                "nodeInfo": {"architecture": "arm64"},
                                "conditions": [{"type": "Ready", "status": "True"}],
                            },
                        }
                    ]
                }
            )
        return json.dumps({"items": []})

    return execute


def config(**kwargs):
    return InventoryConfig(
        project_ref="team-a",
        cluster_ref="lab",
        node_refs=("node-a",),
        resource_types={
            "vendor/gpu.shared": {"device_class": "gpu", "allocation_mode": "virtual_slot"}
        },
        **kwargs,
    )


def test_inventory_separates_measurement_and_request_headroom_without_pod_identity():
    snapshot = InventoryCollector(config(), execute=source()).collect()
    node = fresh_view(snapshot)["nodes"][0]
    assert node["resources"]["cpu"]["request_headroom"]["value"] == 2
    assert node["resources"]["vendor/gpu.shared"]["allocation_mode"] == "virtual_slot"
    assert node["resources"]["vendor/gpu.shared"]["request_headroom"]["value"] == 1
    assert node["telemetry"]["cpu_usage_cores"]["value"] == 0  # Measured zero is not missing.
    assert "another-team" not in json.dumps(snapshot) and "private-job" not in json.dumps(snapshot)
    assert snapshot["execution_qualification"].startswith("inventory never")


def test_failed_pod_listing_cannot_show_free_capacity():
    node = fresh_view(InventoryCollector(config(), execute=source(pods_fail=True)).collect())[
        "nodes"
    ][0]
    assert node["resources"]["cpu"]["capacity"]["value"] == 4
    assert node["resources"]["cpu"]["request_headroom"]["value"] is None


def test_stale_metric_and_stopped_collector_invalidate_values():
    stamp = (now() - timedelta(minutes=10)).isoformat()
    snapshot = InventoryCollector(config(), execute=source(metrics_time=stamp)).collect()
    view = fresh_view(snapshot)
    assert view["nodes"][0]["telemetry"]["cpu_usage_cores"]["status"] == "stale"
    assert view["nodes"][0]["telemetry"]["cpu_usage_cores"]["value"] is None
    later = fresh_view(snapshot, at=now() + timedelta(minutes=20))
    assert later["status"] == "stale"
    assert later["nodes"][0]["resources"]["cpu"]["request_headroom"]["value"] is None
    assert snapshot["nodes"][0]["telemetry"]["cpu_usage_cores"]["value"] == 0


@pytest.mark.parametrize(
    "value,health,stamp,expected",
    [
        (0, 1, 0, "ok"),
        (10, 0, 0, "collector_unhealthy"),
        ("NaN", 1, 0, "unavailable"),
        (101, 1, 0, "unavailable"),
        (20, 1, -600, "stale"),
    ],
)
def test_prometheus_uses_real_sample_time_and_health(value, health, stamp, expected):
    binding = MetricBinding(
        node_ref="node-a",
        name="utilization",
        unit="percent",
        query="load",
        timestamp_query="sample_timestamp",
        health_query="health",
    )

    def request(req):
        v = {"load": value, "health": health, "sample_timestamp": now().timestamp() + stamp}[
            req.url.params["query"]
        ]
        # Evaluation time is fresh even when the underlying sample is stale.
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"value": [now().timestamp(), str(v)]}],
                },
            },
        )

    with httpx.Client(
        base_url="http://metrics.invalid", transport=httpx.MockTransport(request)
    ) as client:
        snap = InventoryCollector(
            config(prometheus_url="http://metrics.invalid", metrics=(binding,)),
            execute=source(),
            client=client,
        ).collect()
    measured = fresh_view(snap)["nodes"][0]["telemetry"]["node:utilization"]
    assert measured["status"] == expected
    assert measured["value"] == (0 if expected == "ok" else None)


def test_inventory_api_is_project_scoped_and_history_is_immutable(service):
    snapshot = InventoryCollector(config(), execute=source()).collect()
    save_inventory(service.store, "team-a", snapshot)
    assert latest_inventory(service.store, "team-b", "lab") is None
    credentials = {
        hashlib.sha256(token.encode()).hexdigest(): Principal(project)
        for token, project in [("a", "team-a"), ("b", "team-b")]
    }
    with TestClient(create_app(service, credentials)) as client:
        assert client.get("/api/v1/compute/inventory/lab").status_code == 401
        assert (
            client.get(
                "/api/v1/compute/inventory/lab", headers={"Authorization": "Bearer b"}
            ).status_code
            == 404
        )
        response = client.get(
            "/api/v1/compute/inventory/lab", headers={"Authorization": "Bearer a"}
        )
        assert response.status_code == 200
        assert response.json()["ref"] == snapshot["ref"]
    with service.store.transaction() as conn:
        assert not service.store.list(conn, "capability", "team-b")
        assert service.store.get(conn, "inventory", snapshot["ref"])["body"] == snapshot


def test_missing_prometheus_credential_preserves_kubernetes_data(monkeypatch):
    monkeypatch.delenv("RA_TEST_MISSING_TOKEN", raising=False)
    binding = MetricBinding(
        node_ref="node-a",
        name="power",
        unit="watts",
        query="power",
        timestamp_query="timestamp(power)",
    )
    snapshot = InventoryCollector(
        config(
            prometheus_url="http://metrics.invalid",
            prometheus_token_env="RA_TEST_MISSING_TOKEN",
            metrics=(binding,),
        ),
        execute=source(),
    ).collect()
    node = fresh_view(snapshot)["nodes"][0]
    assert node["resources"]["cpu"]["request_headroom"]["value"] == 2
    assert node["telemetry"]["node:power"]["status"] == "unavailable"
    assert node["telemetry"]["node:power"]["value"] is None


@pytest.mark.parametrize("bounds", [{"minimum": 2, "maximum": 1}, {"maximum": float("inf")}])
def test_invalid_metric_bounds_rejected(bounds):
    with pytest.raises(ValueError):
        MetricBinding(
            node_ref="node-a",
            name="temperature",
            unit="celsius",
            query="temp",
            timestamp_query="timestamp(temp)",
            **bounds,
        )
