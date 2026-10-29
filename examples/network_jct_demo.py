"""Bounded local actual-byte demonstration of destination dataset access.

  python examples/network_jct_demo.py --out DIR [--mib 16] [--repeats 3]

Generates a random dataset file under DIR, serves it on a loopback HTTP port,
measures cold (copy into DIR/cache) and hot (verified cache read) access with the
real collector CLI, and runs the staged workload once against the hot cache.
Everything happens on this host: it is local loopback evidence, never remote
Kubernetes/Slurm node or physical NIC evidence. Writes DIR/evidence.json.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import socket
import sys
import threading
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from resource_advisor.contracts import now
from resource_advisor.network_profile import NetworkProfile, summary
from resource_advisor.network_profile import main as network_cli
from resource_advisor.staged_workload import run as staged_run


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def measure(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = network_cli(argv)
    return code, json.loads(out.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--mib", type=int, default=16, choices=range(1, 257), metavar="1-256")
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 11), metavar="1-10")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    (out / "source").mkdir(parents=True, exist_ok=True)
    dataset = out / "source" / "dataset.bin"
    dataset.write_bytes(os.urandom(args.mib * 2**20))
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    size = dataset.stat().st_size
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(out / "source")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/dataset.bin"
    common = [
        "--source", url, "--sha256", digest, "--size-bytes", str(size),
        "--cache-dir", str(out / "cache"), "--project", "demo",
        "--workload-ref", "demo-work", "--dataset-version", "demo-v1",
        "--source-endpoint-ref", "loopback-http", "--backend", "kubernetes",
        "--cluster-ref", "local", "--repeats", str(args.repeats),
        "--max-bytes", str(size), "--timeout-seconds", "60",
    ]  # fmt: skip
    evidence = {
        "kind": "local loopback actual-byte demonstration (not remote node evidence)",
        "host": socket.gethostname(),
        "python": platform.python_version(),
        "started_at": now().isoformat(),
        "dataset": {"sha256": digest, "size_bytes": size},
        "measurements": {},
    }
    try:
        for mode in ("cold", "hot"):
            code, body = measure(["measure", *common, "--mode", mode, "--ref", f"demo-{mode}"])
            evidence["measurements"][mode] = {
                "exit_code": code,
                "profile": body,
                "summary": summary(NetworkProfile.model_validate(body)) if code == 0 else None,
            }
        # Corrupt-cache check on a copy: reported, never deleted.
        corrupt = out / "corrupt-cache"
        corrupt.mkdir(exist_ok=True)
        (corrupt / digest).write_bytes(b"corrupted")
        argv = ["measure", *common, "--mode", "hot", "--ref", "demo-corrupt"]
        argv[argv.index("--cache-dir") + 1] = str(corrupt)
        code, body = measure(argv)
        evidence["corrupt_cache"] = {"exit_code": code, "result": body}
        env = {
            "RA_JOB_ID": "demo-job",
            "RA_ATTEMPT_ID": "demo-attempt",
            "RA_EPOCH": "1",
            "RA_WORKLOAD_SIGNATURE": "sha256:" + "0" * 64,
            "RA_CONTEXT_SIGNATURE": "sha256:" + "0" * 64,
            "RA_WORK_UNITS": "3",
            "RA_DATASET_SOURCE": url,
            "RA_DATASET_SHA256": digest,
            "RA_DATASET_SIZE_BYTES": str(size),
            "RA_DATASET_CACHE_DIR": str(out / "cache"),
            "RA_DATASET_SOURCE_ENDPOINT": "loopback-http",
        }
        result, receipt = staged_run(env, write=lambda _line: None)
        evidence["staged_workload"] = {
            "outcome": result.outcome,
            "compute_seconds": result.measurements.elapsed_seconds if result.measurements else None,
            "quality_value": result.measurements.quality_value if result.measurements else None,
            "staging": receipt.model_dump(mode="json") if receipt else None,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
    evidence["finished_at"] = now().isoformat()
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v.get("summary") for k, v in evidence["measurements"].items()}))
    return 0 if all(m["exit_code"] == 0 for m in evidence["measurements"].values()) else 2


if __name__ == "__main__":
    sys.exit(main())
