"""Validate archived Hailo evidence and render an operator import; no API writes.

Inputs are private execution snapshots, not proof from an independently trusted
attestor. The API labels the resulting record as operator-imported evidence.
"""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from resource_advisor.hailo_qualification import quality_gates
from resource_advisor.qualifications import QualificationImport


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(args):
    report = json.loads(args.report.read_text())
    manifest = json.loads(args.manifest.read_text())
    job = json.loads(args.job.read_text())
    pods = json.loads(args.pods.read_text())["items"]
    workload = json.loads(args.workload.read_text())
    image = json.loads(args.image_report.read_text())
    logs = [
        json.loads(line.split(" ", 1)[1])
        for line in args.log.read_text().splitlines()
        if line.startswith("RESOURCE_ADVISOR_HAILO_REPORT ")
    ]
    if logs != [report] or report["kind"] != "hailo-model-qualification":
        raise ValueError("exactly one log report matching the archived report is required")
    if report["manifest_sha256"] != digest(args.manifest):
        raise ValueError("input manifest digest mismatch")
    if len(pods) != 1:
        raise ValueError("exactly one captured Pod required")
    pod = pods[0]
    uid = job["metadata"]["uid"]
    for item in (pod, workload):
        if item["metadata"]["namespace"] != job["metadata"]["namespace"] or not any(
            owner["uid"] == uid for owner in item["metadata"].get("ownerReferences", [])
        ):
            raise ValueError("execution evidence has foreign ownership")
    status = pod["status"]["containerStatuses"]
    if len(status) != 1 or len(pod["spec"]["containers"]) != 1:
        raise ValueError("one measured container required")
    state = status[0]["state"]["terminated"]
    actual_image = status[0]["imageID"].split("@")[-1]
    if image["image"].rsplit("@", 1)[-1] != actual_image:
        raise ValueError("actual image differs from the captured image manifest")
    for path, value in [
        ("opt/fixture/manifest.json", report["manifest_sha256"]),
        ("opt/fixture/model.hef", report["hef_sha256"]),
        ("opt/fixture/inputs.npy", manifest["inputs_sha256"]),
        ("opt/fixture/reference.npy", manifest["reference_sha256"]),
    ]:
        if image["files"].get(path) != value:
            raise ValueError("fixture differs from the captured image contents")
    if report["original_model_sha256"] != manifest["model_sha256"]:
        raise ValueError("original model digest mismatch")
    rows = report["predictions"]
    for row, sample in zip(rows, manifest["samples"], strict=True):
        if (row["input_sha256"], row["label"], row["reference_top1"]) != (
            sample["tensor_sha256"],
            sample["label"],
            sample["reference_top1"],
        ):
            raise ValueError("per-image input/reference binding mismatch")
    quality = quality_gates(
        [r["prediction"] for r in rows],
        [r["label"] for r in rows],
        [r["reference_top1"] for r in rows],
    )
    if quality != report["quality"] or state["exitCode"] != (0 if quality["qualified"] else 2):
        raise ValueError("verdict or container exit code disagrees with measured outputs")
    if pod["spec"]["containers"][0]["resources"]["requests"].get("hailo.ai/h8") != "1":
        raise ValueError("one allocated Hailo device is required")
    times = {
        key: next(
            c["lastTransitionTime"]
            for c in workload["status"]["conditions"]
            if c["type"] == key and c["status"] == "True"
        )
        for key in ("Admitted", "Finished")
    }
    scheduled = next(
        c["lastTransitionTime"]
        for c in pod["status"]["conditions"]
        if c["type"] == "PodScheduled" and c["status"] == "True"
    )

    def seconds(start, end):
        return (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()

    value = QualificationImport(
        ref=args.ref,
        project_ref=args.project,
        evidence_kind=report["evidence_kind"],
        model_name=args.model_name,
        accelerator_model=report["architecture"],
        device_class="npu",
        model_digest="sha256:" + report["original_model_sha256"],
        compiled_artifact_digest="sha256:" + report["hef_sha256"],
        image_digest=actual_image,
        runner_digest="sha256:" + image["files"]["opt/qualification/hailo_qualification.py"],
        input_manifest_digest="sha256:" + report["manifest_sha256"],
        source_report_digest="sha256:" + digest(args.report),
        plan_ref=args.plan_ref,
        runtime_versions={
            k: report[k] for k in ("hailort", "driver", "firmware", "python", "numpy")
        },
        backend="kubernetes",
        external_job_ref=job["metadata"]["name"],
        external_job_uid=uid,
        started_at=state["startedAt"],
        finished_at=state["finishedAt"],
        container_exit_code=state["exitCode"],
        measurement_boundary=report["measurement_boundary"],
        host_process_peak_rss_mib=report["host_process_peak_rss_mib"],
        scheduled_to_container_finished_seconds=seconds(scheduled, state["finishedAt"]),
        admission_to_workload_finished_seconds=seconds(times["Admitted"], times["Finished"]),
        expected_images=report["measured_images"],
        gates={
            "minimum_accuracy": 0.75,
            "minimum_reference_agreement": 0.90,
            "maximum_accuracy_loss": 0.05,
        },
        samples=[
            {
                "input_digest": "sha256:" + r["input_sha256"],
                "output_digest": "sha256:" + r["output_sha256"],
                **{k: r[k] for k in ("label", "reference_top1", "prediction", "elapsed_seconds")},
            }
            for r in rows
        ],
    )
    return value.model_dump(mode="json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("report", "manifest", "job", "pods", "workload", "image-report", "log"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("ref", "project", "model-name", "plan-ref"):
        parser.add_argument("--" + name, required=True)
    print(json.dumps(build(parser.parse_args()), indent=2))
