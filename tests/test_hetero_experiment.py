"""Frozen experiment harness and read-only collectors (software only, no live jobs)."""

from collections import Counter

import pytest

from resource_advisor.contracts import signature
from resource_advisor.hetero_collect import (
    link_command,
    link_measurement,
    node_fingerprints,
    slurm_fingerprints,
)
from resource_advisor.hetero_experiment import (
    Runner,
    analyze,
    freeze_plan,
    render_reports,
    rerun_commands,
    scenario_status,
)

# Shape of the 2026-10-29 read-only observation: no free GPU training route,
# no measured links, Slurm VM CPU only, two exposed NPU inference routes.
CAPACITY = {
    "observed_at": "2026-10-29T02:31:00+00:00",
    "expected_job_seconds": 20,
    "facts": {
        "gpu_training_routes": 0,
        "exclusive_gpus": 0,
        "measured_data_links": 0,
        "equivalent_inference_routes": 2,
        "slurm_cpu_training": 1,
        "kubernetes_cpu_training": 1,
    },
    "bindings": {
        "E5": {
            "workload_ref": "infer-family",
            "static_candidate_ref": "hailo-b",
            "candidate_order": ["hailo-b", "mobilint"],
            "profiling_routes": ["hailo-b", "mobilint"],
            "profiles": {"B2": "legacy", "B3": "rec-b3", "B4": "rec-b4"},
        },
        "E6": {
            "workload_ref": "cpu-train",
            "static_candidate_ref": "k8s-cpu",
            "candidate_order": ["k8s-cpu", "slurm-vm"],
            "profiling_routes": ["k8s-cpu", "slurm-vm"],
            "profiles": {"B2": "legacy", "B3": "rec-b3", "B4": "rec-b4"},
        },
    },
}


def test_frozen_plan_lists_blocked_cases_and_stays_inside_the_budget():
    plan, digest = freeze_plan(CAPACITY, seed=20261029)
    assert digest == signature(plan)
    status = {k: v["status"] for k, v in plan["scenarios"].items()}
    assert status["E1"] == status["E2"] == status["E3"] == "BLOCKED"
    assert status["E4"] == "NOT_VALIDATED"
    assert plan["planned"]["jobs"] <= 60
    assert plan["planned"]["slot_seconds"] <= 1800
    # Network arm is dropped without measured links rather than run on unknown cost.
    assert all(u["arm"] != "B5" for u in plan["units"])
    assert len(plan["profiling"]) == 4
    assert set(rerun_commands(plan, "plan.json")) == {"E1", "E2", "E3", "E4"}
    # 4 profiling + 2 scenarios x 5 arms x 3 blocks x 1 job.
    assert status["E5"] == status["E6"] == "PLANNED"
    assert plan["planned"] == {"jobs": 34, "slot_seconds": 680.0, "wall_seconds": 680.0}


def test_smaller_budget_prospectively_defers_the_lower_priority_scenario():
    plan, _ = freeze_plan(CAPACITY, seed=1, budget={"max_jobs": 25})
    assert plan["scenarios"]["E6"]["status"] == "PLANNED"
    assert plan["scenarios"]["E5"] == {"status": "DEFERRED_BUDGET", "reasons": ["BUDGET"]}
    assert plan["planned"]["jobs"] == 19


def test_each_block_contains_every_arm_once_in_a_seeded_order():
    plan, _ = freeze_plan(CAPACITY, seed=7)
    again, _ = freeze_plan(CAPACITY, seed=7)
    assert plan["units"] == again["units"]
    blocks = Counter((u["scenario"], u["pattern"], u["block"]) for u in plan["units"])
    arms = {
        s: len(v["arms"])
        for s, v in plan["scenarios"].items()
        if v["status"] == "PLANNED" and "arms" in v
    }
    assert all(count == arms[key[0]] for key, count in blocks.items())
    orders = {
        key: [u["arm"] for u in plan["units"] if (u["scenario"], u["pattern"], u["block"]) == key]
        for key in blocks
    }
    assert len({tuple(v) for v in orders.values()}) > 1


class FakeClient:
    def __init__(self, terminal="SUCCEEDED"):
        self.calls, self.terminal, self.n = [], terminal, 0

    def post(self, path, body, idempotency_key=None):
        self.calls.append(("POST", path, body, idempotency_key))
        if path == "/scheduling-plans":
            return {"accepted": True, "digest": "sha256:" + "1" * 64}
        if path == "/jobs":
            self.n += 1
            return {"job_id": f"j-{self.n}", "state": "VALIDATED"}
        return {}

    def get(self, path):
        self.calls.append(("GET", path))
        return {
            "state": self.terminal,
            "native_timestamps": {
                "created_at": "2026-10-29T05:00:00+00:00",
                "scheduler_submitted_at": "2026-10-29T05:00:01+00:00",
                "started_at": "2026-10-29T05:00:03+00:00",
                "backend_finished_at": "2026-10-29T05:00:13+00:00",
            },
            "recommendation": None,
            "allocation": {"accelerator_count": 1},
        }


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_runner_refuses_unfrozen_plans_and_dry_runs_without_any_call():
    plan, digest = freeze_plan(CAPACITY, seed=1)
    with pytest.raises(ValueError, match="digest"):
        Runner(plan, "sha256:" + "0" * 64, FakeClient())
    client, out = FakeClient(), []
    Runner(plan, digest, client, write=out.append).run()
    assert client.calls == [] and len(out) == len(plan["units"])
    assert {r["source"] for r in out} == {"dry_run"}


def test_runner_submits_through_the_api_and_cancels_only_its_own_late_jobs():
    plan, digest = freeze_plan(CAPACITY, seed=1)
    clock, out = Clock(), []
    client = FakeClient(terminal="RUNNING")
    runner = Runner(
        plan,
        digest,
        client,
        clock=clock,
        sleep=clock.sleep,
        write=out.append,
        capture=lambda: {"pods": ["other-project/svc"]},
    )
    unit = next(u for u in plan["units"] if u["arm"] == "B0")
    plan_units = plan["units"]
    runner.plan = dict(plan, units=[unit])
    runner.run(execute=True)
    runner.plan["units"] = plan_units
    posts = [c for c in client.calls if c[0] == "POST"]
    assert posts[0][1] == "/scheduling-plans" and posts[1][1] == "/jobs"
    assert posts[1][2]["candidate_ref"] == "hailo-b" or posts[1][2]["candidate_ref"] == "k8s-cpu"
    assert posts[1][3].startswith(digest[7:19])
    assert posts[-1][1] == "/jobs/j-1/cancel"
    assert out[-1]["jobs"]["j-1"]["censored"] == "TIMEOUT_CANCELLED_BY_RUNNER"
    assert out[-1]["external_state_before"] == {"pods": ["other-project/svc"]}


def test_analysis_rejects_replay_and_keeps_censored_jobs_in_the_denominator():
    plan, digest = freeze_plan(CAPACITY, seed=1)
    unit = plan["units"][0]
    with pytest.raises(ValueError, match="live"):
        analyze(plan, [{**unit, "source": "dry_run", "plan_digest": digest}])
    job = FakeClient().get("")
    records = [
        {
            **unit,
            "source": "live",
            "plan_digest": digest,
            "jobs": {"a": job},
            "submissions": [{"job_id": "a"}],
        },
        {
            **unit,
            "source": "live",
            "plan_digest": digest,
            "jobs": {"b": {**job, "state": "CANCELED", "censored": "TIMEOUT_CANCELLED_BY_RUNNER"}},
            "submissions": [{"job_id": "b"}],
        },
    ]
    table = analyze(plan, records).splitlines()
    row = dict(zip(table[0].split(","), table[1].split(","), strict=True))
    assert row["completed"] == "1" and row["censored"] == "1" and row["planned"] == "2"
    assert row["completion_rate"] == "0.5" and row["jct_completed_mean"] == "12.0"


def test_inventory_keeps_logical_slots_and_unverified_runtime_separate():
    nodes = {
        "items": [
            {
                "metadata": {"name": "orin", "labels": {"nvidia.com/gpu.product": "Orin"}},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "nodeInfo": {"architecture": "arm64"},
                    "allocatable": {"nvidia.com/gpu.shared": "2", "cpu": "6"},
                },
            }
        ]
    }
    [node] = node_fingerprints(nodes)
    assert node["devices"] == [
        {
            "resource_key": "nvidia.com/gpu.shared",
            "device_class": "gpu",
            "allocation_mode": "virtual_slot",
            "allocatable": 2,
            "count_unit": "logical_slot",
        }
    ]
    assert node["runtime_verified"] is False and node["driver_sdk_firmware"] is None
    [slurm] = slurm_fingerprints("vm1|(null)|(null)|2|3072|idle\n")
    assert slurm["gres"] == [] and slurm["memory_mib"] == 3072


def test_link_probe_is_capped_and_a_capped_result_is_only_a_lower_bound():
    with pytest.raises(ValueError):
        link_command("file:///etc/passwd", max_bytes=1, max_seconds=1, limit_rate_bytes=1)
    with pytest.raises(ValueError):
        link_command("http://store/obj", max_bytes=2**31, max_seconds=1, limit_rate_bytes=1)
    command = link_command("http://store/obj", max_bytes=1000, max_seconds=5, limit_rate_bytes=100)
    assert "--limit-rate" in command and "--max-time" in command
    measured = link_measurement(
        "1000 0.5 10.5", source="s", target="t", evidence_ref="e", limit_rate_bytes=100
    )
    assert measured["link"]["bandwidth_bytes_per_second"] == 100.0
    assert measured["link"]["request_overhead_ms"] == 500.0
    assert measured["provenance"]["rate_capped"] is True


def test_reports_render_only_analysis_outputs_without_superiority_wording():
    plan, digest = freeze_plan(CAPACITY, seed=1, budget={"max_jobs": 25})
    unit = plan["units"][0]
    job = FakeClient().get("")
    table = analyze(
        plan,
        [
            {
                **unit,
                "source": "live",
                "plan_digest": digest,
                "jobs": {"a": job},
                "submissions": [{"job_id": "a"}],
            }
        ],
    )
    results, summary = render_reports(plan, table, scenario_status(plan))
    assert digest in results and "| E5 | equivalent inference" in results
    assert "live cell 수: 1" in summary
    assert "E5=DEFERRED_BUDGET" in summary
    text = (results + summary).lower()
    assert not any(claim in text for claim in ("outperform", "better than", "우월하다", "개선되었다"))
