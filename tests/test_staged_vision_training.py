import hashlib
import io
import json
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from resource_advisor import staged_vision_training as svt
from resource_advisor import vision_training as vt
from resource_advisor.network_profile import StagingReceipt

TRAIN_ARGS = ["--isolated", "--model", "mobilenet_v3_small", "--steps", "2"]


def bundle(tmp_path, members=None):
    """A tar of a dataset directory (manifest + one image), like the pinned dataset-v1."""
    members = members or {"dataset-v1/manifest.json": b'{"schema":"ra-vision-dataset-v1"}',
                          "dataset-v1/train/a.jpg": b"jpeg-bytes"}  # fmt: skip
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path = tmp_path / "dataset-v1.tar"
    path.write_bytes(buffer.getvalue())
    return path, hashlib.sha256(buffer.getvalue()).hexdigest(), len(buffer.getvalue())


def env(**overrides):
    values = {
        "RA_JOB_ID": "job-1",
        "RA_ATTEMPT_ID": "att-1",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": "sha256:" + "a" * 64,
        "RA_CONTEXT_SIGNATURE": "sha256:" + "b" * 64,
        "RA_NODE_NAME": "node-a",
        "RA_POD_NAME": "pod-a",
        "RA_POD_UID": "uid-a",
    }
    values.update(overrides)
    return values


def argv(source, digest, size, cache, *extra, runtime="vision_training", train=TRAIN_ARGS):
    return [
        "--runtime", runtime, "--source", str(source), "--sha256", digest, "--size-bytes", str(size),
        "--source-endpoint-ref", "dataset-http", "--cache-dir", str(cache), *extra,
        "--", *train,
    ]  # fmt: skip


class Trainer:
    """Stands in for vision_training.run(args, environ, stage=...): it calls the hook,
    then (rank 0) emits the envelope with the returned receipt, per the contract."""

    def __init__(self, out=None):
        self.calls, self.out = [], out

    def __call__(self, runtime, train_argv, environ, stage):
        args = SimpleNamespace(data=None)
        receipt = stage(args)
        self.calls.append((train_argv, (svt.Path(args.data) / "manifest.json").read_bytes()))
        result = {"job_id": environ["RA_JOB_ID"], "outcome": "COMPLETED"}
        if environ.get("RANK", "0") == "0" and self.out is not None:
            envelope = {
                "result": result,
                "digest": vt.signature(result),
                "staging_receipt": receipt,
            }
            self.out.append(vt.RESULT + json.dumps(envelope))


def lines(out, prefix):
    return [json.loads(x[len(prefix) :]) for x in out if x.startswith(prefix)]


def test_stages_then_trains_on_the_verified_extracted_bundle(tmp_path):
    source, digest, size = bundle(tmp_path)
    cache, out = tmp_path / "cache", []
    trainer = Trainer(out)

    assert svt.run(argv(source, digest, size, cache), env(), train=trainer, write=out.append) == 0

    [(train_argv, manifest)] = trainer.calls
    assert train_argv == TRAIN_ARGS  # --data comes only from the stage hook
    assert manifest == b'{"schema":"ra-vision-dataset-v1"}'
    [member] = lines(out, svt.RECEIPT)
    [envelope] = lines(out, vt.RESULT)
    assert envelope["staging_receipt"] == member  # rank 0 also binds it to its result
    receipt = StagingReceipt.model_validate(member)
    assert (receipt.job_id, receipt.attempt_id, receipt.cache_state) == ("job-1", "att-1", "cold")
    assert receipt.content_digest == "sha256:" + digest and receipt.size_bytes == size
    assert (receipt.identity_source, receipt.node_name, receipt.pod_uid) == (
        "kubernetes_downward_api",
        "node-a",
        "uid-a",
    )
    assert receipt.sample.content_verified and receipt.sample.bytes == size
    assert receipt.cache_locator == str(cache.resolve()) and receipt.staged_at is not None
    # The unpack is reported apart from the transfer: never part of data_staging.
    [report] = lines(out, svt.REPORT)
    assert report["unpack_seconds"] == receipt.unpack_seconds >= 0
    assert report["staging_seconds"] == receipt.sample.elapsed_seconds

    out.clear()
    assert svt.run(argv(source, digest, size, cache), env(RA_ATTEMPT_ID="att-2"), train=trainer,
                   write=out.append) == 0  # fmt: skip
    assert StagingReceipt.model_validate(lines(out, svt.RECEIPT)[0]).cache_state == "hot"


def test_http_source_is_streamed_and_its_locator_carries_no_secret(tmp_path):
    source, digest, size = bundle(tmp_path)
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://user:pw@127.0.0.1:{server.server_port}/{source.name}?token=x"
        out = []
        assert svt.run(argv(url, digest, size, tmp_path / "c"), env(), train=Trainer(),
                       write=out.append) == 0  # fmt: skip
    finally:
        server.shutdown()
    receipt = StagingReceipt.model_validate(lines(out, svt.RECEIPT)[0])
    assert receipt.source_kind == "http"
    assert receipt.source_locator == f"http://127.0.0.1:{server.server_port}/{source.name}"


def failure(out):
    [envelope] = lines(out, vt.RESULT)
    assert "staging_receipt" not in envelope and envelope["digest"] == vt.signature(
        envelope["result"]
    )
    assert envelope["result"]["outcome"] == "FAILED" and envelope["result"]["measured"] is True
    return envelope["result"]["error_code"]


def test_unverified_or_unsafe_data_never_reaches_training(tmp_path):
    source, digest, size = bundle(tmp_path)
    trainer, out = Trainer(), []
    assert svt.run(argv(source, "0" * 64, size, tmp_path / "c1"), env(), train=trainer,
                   write=out.append) == 2  # fmt: skip
    assert failure(out) == "STAGING_CONTENT_DIGEST_MISMATCH"

    cache = tmp_path / "c2"
    cache.mkdir()
    (cache / digest).write_bytes(b"corrupt")
    out.clear()
    assert svt.run(argv(source, digest, size, cache), env(), train=trainer, write=out.append) == 2
    assert failure(out) == "STAGING_CACHE_CORRUPT"
    assert (cache / digest).read_bytes() == b"corrupt"  # reported, never deleted

    for name in ("../escape.json", "/abs.json"):
        bad, bad_digest, bad_size = bundle(tmp_path, {name: b"x"})
        out.clear()
        assert svt.run(argv(bad, bad_digest, bad_size, tmp_path / "c3"), env(), train=trainer,
                       write=out.append) == 2  # fmt: skip
        assert failure(out) == "STAGING_ARCHIVE_UNSAFE"
    assert trainer.calls == [] and not (tmp_path / "escape.json").exists()


def test_plan_cache_binding_replaces_the_cache_dir(tmp_path):
    source, digest, size = bundle(tmp_path)
    bound = tmp_path / "bound"
    bindings = {"node-a": {"ref": "bind-a", "cache_path": str(bound)}}
    out = []
    assert svt.run(argv(source, digest, size, tmp_path / "ignored"),
                   env(RA_STAGING_BINDINGS_JSON=json.dumps(bindings)), train=Trainer(),
                   write=out.append) == 0  # fmt: skip
    receipt = StagingReceipt.model_validate(lines(out, svt.RECEIPT)[0])
    assert (receipt.cache_binding_ref, receipt.cache_locator) == ("bind-a", str(bound.resolve()))
    assert receipt.cache_storage_identity == svt.storage_identity(bound)

    out.clear()
    other = env(RA_STAGING_BINDINGS_JSON=json.dumps({"node-b": bindings["node-a"]}))
    assert svt.run(argv(source, digest, size, tmp_path / "x"), other, train=Trainer(),
                   write=out.append) == 2  # fmt: skip
    assert failure(out) == "STAGING_BINDING_MISSING_FOR_NODE"


def test_every_ddp_rank_reports_its_own_staging_but_only_rank0_results(tmp_path):
    source, digest, size = bundle(tmp_path)
    out = []
    worker = env(RANK="1", RA_NODE_NAME="node-b", RA_POD_UID="uid-b")
    assert svt.run(argv(source, digest, size, tmp_path / "c"), worker, train=Trainer(),
                   write=out.append) == 0  # fmt: skip
    [member] = lines(out, svt.RECEIPT)
    assert (member["node_name"], member["pod_uid"]) == ("node-b", "uid-b")
    assert lines(out, vt.RESULT) == []

    out.clear()
    assert svt.run(argv(source, "0" * 64, size, tmp_path / "d"), worker, train=Trainer(),
                   write=out.append) == 2  # fmt: skip
    assert lines(out, vt.RESULT) == []  # a non-zero rank never emits the job result


def test_data_is_supplied_by_staging_only(tmp_path):
    source, digest, size = bundle(tmp_path)
    with pytest.raises(SystemExit):
        svt.run(argv(source, digest, size, tmp_path / "c") + ["--data", "/opt/ra-train/data"],
                env(), train=Trainer(), write=[].append)  # fmt: skip


def test_cache_collision_winner_must_hold_the_receipted_bytes(tmp_path, monkeypatch):
    source, digest, size = bundle(tmp_path)
    cache = tmp_path / "c"
    real_link = svt.os.link

    def racing_link(src, dst):  # another stager installs a corrupt entry first
        svt.Path(dst).write_bytes(b"corrupt")
        real_link(src, dst)

    monkeypatch.setattr(svt.os, "link", racing_link)
    out, trainer = [], Trainer()
    assert svt.run(argv(source, digest, size, cache), env(), train=trainer, write=out.append) == 2
    assert failure(out) == "STAGING_CACHE_CORRUPT" and trainer.calls == []


def test_ipv6_source_keeps_its_brackets():
    assert svt.sanitize("http://u:p@[fd00::1]:8080/d.tar?t=1") == (
        "http",
        "http://[fd00::1]:8080/d.tar",
    )
    assert svt._target("http://[fd00::1]:8080/d.tar?t=1")[1:4] == ("fd00::1", 8080, "/d.tar?t=1")


JOINT_ARGS = [
    "--model", "mobilenet_v3_small", "--dataset-digest", "sha256:" + "0" * 64,
    "--initial-checkpoint-digest", "sha256:" + "1" * 64, "--global-batch", "16",
    "--steps", "2", "--seed", "7", "--teacher-url", "http://teacher:8080",
    "--teacher-model-sha256", "f" * 64, "--teacher-runtime-version", "1.4.0",
    "--teacher-pod-uid", "uid-t", "--kd-config-digest", "sha256:" + "2" * 64,
]  # fmt: skip


def test_joint_runtime_is_an_explicit_choice_and_its_receipt_reaches_the_envelope(
    tmp_path, monkeypatch
):
    from resource_advisor import vision_joint_training as vjt

    source, digest, size = bundle(tmp_path)
    seen = []

    def joint_run(args, environ, stage):  # stands in for the GPU/teacher work
        receipt = stage(args)
        seen.append((args.teacher_url, args.data))
        vt.emit(vt.RESULT, {"result": {}, "training_evidence": {"staging_receipt": receipt}})

    monkeypatch.setattr(vjt, "run", joint_run)
    out = []
    code = svt.run(argv(source, digest, size, tmp_path / "c", runtime="vision_joint_training",
                        train=JOINT_ARGS), env(), write=out.append)  # fmt: skip
    assert code == 0
    [(url, data)] = seen
    assert url == "http://teacher:8080" and (svt.Path(data) / "manifest.json").is_file()
    [envelope] = lines(out, vt.RESULT)
    assert envelope["staging_receipt"] == lines(out, svt.RECEIPT)[0]
    assert vt.emit is not None and vt.emit.__module__ == vt.__name__  # restored

    with pytest.raises(SystemExit):  # no implicit or unknown runtime
        svt.run(argv(source, digest, size, tmp_path / "d", runtime="inference"), env(),
                write=[].append)  # fmt: skip


def test_ddp_rank_receipts_are_lifted_in_rank_order():
    # Each rank's runtime records its own measured preparation inside its receipt.
    rank0 = {"pod_uid": "u0", "preparation_seconds": 2.5}
    envelope = {"result": {}, "training_evidence": {
        "staging_receipt": rank0, "preparation_seconds": 2.5,
        "ranks": [{"rank": 1, "staging_receipt": {"pod_uid": "u1", "preparation_seconds": 9.0}},
                  {"rank": 0, "staging_receipt": rank0}]}}  # fmt: skip
    lifted = svt.lift_receipts(envelope)
    # The primary receipt is exactly rank 0's own; nothing is synthesized onto it.
    assert lifted["staging_receipt"] == rank0 == lifted["staging_receipts"][0]
    assert [r["pod_uid"] for r in lifted["staging_receipts"]] == ["u0", "u1"]
    assert lifted["staging_receipts"][1]["preparation_seconds"] == 9.0


def test_single_receipt_is_lifted_unchanged():
    receipt = {"pod_uid": "u0", "preparation_seconds": 1.5}
    lifted = svt.lift_receipts({"training_evidence": {"staging_receipt": receipt,
                                                      "preparation_seconds": 7.0}})  # fmt: skip
    assert lifted["staging_receipt"] == receipt


def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_redirects_are_refused_and_credentials_never_leave_the_source(tmp_path):
    from http.server import BaseHTTPRequestHandler

    source, digest, size = bundle(tmp_path)
    seen = []

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(source.read_bytes())

        def log_message(self, *a):
            pass

    other = serve(Other)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{other.server_port}/x.tar")
            self.end_headers()

        def log_message(self, *a):
            pass

    origin = serve(Redirect)
    try:
        out = []
        url = f"http://user:pw@127.0.0.1:{origin.server_port}/x.tar"
        svt.run(
            argv(url, digest, size, tmp_path / "c"), env(), train=Trainer(out), write=out.append
        )
    finally:
        origin.shutdown()
        other.shutdown()
    assert failure(out) == "STAGING_SOURCE_HTTP_302" and seen == []


def test_the_transfer_deadline_bounds_the_whole_transfer(tmp_path):
    import time as clock
    from http.server import BaseHTTPRequestHandler

    source, digest, size = bundle(tmp_path)

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            for byte in source.read_bytes()[:20]:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                clock.sleep(0.25)

        def log_message(self, *a):
            pass

    server = serve(Slow)
    try:
        out, started = [], clock.perf_counter()
        url = f"http://127.0.0.1:{server.server_port}/x.tar"
        svt.run(argv(url, digest, size, tmp_path / "c", "--timeout-seconds", "1"), env(),
                train=Trainer(out), write=out.append)  # fmt: skip
        took = clock.perf_counter() - started
    finally:
        server.shutdown()
    assert failure(out) == "STAGING_TRANSFER_TIMEOUT" and took < 1.6


@pytest.fixture
def tls(tmp_path, monkeypatch):
    """A trusted self-signed certificate for 127.0.0.1 and a server-side context."""
    import shutil
    import ssl
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )  # fmt: skip
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


def serve_tls(handler_class, context):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_https_source_is_staged_and_verified(tmp_path, tls):
    source, digest, size = bundle(tmp_path)
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    server = serve_tls(handler, tls)
    try:
        out = []
        url = f"https://127.0.0.1:{server.server_port}/{source.name}"
        code = svt.run(argv(url, digest, size, tmp_path / "c"), env(), train=Trainer(out),
                       write=out.append)  # fmt: skip
    finally:
        server.shutdown()
    assert code == 0
    receipt = StagingReceipt.model_validate(lines(out, svt.RECEIPT)[0])
    assert receipt.source_kind == "http" and receipt.sample.content_verified
    assert receipt.source_locator.startswith("https://127.0.0.1:")


def test_https_transfer_deadline_bounds_the_whole_transfer(tmp_path, tls):
    import time as clock
    from http.server import BaseHTTPRequestHandler

    source, digest, size = bundle(tmp_path)

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            for byte in source.read_bytes()[:20]:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                clock.sleep(0.25)

        def log_message(self, *a):
            pass

    server = serve_tls(Slow, tls)
    try:
        out, started = [], clock.perf_counter()
        url = f"https://127.0.0.1:{server.server_port}/x.tar"
        svt.run(argv(url, digest, size, tmp_path / "c", "--timeout-seconds", "1"), env(),
                train=Trainer(out), write=out.append)  # fmt: skip
        took = clock.perf_counter() - started
    finally:
        server.shutdown()
    assert failure(out) == "STAGING_TRANSFER_TIMEOUT" and took < 1.6
