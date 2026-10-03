"""Synthetic trace contracts and CPU fallback guard; no synthetic GPU claim."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from resource_advisor import e5_benchmark
from resource_advisor.contracts import ThermalPolicy, signature
from resource_advisor.kernel_diagnostics import PREFIX, summarize_trace, union_duration


@pytest.fixture
def trace():
    events = []
    for i in range(3):
        start = 100 + i * 100
        events.extend(
            [
                {
                    "name": f"{PREFIX}{i:04d}",
                    "cat": "user_annotation",
                    "ph": "X",
                    "ts": start,
                    "dur": 80,
                },
                {
                    "name": "conv-kernel",
                    "cat": "kernel",
                    "ph": "X",
                    "ts": start + 10,
                    "dur": 40,
                    "args": {"device": 0, "stream": 7},
                },
                {
                    "name": "other-stream",
                    "cat": "kernel",
                    "ph": "X",
                    "ts": start + 30,
                    "dur": 40,
                    "args": {"device": 0, "stream": 8},
                },
                {"name": "ignored-cpu-op", "cat": "cpu_op", "ph": "X", "ts": start, "dur": 80},
            ]
        )
    return {"traceEvents": events, "private_metadata_not_exported": "example"}


def test_overlapping_kernels_use_interval_union_not_sum(trace):
    original = deepcopy(trace)
    report = summarize_trace(trace, 3)
    assert report["kernel_count"] == 6
    assert report["kernel_span_fraction"] == pytest.approx(0.75)
    assert report["kernel_union_seconds"] == pytest.approx(180e-6)
    assert report["samples"][0]["kernel_sum_seconds"] == pytest.approx(80e-6)
    assert report["samples"][0]["kernel_union_seconds"] == pytest.approx(60e-6)
    assert report["performance_profile_eligible"] is False
    assert report["auto_apply"] is False and report["resource_change"] is None
    assert "private_metadata_not_exported" not in report
    assert trace == original


def test_nested_and_disjoint_intervals():
    assert union_duration([(5, 8), (1, 10), (12, 15), (15, 17)]) == 14


def test_cpu_only_trace_rejected(trace):
    trace["traceEvents"] = [e for e in trace["traceEvents"] if e["cat"] != "kernel"]
    with pytest.raises(ValueError, match="CPU fallback"):
        summarize_trace(trace, 3)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "incomplete", "wrong_category", "overlap"]
)
def test_bad_forward_ranges_rejected(trace, mutation):
    if mutation == "missing":
        trace["traceEvents"].pop(0)
    elif mutation == "duplicate":
        trace["traceEvents"].append(deepcopy(trace["traceEvents"][0]))
    elif mutation == "incomplete":
        trace["traceEvents"][0]["ph"] = "B"
    elif mutation == "wrong_category":
        trace["traceEvents"][0]["cat"] = "cpu_op"
    else:
        trace["traceEvents"][0]["dur"] = 180
    with pytest.raises(ValueError):
        summarize_trace(trace, 3)


@pytest.mark.parametrize("device", [1, None, "0", 0.0, False])
def test_other_or_unidentified_device_rejected(trace, device):
    trace["traceEvents"][1]["args"]["device"] = device
    with pytest.raises(ValueError, match="CUDA device"):
        summarize_trace(trace, 3)


@pytest.mark.parametrize("value", [-1, 0, float("nan"), float("inf"), True])
def test_invalid_kernel_duration_rejected(trace, value):
    trace["traceEvents"][1]["dur"] = value
    with pytest.raises(ValueError, match="timestamp/duration"):
        summarize_trace(trace, 3)


def test_unowned_kernel_cannot_be_clipped_into_good_evidence(trace):
    trace["traceEvents"][1]["ts"] = 80
    with pytest.raises(ValueError, match="outside an owned"):
        summarize_trace(trace, 3)


def test_missing_cuda_activity_in_one_block_rejected(trace):
    trace["traceEvents"] = [
        e for e in trace["traceEvents"] if not (e["cat"] == "kernel" and e["ts"] > 300)
    ]
    with pytest.raises(ValueError, match="block has no CUDA"):
        summarize_trace(trace, 3)


def context():
    return {
        "allocation_mode": "physical_device",
        "parameters": {"input_strategy": "cache"},
        "resources": {"host_cpu": 1, "host_memory_mib": 2048, "accelerator_count": 1},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("allocation_mode", "shared"),
        ("parameters", {"input_strategy": "made-up"}),
        ("resources", {"host_cpu": 2, "host_memory_mib": 2048, "accelerator_count": 1}),
    ],
)
def test_runner_rejects_unapproved_configuration(field, value):
    c = context()
    c[field] = value
    with pytest.raises(ValueError):
        e5_benchmark.validate(c, [32, 3, 128, 128], 12, "fp32")


def test_runner_never_falls_back_to_cpu(monkeypatch):
    torch = pytest.importorskip("torch")
    policy = ThermalPolicy(
        driver_version="fixture",
        device_uuid_digest=signature("fixture-device"),
        maximum_temperature_c=80,
        maximum_gap_seconds=0.5,
        maximum_query_seconds=0.1,
    )
    for key, value in {
        "RA_CONTEXT_JSON": json.dumps(context()),
        "RA_INPUT_SHAPE": "[32,3,128,128]",
        "RA_WORK_UNITS": "12",
        "RA_PRECISION": "fp32",
        "RA_THERMAL_POLICY_JSON": policy.model_dump_json(),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="no CPU fallback"):
        e5_benchmark.run()


def test_fixed_schedule_has_three_independent_jobs_per_arm():
    np = pytest.importorskip("numpy")
    plan = json.loads(
        (Path(__file__).parents[1] / "docs/evidence/e5-kernel-plan-v1.json").read_text()
    )
    rng = np.random.default_rng(plan["seed"])
    expected = []
    for block in range(3):
        arms = plan["arms"].copy()
        rng.shuffle(arms)
        for arm in arms:
            expected.append({"unit_id": len(expected) + 1, "block": block, "arm": arm})
    assert expected == plan["schedule"]
    assert len(expected) + len(plan["qualification"]) == plan["maximum_jobs"] == 14
    assert (
        plan["maximum_jobs"] * plan["per_job_max_run_seconds"]
        == plan["maximum_gpu_reservation_seconds"]
    )
