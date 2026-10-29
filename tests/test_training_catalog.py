"""Model-first training catalog: models, exact training conditions, resource combinations."""

import json
from datetime import timedelta

import pytest
import test_heterogeneous_recommendation as hetero
import test_network_recommendation as network_rec
from test_network_recommendation import URL, h, post

from resource_advisor.contracts import JobRequest, now, signature
from resource_advisor.policy import context_signature
from resource_advisor.training import TrainingIsolation

pair = hetero.pair  # shared fixtures
client = network_rec.client


def get(client, path, token="user", status=200):
    response = client.get(URL + path, headers=h(token))
    assert response.status_code == status, response.text
    return response.json()


def training(
    service,
    pair,
    ref,
    *,
    model="mobilenet_v3_small",
    steps=50,
    code="a",
    task_type="training",
    verification="TRAINING_VERIFIED",
    parameters=None,
    **identity_change,
):
    """A registered training workload on the fixture GPU candidates (base, spare)."""
    spec, variant, _ = pair
    identity = spec.identity.model_copy(
        update={
            "task_type": task_type,
            "model_name": model,
            "work_units": steps,
            "code_digest": signature("code-" + code),
            "optimizer": "adamw" if model else None,
            "model_digest": signature("checkpoint-" + str(model)),
            "dataset_version": signature("input"),
            **identity_change,
        }
    )
    profiling = spec.profiling.model_copy(update={"checkpoint_digest": identity.model_digest})
    v = variant.model_copy(
        update={
            "ref": ref + "-variant",
            "workload_ref": ref,
            "workload_signature": signature(identity),
            "model_digest": identity.model_digest,
            "verification": verification,
            "validation_refs": ("fixture-training-evidence",),
        }
    )
    candidates = tuple(
        c.model_copy(
            update={
                "variant_ref": v.ref,
                "context": c.context.model_copy(update={"parameters": parameters or {}}),
            }
        )
        for c in spec.candidates
    )
    w = spec.model_copy(
        update={"ref": ref, "identity": identity, "candidates": candidates, "profiling": profiling}
    )
    service.register("variant", v, "team-a")
    service.register("workload", w, "team-a")
    if task_type == "training":
        binding = TrainingIsolation(
            ref=ref,
            project_ref="team-a",
            workload_ref=ref,
            workload_digest=signature(w),
            checkpoint_digest=identity.model_digest,
            input_digest=identity.dataset_version,
            checkpoint_size_bytes=64,
            input_size_bytes=64,
            validation_refs=("fixture-isolation",),
        )
        service.register("training_isolation", binding, "team-a")
    return w, v


def models(client, token="user"):
    return {m["model_name"]: m for m in get(client, "/training-catalog", token)["models"]}


def test_only_named_training_verified_workloads_become_models(client, pair, service):
    training(service, pair, "mnv3-a")
    training(service, pair, "resnet-infer", model="resnet18", task_type="inference")
    training(service, pair, "fixture-sgd", model=None)
    training(service, pair, "resnet-detected", model="resnet18", verification="RUNTIME_VERIFIED")

    catalog = get(client, "/training-catalog")
    listed = models(client)
    assert set(listed) == {"mobilenet_v3_small", "resnet18"}  # never the inference workload
    assert listed["mobilenet_v3_small"]["qualified"] is True
    assert listed["resnet18"]["qualified"] is False  # selectable, but nothing can run
    unqualified = {u["workload_ref"]: u["reasons"] for u in catalog["unqualified"]}
    assert unqualified["fixture-sgd"] == ["MODEL_NAME_MISSING"]
    assert unqualified["resnet-detected"] == ["NO_TRAINING_VERIFIED_CANDIDATE"]
    assert "resnet-infer" not in unqualified  # inference is never listed as learning
    assert get(client, "/training-catalog", token="other-operator")["models"] == []


def test_exact_training_conditions_group_without_merging_identities(client, pair, service):
    training(service, pair, "mnv3-a", code="a")
    training(service, pair, "mnv3-b", code="b")  # same condition, different identity
    training(service, pair, "mnv3-long", steps=200)
    key = models(client)["mobilenet_v3_small"]["model_key"]

    configs = get(client, f"/training-catalog?model={key}")["selected"]["configs"]
    by_steps = {c["condition"]["steps"]: c for c in configs}
    assert set(by_steps) == {50, 200}
    same = by_steps[50]
    assert sorted(w["workload_ref"] for w in same["workloads"]) == ["mnv3-a", "mnv3-b"]
    assert len({w["identity_signature"] for w in same["workloads"]}) == 2
    assert same["condition"]["optimizer"] == "adamw"
    get(client, "/training-catalog?model=missing", status=404)


def jct_profile_for(service, pair, workload, variant, candidate_ref, compute):
    spec, _, caps = pair
    profile = hetero.jct_profile(workload, variant, caps[candidate_ref], candidate_ref, compute)
    candidate = next(c for c in workload.candidates if c.ref == candidate_ref)
    return profile.model_copy(
        update={
            "ref": f"jct-{workload.ref}-{candidate_ref}",
            "context_signature": context_signature(candidate, variant),
        }
    )


def test_combinations_use_existing_estimates_only_for_their_own_signature(client, pair, service):
    a, va = training(service, pair, "mnv3-a", code="a")
    training(service, pair, "mnv3-b", code="b")
    post(
        client,
        "/scheduling-profiles",
        hetero.scheduling_profile("only", {"policy": "profile_only"}),
    )
    post(client, "/jct-profiles", jct_profile_for(service, pair, a, va, "base", 2.0))
    key = models(client)["mobilenet_v3_small"]["model_key"]

    selected = get(client, f"/training-catalog?model={key}")["selected"]
    assert selected["estimate_profile"]["ref"] == "only"
    combos = {(c["workload_ref"], c["candidate_ref"]): c for c in selected["combinations"]}
    measured = combos[("mnv3-a", "base")]
    assert measured["jct"]["predicted_seconds"] is not None
    assert measured["jct"]["source"] == "existing recommendation preview"
    assert measured["evidence"] == "MEASURED"
    for other in [("mnv3-a", "spare"), ("mnv3-b", "base"), ("mnv3-b", "spare")]:
        assert combos[other]["jct"]["predicted_seconds"] is None, other
        assert combos[other]["evidence"] == "UNMEASURED"
        assert combos[other]["jct"]["status"] == "PILOT_REQUIRED"


def test_without_a_recommendation_profile_every_jct_is_unknown(client, pair, service):
    training(service, pair, "mnv3-a")
    key = models(client)["mobilenet_v3_small"]["model_key"]
    selected = get(client, f"/training-catalog?model={key}")["selected"]
    assert selected["estimate_profile"] is None
    assert {c["jct"]["status"] for c in selected["combinations"]} == {"NO_ESTIMATE_POLICY"}


def test_single_device_combinations_report_one_exclusive_physical_gpu(client, pair, service):
    training(service, pair, "mnv3-a")
    training(service, pair, "mnv3-param", code="p", parameters={"distributed_strategy": "ddp"})
    key = models(client)["mobilenet_v3_small"]["model_key"]
    combos = get(client, f"/training-catalog?model={key}")["selected"]["combinations"]
    single = next(
        c for c in combos if (c["workload_ref"], c["candidate_ref"]) == ("mnv3-a", "base")
    )
    assert single["strategy"] == "single" and single["distributed"] is None
    assert single["physical_devices"] == 1 and single["exclusive"] is True
    assert single["shared_slots"] is None
    assert single["node_refs"] == ["test-worker"]
    assert single["memory"]["aggregation"] == "per device; never summed across devices"
    # Untyped parameter keys never declare a strategy; training rejects overrides.
    unqualified = {u["workload_ref"]: u for u in get(client, "/training-catalog")["unqualified"]}
    assert unqualified["mnv3-param"]["candidate_reasons"] == ["QUALIFICATION_REJECTED"]
    assert unqualified["mnv3-param"]["strategies"] == ["single"]


def test_typed_distributed_context_reports_world_size_and_node_set(client, pair, service):
    from resource_advisor import contracts
    from resource_advisor.training_catalog import _combination

    if not hasattr(contracts, "DistributedTraining"):
        pytest.skip("typed distributed contract not present")
    ddp = contracts.DistributedTraining(
        strategy="ddp",
        launcher="torchrun",
        collective="gloo",
        nodes=(
            contracts.DistributedNode(
                node_ref="spare-node",
                capability_ref="spare-cap",
                gpus=1,
                accelerator_model="fixture-gpu",
                validation_ref="fixture-validation",
            ),
            contracts.DistributedNode(
                node_ref="test-worker",
                capability_ref="cap-1",
                gpus=1,
                accelerator_model="fixture-gpu",
                validation_ref="fixture-validation",
            ),
        ),
        nproc_per_node=1,
        world_size=2,
    )
    w, _ = training(service, pair, "mnv3-a")
    candidate = w.candidates[0]
    typed = candidate.model_copy(
        update={"context": candidate.context.model_copy(update={"distributed": ddp})}
    )
    spec = w.model_copy(update={"candidates": (typed,)})
    row = {
        "candidate_ref": typed.ref,
        "node_ref": "test-worker",
        "backend": "kubernetes",
        "cluster_ref": "lab",
        "resource_key": "nvidia.com/gpu",
        "model": "fixture-gpu",
        "device_class": "gpu",
        "submission_reasons": [],
        "submittable_now": False,
        "verification": "RUNTIME_VERIFIED",
    }
    with service.store.transaction() as conn:
        result = _combination(
            service,
            conn,
            "team-a",
            spec,
            row,
            at=now(),
            inventory=[],
            queue_age=30,
            profile=None,
            preview=None,
            error=None,
            runs=[],
        )
    assert result["strategy"] == "ddp"
    assert result["physical_devices"] == 2  # world_size: one physical GPU per rank
    assert result["node_refs"] == ["spare-node", "test-worker"]
    assert result["distributed"]["placement"] == "multi-node"
    assert result["memory"]["semantics"] == "replicated-per-rank"
    assert "DISTRIBUTED_STRATEGY_NOT_VALIDATED" in result["blockers"]
    assert result["jct"]["status"] == "NO_ESTIMATE_POLICY"


def ddp_workload(service, pair, *, stale_spare=False):
    """Typed two-node DDP candidate built from the pair's real fixture capabilities."""
    from resource_advisor.distributed_allocation import build_candidates

    src, v = training(service, pair, "mnv3-src", code="ddp", batch_size=1, global_batch_size=2)
    _, _, caps = pair
    [ddp] = build_candidates(
        src.candidates[0], caps["base"], list(caps.values()),
        {"fixture-gpu": "fixture-training-evidence"}, counts=(2,),
    )[2]  # fmt: skip
    if stale_spare:  # the spare node's own evidence is old; the anchor stays fresh
        old = caps["spare"].model_copy(
            update={"ref": "spare-cap-old", "observed_at": now() - timedelta(hours=2)}
        )
        service.register("capability", old, "team-a")
        d = ddp.context.distributed
        nodes = tuple(
            n.model_copy(update={"capability_ref": old.ref}) if n.node_ref == old.node_ref else n
            for n in d.nodes
        )
        context = ddp.context.model_copy(
            update={"distributed": d.model_copy(update={"nodes": nodes})}
        )
        ddp = ddp.model_copy(update={"context": context})
    v = v.model_copy(
        update={
            "ref": "mnv3-ddp-variant",
            "workload_ref": "mnv3-ddp",
            "distributed_launch": "torchrun-ddp",
        }
    )
    ddp = ddp.model_copy(update={"variant_ref": v.ref})
    w = src.model_copy(
        update={"ref": "mnv3-ddp", "candidates": (ddp,), "baseline_candidate_ref": ddp.ref}
    )
    service.register("variant", v, "team-a")
    service.register("workload", w, "team-a")
    binding = TrainingIsolation(
        ref=w.ref,
        project_ref="team-a",
        workload_ref=w.ref,
        workload_digest=signature(w),
        checkpoint_digest=w.identity.model_digest,
        input_digest=w.identity.dataset_version,
        checkpoint_size_bytes=64,
        input_size_bytes=64,
        validation_refs=("fixture-isolation",),
    )
    service.register("training_isolation", binding, "team-a")
    return w, ddp


@pytest.mark.parametrize("stale", [False, True])
def test_query_api_validates_per_node_evidence_for_distributed_combinations(
    client, pair, service, stale
):
    w, ddp = ddp_workload(service, pair, stale_spare=stale)
    key = models(client)["mobilenet_v3_small"]["model_key"]
    combos = get(client, f"/training-catalog?model={key}")["selected"]["combinations"]
    [row] = [c for c in combos if c["candidate_ref"] == ddp.ref]
    assert row["strategy"] == "ddp" and row["physical_devices"] == 2
    distributed = {b for b in row["blockers"] if b.startswith("DISTRIBUTED_NODE")}
    # The query path resolves each node's own capability: never a permanent
    # evidence-required rejection, and a stale node is still caught.
    assert "DISTRIBUTED_NODE_EVIDENCE_REQUIRED" not in row["blockers"]
    assert distributed == ({"DISTRIBUTED_NODE_STALE"} if stale else set())
    post(client, "/scheduling-profiles", hetero.scheduling_profile())
    plan = post(
        client,
        "/scheduling-plans",
        {"profile_ref": "hetero-v1", "workload_ref": w.ref, "candidate_ref": ddp.ref},
    )
    excluded = plan.get("excluded", {}).get(ddp.ref, [])
    assert "DISTRIBUTED_NODE_EVIDENCE_REQUIRED" not in excluded
    assert ("DISTRIBUTED_NODE_STALE" in excluded) == stale


def test_fixed_global_batch_groups_allocations_with_different_local_batches(client, pair, service):
    training(
        service,
        pair,
        "mnv3-l16",
        code="l16",
        global_batch_size=64,
        batch_size=16,
        gradient_accumulation=4,
    )
    training(
        service,
        pair,
        "mnv3-l32",
        code="l32",
        global_batch_size=64,
        batch_size=32,
        gradient_accumulation=2,
    )
    training(service, pair, "mnv3-plain", code="p")  # no global batch: separate condition
    key = models(client)["mobilenet_v3_small"]["model_key"]
    configs = get(client, f"/training-catalog?model={key}")["selected"]["configs"]
    grouped = next(c for c in configs if c["condition"]["global_batch_size"] == 64)
    assert sorted(w["workload_ref"] for w in grouped["workloads"]) == ["mnv3-l16", "mnv3-l32"]
    assert len(configs) == 2


def test_runs_map_to_their_exact_workload(client, pair, service):
    training(service, pair, "mnv3-a", code="a")
    training(service, pair, "mnv3-b", code="b")
    settle = network_rec.finish_with_native_timestamps(service, seconds=7.0)
    job = service.submit("team-a", JobRequest(workload_ref="mnv3-a", candidate_ref="base"), "t1")
    settle(job["job_id"])
    key = models(client)["mobilenet_v3_small"]["model_key"]
    selected = get(client, f"/training-catalog?model={key}")["selected"]
    runs = {w["workload_ref"]: w["recent_runs"] for w in selected["configs"][0]["workloads"]}
    assert [r["job_id"] for r in runs["mnv3-a"]] == [job["job_id"]]
    # Training stays COLLECTING until its receipt is verified: no JCT before that.
    assert runs["mnv3-a"][0]["state"] == "COLLECTING"
    assert runs["mnv3-a"][0]["jct"]["jct_seconds"] is None
    assert runs["mnv3-b"] == []


def test_inventory_coverage_never_counts_shared_slots_as_gpus_or_npus_as_trainable(
    client, pair, service
):
    from test_inventory import config, source

    from resource_advisor.inventory import InventoryCollector, save_inventory

    snapshot = InventoryCollector(config(), execute=source()).collect()
    npu = json.loads(json.dumps(snapshot["nodes"][0]))
    npu["node_ref"] = "npu-node"
    shared = npu["resources"]["vendor/gpu.shared"]
    npu["resources"] = {
        "vendor/npu": {**shared, "device_class": "npu", "allocation_mode": "physical_device"}
    }
    snapshot["nodes"].append(npu)
    snapshot["collected_at"] = (now() - timedelta(seconds=1)).isoformat()
    save_inventory(service.store, "team-a", snapshot)

    coverage = {
        (r["node_ref"], r["resource_key"]): r for r in get(client, "/training-catalog")["inventory"]
    }
    assert ("node-a", "cpu") not in coverage and ("node-a", "memory") not in coverage
    slot = coverage[("node-a", "vendor/gpu.shared")]
    assert slot["physical_devices"] is None and slot["shared_slots"] == 2
    assert slot["training_reason"] == "NO_TRAINING_VARIANT"
    assert coverage[("npu-node", "vendor/npu")]["training_reason"] == "NPU_NO_TRAINING_RUNTIME"
    assert coverage[("npu-node", "vendor/npu")]["physical_devices"] == 2
