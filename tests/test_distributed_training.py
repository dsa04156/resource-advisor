"""Multi-GPU (DDP/torchrun) allocation: contract, admission and Kubernetes adapter.

Uses kubectl doubles only; nothing here is native cluster or collective evidence.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_training import setup_training

from resource_advisor.backends import BackendError, KubernetesBackend, SlurmBackend
from resource_advisor.contracts import (
    Candidate,
    DistributedNode,
    DistributedTraining,
    JobRequest,
    RuntimeVariant,
    State,
    WorkloadIdentity,
    WorkloadSpec,
    now,
    signature,
)
from resource_advisor.policy import context_signature
from resource_advisor.service import Rejected
from resource_advisor.worker import Worker

NODES = ("node-a", "node-b")


def ddp(hosts=NODES, per_node=1, **change):
    value = dict(
        strategy="ddp",
        launcher="torchrun",
        collective="nccl",
        nodes=tuple(
            DistributedNode(
                node_ref=n,
                capability_ref="cap-" + n,
                gpus=per_node,
                accelerator_model="fixture-gpu",
                validation_ref="fixture-validation",
            )
            for n in hosts
        ),
        nproc_per_node=per_node,
        world_size=len(hosts) * per_node,
    )
    value.update(change)
    return DistributedTraining(**value)


def setup(
    service, bundle, *, d=None, caps=None, identity=None, launch="torchrun-ddp", validations=()
):
    """A training workload whose only candidate is a two-node DDP allocation."""
    spec, binding, _, _ = setup_training(service, bundle)
    _, _, _, cap = bundle
    d = d or ddp()
    for node in d.nodes:
        member = cap.model_copy(update={"ref": node.capability_ref, "node_ref": node.node_ref})
        service.register("capability", (caps or {}).get(node.node_ref, member), "team-a")
    ident = identity or spec.identity.model_copy(
        update={"batch_size": 4, "global_batch_size": 4 * d.world_size}
    )
    with service.store.transaction() as conn:
        variant = RuntimeVariant.model_validate(
            service.store.get(conn, "variant", "train-variant")["body"]
        )
    variant = variant.model_copy(
        update={
            "ref": "ddp-variant",
            "workload_ref": "ddp-workload",
            "workload_signature": signature(ident),
            "distributed_launch": launch,
            "validation_refs": (*variant.validation_refs, *validations),
        }
    )
    base = spec.candidates[0]
    candidate = Candidate(
        ref="ddp",
        variant_ref=variant.ref,
        capability_ref=d.nodes[0].capability_ref,
        backend="kubernetes",
        context=base.context.model_copy(
            update={
                "distributed": d,
                "resources": base.context.resources.model_copy(
                    update={"accelerator_count": d.nproc_per_node}
                ),
            }
        ),
    )
    ddp_spec = WorkloadSpec.model_validate(
        spec.model_copy(
            update={
                "ref": "ddp-workload",
                "identity": ident,
                "candidates": (candidate,),
                "baseline_candidate_ref": "ddp",
            }
        ).model_dump()
    )
    service.register("variant", variant, "team-a")
    service.register("workload", ddp_spec, "team-a")
    service.register(
        "training_isolation",
        binding.model_copy(update={"ref": "ddp-workload", "workload_ref": "ddp-workload",
                                   "workload_digest": signature(ddp_spec)}),
        "team-a",
    )  # fmt: skip
    return ddp_spec


def submit(service, key="ddp-1"):
    job = service.submit(
        "team-a", JobRequest(workload_ref="ddp-workload", candidate_ref="ddp"), key
    )
    with service.store.transaction() as conn:
        return dict(service.store.job(conn, job["job_id"]))


SOURCES = None


def backend(execute, **kw):
    return KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        training_sources=SOURCES,
        execute=execute,
        **kw,
    )


@pytest.fixture
def sources(service, bundle):
    def bind(job):
        from resource_advisor.training import TrainingIsolation

        b = TrainingIsolation.model_validate(job["body"]["training_isolation"])
        return {
            b.ref: {
                "binding_digest": signature(b),
                "source": {"config_map": "train-src"},
                "checkpoint_key": "checkpoint.json",
                "input_key": "input.json",
            }
        }

    return bind


# --- contract -------------------------------------------------------------------


def test_existing_single_gpu_signatures_are_byte_identical_to_live_records():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/live-single-gpu-context-20261029.json").read_text()
    )
    candidate = Candidate.model_validate(fixture["candidate"])
    variant = RuntimeVariant.model_validate(fixture["variant"])
    assert "distributed" not in candidate.context.model_dump(mode="json")
    assert "distributed_launch" not in variant.model_dump(mode="json")
    assert context_signature(candidate, variant) == fixture["recorded"]["context_signature"]
    identity = WorkloadIdentity.model_validate(fixture["identity"])
    assert signature(identity) == fixture["recorded"]["workload_signature"]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"nodes": (DistributedNode(node_ref="node-a", capability_ref="cap-a", gpus=1, accelerator_model="fixture-gpu", validation_ref="fixture-validation"),) * 2},
         "distinct"),
        ({"nodes": tuple(DistributedNode(node_ref=n, capability_ref="c-" + n, gpus=1, accelerator_model="fixture-gpu", validation_ref="fixture-validation")
                         for n in ("node-b", "node-a"))}, "sorted"),
        ({"nodes": tuple(DistributedNode(node_ref=n, capability_ref="same", gpus=1, accelerator_model="fixture-gpu", validation_ref="fixture-validation")
                         for n in NODES)}, "own capability"),
        ({"world_size": 3}, "world_size"),
        ({"nproc_per_node": 2, "world_size": 4}, "nproc_per_node"),
    ],
)  # fmt: skip
def test_allocation_is_an_explicit_distinct_fixed_world(change, message):
    with pytest.raises(ValidationError, match=message):
        ddp(**change)


def test_single_gpu_cannot_be_called_distributed_and_resources_are_per_node(bundle):
    with pytest.raises(ValidationError):
        ddp(hosts=("node-a",), per_node=1)  # world_size 1 is not distributed
    same_node = ddp(hosts=("node-a",), per_node=2)
    assert same_node.placement == "same-node" and ddp().placement == "multi-node"
    _, candidate, _, _ = bundle
    with pytest.raises(ValidationError, match="per node"):
        candidate.context.model_copy(update={"distributed": same_node}).model_validate(
            candidate.context.model_copy(update={"distributed": same_node}).model_dump()
        )


# --- admission (submission seam) -------------------------------------------------


def test_two_node_allocation_is_admitted_with_per_node_evidence(service, bundle):
    setup(service, bundle)
    job = submit(service)
    assert job["state"] == State.VALIDATED
    d = job["body"]["candidate"]["context"]["distributed"]
    assert [n["node_ref"] for n in d["nodes"]] == list(NODES) and d["world_size"] == 2


@pytest.mark.parametrize(
    "case, reason",
    [
        ("missing", "DISTRIBUTED_NODE_EVIDENCE_MISSING"),
        ("stale", "DISTRIBUTED_NODE_STALE"),
        ("smuggled", "DISTRIBUTED_NODE_CAPABILITY_MISMATCH"),
        ("heterogeneous", "DISTRIBUTED_NODE_CAPABILITY_MISMATCH"),  # undeclared model
        ("other-route", "DISTRIBUTED_NODE_HETEROGENEOUS"),
        ("busy", "DISTRIBUTED_NODE_INSUFFICIENT_GPUS"),
        ("shared", "DISTRIBUTED_NODE_HETEROGENEOUS"),
    ],
)
def test_every_node_needs_its_own_fresh_identical_physical_evidence(service, bundle, case, reason):
    _, _, _, cap = bundle
    b = cap.model_copy(update={"ref": "cap-node-b", "node_ref": "node-b"})
    caps = {
        "stale": b.model_copy(update={"observed_at": now() - timedelta(hours=2)}),
        "smuggled": b.model_copy(update={"node_ref": "elsewhere"}),
        "heterogeneous": b.model_copy(update={"accelerator_model": "other-gpu"}),
        # Another cluster binding (LocalQueue/runtimeClass) cannot share one Job.
        "other-route": b.model_copy(update={"backend_cluster_id": "other-cluster"}),
        "busy": b.model_copy(update={"allocatable_count": 0}),
        # A shared slot advertised as 2 is still one device, never two GPUs.
        "shared": b.model_copy(
            update={"allocation_mode": "virtual_slot", "allocatable_count": 2,
                    "resource_key": "nvidia.com/gpu.shared"}
        ),
    }  # fmt: skip
    if case == "missing":
        caps["node-b"] = b.model_copy(update={"ref": "unrelated-cap"})
    else:
        caps["node-b"] = caps[case]
    setup(service, bundle, caps=caps)
    with pytest.raises(Rejected, match=reason):
        submit(service)


def test_shared_slot_context_is_explicitly_unsupported(service, bundle):
    spec = setup(service, bundle)
    candidate = spec.candidates[0]
    with service.store.transaction() as conn:
        cap = service.store.get(conn, "capability", "cap-node-a")["body"]
    from resource_advisor.contracts import CapabilitySnapshot
    from resource_advisor.policy import compatibility

    shared = candidate.model_copy(
        update={"context": candidate.context.model_copy(update={"allocation_mode": "virtual_slot"})}
    )
    with service.store.transaction() as conn:
        variant = RuntimeVariant.model_validate(
            service.store.get(conn, "variant", "ddp-variant")["body"]
        )
    reasons = compatibility(
        spec, shared, variant, CapabilitySnapshot.model_validate(cap), node_caps={}
    )
    assert "DISTRIBUTED_SHARED_SLOT_UNSUPPORTED" in reasons


@pytest.mark.parametrize("case", ["global_batch", "no_launch", "inference"])
def test_declared_strategy_launcher_and_batch_must_match(service, bundle, case):
    spec, _, _, _ = setup_training(service, bundle)
    identity = spec.identity.model_copy(update={"batch_size": 4, "global_batch_size": 4})
    if case == "inference":
        identity = identity.model_copy(update={"task_type": "inference", "global_batch_size": 8})
    if case == "no_launch":
        identity = identity.model_copy(update={"global_batch_size": 8})
    setup(
        service,
        bundle,
        identity=identity,
        launch=None if case == "no_launch" else "torchrun-ddp",
    )
    reason = {
        "global_batch": "DISTRIBUTED_GLOBAL_BATCH_MISMATCH",
        "no_launch": "DISTRIBUTED_LAUNCH_MISMATCH",
        "inference": "DISTRIBUTED_TRAINING_ONLY",
    }[case]
    with pytest.raises(Rejected, match=reason):
        submit(service)


def test_other_callers_fail_closed_without_node_evidence(service, bundle):
    from resource_advisor.policy import compatibility

    spec = setup(service, bundle)
    with service.store.transaction() as conn:
        variant = RuntimeVariant.model_validate(
            service.store.get(conn, "variant", "ddp-variant")["body"]
        )
        from resource_advisor.contracts import CapabilitySnapshot

        cap = CapabilitySnapshot.model_validate(
            service.store.get(conn, "capability", "cap-node-a")["body"]
        )
    assert "DISTRIBUTED_NODE_EVIDENCE_REQUIRED" in compatibility(
        spec, spec.candidates[0], variant, cap
    )


def mixed(model="fixture-gpu-2", qualification="fixture-validation-2"):
    """node-b exposes a different GPU model, declared with its own qualification."""
    nodes = tuple(
        DistributedNode(
            node_ref=n,
            capability_ref="cap-" + n,
            gpus=1,
            accelerator_model="fixture-gpu" if n == "node-a" else model,
            validation_ref="fixture-validation" if n == "node-a" else qualification,
        )
        for n in NODES
    )
    return ddp(nodes=nodes)


def test_mixed_gpu_models_are_admitted_only_with_per_model_qualification(service, bundle):
    _, _, _, cap = bundle
    b = cap.model_copy(
        update={"ref": "cap-node-b", "node_ref": "node-b", "accelerator_model": "fixture-gpu-2"}
    )
    setup(service, bundle, d=mixed(), caps={"node-b": b}, validations=("fixture-validation-2",))
    job = submit(service)
    nodes = job["body"]["candidate"]["context"]["distributed"]["nodes"]
    assert [n["accelerator_model"] for n in nodes] == ["fixture-gpu", "fixture-gpu-2"]


def test_mixed_gpu_model_without_its_own_qualification_is_rejected(service, bundle):
    _, _, _, cap = bundle
    b = cap.model_copy(
        update={"ref": "cap-node-b", "node_ref": "node-b", "accelerator_model": "fixture-gpu-2"}
    )
    setup(service, bundle, d=mixed(), caps={"node-b": b})  # qualification not on the variant
    with pytest.raises(Rejected, match="DISTRIBUTED_NODE_MODEL_UNQUALIFIED"):
        submit(service)


def test_declared_node_model_must_match_that_nodes_capability(service, bundle):
    setup(service, bundle, d=mixed(), validations=("fixture-validation-2",))  # b is fixture-gpu
    with pytest.raises(Rejected, match="DISTRIBUTED_NODE_CAPABILITY_MISMATCH"):
        submit(service)


def test_mixed_models_keep_every_runtime_and_route_identity(service, bundle):
    _, _, _, cap = bundle
    b = cap.model_copy(
        update={
            "ref": "cap-node-b",
            "node_ref": "node-b",
            "accelerator_model": "fixture-gpu-2",
            "runtime_versions": {"cuda": "other-abi"},
        }
    )
    setup(service, bundle, d=mixed(), caps={"node-b": b}, validations=("fixture-validation-2",))
    with pytest.raises(Rejected, match="DISTRIBUTED_NODE_HETEROGENEOUS"):
        submit(service)


# --- combination builder ------------------------------------------------------------


def test_builder_enumerates_only_real_qualified_physical_node_sets(bundle):
    from resource_advisor.distributed_allocation import build_candidates

    spec, candidate, _, cap = bundle
    caps = [cap] + [
        cap.model_copy(update={"ref": f"cap-{n}", "node_ref": n, **change})
        for n, change in [
            ("node-m", {"accelerator_model": "fixture-gpu-2"}),
            ("node-s", {"allocation_mode": "virtual_slot", "allocatable_count": 2}),
            ("node-t", {"observed_at": now() - timedelta(hours=2)}),
            ("node-r", {"backend_cluster_id": "other-cluster"}),
            ("node-u", {"accelerator_model": "unqualified-gpu"}),
        ]
    ]
    qualified = {"fixture-gpu": "fixture-validation", "fixture-gpu-2": "fixture-validation-2"}
    built = build_candidates(candidate, cap, caps, qualified, counts=(1, 2, 4))
    assert built[1] == [candidate]
    assert 4 not in built  # only two qualified nodes exist: no invented 4-GPU set
    [pair] = built[2]
    d = pair.context.distributed
    assert [(n.node_ref, n.accelerator_model) for n in d.nodes] == [
        ("node-m", "fixture-gpu-2"),
        ("test-worker", "fixture-gpu"),
    ]
    assert d.world_size == 2 and pair.context.resources.accelerator_count == 1
    assert pair.capability_ref == cap.ref
    assert build_candidates(candidate, cap, caps, qualified, counts=(2,)) == {2: built[2]}


def test_builder_same_node_only_when_physical_count_covers_every_rank(bundle):
    from resource_advisor.distributed_allocation import build_candidates

    _, candidate, _, cap = bundle
    two = cap.model_copy(update={"allocatable_count": 2})
    base = candidate.model_copy(update={"capability_ref": two.ref})
    built = build_candidates(base, two, [two], {"fixture-gpu": "fixture-validation"}, counts=(2, 4))
    [same] = built[2]
    assert same.context.distributed.placement == "same-node"
    assert same.context.distributed.nproc_per_node == 2 and 4 not in built


# --- Kubernetes adapter -----------------------------------------------------------


class Kubectl:
    """kubectl double: records every call; never a real scheduler."""

    def __init__(self):
        self.calls, self.job, self.pods, self.service, self.logs = [], None, [], None, {}

    def __call__(self, args, stdin=None, **_):
        self.calls.append((args, stdin))
        verb, kind = args[3], args[4] if len(args) > 4 else None
        if verb == "create":
            obj = json.loads(stdin)
            obj["metadata"]["uid"] = "uid-" + obj["kind"].lower()
            if obj["kind"] == "Job":
                self.job = obj
            else:
                self.service = obj
            return json.dumps(obj)
        if verb == "get" and kind == "job":
            return json.dumps(self.job) if self.job else ""
        if verb == "get" and kind == "service":
            return json.dumps(self.service) if self.service else ""
        if verb == "get" and kind == "pods":
            return json.dumps({"items": self.pods})
        if verb == "logs":
            return self.logs.get(args[4].split("/", 1)[1], "")
        if verb == "delete":
            self.job = None
            return ""
        raise AssertionError(args)


def rank_pod(index, node, *, uid="uid-job", phase="Succeeded", exit_ok=True):
    terminated = {"startedAt": "2026-10-29T10:00:05Z", "finishedAt": "2026-10-29T10:00:20Z",
                  "exitCode": 0 if exit_ok else 1}  # fmt: skip
    return {
        "metadata": {
            "name": f"pod-{index}",
            "uid": f"pod-uid-{index}",
            "annotations": {"batch.kubernetes.io/job-completion-index": str(index)},
            "ownerReferences": [{"kind": "Job", "uid": uid}],
        },
        "spec": {
            "nodeName": node,
            "containers": [
                {"name": "workload", "resources": {"requests": {"nvidia.com/gpu": "1"}}}
            ],
        },
        "status": {
            "phase": phase,
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "True",
                    "lastTransitionTime": "2026-10-29T10:00:01Z",
                }
            ],
            "containerStatuses": [{"name": "workload", "state": {"terminated": terminated}}],
        },
    }


def complete(k, condition="Complete"):
    k.job["status"] = {
        "conditions": [{"type": condition, "status": "True", "reason": "BackoffLimitExceeded"}]
    }
    k.job["spec"]["suspend"] = False


def started(service, bundle, sources):
    global SOURCES
    setup(service, bundle)
    job = submit(service)
    SOURCES = sources(job)
    k = Kubectl()
    adapter = backend(k)
    external = adapter.submit(job)
    job["body"]["external_id"] = external
    return job, k, adapter


def test_indexed_job_requests_gpus_per_pod_and_keeps_kueue(service, bundle, sources):
    job, k, _ = started(service, bundle, sources)
    manifest = k.job
    spec, pod = manifest["spec"], manifest["spec"]["template"]["spec"]
    assert (spec["completionMode"], spec["completions"], spec["parallelism"]) == ("Indexed", 2, 2)
    assert spec["suspend"] is True and spec["backoffLimit"] == 0
    assert manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "batch"
    main = pod["containers"][0]
    assert main["resources"]["requests"]["nvidia.com/gpu"] == "1"  # per Pod, not world_size
    assert "kubernetes.io/hostname" not in pod["nodeSelector"] and "nodeName" not in pod
    terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert terms["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == list(NODES)
    anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert anti["topologyKey"] == "kubernetes.io/hostname"
    attempt = job["body"]["attempt_id"]
    assert pod["subdomain"] == attempt
    assert main["command"][:3] == ["python", "-m", "torch.distributed.run"]
    assert "--nnodes=2" in main["command"] and "--max-restarts=0" in main["command"]
    assert f"--rdzv-endpoint={attempt}-0.{attempt}:29400" in main["command"]
    assert main["command"][-1] == "workload.py"
    env = {e["name"]: e for e in main["env"]}
    assert json.loads(env["RA_DISTRIBUTED_JSON"]["value"])["world_size"] == 2


def test_rendezvous_service_is_created_after_and_owned_by_the_job_uid(service, bundle, sources):
    job, k, adapter = started(service, bundle, sources)
    kinds = [json.loads(stdin)["kind"] for args, stdin in k.calls if args[3] == "create"]
    assert kinds == ["Job", "Service"]
    svc = k.service
    assert svc["metadata"]["ownerReferences"][0]["uid"] == "uid-job"
    assert svc["spec"]["clusterIP"] == "None" and svc["spec"]["publishNotReadyAddresses"] is True
    assert svc["spec"]["selector"]["batch.kubernetes.io/controller-uid"] == "uid-job"
    # A lost Service is repaired on reconcile; a foreign one is never adopted or deleted.
    k.service = None
    assert adapter.reconcile(job) == job["body"]["attempt_id"] and k.service is not None
    k.service["metadata"]["ownerReferences"][0]["uid"] = "someone-else"
    with pytest.raises(BackendError, match="SERVICE_CONFLICT"):
        adapter.reconcile(job)
    assert not any(args[3] == "delete" for args, _ in k.calls)


def test_complete_on_distinct_allocated_nodes_collects_with_rank_placement(
    service, bundle, sources
):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-b"), rank_pod(1, "node-a"), rank_pod(0, "x", uid="foreign")]
    complete(k)
    observation = adapter.status(job)
    assert observation.state == State.COLLECTING
    ranks = observation.placement["distributed"]["ranks"]
    assert {r["node_name"] for r in ranks} == set(NODES) and len(ranks) == 2
    assert observation.allocation["accelerator_count"] == 2


@pytest.mark.parametrize("nodes", [("node-a", "node-a"), ("node-a", "outside")])
def test_placement_off_the_node_set_fails_and_stops_the_job(service, bundle, sources, nodes):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(i, n, phase="Running") for i, n in enumerate(nodes)]
    observation = adapter.status(job)
    assert (observation.state, observation.error) == (
        State.FAILED,
        "DISTRIBUTED_PLACEMENT_MISMATCH",
    )
    assert any(args[3] == "delete" and args[4] == "job" for args, _ in k.calls)


def test_one_rank_failure_fails_the_whole_job_without_reading_a_result(service, bundle, sources):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-a"), rank_pod(1, "node-b", phase="Failed", exit_ok=False)]
    complete(k, "Failed")
    observation = adapter.status(job)
    assert observation.state == State.FAILED
    assert not any(args[3] == "logs" for args, _ in k.calls)


def test_complete_with_a_missing_rank_is_not_success(service, bundle, sources):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-a")]
    complete(k)
    assert adapter.status(job).error == "DISTRIBUTED_PLACEMENT_UNVERIFIED"


def envelope(job):
    result = {"job_id": job["id"], "attempt_id": job["body"]["attempt_id"]}
    return "RESOURCE_ADVISOR_RESULT " + json.dumps({"result": result, "digest": signature(result)})


@pytest.mark.parametrize("emitters, ok", [((1,), True), ((), False), ((0, 1), False)])
def test_result_binds_exactly_one_rank0_envelope(service, bundle, sources, emitters, ok):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-a"), rank_pod(1, "node-b")]
    complete(k)
    job["body"]["placement"] = adapter.status(job).placement  # recorded as the worker does
    k.logs = {f"pod-{i}": ("step\n" + (envelope(job) if i in emitters else "")) for i in (0, 1)}
    if ok:
        assert adapter.result(job)["result"]["attempt_id"] == job["body"]["attempt_id"]
    else:
        with pytest.raises(BackendError, match="exactly one rank"):
            adapter.result(job)


def collected(service, bundle, sources):
    """A completed two-rank job whose verified placement the worker recorded."""
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-a"), rank_pod(1, "node-b")]
    complete(k)
    observation = adapter.status(job)
    assert observation.state == State.COLLECTING
    job["body"]["placement"] = observation.placement
    k.logs = {"pod-0": envelope(job), "pod-1": "step"}
    return job, k, adapter


def test_result_is_never_read_from_a_same_name_replacement_job(service, bundle, sources):
    job, k, adapter = collected(service, bundle, sources)
    k.job["metadata"]["uid"] = "replacement-uid"
    replacement = [rank_pod(0, "outside", uid="replacement-uid", phase="Failed")]
    del replacement[0]["metadata"]["annotations"]
    k.pods = replacement
    k.logs = {"pod-0": envelope(job)}
    with pytest.raises(BackendError, match="recorded"):
        adapter.result(job)
    assert not any(args[3] == "logs" for args, _ in k.calls)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["status"].update(phase="Failed"),
        lambda p: p["spec"].update(nodeName="outside"),
        lambda p: p["metadata"].update(uid="pod-uid-new"),
        lambda p: p["metadata"]["annotations"].update(
            {"batch.kubernetes.io/job-completion-index": "0"}
        ),
    ],
)
def test_result_revalidates_every_recorded_rank_before_reading_logs(
    service, bundle, sources, change
):
    job, k, adapter = collected(service, bundle, sources)
    change(k.pods[1])
    with pytest.raises(BackendError, match="recorded"):
        adapter.result(job)
    assert not any(args[3] == "logs" for args, _ in k.calls)


def test_result_requires_a_recorded_complete_placement(service, bundle, sources):
    job, k, adapter = collected(service, bundle, sources)
    job["body"]["placement"] = None
    with pytest.raises(BackendError, match="recorded"):
        adapter.result(job)


def test_active_monitoring_repairs_a_lost_owned_service(service, bundle, sources):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-a", phase="Running"), rank_pod(1, "node-b", phase="Pending")]
    k.job["status"] = {}
    k.service = None
    adapter.status(job)
    assert k.service is not None
    assert k.service["metadata"]["ownerReferences"][0]["uid"] == "uid-job"
    k.service["metadata"]["ownerReferences"][0]["uid"] = "someone-else"
    with pytest.raises(BackendError, match="SERVICE_CONFLICT"):
        adapter.status(job)
    assert not any(args[3] == "delete" and args[4] == "service" for args, _ in k.calls)


def test_terminal_job_never_recreates_its_service(service, bundle, sources):
    job, k, adapter = started(service, bundle, sources)
    k.pods = [rank_pod(0, "node-a"), rank_pod(1, "node-b")]
    complete(k)
    k.service = None
    created = len([a for a, _ in k.calls if a[3] == "create"])
    adapter.status(job)
    assert k.service is None
    assert len([a for a, _ in k.calls if a[3] == "create"]) == created


def test_cancel_deletes_only_the_owned_job(service, bundle, sources):
    job, k, adapter = started(service, bundle, sources)
    adapter.cancel(job)
    deletes = [args for args, _ in k.calls if args[3] == "delete"]
    assert deletes == [k.calls[-1][0]] and deletes[0][4:6] == ["job", job["body"]["attempt_id"]]
    # The Service is a garbage-collected dependent (ownerReference), never deleted by name.
    assert k.service["metadata"]["ownerReferences"][0]["uid"] == "uid-job"


def test_unsupported_shapes_fail_closed(service, bundle, sources):
    global SOURCES
    setup(service, bundle)
    job = submit(service)
    SOURCES = sources(job)
    with pytest.raises(BackendError, match="RETENTION_UNSUPPORTED"):
        backend(Kubectl(), retain_termination_evidence=True).manifest(job)
    with pytest.raises(BackendError, match="hostname allowlist"):
        KubernetesBackend(
            namespace="research-a", local_queue="batch",
            node_selector={"kubernetes.io/hostname": "node-a"}, training_sources=SOURCES,
        ).manifest(job)  # fmt: skip
    with pytest.raises(BackendError, match="DISTRIBUTED_SLURM_UNSUPPORTED"):
        SlurmBackend(
            partition="p", account="a", qos="q", output_dir="/tmp/ra", execute=Kubectl()
        ).script(job)


# --- worker (native completion seam) --------------------------------------------


def test_worker_completes_only_after_every_rank_and_binds_rank0_result(service, bundle, sources):
    global SOURCES
    setup(service, bundle)
    job = submit(service)
    SOURCES = sources(job)
    k = Kubectl()
    worker = Worker(service, {("team-a", "lab"): backend(k)})
    assert worker.submit_one()
    with service.store.transaction() as conn:
        row = dict(service.store.job(conn, job["id"]))
    assert row["state"] == State.QUEUED and k.service is not None
    k.pods = [rank_pod(0, "node-a", phase="Running"), rank_pod(1, "node-b", phase="Pending")]
    k.job["status"] = {}
    worker.reconcile_all()
    with service.store.transaction() as conn:
        assert service.store.job(conn, job["id"])["state"] in {State.QUEUED, State.RUNNING}
    complete(k, "Failed")
    worker.reconcile_all()
    with service.store.transaction() as conn:
        final = service.store.job(conn, job["id"])
    assert final["state"] == State.FAILED and final["body"].get("result_digest") is None
