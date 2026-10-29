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


def argv(source, digest, size, cache, *extra):
    return [
        "--source", str(source), "--sha256", digest, "--size-bytes", str(size),
        "--source-endpoint-ref", "dataset-http", "--cache-dir", str(cache), *extra,
        "--", *TRAIN_ARGS,
    ]  # fmt: skip


class Trainer:
    """Stands in for vision_training.run(args, environ, stage=...): it calls the hook,
    then (rank 0) emits the envelope with the returned receipt, per the contract."""

    def __init__(self, out=None):
        self.calls, self.out = [], out

    def __call__(self, train_argv, environ, stage):
        args = SimpleNamespace(data=None)
        receipt = stage(args)
        self.calls.append((train_argv, (svt.Path(args.data) / "manifest.json").read_bytes()))
        result = {"job_id": environ["RA_JOB_ID"], "outcome": "COMPLETED"}
        if environ.get("RANK", "0") == "0" and self.out is not None:
            envelope = {"result": result, "digest": vt.signature(result), "staging_receipt": receipt}
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
    assert report["unpack_seconds"] >= 0 and report["staging_seconds"] == receipt.sample.elapsed_seconds

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
    assert "staging_receipt" not in envelope and envelope["digest"] == vt.signature(envelope["result"])
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
