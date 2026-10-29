"""Read views over JCT comparison evidence: comparison list and per-workload readiness."""

from datetime import timedelta

import test_heterogeneous_recommendation as hetero
import test_network_recommendation as network_rec
from test_network_recommendation import (
    URL,
    comparison,
    comparison_setup,
    h,
    network,
    post,
)

from resource_advisor.contracts import now

pair = hetero.pair  # shared fixtures
client = network_rec.client


def get(client, path, token="user"):
    response = client.get(URL + path, headers=h(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_comparisons_are_listed_per_project_and_workload(client, pair):
    comparison_setup(client, pair)
    frozen = post(client, "/recommendation-comparisons", comparison(), token="user")

    listed = get(client, "/recommendation-comparisons?workload_ref=pair-work")
    assert listed["count"] == 1
    assert listed["items"][0]["frozen_digest"] == frozen["frozen_digest"]
    assert listed["items"][0]["executed"] == {arm: [] for arm in frozen["arm_order"]}
    assert get(client, "/recommendation-comparisons?workload_ref=other-work")["count"] == 0
    assert get(client, "/recommendation-comparisons", token="other-operator")["items"] == []


def npu_workload(service, pair):
    """A different workload whose only candidate is a Mobilint-style NPU."""
    spec, variant, caps = pair
    cap = caps["base"].model_copy(
        update={
            "ref": "npu-cap",
            "node_ref": "npu-node",
            "accelerator_vendor": "mobilint",
            "accelerator_model": "ARIES2",
            "device_class": "npu",
            "resource_key": "mobilint.com/npu",
        }
    )
    identity = spec.identity.model_copy(update={"input_shape": (1, 3, 224, 224)})
    npu_variant = variant.model_copy(
        update={
            "ref": "npu-variant",
            "workload_ref": "npu-work",
            "workload_signature": hetero.signature(identity),
            "accelerator_vendor": "mobilint",
            "device_class": "npu",
        }
    )
    candidate = spec.candidates[0].model_copy(
        update={"ref": "aries", "capability_ref": cap.ref, "variant_ref": npu_variant.ref}
    )
    npu = spec.model_copy(
        update={
            "ref": "npu-work",
            "identity": identity,
            "candidates": (candidate,),
            "baseline_candidate_ref": "aries",
        }
    )
    for kind, model in [("capability", cap), ("variant", npu_variant), ("workload", npu)]:
        service.register(kind, model, "team-a")


def test_readiness_names_candidates_evidence_freshness_and_other_accelerators(
    client, pair, service
):
    comparison_setup(client, pair)
    stale = network("spare-node", [0.5], ref="net-old", observed_at=now() - timedelta(hours=2))
    post(client, "/network-profiles", stale)
    post(client, "/recommendation-comparisons", comparison(), token="user")
    npu_workload(service, pair)

    index = get(client, "/jct-report")
    assert index["selected"] is None
    workloads = {w["workload_ref"]: w for w in index["workloads"]}
    assert workloads["pair-work"]["registered"] == 2
    assert workloads["pair-work"]["device_classes"] == ["gpu"]
    assert workloads["npu-work"]["accelerator_models"] == ["ARIES2"]

    report = get(client, "/jct-report?workload_ref=pair-work")["selected"]
    assert report["workload"]["workload_ref"] == "pair-work"
    assert report["workload"]["input_shape"] == [1, 3, 32, 32]
    counts = report["candidate_counts"]
    assert counts["registered"] == 2
    assert counts["submission_compatible"] == 2
    rows = {c["candidate_ref"]: c for c in report["candidates"]}
    assert rows["base"]["node_ref"] == "test-worker"
    base, spare = rows["base"]["readiness"], rows["spare"]["readiness"]
    assert base["jct_profiles"]["fresh"] == 1 and base["jct_profiles"]["stale"] == 0
    assert base["queue"]["complete"] is True and base["queue"]["stale"] is False
    assert base["network_profiles"] == {
        "fresh": 1,
        "stale": 0,
        "latest_observed_at": base["network_profiles"]["latest_observed_at"],
    }
    assert spare["network_profiles"]["fresh"] == 1
    assert spare["network_profiles"]["stale"] == 1
    assert spare["cache_bindings"] == 1
    assert report["policy_profiles"]["static"] == ["legacy"]
    assert report["policy_profiles"]["profile_queue_network"] == ["measured"]
    assert [c["ref"] for c in report["comparisons"]] == ["cmp-1"]
    alternatives = {a["workload_ref"]: a for a in report["alternatives"]}
    assert alternatives["npu-work"]["relation"] == "DIFFERENT_WORKLOAD_IDENTITY"
    assert alternatives["npu-work"]["device_classes"] == ["npu"]


def test_readiness_is_project_scoped(client, pair):
    comparison_setup(client, pair)
    assert get(client, "/jct-report", token="other-operator")["workloads"] == []
    response = client.get(URL + "/jct-report?workload_ref=pair-work", headers=h("other-operator"))
    assert response.status_code == 404
