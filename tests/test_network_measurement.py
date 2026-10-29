"""Measurement CLI seam: real loopback HTTP and local file bytes, never assumed links.

The loopback server and temp files are local software fixtures. They prove the
collector's byte/digest/timing path, not remote Kubernetes/Slurm node evidence.
"""

import hashlib
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from resource_advisor.network_profile import NetworkProfile, main

PAYLOAD = os.urandom(256 * 1024)
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Handler(BaseHTTPRequestHandler):
    truncate = False
    drip = False
    requests = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).requests.append(self.path)
        body = PAYLOAD[: len(PAYLOAD) // 2] if type(self).truncate else PAYLOAD
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        if type(self).drip:
            # Keeps the socket active (no inactivity timeout) far past the budget.
            import time

            try:
                for offset in range(0, 8000, 100):
                    self.wfile.write(PAYLOAD[offset : offset + 100])
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Handler.truncate, Handler.drip, Handler.requests = False, False, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


SIZE = len(PAYLOAD)


def run(tmp_path, capsys, source, *extra, mode="cold", digest=DIGEST, size=SIZE):
    argv = [
        "measure",
        "--source",
        source,
        "--sha256",
        digest,
        "--size-bytes",
        str(size),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--mode",
        mode,
        "--ref",
        f"net-{mode}",
        "--project",
        "team-a",
        "--workload-ref",
        "pair-work",
        "--dataset-version",
        "fixture-v1",
        "--source-endpoint-ref",
        "storage",
        "--backend",
        "kubernetes",
        "--cluster-ref",
        "lab",
        "--repeats",
        "2",
        "--max-bytes",
        str(1024 * 1024),
        "--timeout-seconds",
        "10",
        *extra,
    ]
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_cold_then_hot_measure_actual_bytes_on_this_host(tmp_path, capsys, server):
    code, cold = run(tmp_path, capsys, server + "/data.bin")
    assert code == 0, cold
    profile = NetworkProfile.model_validate(cold)
    assert profile.cache_state == "cold"
    assert profile.node_ref == profile.observer_host == socket.gethostname()
    assert profile.content_digest == "sha256:" + DIGEST
    assert len(profile.samples) == 2 and len(Handler.requests) == 2
    assert all(s.bytes == len(PAYLOAD) and s.content_verified for s in profile.samples)
    assert all(s.elapsed_seconds > 0 for s in profile.samples)
    cached = tmp_path / "cache" / DIGEST
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == DIGEST
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [DIGEST]

    code, hot = run(tmp_path, capsys, server + "/data.bin", mode="hot")
    assert code == 0, hot
    assert hot["cache_state"] == "hot" and len(Handler.requests) == 2  # no source reads


def test_local_file_copy_is_measured_like_a_stream(tmp_path, capsys):
    source = tmp_path / "dataset.bin"
    source.write_bytes(PAYLOAD)
    code, body = run(tmp_path, capsys, str(source))
    assert code == 0, body
    assert body["source_kind"] == "file" and body["samples"][0]["bytes"] == len(PAYLOAD)


def test_credentials_and_query_tokens_never_reach_evidence(tmp_path, capsys, server):
    secret = server.replace("http://", "http://user:hunter2@") + "/data.bin?token=hunter2#x"
    code, body = run(tmp_path, capsys, secret)
    assert code == 0, body
    assert "hunter2" not in json.dumps(body)
    assert body["source_locator"] == server + "/data.bin"


def test_truncated_transfer_cannot_claim_full_dataset_evidence(tmp_path, capsys, server):
    Handler.truncate = True
    code, body = run(tmp_path, capsys, server + "/data.bin")
    assert code == 2
    assert body["status"] == "UNAVAILABLE" and "TRANSFER_INCOMPLETE" in body["reasons"]
    assert not (tmp_path / "cache" / DIGEST).exists()
    assert list((tmp_path / "cache").iterdir()) == []  # own partial file removed


def test_digest_mismatch_and_byte_cap_are_explicit_reasons(tmp_path, capsys, server):
    code, body = run(tmp_path, capsys, server + "/data.bin", digest="0" * 64)
    assert code == 2 and "CONTENT_DIGEST_MISMATCH" in body["reasons"]
    code, body = run(tmp_path, capsys, server + "/data.bin", "--max-bytes", "1024")
    assert code == 2 and "SIZE_EXCEEDS_LIMIT" in body["reasons"]


def test_corrupt_shared_cache_is_reported_and_left_in_place(tmp_path, capsys, server):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / DIGEST).write_bytes(b"corrupted")
    code, body = run(tmp_path, capsys, server + "/data.bin", mode="hot")
    assert code == 2 and "CACHE_CORRUPT" in body["reasons"]
    assert (cache / DIGEST).read_bytes() == b"corrupted"  # shared cache never deleted
    code, body = run(tmp_path, capsys, server + "/data.bin", mode="hot", digest="1" * 64)
    assert code == 2 and "CACHE_MISSING" in body["reasons"]


def test_coordinator_cannot_label_itself_as_a_remote_node(tmp_path, capsys, server):
    code, body = run(tmp_path, capsys, server + "/data.bin", "--node-ref", "remote-gpu-node")
    assert code == 2 and "OBSERVER_NOT_TARGET" in body["reasons"]
    with pytest.raises(ValueError):
        NetworkProfile.model_validate(
            {**_valid(tmp_path, capsys, server), "node_ref": "remote-gpu-node"}
        )


def _valid(tmp_path, capsys, server):
    code, body = run(tmp_path, capsys, server + "/data.bin")
    assert code == 0
    return body


def test_profile_rejects_partial_or_nonfinite_samples(tmp_path, capsys, server):
    body = _valid(tmp_path, capsys, server)
    for sample in (
        {**body["samples"][0], "bytes": 10},
        {**body["samples"][0], "content_verified": False},
        {**body["samples"][0], "elapsed_seconds": 0},
    ):
        with pytest.raises(ValueError):
            NetworkProfile.model_validate({**body, "samples": [sample]})


def test_slow_drip_is_cut_at_the_total_deadline(tmp_path, capsys, server):
    import time

    from resource_advisor.network_profile import DEADLINE_TOLERANCE_SECONDS

    Handler.drip = True
    started = time.monotonic()
    code, body = run(tmp_path, capsys, server + "/data.bin", "--timeout-seconds", "1")
    elapsed = time.monotonic() - started
    assert code == 2 and body["reasons"] == ["TRANSFER_TIMEOUT"]
    # Two repeats are requested but the first sample already fails: one deadline.
    assert elapsed < 1 + DEADLINE_TOLERANCE_SECONDS + 0.25
    assert list((tmp_path / "cache").iterdir()) == []  # own partial file removed


def test_cli_inside_a_pod_refuses_to_name_a_node(tmp_path, capsys, server, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    code, body = run(tmp_path, capsys, server + "/data.bin")
    assert code == 2 and "KUBERNETES_POD_REQUIRES_NATIVE_RECEIPT" in body["reasons"]


def test_measurement_records_the_observed_cache_directory_identity(tmp_path, capsys, server):
    from resource_advisor.network_profile import storage_identity

    code, body = run(tmp_path, capsys, server + "/data.bin")
    assert code == 0
    assert body["cache_storage_identity"] == storage_identity(tmp_path / "cache")
    assert "identity_source" not in body  # unset optional fields keep legacy digests
