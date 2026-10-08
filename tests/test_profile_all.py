"""Public protocol and native-result boundaries for the two-arm rerun."""

import copy
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from run_profile_all import (
    freeze_microprofiles,
    qualify_receipt,
    run,
    select_assignments,
    validate_plan,
)


def plan_fixture():
    return {
        "experiment_id": "mlp-all-test",
        "seed": 20261008,
        "fixture_sha256": "f" * 64,
        "source_sha256": "c" * 64,
        "kernel_sha256": "k" * 64,
        "accuracy": 0.98046875,
        "qualification_rounds": 8,
        "main_rounds": 16384,
        "jobs_per_arm": 2,
        "limits": {"per_job_seconds": 600, "protocol_seconds": 1800},
        "pool": [
            {
                "ref": "gpu",
                "node": "gpu-node",
                "backend": "kubernetes",
                "runtime": "cuda",
                "resource_key": "nvidia.com/gpu",
                "image": "cuda@sha256:" + "a" * 64,
                "arch": "amd64",
                "nominal_slots": 1,
                "source_configmap": "old-cuda-source",
                "qualification_file": "qualifications/gpu.json",
                "qualification_attempt": "micro-gpu",
                "template_file": "templates/gpu.json",
            },
            {
                "ref": "intel",
                "node": "intel-node",
                "backend": "kubernetes",
                "runtime": "openvino",
                "resource_key": "intel.com/npu",
                "image": "openvino@sha256:" + "b" * 64,
                "arch": "amd64",
                "nominal_slots": 1,
                "source_configmap": "npu-mlp-source",
                "source_sha256": "n" * 64,
                "qualification_file": "qualifications/intel.json",
                "qualification_attempt": "micro-intel",
                "template_file": "templates/intel.json",
            },
        ],
    }


def test_shared_mlp_protocol_rejects_a_different_npu_workload():
    plan = plan_fixture()
    assert validate_plan(plan) == plan
    incompatible = copy.deepcopy(plan)
    incompatible["pool"][1]["runtime"] = "hailo-resnet50"
    with pytest.raises(ValueError, match="compatible MLP runtime"):
        validate_plan(incompatible)


def qualification(plan, node, elapsed, startup):
    def stamp(value):
        return datetime.fromtimestamp(value, UTC).isoformat()

    name = node["qualification_attempt"]
    result = {
        "measured": True,
        "evidence_kind": "hardware",
        "outcome": "COMPLETED",
        "fixture_sha256": plan["fixture_sha256"],
        "kernel_sha256": node.get("source_sha256", plan["kernel_sha256"]),
        "accuracy": plan["accuracy"],
        "quality": 1,
        "quality_passed": True,
        "numerical_close": True,
        "arch": node["arch"],
        "batch_size": 256,
        "runtime_versions": {"test_runtime": "1"},
        "rounds": 8,
        "images": 2048,
        "elapsed_seconds": elapsed,
        "round_seconds": [elapsed / 8] * 8,
        "compute_started_at": 100 + startup,
        "compute_finished_at": 100 + startup + elapsed,
        "source_sha256": node.get("source_sha256", plan["source_sha256"]),
        "execution_devices": ["NPU"] if node["runtime"] == "openvino" else ["CUDA"],
        "model_sha256": "m" * 64,
        "input_sha256": "i" * 64,
        "compiled_sha256": "b" * 64,
        "sensor": [],
    }
    container = {
        "image": node["image"],
        "command": ["python", "--rounds", "8"],
        "resources": {key: {node["resource_key"]: "1"} for key in ("requests", "limits")},
    }
    spec = {
        "nodeName": node["node"],
        "nodeSelector": {"kubernetes.io/hostname": node["node"]},
        "containers": [container],
        "volumes": [{"configMap": {"name": node["source_configmap"]}}],
    }
    native_job = {
        "metadata": {
            "name": name,
            "uid": name + "-uid",
            "creationTimestamp": stamp(99),
            "labels": {"resource-advisor/experiment": plan["experiment_id"]},
        },
        "spec": {"backoffLimit": 0, "template": {"spec": spec}},
        "status": {"succeeded": 1},
    }
    pod = {
        "metadata": {"ownerReferences": [{"kind": "Job", "uid": name + "-uid"}]},
        "spec": spec,
        "status": {
            "phase": "Succeeded",
            "conditions": [
                {"type": "PodScheduled", "status": "True", "lastTransitionTime": stamp(100)}
            ],
            "containerStatuses": [
                {
                    "state": {
                        "terminated": {"exitCode": 0, "finishedAt": stamp(100 + startup + elapsed)}
                    }
                }
            ],
        },
    }
    return {
        "attempt_id": name,
        "candidate_ref": node["ref"],
        "native_id": name,
        "backend": node["backend"],
        "requested_work": 8,
        "native_receipt": {"job": native_job, "pod": pod, "result": result},
    }


def test_microprofiles_scale_compute_once_and_reuse_static_cohort_backlog():
    plan = plan_fixture()
    qualifications = [
        qualification(plan, plan["pool"][0], 0.0048828125, 2),
        qualification(plan, plan["pool"][1], 0.001953125, 1),
    ]
    profiles = freeze_microprofiles(plan, qualifications)
    assert profiles["gpu"]["predicted_service_seconds"] == pytest.approx(12, abs=1e-6)
    assert profiles["intel"]["predicted_service_seconds"] == pytest.approx(5, abs=1e-6)
    assert select_assignments(plan, profiles, "baseline") == ["gpu", "intel"]
    assert select_assignments(plan, profiles, "reuse") == ["intel", "intel"]


def prepare_archive(directory):
    plan = plan_fixture()
    for node in plan["pool"]:
        job = qualification(plan, node, 0.001953125, 1)
        log = "POOL_INFERENCE_RESULT " + json.dumps(job["native_receipt"]["result"]) + "\n"
        log_file = "qualification-" + node["ref"] + ".log"
        (directory / log_file).write_text(log)
        job["native_receipt"].update(
            log_file=log_file, log_sha256=hashlib.sha256(log.encode()).hexdigest()
        )
        target = directory / node["qualification_file"]
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(job))
        template = copy.deepcopy(job["native_receipt"]["job"])
        template["spec"]["template"]["metadata"] = {"labels": {}}
        target = directory / node["template_file"]
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(template))
    (directory / "plan.json").write_text(json.dumps(plan))
    return plan


class UncertainNative:
    """External native-system seam: acceptance response is lost."""

    def __init__(self):
        self.submitted = []

    def preflight(self):
        return {"source_verified": True}

    def snapshot(self, known):
        return {
            "observed_at": 100,
            "errors": [],
            "jobs": [],
            "pods": [],
            "workloads": [],
            "all_pods": [],
            "slurm_rows": [],
            "accounting_rows": [],
            "clusterqueues": [],
        }

    def submit(self, item, node, manifest):
        self.submitted.append(item["attempt_id"])
        raise TimeoutError("native response lost")

    def stop_owned(self, known):
        return {"errors": [], "retained_intents": [i["attempt_id"] for i in known]}


def test_uncertain_native_acceptance_preserves_intents_and_cannot_be_replayed(tmp_path):
    prepare_archive(tmp_path)
    native = UncertainNative()
    with pytest.raises(ValueError, match="uncertain"):
        run(tmp_path, transport=native)
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert len(capture["attempts"]) == 2
    assert len(set(native.submitted)) == len(native.submitted) == 2
    assert all(i["submit_error"] == "native response lost" for i in capture["attempts"])
    assert (tmp_path / "intent-ledger.jsonl").read_text().count("attempt_id") == 2
    before = (tmp_path / "capture.json").read_bytes()
    with pytest.raises(ValueError, match="never replay"):
        run(tmp_path, transport=native)
    assert (tmp_path / "capture.json").read_bytes() == before


class CompletedNative(UncertainNative):
    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def submit(self, item, node, manifest):
        self.submitted.append(item["attempt_id"])
        item["native_id"] = item["attempt_id"]

    def receipt(self, item, node, snapshot):
        job = qualification(self.plan, node, 2, 1)
        job.update(item)
        raw = job["native_receipt"]
        raw["job"]["metadata"]["name"] = item["attempt_id"]
        raw["job"]["metadata"]["uid"] = item["attempt_id"] + "-uid"
        raw["pod"]["metadata"]["ownerReferences"][0]["uid"] = item["attempt_id"] + "-uid"
        raw["pod"]["spec"]["containers"][0]["command"][-1] = "16384"
        result = raw["result"]
        result.update(rounds=16384, images=4194304, round_seconds=[2 / 16384] * 16384)
        log = "POOL_INFERENCE_RESULT " + json.dumps(result) + "\n"
        return qualify_receipt(item, raw, self.plan, node, log)


def test_equal_work_two_arm_run_exports_native_metrics_and_frozen_choices(tmp_path):
    plan = prepare_archive(tmp_path)
    state = run(tmp_path, transport=CompletedNative(plan))
    assert {c["arm"]: len(c["jobs"]) for c in state["cohorts"]} == {"baseline": 2, "reuse": 2}
    baseline = next(c for c in state["cohorts"] if c["arm"] == "baseline")
    assert [j["candidate_ref"] for j in baseline["jobs"]] == ["gpu", "intel"]
    assert all(j["details"]["images"] == 4194304 for j in state["attempts"])
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["statistical_significance_established"] is False
    assert summary["arms"]["baseline"]["jobs"] == 2
    assert (tmp_path / "main.csv").read_text().count("SUCCEEDED") == 4
    assert (tmp_path / "frozen-profiles-and-schedule.json").exists()


@pytest.mark.parametrize("tamper", ["cpu_fallback", "quality", "source", "owner", "work"])
def test_ineligible_native_microprofile_cannot_enter_either_arm(tamper):
    plan = plan_fixture()
    qualifications = [qualification(plan, node, 0.01, 1) for node in plan["pool"]]
    receipt = qualifications[1]["native_receipt"]
    if tamper == "cpu_fallback":
        receipt["result"]["execution_devices"] = ["NPU", "CPU"]
    elif tamper == "quality":
        receipt["result"]["numerical_close"] = False
    elif tamper == "source":
        receipt["result"]["source_sha256"] = "foreign"
    elif tamper == "owner":
        receipt["pod"]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    else:
        receipt["result"]["rounds"] = 7
    with pytest.raises(ValueError):
        freeze_microprofiles(plan, qualifications)


def test_colocated_gpu_and_npu_remain_distinct_physical_accelerator_candidates():
    plan = plan_fixture()
    plan["pool"][1]["node"] = plan["pool"][0]["node"]
    assert validate_plan(plan) == plan
    plan["pool"][1]["resource_key"] = plan["pool"][0]["resource_key"]
    with pytest.raises(ValueError, match="duplicate physical candidate"):
        validate_plan(plan)
