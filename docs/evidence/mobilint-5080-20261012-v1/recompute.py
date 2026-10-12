"""Recompute descriptive metrics; these models have unestablished equivalence."""
import argparse
import base64
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import tarfile
import zlib


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def calculate(root):
    capture = json.loads((root / "capture.json").read_text())
    protocol = json.loads((root / "protocol.json").read_text())
    source = (root / "probe.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == protocol["source_sha256"]
    assert protocol["revision"] == 4
    assert len(capture["main"]) == 6
    assert [(x["block"], x["arm"]) for x in capture["main"]] == [
        (x["block"], x["arm"]) for x in protocol["schedule"]]
    expected_input = "005d18ceb13076219f5a4cead7611755c19859334d0671e7545cbded1bad6edc"
    groups = {"mobilint": [], "cuda": []}
    rows = []
    with tarfile.open(root / "native-records-public.tar.gz") as archive:
        manifest = json.loads((root / "native-files-sha256.json").read_text())
        assert set(archive.getnames()) == {entry["path"] for entry in manifest}
        for entry in manifest:
            raw = archive.extractfile(entry["path"]).read()
            assert hashlib.sha256(raw).hexdigest() == entry["public_sha256"]
        current_cm = json.load(archive.extractfile("source-configmap-native-v3.json"))
        assert current_cm["immutable"] is True
        assert current_cm["data"]["probe.py"].encode() == source
        for item in capture["main"]:
            receipt = item["receipt"]
            result = receipt["result"]
            archived_receipt = json.load(archive.extractfile("jobs/" + item["name"] + "/receipt.json"))
            assert receipt == archived_receipt
            assert receipt["native_succeeded"]
            assert result["runtime"] == item["arm"]
            assert result["input_sha256"] == expected_input
            assert result["model_equivalence"] == "not-established"
            model_key = "npu_model_sha256" if item["arm"] == "mobilint" else "gpu_model_sha256"
            assert result["model_sha256"] == protocol[model_key]
            assert len(result["samples_seconds"]) == 30
            assert len(result["warmup_seconds"]) == 5
            assert result["repeat_element_agreement"] == 1
            assert result["output_c_contiguous"] is True
            assert result["boundary"] == protocol["boundary"]
            output = zlib.decompress(base64.b64decode(result["output_zlib_base64"]))
            assert len(output) == 224 * 344 * 3 * 4
            digest = hashlib.sha256(output).hexdigest()
            assert result["output_sha256"] == [digest] * 30
            for index, seconds in enumerate(result["samples_seconds"]):
                assert math.isfinite(seconds) and seconds > 0
                rows.append({"block": item["block"], "runtime": item["arm"],
                             "sample": index, "seconds": seconds, "output_sha256": digest})
            if item["arm"] == "cuda":
                assert "RTX 5080" in receipt["gpu_identity"]["name"]
                counts = result["metadata"]["placement_counts"]
                assert sum(x["count"] for x in counts) == 64
                assert all(x["provider"] == "CUDAExecutionProvider" for x in counts)
                assert result["metadata"]["provider_options"]["CUDAExecutionProvider"]["use_tf32"] == "0"
            job = json.load(archive.extractfile("jobs/" + item["name"] + "/job.json"))
            pods = json.load(archive.extractfile("jobs/" + item["name"] + "/pods.json"))["items"]
            assert job["metadata"]["uid"] == receipt["job_uid"]
            assert len(pods) == 1 and pods[0]["status"]["phase"] == "Succeeded"
            pod = pods[0]
            raw_log = archive.extractfile("jobs/" + item["name"] + "/" + pod["metadata"]["name"] + ".log").read().decode()
            reported = [json.loads(line[len("CANDY_RESULT "):]) for line in raw_log.splitlines()
                        if line.startswith("CANDY_RESULT ")]
            assert reported == [result]
            assert job["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == current_cm["metadata"]["name"]
            container = job["spec"]["template"]["spec"]["containers"][0]
            resources = container["resources"]
            assert resources["requests"]["cpu"] == resources["limits"]["cpu"] == "1"
            assert resources["requests"]["memory"] == "1Gi"
            assert resources["limits"]["memory"] == "4Gi"
            accelerator_key = "mobilint.com/npu" if item["arm"] == "mobilint" else "nvidia.com/gpu"
            assert resources["requests"][accelerator_key] == resources["limits"][accelerator_key] == "1"
            assert pod["status"]["containerStatuses"][0]["imageID"].endswith(container["image"])
            ended = pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
            scheduled = next(x["lastTransitionTime"] for x in pod["status"]["conditions"]
                             if x["type"] == "PodScheduled")
            jct = (timestamp(ended) - timestamp(job["metadata"]["creationTimestamp"])).total_seconds()
            allocation = (timestamp(ended) - timestamp(scheduled)).total_seconds()
            groups[item["arm"]].append({"block": item["block"], "samples": result["samples_seconds"],
                                        "mean_ms": 1000 * statistics.mean(result["samples_seconds"]),
                                        "setup_ms": result["setup_seconds"] * 1000,
                                        "first_warmup_ms": result["warmup_seconds"][0] * 1000,
                                        "native_job_jct_seconds": jct,
                                        "device_allocation_seconds": allocation})
    summary = {"scope": "synthetic Candy deployment microbenchmark; model equivalence not established",
               "hardware_speedup": None, "accuracy_parity": None, "energy_efficiency": None,
               "production_integration": False, "protocol_revision": protocol["revision"],
               "boundary": protocol["boundary"], "independent_jobs_per_runtime": 3,
               "timed_calls_per_job": 30, "groups": {}}
    for runtime, jobs in groups.items():
        assert len(jobs) == 3
        samples = [v for job in jobs for v in job["samples"]]
        summary["groups"][runtime] = {
            "mean_ms": 1000 * statistics.mean(samples),
            "p95_ms": 1000 * percentile(samples, .95),
            "timed_loop_calls_per_second": len(samples) / sum(samples),
            "mean_setup_ms": statistics.mean(j["setup_ms"] for j in jobs),
            "mean_first_warmup_ms": statistics.mean(j["first_warmup_ms"] for j in jobs),
            "mean_native_job_jct_seconds": statistics.mean(j["native_job_jct_seconds"] for j in jobs),
            "main_device_allocation_seconds": sum(j["device_allocation_seconds"] for j in jobs),
            "per_job": [{k: v for k, v in job.items() if k != "samples"} for job in jobs]}
    pilot = json.loads((root / "capture-v3.json").read_text())
    old_protocol = json.loads((root / "protocol-v3.json").read_text())
    assert hashlib.sha256((root / "probe-v2.py").read_bytes()).hexdigest() == old_protocol["source_sha256"]
    old_summary = json.loads((root / "summary-v3.json").read_text())
    with (root / "samples-v3.csv").open(newline="") as file:
        old_rows = list(csv.DictReader(file))
    expected_old_rows = []
    with tarfile.open(root / "native-records-public.tar.gz") as archive:
        pilot_cm = json.load(archive.extractfile("source-configmap-native-v2.json"))
        assert pilot_cm["immutable"] is True
        assert pilot_cm["data"]["probe.py"].encode() == (root / "probe-v2.py").read_bytes()
        for arm in ["mobilint", "cuda"]:
            values = []
            for item in pilot["main"]:
                if item["arm"] != arm:
                    continue
                archived = json.load(archive.extractfile("jobs/" + item["name"] + "/receipt.json"))
                assert archived == item["receipt"] and archived["native_succeeded"]
                result = archived["result"]
                assert result["input_sha256"] == expected_input
                values.extend(result["samples_seconds"])
            assert len(values) == 90
            assert old_summary["groups"][arm]["mean_ms"] == 1000 * statistics.mean(values)
            assert old_summary["groups"][arm]["p95_ms"] == 1000 * percentile(values, .95)
    for item in pilot["main"]:
        result = item["receipt"]["result"]
        for index, seconds in enumerate(result["samples_seconds"]):
            expected_old_rows.append({"block": str(item["block"]), "runtime": item["arm"],
                                      "sample": str(index), "seconds": str(seconds),
                                      "output_sha256": result["output_sha256"][index]})
    assert old_rows == expected_old_rows
    return summary, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    summary, rows = calculate(root)
    if args.write:
        (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        with (root / "samples.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    else:
        assert summary == json.loads((root / "summary.json").read_text())
        with (root / "samples.csv").open(newline="") as file:
            recorded = list(csv.DictReader(file))
        assert len(recorded) == len(rows)
        assert all({key: str(value) for key, value in row.items()} == actual
                   for row, actual in zip(rows, recorded))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
