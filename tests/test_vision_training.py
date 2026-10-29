import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from resource_advisor import vision_training as vt

HAS_TORCH = importlib.util.find_spec("torch") is not None and importlib.util.find_spec(
    "torchvision"
)
needs_torch = pytest.mark.skipif(not HAS_TORCH, reason="torch/torchvision not installed")


def args(**overrides):
    values = dict(
        model="mobilenet_v3_small",
        data="/data",
        dataset_digest="sha256:" + "0" * 64,
        initial_checkpoint_digest="sha256:" + "1" * 64,
        global_batch=16,
        steps=20,
        lr=1e-3,
        weight_decay=0.01,
        seed=7,
        strategy="single",
        output=None,
        print_initial_digest=False,
    )
    values.update(overrides)
    return vt.parser().parse_args(
        [
            f"--{k.replace('_', '-')}={v}"
            for k, v in values.items()
            if v is not None and not isinstance(v, bool)
        ]
    )


def environ(**overrides):
    context = {
        "arch": "arm64",
        "accelerator_model": "Orin",
        "allocation_mode": "virtual_slot",
        "parameters": {},
        "runtime_versions": {"pytorch": "2.9.1+cu130"},
        "resources": {"host_cpu": 2, "host_memory_mib": 4096, "accelerator_count": 1},
    }
    context.update(overrides.pop("context", {}))
    values = {
        "RA_CONTEXT_JSON": json.dumps(context),
        "RA_WORK_UNITS": "20",
        "RA_INPUT_SHAPE": "[16, 3, 224, 224]",
        "RA_PRECISION": "fp32",
        "RA_SEED": "7",
    }
    values.update(overrides)
    return values


def test_signed_semantics_accepted():
    context, world = vt.validate_environment(args(), environ())
    assert world == 1 and context["accelerator_model"] == "Orin"


@pytest.mark.parametrize(
    "overrides",
    [
        {"RA_WORK_UNITS": "50"},
        {"RA_INPUT_SHAPE": "[8, 3, 224, 224]"},
        {"RA_PRECISION": "fp16"},
        {"RA_SEED": "8"},
        {"context": {"parameters": {"lr": 0.1}}},
        {"context": {"allocation_mode": "cpu_only"}},
        {"context": {"resources": {"host_cpu": 2, "host_memory_mib": 1, "accelerator_count": 2}}},
        {"WORLD_SIZE": "2"},
    ],
)
def test_mismatched_semantics_refused_before_work(overrides):
    with pytest.raises((ValueError, RuntimeError)):
        vt.validate_environment(args(), environ(**overrides))


DIST = {
    "schema_version": "v1",
    "strategy": "ddp",
    "launcher": "torchrun",
    "collective": "nccl",
    "world_size": 2,
    "nproc_per_node": 1,
    "memory_semantics": "replicated-per-rank",
    "nodes": [
        {
            "node_ref": "n1",
            "capability_ref": "c1",
            "gpus": 1,
            "accelerator_model": "A",
            "validation_ref": "q1",
        },
        {
            "node_ref": "n2",
            "capability_ref": "c2",
            "gpus": 1,
            "accelerator_model": "B",
            "validation_ref": "q2",
        },
    ],
}


def ddp_env(**overrides):
    values = dict(
        context={"allocation_mode": "physical_device", "distributed": DIST},
        WORLD_SIZE="2",
        LOCAL_WORLD_SIZE="1",
        RA_NODE_NAME="n2",
        RA_DISTRIBUTED_JSON=json.dumps(DIST),
        RA_INPUT_SHAPE="[8, 3, 224, 224]",
    )
    values.update(overrides)
    return environ(**values)


def test_step_bound_and_ddp_world_checks():
    with pytest.raises(ValueError):
        vt.validate_environment(args(steps=101), environ(RA_WORK_UNITS="101"))
    with pytest.raises(RuntimeError):
        vt.validate_environment(args(strategy="ddp"), environ())
    _, world = vt.validate_environment(args(strategy="ddp"), ddp_env())
    assert world == 2
    assert (
        vt.distributed_node(json.loads(ddp_env()["RA_CONTEXT_JSON"]), ddp_env(), 2)[
            "accelerator_model"
        ]
        == "B"
    )
    with pytest.raises(ValueError):
        vt.validate_environment(args(strategy="ddp", global_batch=15), ddp_env())


@pytest.mark.parametrize(
    "overrides",
    [
        {"RA_NODE_NAME": "n3"},
        {"LOCAL_WORLD_SIZE": "2"},
        {"RA_DISTRIBUTED_JSON": json.dumps(DIST | {"world_size": 4})},
        {"context": {"allocation_mode": "virtual_slot", "distributed": DIST}},
        {"RA_INPUT_SHAPE": "[16, 3, 224, 224]"},
    ],
)
def test_ddp_own_node_contract_refused(overrides):
    with pytest.raises((ValueError, RuntimeError)):
        vt.validate_environment(args(strategy="ddp"), ddp_env(**overrides))


def test_distributed_context_requires_ddp_strategy():
    with pytest.raises(RuntimeError):
        vt.validate_environment(args(), environ(context={"distributed": DIST}))


def write_dataset(root):
    from PIL import Image

    items = []
    for i in range(4):
        path = root / f"{i}.png"
        Image.new("RGB", (300, 260), (40 * i, 10, 200)).save(path)
        items.append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "label": i % 2,
                "split": "train" if i < 3 else "holdout",
            }
        )
    manifest = {"schema": "ra-vision-dataset-v1", "classes": [1, 2], "items": items}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return vt.signature(manifest)


@needs_torch
def test_manifest_digest_and_file_hashes_enforced(tmp_path):
    digest = write_dataset(tmp_path)
    assert len(vt.load_manifest(tmp_path, digest)["items"]) == 4
    with pytest.raises(ValueError):
        vt.load_manifest(tmp_path, "sha256:" + "f" * 64)
    (tmp_path / "0.png").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        vt.load_manifest(tmp_path, digest)


@needs_torch
def test_preprocess_is_real_image_nchw(tmp_path):
    digest = write_dataset(tmp_path)
    data = vt.preprocess(tmp_path, vt.load_manifest(tmp_path, digest))
    assert tuple(data["train"][0].shape) == (3, 3, 224, 224)
    assert data["holdout"][1].tolist() == [1]


@needs_torch
def test_schedule_fixed_global_batch_and_rank_partition():
    first = vt.batch_schedule(80, 16, 12, seed=3)
    assert first == vt.batch_schedule(80, 16, 12, seed=3)
    assert all(len(b) == 16 for b in first) and len(first) == 12
    assert sorted(first[0] + first[1] + first[2] + first[3] + first[4]) == list(range(80))
    halves = [b[r * 8 : (r + 1) * 8] for b in first[:1] for r in range(2)]
    assert sorted(halves[0] + halves[1]) == sorted(first[0])


@needs_torch
@pytest.mark.parametrize("name", vt.MODELS)
def test_seeded_init_identical_and_update_changes_digest(name):
    import torch

    a, b = vt.build_model(name, 10, 5), vt.build_model(name, 10, 5)
    assert vt.checkpoint_digest(a) == vt.checkpoint_digest(b)
    assert vt.checkpoint_digest(vt.build_model(name, 10, 6)) != vt.checkpoint_digest(a)
    before = vt.checkpoint_digest(a)
    optimizer = torch.optim.AdamW(a.parameters(), lr=1e-3)
    a.train()
    loss = torch.nn.functional.cross_entropy(a(torch.rand(2, 3, 64, 64)), torch.tensor([0, 1]))
    loss.backward()
    optimizer.step()
    assert vt.checkpoint_digest(a) != before


@needs_torch
def test_no_cpu_fallback(monkeypatch):
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="no CPU fallback"):
        vt.run(args(), environ(RA_JOB_ID="j", RA_ATTEMPT_ID="a"))


def test_dataset_selection_deterministic():
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
    import build_vision_training_dataset as ds

    names = [f"{ds.PREFIX}{c}/{i:03d}.jpeg" for c in range(30) for i in range(12)]
    a = ds.select(names, 1, 10, 10, 8)
    assert a == ds.select(names, 1, 10, 10, 8)
    classes, picks = a
    assert len(classes) == 10 and len(picks) == 100
    assert sum(p[3] == "holdout" for p in picks) == 20
    assert {p[2] for p in picks} == set(range(10))


def bind(tmp_path, init, manifest):
    files = {}
    for name, value in (("checkpoint", init), ("input", manifest)):
        path = tmp_path / f"{name}.json"
        data = json.dumps(value).encode()
        path.write_bytes(data)
        path.chmod(0o444)
        files[name] = (path, "sha256:" + hashlib.sha256(data).hexdigest(), len(data))
    binding = {
        "checkpoint_digest": files["checkpoint"][1],
        "checkpoint_size_bytes": files["checkpoint"][2],
        "input_digest": files["input"][1],
        "input_size_bytes": files["input"][2],
    }
    return binding, files["checkpoint"][0], files["input"][0]


def test_bound_inputs_match_command(tmp_path):
    a = args()
    init = vt.init_descriptor(a.model, a.seed, 2, a.initial_checkpoint_digest)
    manifest = {"schema": "ra-vision-dataset-v1", "classes": [1, 2], "items": []}
    binding, cp, inp = bind(tmp_path, init, manifest)
    if __import__("os").geteuid() == 0:
        pytest.skip("root can write read-only files")
    assert vt.bound_inputs(a, binding, cp, inp) == (manifest, (True, True))


@pytest.mark.parametrize(
    "change",
    [{"model": "resnet18"}, {"seed": 8}, {"state_digest": "sha256:" + "2" * 64}, {"classes": 3}],
)
def test_bound_inputs_refuse_mismatch(tmp_path, change):
    a = args()
    init = vt.init_descriptor(a.model, a.seed, 2, a.initial_checkpoint_digest) | change
    manifest = {"schema": "ra-vision-dataset-v1", "classes": [1, 2], "items": []}
    binding, cp, inp = bind(tmp_path, init, manifest)
    with pytest.raises(ValueError):
        vt.bound_inputs(a, binding, cp, inp)


def test_bound_inputs_refuse_tampered_bytes(tmp_path):
    a = args()
    init = vt.init_descriptor(a.model, a.seed, 2, a.initial_checkpoint_digest)
    binding, cp, inp = bind(tmp_path, init, {"schema": "ra-vision-dataset-v1", "classes": [1, 2]})
    binding["input_digest"] = "sha256:" + "3" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        vt.bound_inputs(a, binding, cp, inp)


@needs_torch
def test_pinned_initial_state_overrides_seed(tmp_path):
    source = vt.build_model("mobilenet_v3_small", 10, 1)
    path = tmp_path / "init.pt"
    __import__("torch").save(source.state_dict(), path)
    loaded = vt.build_model("mobilenet_v3_small", 10, 2, str(path))
    assert vt.checkpoint_digest(loaded) == vt.checkpoint_digest(source)


def rank_record(rank, **overrides):
    record = {
        "rank": rank,
        "local_rank": 0,
        "node": f"node-{rank}",
        "pod_uid": f"uid-{rank}",
        "device_name": "GPU",
        "initial_checkpoint_digest": "sha256:" + "1" * 64,
        "output_checkpoint_digest": "sha256:" + "2" * 64,
        "output_parameter_digest": "sha256:" + "4" * 64,
        "loop_seconds": 1.0,
        "peak_memory_mib": 10.0,
        "losses_digest": f"sha256:{rank}",
    }
    record.update(overrides)
    return record


def test_rank_records_accept_distinct_members_with_identical_replicas():
    records = [rank_record(1), rank_record(0)]
    assert [r["rank"] for r in vt.check_ranks(records, 2)] == [0, 1]


def test_rank_local_batchnorm_buffers_may_differ_but_parameters_may_not():
    # DDP broadcasts rank 0 buffers before each forward; the last forward updates them locally.
    buffers = [rank_record(0), rank_record(1, output_checkpoint_digest="sha256:" + "5" * 64)]
    assert len(vt.check_ranks(buffers, 2)) == 2
    with pytest.raises(RuntimeError, match="diverged"):
        vt.check_ranks([rank_record(0), rank_record(1, output_parameter_digest="sha256:6")], 2)


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch not installed")
def test_parameter_digest_ignores_buffers_only():
    import torch

    model = torch.nn.Sequential(torch.nn.Conv2d(3, 4, 3), torch.nn.BatchNorm2d(4))
    before = vt.parameter_digest(model), vt.checkpoint_digest(model)
    model.train()
    with torch.no_grad():
        model(torch.rand(2, 3, 64, 64))  # BatchNorm running stats only
    assert vt.parameter_digest(model) == before[0] and vt.checkpoint_digest(model) != before[1]


@pytest.mark.parametrize(
    "bad",
    [
        [rank_record(0)],
        [rank_record(0), rank_record(0)],
        [rank_record(0), rank_record(1, node="node-0")],
        [rank_record(0), rank_record(1, pod_uid=None)],
        [rank_record(0), rank_record(1, output_parameter_digest="sha256:" + "3" * 64)],
        [rank_record(0), rank_record(1, initial_checkpoint_digest="sha256:" + "3" * 64)],
        [
            rank_record(0, output_checkpoint_digest="sha256:" + "1" * 64),
            rank_record(1, output_checkpoint_digest="sha256:" + "1" * 64),
        ],
    ],
)
def test_rank_records_refuse_missing_duplicate_or_diverged(bad):
    with pytest.raises(RuntimeError):
        vt.check_ranks(bad, 2)


def _gather_worker(rank, port, out):
    import torch.distributed as dist

    dist.init_process_group("gloo", init_method=f"tcp://127.0.0.1:{port}", rank=rank, world_size=2)
    try:
        out.put((rank, vt.gather_ranks(rank_record(rank))))
    finally:
        dist.destroy_process_group()


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch not installed")
def test_gather_ranks_collects_every_rank_over_a_real_process_group():
    import multiprocessing
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_gather_worker, args=(r, port, out)) for r in range(2)]
    for p in procs:
        p.start()
    results = dict(out.get(timeout=60) for _ in procs)
    for p in procs:
        p.join(timeout=60)
    assert results[0] == results[1]
    assert [r["pod_uid"] for r in results[0]] == ["uid-0", "uid-1"]


def test_staging_runs_between_two_barriers_and_reports_both_durations():
    calls = []

    def stage(a):
        calls.append("stage")
        a.data = "/staged"
        return {"node": "n", "bytes": 3}

    a = args()
    receipt, own, window = vt.run_staging(a, stage, barrier=lambda: calls.append("barrier"))
    assert calls == ["barrier", "stage", "barrier"]
    assert receipt == {"node": "n", "bytes": 3} and a.data == "/staged"
    assert 0 <= own <= window


@pytest.mark.parametrize("bad", [None, ["x"], {"x": float("nan")}])
def test_staging_receipt_must_be_a_json_object(bad):
    with pytest.raises((RuntimeError, ValueError)):
        vt.run_staging(args(), lambda a: bad, barrier=lambda: None)
