"""Recompute the additional RTX5060Ti cohort; prior devices are separate cohorts."""
import argparse
import base64
import collections
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
import struct
import tarfile
import zlib


def calculate(root):
    reference = root.parent / "mobilint-5080-20261012-v1"
    spec = importlib.util.spec_from_file_location("reference_metrics", reference / "recompute.py")
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    plan = json.loads((root / "protocol.json").read_text())
    source = (reference / "probe.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == plan["source_sha256"]
    reference_raw = (reference / "summary.json").read_bytes()
    assert hashlib.sha256(reference_raw).hexdigest() == plan["reference_summary_sha256"]
    reference_plan = json.loads((reference / "protocol.json").read_text())
    assert plan["gpu_model_sha256"] == reference_plan["gpu_model_sha256"]
    reference_template = json.loads((reference / "gpu-template.json").read_text())
    reference_image = reference_template["spec"]["template"]["spec"]["containers"][0]["image"]
    prior = json.loads(reference_raw)
    assert prior["protocol_revision"] == 4
    capture = json.loads((root / "capture.json").read_text())
    assert [(i["block"], i["name"]) for i in capture["main"]] == [(i["block"], i["name"]) for i in plan["schedule"]]
    assert len(capture["main"]) == 3 and len(capture["qualification"]) == 1
    rows, jobs = [], []
    with tarfile.open(root / "native-records-public.tar.gz") as archive:
        ledger = json.loads((root / "native-files-sha256.json").read_text())
        assert set(archive.getnames()) == {e["path"] for e in ledger}
        for e in ledger:
            assert hashlib.sha256(archive.extractfile(e["path"]).read()).hexdigest() == e["public_sha256"]
        cm = json.load(archive.extractfile("source-configmap-native-v3.json"))
        assert cm["immutable"] and cm["data"]["probe.py"].encode() == source
        trials = [(None, capture["qualification"][0])] + [(i, i["receipt"]) for i in capture["main"]]
        for item, receipt in trials:
            name = receipt["name"]
            path = "jobs/" + name + "/"
            assert receipt == json.load(archive.extractfile(path + "receipt.json"))
            assert receipt["native_succeeded"]
            assert receipt["gpu_identity"]["name"] == plan["expected_gpu"]
            r = receipt["result"]
            count = plan["samples"] if item is not None else plan["qualification_samples"]
            warmups = plan["warmups"] if item is not None else plan["qualification_warmups"]
            assert r["runtime"] == "cuda" and r["model_sha256"] == plan["gpu_model_sha256"]
            assert r["input_sha256"] == "005d18ceb13076219f5a4cead7611755c19859334d0671e7545cbded1bad6edc"
            assert r["boundary"] == plan["boundary"] and r["output_c_contiguous"] is True
            assert r["repeat_element_agreement"] == 1
            assert len(r["samples_seconds"]) == count and len(r["warmup_seconds"]) == warmups
            assert all(math.isfinite(s) and s > 0 for s in r["samples_seconds"])
            output = zlib.decompress(base64.b64decode(r["output_zlib_base64"]))
            assert len(output) == 224 * 344 * 3 * 4
            assert all(math.isfinite(v[0]) for v in struct.iter_unpack("<f", output))
            digest = hashlib.sha256(output).hexdigest()
            assert r["output_sha256"] == [digest] * count
            trace = json.loads(zlib.decompress(base64.b64decode(r["metadata"]["placement_trace_zlib_base64"])))
            counts = collections.Counter((e.get("args", {}).get("provider"), e.get("args", {}).get("op_name"))
                                         for e in trace if e.get("cat") == "Node" and e.get("args", {}).get("provider"))
            assert sum(counts.values()) == 64 and all(p == "CUDAExecutionProvider" for p, _ in counts)
            assert counts == {(x["provider"], x["op"]): x["count"] for x in r["metadata"]["placement_counts"]}
            assert r["metadata"]["provider_options"]["CUDAExecutionProvider"]["use_tf32"] == "0"
            job = json.load(archive.extractfile(path + "job.json"))
            pods = json.load(archive.extractfile(path + "pods.json"))["items"]
            assert job["metadata"]["uid"] == receipt["job_uid"] and len(pods) == 1
            pod = pods[0]
            assert pod["status"]["phase"] == "Succeeded"
            spec = job["spec"]["template"]["spec"]
            assert spec["volumes"][0]["configMap"]["name"] == cm["metadata"]["name"]
            container = spec["containers"][0]
            assert container["image"] == reference_image
            assert container["resources"]["requests"] == {"cpu": "1", "memory": "1Gi", "nvidia.com/gpu": "1"}
            assert container["resources"]["limits"] == {"cpu": "1", "memory": "4Gi", "nvidia.com/gpu": "1"}
            assert pod["status"]["containerStatuses"][0]["imageID"].endswith(container["image"])
            raw = archive.extractfile(path + pod["metadata"]["name"] + ".log").read().decode()
            logged = [json.loads(line[len("CANDY_RESULT "):]) for line in raw.splitlines() if line.startswith("CANDY_RESULT ")]
            assert logged == [r]
            if item is None:
                continue
            for index, seconds in enumerate(r["samples_seconds"]):
                rows.append({"block": item["block"], "sample": index, "seconds": seconds, "output_sha256": digest})
            ended = pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
            scheduled = next(c["lastTransitionTime"] for c in pod["status"]["conditions"] if c["type"] == "PodScheduled" and c["status"] == "True")
            jobs.append({"block": item["block"], "mean_ms": 1000 * statistics.mean(r["samples_seconds"]),
                         "setup_ms": r["setup_seconds"] * 1000, "first_warmup_ms": r["warmup_seconds"][0] * 1000,
                         "native_job_jct_seconds": (base.timestamp(ended) - base.timestamp(job["metadata"]["creationTimestamp"])).total_seconds(),
                         "device_allocation_seconds": (base.timestamp(ended) - base.timestamp(scheduled)).total_seconds()})
    samples = [r["seconds"] for r in rows]
    assert len(samples) == 90
    summary = {"device": plan["expected_gpu"], "independent_jobs": 3, "timed_calls_per_job": 30,
               "boundary": plan["boundary"], "reference_is_concurrent": False,
               "gpu_reference_same_artifact_sha256": True, "mobilint_model_equivalence": "not-established",
               "hardware_general_speedup": None, "accuracy_parity": None, "energy_efficiency": None,
               "mean_ms": 1000 * statistics.mean(samples), "p95_ms": 1000 * base.percentile(samples, .95),
               "timed_loop_calls_per_second": len(samples) / sum(samples),
               "mean_setup_ms": statistics.mean(j["setup_ms"] for j in jobs),
               "mean_first_warmup_ms": statistics.mean(j["first_warmup_ms"] for j in jobs),
               "mean_native_job_jct_seconds": statistics.mean(j["native_job_jct_seconds"] for j in jobs),
               "main_device_allocation_seconds": sum(j["device_allocation_seconds"] for j in jobs),
               "per_job": jobs, "reference_groups": prior["groups"]}
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
            actual = list(csv.DictReader(file))
        assert actual == [{k: str(v) for k, v in row.items()} for row in rows]
    print(json.dumps({k: v for k, v in summary.items() if k not in ["reference_groups", "per_job"]}, indent=2))


if __name__ == "__main__":
    main()
