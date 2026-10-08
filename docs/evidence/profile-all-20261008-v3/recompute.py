"""Recompute recorded descriptive metrics from CSV; submits no hardware work."""

import csv
import json
import math
import statistics
import tarfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def rows(name):
    with (ROOT / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


def summary(records, jct_key, compute_key, reservation_key):
    jcts = [float(r[jct_key]) for r in records]
    return {
        "jobs": len(records),
        "mean_jct_seconds": statistics.mean(jcts),
        "p95_jct_seconds": sorted(jcts)[math.ceil(len(jcts) * 0.95) - 1],
        "compute_seconds_sum": sum(float(r[compute_key]) for r in records),
        "reservation_seconds_sum": sum(float(r[reservation_key]) for r in records),
    }


def native_rows():
    """Read preserved owned receipts; never extract arbitrary archive paths."""
    with tarfile.open(ROOT / "native-records-public.tar.gz", "r:gz") as archive:
        capture = json.load(archive.extractfile("mlp/capture.json"))
        corrected = rows("npu-corrected-rows.csv")
        for row in corrected:
            pods = json.load(
                archive.extractfile("npu-groups/jobs/" + row["attempt"] + "/pods.json")
            )["items"]
            assert len(pods) == 1
            pod = pods[0]
            scheduled = [
                c["lastTransitionTime"]
                for c in pod["status"]["conditions"]
                if c["type"] == "PodScheduled" and c["status"] == "True"
            ]
            assert len(scheduled) == 1
            end = pod["status"]["containerStatuses"][0]["state"]["terminated"]
            assert end["exitCode"] == 0
            assert epoch(end["finishedAt"]) - epoch(scheduled[0]) == float(
                row["allocated_device_seconds_point"]
            )
    saved = {}
    for cohort in capture["cohorts"]:
        for job in cohort["jobs"]:
            receipt = job["native_receipt"]
            result = receipt["result"]
            assert result["measured"] and result["outcome"] == "COMPLETED"
            assert result["quality"] == 1 and result["accuracy"] == capture["plan"]["accuracy"]
            assert result["fixture_sha256"] == capture["plan"]["fixture_sha256"]
            assert len(result["round_seconds"]) == result["rounds"] == 16384
            assert all(math.isfinite(t) and t >= 0 for t in result["round_seconds"])
            assert math.isclose(
                sum(result["round_seconds"]), result["elapsed_seconds"], abs_tol=1e-9
            )
            if job["backend"] == "kubernetes":
                accepted = epoch(receipt["job"]["metadata"]["creationTimestamp"])
                pod = receipt["pod"]
                scheduled = next(
                    c["lastTransitionTime"]
                    for c in pod["status"]["conditions"]
                    if c["type"] == "PodScheduled" and c["status"] == "True"
                )
                started = epoch(scheduled)
                finished = epoch(
                    pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
                )
                assert any(
                    o["uid"] == receipt["job"]["metadata"]["uid"]
                    for o in pod["metadata"]["ownerReferences"]
                )
            else:
                native = receipt["native_accounting"]
                accepted, started, finished = (
                    epoch(native[key]) for key in ("submit", "start", "end")
                )
            if job["runtime"] == "openvino":
                assert result["execution_devices"] == ["NPU"] and result["quality_passed"]
            saved[job["attempt_id"]] = (
                finished - accepted,
                finished - started,
                result["elapsed_seconds"],
            )
    return saved


def epoch(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.timestamp()


def main():
    native = native_rows()
    mlp = defaultdict(list)
    for row in rows("mlp-main.csv"):
        assert row["outcome"] == "SUCCEEDED" and float(row["quality"]) == 1
        assert int(row["requested_forwards"]) == 16384 and int(row["images"]) == 4194304
        observed = native[row["attempt_id"]]
        assert observed[:2] == (float(row["jct_seconds"]), float(row["reservation_seconds"]))
        assert math.isclose(observed[2], float(row["synchronized_compute_seconds"]), abs_tol=1e-9)
        mlp[row["arm"]].append(row)
    results = {
        arm: summary(jobs, "jct_seconds", "synchronized_compute_seconds", "reservation_seconds")
        for arm, jobs in mlp.items()
    }
    archived = json.loads((ROOT / "mlp-summary.json").read_text())
    for arm, metrics in results.items():
        expected = archived["arms"][arm]
        for key in ("jobs", "mean_jct_seconds", "p95_jct_seconds"):
            assert math.isclose(metrics[key], expected[key], rel_tol=1e-12)
        units = defaultdict(float)
        for row in mlp[arm]:
            units[row["reservation_unit"]] += float(row["reservation_seconds"])
        assert dict(units) == expected["reservation_seconds"]
        metrics.pop("reservation_seconds_sum")
        metrics["reservation_seconds_by_unit"] = dict(units)
    results["observed_compute_reduction_percent"] = 100 * (
        1 - results["reuse"]["compute_seconds_sum"] / results["baseline"]["compute_seconds_sum"]
    )
    npu = defaultdict(list)
    profiling_cost = 0.0
    for row in rows("npu-corrected-rows.csv"):
        assert row["qualified"] == "True"
        if row["phase"] == "profiling":
            profiling_cost += float(row["allocated_device_seconds_point"])
        else:
            npu[(row["group"], row["arm"])].append(row)
    corrected = json.loads((ROOT / "npu-corrected-summary.json").read_text())
    npu_results = []
    for cohort in corrected["cohorts"]:
        key = (cohort["group"], cohort["arm"])
        metrics = summary(
            npu[key],
            "native_jct_seconds_point",
            "measured_inference_seconds",
            "allocated_device_seconds_point",
        )
        for field, value in metrics.items():
            assert math.isclose(value, cohort[field], rel_tol=1e-12)
        npu_results.append(dict(group=key[0], arm=key[1], **metrics))
    assert profiling_cost == corrected["profiling_allocated_device_seconds_point"]
    assert (
        sum(r["reservation_seconds_sum"] for r in npu_results)
        == corrected["main_allocated_device_seconds_point"]
    )
    print(
        json.dumps(
            dict(
                mlp=results,
                npu=npu_results,
                npu_profiling_device_seconds=profiling_cost,
                measured_hardware_reexecution=False,
                statistical_significance_established=False,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
