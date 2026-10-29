"""Measured-network recommendation and policy comparison through the public API.

NetworkProfiles here are software fixtures built on the contract (not CLI runs);
the measurement path itself is covered in test_network_measurement.py.
"""

import hashlib
from datetime import timedelta

import pytest
import test_heterogeneous_recommendation as hetero
from fastapi.testclient import TestClient
from test_heterogeneous_recommendation import (
    jct_profile,
    location,
    queue_snapshot,
    scheduling_profile,
)

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import now
from resource_advisor.hetero_experiment import run_comparison
from resource_advisor.network_profile import NetworkProfile
from resource_advisor.recommendation import RecommendationPolicy

URL = "/api/v1/compute"
DIGEST = "sha256:" + "ab" * 32
TOKENS = {
    "operator": Principal("team-a", True),
    "user": Principal("team-a"),
    "other-operator": Principal("team-b", True),
}

pair = hetero.pair  # shared fixture


def h(token, key=None):
    return {"Authorization": "Bearer " + token, **({"Idempotency-Key": key} if key else {})}


@pytest.fixture
def client(service, pair):
    keyed = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in TOKENS.items()}
    return TestClient(create_app(service, keyed))


def post(client, path, model, token="operator"):
    body = model if isinstance(model, dict) else model.model_dump(mode="json")
    response = client.post(URL + path, headers=h(token), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def network(node, seconds, *, ref=None, cache="cold", project="team-a", **change):
    body = {
        "ref": ref or f"net-{node}-{cache}",
        "project_ref": project,
        "workload_ref": "pair-work",
        "dataset_version": "fixture-v1",
        "content_digest": DIGEST,
        "size_bytes": 1_000_000,
        "source_endpoint_ref": "storage",
        "source_kind": "http",
        "source_locator": "http://storage.invalid/data.bin",
        "backend": "kubernetes",
        "cluster_ref": "lab",
        "node_ref": node,
        "observer_host": node,
        "cache_state": cache,
        "cache_locator": "/var/cache/ra",
        "observed_at": now() - timedelta(minutes=1),
        "valid_for_seconds": 3600,
        "samples": [
            {"elapsed_seconds": s, "bytes": 1_000_000, "content_verified": True} for s in seconds
        ],
        "limits": {"max_bytes": 2_000_000, "timeout_seconds": 30},
        "method": "fixture",
    }
    body.update(change)
    return NetworkProfile.model_validate(body)


def measured_location(spec, **change):
    body = location(spec, ["test-worker", "spare-node"]).model_dump(mode="json")
    body.update({"size_bytes": 1_000_000, "content_digest": DIGEST, **change})
    return body


NETWORK = {"version": "hetero-rec-v2", "policy": "profile_queue_network"}


def network_setup(client, pair, *, spare_profile=None, base_compute=2.0, staging="separate"):
    spec, variant, caps = pair
    phases = {"staging_boundary": staging}
    if staging != "separate":
        phases["phases"] = {"runtime_startup": 2.0, "compute": base_compute}
    post(client, "/scheduling-profiles", scheduling_profile("measured", NETWORK))
    post(
        client,
        "/jct-profiles",
        jct_profile(spec, variant, caps["base"], "base", base_compute, **phases),
    )
    post(client, "/jct-profiles", jct_profile(spec, variant, caps["spare"], "spare", 4.0))
    post(client, "/route-queue-snapshots", queue_snapshot("q-base", "test-worker"))
    post(client, "/route-queue-snapshots", queue_snapshot("q-spare", "spare-node"))
    post(client, "/data-locations", measured_location(spec))
    post(client, "/network-profiles", network("test-worker", [5.0, 6.0, 7.0]))
    post(client, "/network-profiles", spare_profile or network("spare-node", [0.5], cache="hot"))


def plan(client, profile="measured", **request):
    response = client.post(
        URL + "/scheduling-plans",
        headers=h("user"),
        json={"profile_ref": profile, "workload_ref": "pair-work", **request},
    )
    assert response.status_code == 200, response.text
    return response.json()


def by_candidate(result):
    evidence = result.get("recommendation_evidence") or result["plan"]["recommendation_evidence"]
    return {p["candidate_ref"]: p for p in evidence["predictions"]}


def test_measured_destination_access_replaces_profile_staging_once(client, pair):
    network_setup(client, pair)
    result = plan(client)
    # base: admission 1 + startup 2 + measured median 6 + compute 2 = 11
    # spare: 1 + 2 + hot cache 0.5 + 4 = 7.5; profile staging (1 s) is not added again.
    assert result["plan"]["candidate_ref"] == "spare"
    predictions = by_candidate(result)
    assert predictions["base"]["predicted_jct_seconds"] == 11.0
    assert predictions["spare"]["predicted_jct_seconds"] == 7.5
    spare = predictions["spare"]["breakdown"]
    assert spare["staging"]["source"] == "measured_network"
    assert spare["staging"]["cache_state"] == "hot"
    assert spare["staging"]["seconds"] == 0.5
    assert spare["queue_wait_seconds"] == 0.0 and spare["admission_seconds"] == 1.0
    assert spare["preparation_seconds"] == 2.0 and spare["compute_seconds"] == 4.0
    base = predictions["base"]["breakdown"]
    assert base["staging"]["samples"] == 3
    assert (base["staging"]["min_seconds"], base["staging"]["max_seconds"]) == (5.0, 7.0)
    for item in (spare, base):
        assert item["uncertainty"]["calibrated"] is False
        assert "queue_wait_spread" in item["uncertainty"]["missing"]
    # A single sample is never reported as zero uncertainty.
    assert "staging_spread" in spare["uncertainty"]["missing"]
    assert "staging_spread" not in base["uncertainty"]["missing"]
    record = result["plan"]["recommendation"]
    assert record["version"] == "hetero-rec-v2"
    assert record["network_profiles"]["spare"][0]["ref"] == "net-spare-node-hot"
    assert record["topology_snapshot"] is None


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"observed_at": now() - timedelta(days=2)}, "NETWORK_PROFILE_STALE"),
        ({"content_digest": "sha256:" + "cd" * 32}, "NETWORK_DATASET_MISMATCH"),
        ({"source_endpoint_ref": "other-storage"}, "NETWORK_PROFILE_MISSING"),
        ({"dataset_version": "fixture-v2"}, "NETWORK_PROFILE_MISSING"),
    ],
)
def test_stale_or_mismatched_measurements_stay_unknown(client, pair, bad, reason):
    network_setup(client, pair, spare_profile=network("spare-node", [0.5], **bad))
    result = plan(client)
    assert result["plan"]["candidate_ref"] == "base"
    spare = by_candidate(result)["spare"]
    assert spare["predicted_jct_seconds"] is None
    assert reason in spare["reasons"]


def test_another_projects_measurement_is_invisible(client, pair):
    other = network("spare-node", [0.5], project="team-b", ref="net-b")
    post(client, "/network-profiles", other, token="other-operator")
    network_setup(client, pair, spare_profile=network("spare-node", [9.0], ref="unused-elsewhere"))
    # team-a's own slow measurement is used; team-b's fast one never leaks in.
    spare = by_candidate(plan(client))["spare"]
    assert spare["breakdown"]["staging"]["profile_refs"] == ["unused-elsewhere"]
    response = client.post(
        URL + "/network-profiles", headers=h("operator"), json=other.model_dump(mode="json")
    )
    assert response.status_code == 422  # project_ref differs from the principal


def test_staging_inside_compute_abstains_instead_of_double_counting(client, pair):
    network_setup(client, pair, staging="inside_compute")
    base = by_candidate(plan(client))["base"]
    assert base["predicted_jct_seconds"] is None
    assert "STAGING_NOT_SEPARABLE" in base["reasons"]


def test_location_without_content_digest_cannot_match_measurements(client, pair):
    spec, variant, caps = pair
    post(client, "/scheduling-profiles", scheduling_profile("measured", NETWORK))
    post(client, "/jct-profiles", jct_profile(spec, variant, caps["base"], "base", 2.0))
    post(client, "/route-queue-snapshots", queue_snapshot("q-base", "test-worker"))
    post(client, "/data-locations", measured_location(spec, content_digest=None))
    post(client, "/network-profiles", network("test-worker", [1.0]))
    result = plan(client, candidate_ref="base")
    assert result["accepted"] is False
    assert "DATA_DIGEST_UNKNOWN" in by_candidate(result)["base"]["reasons"]


def test_measured_mode_is_an_explicit_version_and_v1_is_unchanged():
    legacy = RecommendationPolicy(policy="profile_queue_network", topology_snapshot_ref="topo-1")
    assert legacy.version == "hetero-rec-v1"
    with pytest.raises(ValueError):
        RecommendationPolicy(policy="profile_queue_network")
    with pytest.raises(ValueError):
        RecommendationPolicy(
            version="hetero-rec-v2", policy="profile_queue_network", topology_snapshot_ref="t"
        )
    assert "content_digest" not in location_without_digest()


def location_without_digest():
    from resource_advisor.recommendation import DataLocation, NodeAddress

    return DataLocation(
        ref="d",
        project_ref="team-a",
        workload_ref="w",
        dataset_version="v",
        size_bytes=1,
        source_endpoint_ref="s",
        storage_kind="nfs",
        accessible_nodes=(NodeAddress(backend="kubernetes", cluster_ref="lab", node_ref="n"),),
        provenance_ref="p",
        observed_at=now(),
    ).model_dump(mode="json")


class ApiClient:
    """Runner client protocol over the in-process app (no network, no auth bypass)."""

    def __init__(self, client):
        self.client = client

    def post(self, path, body, idempotency_key=None):
        response = self.client.post(URL + path, headers=h("user", idempotency_key), json=body)
        return response.json() | (
            {"http_status": response.status_code} if response.is_error else {}
        )

    def get(self, path):
        return self.client.get(URL + path, headers=h("user")).json()


def comparison(**change):
    return {
        "ref": "cmp-1",
        "workload_ref": "pair-work",
        "static_profile_ref": "legacy",
        "static_candidate_ref": "base",
        "profile_refs": {
            "profile_only": "only",
            "profile_queue": "queue",
            "profile_queue_network": "measured",
        },
        **change,
    }


def comparison_setup(client, pair):
    network_setup(client, pair)
    post(client, "/scheduling-profiles", scheduling_profile("legacy"))
    post(client, "/scheduling-profiles", scheduling_profile("only", {"policy": "profile_only"}))
    post(client, "/scheduling-profiles", scheduling_profile("queue", {"policy": "profile_queue"}))


def finish_with_native_timestamps(service, seconds=9.0):
    """Drive a submitted job to completion with a scheduler double (not hardware)."""
    from test_worker import SchedulerDouble

    from resource_advisor.backends import Observation
    from resource_advisor.contracts import State
    from resource_advisor.worker import Worker

    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})

    def settle(job_id):
        assert worker.submit_one()
        t0 = now() - timedelta(seconds=seconds + 1)
        backend.observation = Observation(
            State.COLLECTING,
            submitted_at=t0.isoformat(),
            started_at=(t0 + timedelta(seconds=1)).isoformat(),
            execution_started_at=(t0 + timedelta(seconds=2)).isoformat(),
            finished_at=(t0 + timedelta(seconds=seconds)).isoformat(),
        )
        worker.reconcile_all()

    return settle


def test_four_policies_are_frozen_as_shadow_estimates_then_linked_to_executed_jobs(
    client, pair, service
):
    comparison_setup(client, pair)
    frozen = run_comparison(ApiClient(client), comparison(), execute=False)
    assert frozen["kind"] == "shadow_estimates_frozen_before_submission"
    arms = frozen["arms"]
    assert list(arms) == ["static", "profile_only", "profile_queue", "profile_queue_network"]
    assert arms["static"]["selected_candidate_ref"] == "base"
    # profile_only: base 2+1+2=5 < spare 2+1+4=7; network: measured staging flips it.
    assert arms["profile_only"]["selected_candidate_ref"] == "base"
    assert arms["profile_queue_network"]["selected_candidate_ref"] == "spare"
    assert {c for a in arms.values() for c in a["candidate_pool"]} == {"base", "spare"}
    assert all(a["digest"].startswith("sha256:") for a in arms.values())
    assert frozen["executed"] == {arm: [] for arm in arms}

    # Arms run one after another and each settles first, so no arm's job is queued
    # demand in another arm's estimate.
    settle = finish_with_native_timestamps(service)
    linked = run_comparison(ApiClient(client), comparison(), execute=True, settle=settle)
    assert linked["frozen_digest"] == frozen["frozen_digest"]  # re-freeze is idempotent
    for arm, jobs in linked["executed"].items():
        assert len(jobs) == 1, (arm, jobs)
        job = jobs[0]
        assert job["kind"] == "independently_executed"
        assert job["state"] == "SUCCEEDED"
        assert job["candidate_ref"] == arms[arm]["selected_candidate_ref"]
        assert job["matches_frozen_selection"] is True
        assert job["outcome"]["actual"]["jct_seconds"] == pytest.approx(9.0)
        link = client.get(URL + f"/jobs/{job['job_id']}/recommendation", headers=h("user"))
        assert link.json()["comparison"] == {"ref": "cmp-1", "arm": arm}
    static = linked["executed"]["static"][0]["outcome"]
    assert static["prediction"] is None and static["error"] is None  # static: no estimate
    network = linked["executed"]["profile_queue_network"][0]["outcome"]
    assert network["error"]["signed_seconds"] == pytest.approx(9.0 - 7.5)


def test_comparison_rejects_an_ineligible_static_candidate_or_wrong_policy_arm(client, pair):
    comparison_setup(client, pair)
    response = client.post(
        URL + "/recommendation-comparisons",
        headers=h("user"),
        json=comparison(static_candidate_ref="missing"),
    )
    assert response.status_code == 422
    swapped = comparison(
        ref="cmp-2",
        profile_refs={
            "profile_only": "queue",
            "profile_queue": "only",
            "profile_queue_network": "measured",
        },
    )
    response = client.post(URL + "/recommendation-comparisons", headers=h("user"), json=swapped)
    assert response.status_code == 422
    other = client.get(URL + "/recommendation-comparisons/cmp-1", headers=h("other-operator"))
    assert other.status_code == 404
