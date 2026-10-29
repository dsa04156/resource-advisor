"""Staged accelerator batch protocol: real loopback HTTP bytes into a controlled runtime.

The runtime doubles stand in for the native SDKs only so the test can prove which
staged bytes reached inference and where the compute boundary lies. This is not
GPU/NPU evidence; native evidence comes from qualified images on real nodes.
"""

import contextlib
import hashlib
import io
import json
import socket
import sys
import threading
import time
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from resource_advisor import npu_probe
from resource_advisor.backends import Observation, identity_environment, result_from_log
from resource_advisor.contracts import JobRequest, State, WorkloadIdentity, now, signature
from resource_advisor.staged_accelerator_workload import run

HOST = socket.gethostname()
MODEL = "ab" * 32
INIT_SECONDS = 0.3


def npy(array):
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def images(count, offset=0):
    rng = np.random.default_rng(7)
    data = rng.uniform(0, 255, (count, 224, 224, 3)).astype(np.float32)
    data[:, 0, 0, 0] = np.arange(offset, offset + count)  # drives the fake top-1
    return data


FIXTURE = images(4)
STAGED = npy(FIXTURE[[2, 0, 3]])


class Source:
    def __init__(self):
        self.payloads = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                body = outer.payloads[self.path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def put(self, name, body):
        self.payloads["/" + name] = body
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"


@pytest.fixture
def source():
    s = Source()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


@pytest.fixture
def fixture_root(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    manifest = {
        "model_sha256": MODEL,
        "samples": [
            {"tensor_sha256": hashlib.sha256(t.tobytes()).hexdigest(), "reference_top1": i}
            for i, t in enumerate(FIXTURE)
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    config = tmp_path / "model.json"
    config.write_text(
        json.dumps(
            {
                "model_ref": "resnet50",
                "source_model_sha256": MODEL,
                "runtime": "cuda",
                "artifact_sha256": "cd" * 32,
                "qualification_digest": "sha256:"
                + hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            }
        )
    )
    return root, config


class FakeRuntime:
    """Runtime-shaped double: slow init, top-1 read from the consumed tensor."""

    consumed: list = []
    fail_infer = False
    closed = 0

    def __init__(self, config, root):
        time.sleep(INIT_SECONDS)
        self.identity = {**config, "input_contract": "rgb-f32-nhwc-224x224x3-v1"}
        self.metadata = {"fake": True}
        self.stack = contextlib.ExitStack()
        self.stack.callback(self._close)

    @classmethod
    def _close(cls):
        cls.closed += 1

    def _scores(self, tensor):
        if FakeRuntime.fail_infer:
            raise AssertionError("native")
        FakeRuntime.consumed.append(hashlib.sha256(tensor.tobytes()).hexdigest())
        scores = np.zeros(1000, dtype=np.float32)
        scores[int(tensor[0, 0, 0])] = 1
        return scores


@pytest.fixture(autouse=True)
def reset_fake():
    FakeRuntime.consumed, FakeRuntime.fail_infer, FakeRuntime.closed = [], False, 0


def workload(body, model=MODEL, units=2, base=None):
    """The accepted identity naming this exact staged object, model and shape."""
    try:
        shape = np.load(io.BytesIO(body), allow_pickle=False).shape
    except ValueError:
        shape = (1,)
    fields = {
        "model_digest": "sha256:" + model,
        "dataset_version": "sha256:" + hashlib.sha256(body).hexdigest(),
        "input_shape": shape,
        "work_units": units,
    }
    if base is not None:
        return base.model_copy(update=fields)
    return WorkloadIdentity(
        code_digest=signature("code"),
        config_digest=signature("config"),
        task_type="inference",
        batch_size=1,
        precision="fp32",
        quality_contract_digest=signature("quality"),
        measurement_boundary="staged-host-input-to-native-output-per-pass",
        **fields,
    )


def argv(fixture_root, url, body, cache, runtime="cuda", model=MODEL, identity=None):
    root, config = fixture_root
    identity = identity or workload(body, model)
    return [
        "--runtime", runtime, "--workload-identity", identity.model_dump_json(),
        "--config", str(config), "--fixture", str(root),
        "--source", url, "--sha256", hashlib.sha256(body).hexdigest(),
        "--size-bytes", str(len(body)), "--source-endpoint-ref", "loopback-http",
        "--cache-dir", str(cache),
    ]  # fmt: skip


def job_env(args):
    identity = WorkloadIdentity.model_validate_json(args[args.index("--workload-identity") + 1])
    return {
        "RA_JOB_ID": "job-1",
        "RA_ATTEMPT_ID": "attempt-1",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": signature(identity),
        "RA_CONTEXT_SIGNATURE": signature("c"),
        "RA_WORK_UNITS": str(identity.work_units),
    }


def invoke(args, env=None, **factories):
    lines = []
    factories.setdefault("resnet_runtime", FakeRuntime)
    result, receipt = run(env or job_env(args), argv=args, write=lines.append, **factories)
    envelope = result_from_log("\n".join(lines))
    assert envelope["digest"] == signature(envelope["result"])
    return result, receipt, envelope, lines


class InProcessBackend:
    """Runs the real CLI entry in-process as a named 'Pod' with the adapter's env."""

    def __init__(self, args):
        self.args, self.runs = args, {}

    def validate(self, job):
        pass

    def submit(self, job):
        uid = str(uuid.uuid4())
        env = {
            **identity_environment(job),
            "RA_NODE_NAME": HOST,
            "RA_POD_NAME": "pod-" + job["id"],
            "RA_POD_UID": uid,
        }
        lines, started = [], now()
        run(env, argv=self.args, write=lines.append, resnet_runtime=FakeRuntime)
        self.runs[job["id"]] = (started, now(), "\n".join(lines), uid)
        return "local-" + job["id"]

    def reconcile(self, job):
        return "local-" + job["id"] if job["id"] in self.runs else None

    def status(self, job):
        started, finished, _, uid = self.runs[job["id"]]
        return Observation(
            State.COLLECTING,
            submitted_at=started.isoformat(),
            started_at=started.isoformat(),
            execution_started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            placement={
                "source": "kubernetes:Pod",
                "node_name": HOST,
                "pod_name": "pod-" + job["id"],
                "pod_uid": uid,
            },
        )

    def result(self, job):
        return result_from_log(self.runs[job["id"]][2])

    def cancel(self, job):
        pass


def test_worker_ingests_staged_bytes_fed_to_inference_cold_then_hot(
    service, bundle, source, fixture_root, tmp_path
):
    from test_staged_execution import rows

    from resource_advisor.recommendation import network_profile_from_receipts
    from resource_advisor.worker import Worker

    spec, candidate, variant, cap = bundle
    accepted = workload(STAGED, units=spec.identity.work_units, base=spec.identity)
    cap = cap.model_copy(update={"ref": "local-cap", "node_ref": HOST})
    candidate = candidate.model_copy(update={"ref": "local", "capability_ref": cap.ref})
    variant = variant.model_copy(
        update={
            "ref": "acc-variant",
            "workload_ref": "acc-work",
            "workload_signature": signature(accepted),
            "model_digest": accepted.model_digest,
            "supported_shapes": (accepted.input_shape,),
        }
    )
    spec = spec.model_copy(
        update={
            "ref": "acc-work",
            "identity": accepted,
            "candidates": (candidate.model_copy(update={"variant_ref": variant.ref}),),
            "baseline_candidate_ref": "local",
        }
    )
    service.operational_mode = True
    for kind, model in [("capability", cap), ("variant", variant), ("workload", spec)]:
        service.register(kind, model, "team-a")
    url = source.put("staged.npy", STAGED)
    cache = (tmp_path / "cache").resolve()
    worker = Worker(
        service,
        {
            ("team-a", "lab"): InProcessBackend(
                argv(fixture_root, url, STAGED, cache, identity=accepted)
            )
        },
    )

    def once(key):
        job = service.submit(
            "team-a", JobRequest(workload_ref=spec.ref, candidate_ref="local"), key
        )
        assert worker.submit_one()
        worker.reconcile_all()
        assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
        [row] = rows(service, [job["job_id"]])
        return row

    cold, hot = once("a-1"), once("a-2")
    staged = [hashlib.sha256(t.tobytes()).hexdigest() for t in FIXTURE[[2, 0, 3]]]
    units = spec.identity.work_units
    assert FakeRuntime.consumed == staged * units * 2  # exactly the staged tensors, both jobs
    assert FakeRuntime.closed == 2
    assert cold["staging"]["cache_state"] == "cold" and hot["staging"]["cache_state"] == "hot"
    for row in (cold, hot):
        assert row["staging"]["content_digest"] == "sha256:" + hashlib.sha256(STAGED).hexdigest()
        assert row["staging"]["sample"]["bytes"] == len(STAGED)
        assert row["staging"]["pod_uid"] == row["body"]["placement"]["pod_uid"]
        m = row["result"]["measurements"]
        assert m["quality_value"] == 1.0 and m["work_units"] == units
        assert m["elapsed_seconds"] < INIT_SECONDS  # initialization is not compute
    assert network_profile_from_receipts([hot], ref="h", valid_for_seconds=60).cache_state == "hot"


def test_elapsed_is_compute_only_and_report_labels_quality(source, fixture_root, tmp_path):
    url = source.put("d.npy", STAGED)
    started = time.perf_counter()
    result, receipt, envelope, lines = invoke(argv(fixture_root, url, STAGED, tmp_path / "c"))
    wall = time.perf_counter() - started
    m = result.measurements
    assert result.outcome == "COMPLETED" and receipt.cache_state == "cold"
    assert m.elapsed_seconds + INIT_SECONDS + receipt.sample.elapsed_seconds <= wall
    assert envelope["staging_receipt"] == receipt.model_dump(mode="json")
    [report] = [json.loads(x.split(" ", 1)[1]) for x in lines if "ACCELERATOR_REPORT" in x]
    assert report["preparation_seconds"] >= INIT_SECONDS
    assert report["quality_scope"] == "staged-subset-of-qualified-fixture"
    assert report["tensor_count"] == 3 and len(report["samples_seconds"]) == 2
    assert sum(report["samples_seconds"]) == pytest.approx(m.elapsed_seconds)


def test_wrong_top1_lowers_quality_instead_of_passing_on_finite_output(
    source, fixture_root, tmp_path, monkeypatch
):
    scores = FakeRuntime._scores
    monkeypatch.setattr(FakeRuntime, "_scores", lambda self, t: np.roll(scores(self, t), 1))
    url = source.put("w.npy", STAGED)
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path))
    assert result.outcome == "COMPLETED" and result.measurements.quality_value == 0.0


@pytest.mark.parametrize(
    ("body", "declared", "code"),
    [
        (STAGED, hashlib.sha256(b"other").hexdigest(), "STAGING_CONTENT_DIGEST_MISMATCH"),
        (npy(FIXTURE[:, :, :200]), None, "STAGED_INPUT_CONTRACT_MISMATCH"),
        (npy(FIXTURE.astype(np.float64)), None, "STAGED_INPUT_CONTRACT_MISMATCH"),
        (b"not an npy file", None, "STAGED_INPUT_INVALID"),
        (npy(images(2, offset=10)), None, "STAGED_INPUT_NO_QUALIFIED_REFERENCE"),
    ],
    ids=["digest", "shape", "dtype", "not-npy", "unqualified"],
)
def test_bad_inputs_fail_without_receipt(source, fixture_root, tmp_path, body, declared, code):
    identity = workload(body)
    if declared:  # the accepted contract names other bytes than the source serves
        identity = identity.model_copy(update={"dataset_version": "sha256:" + declared})
    args = argv(fixture_root, source.put("x.npy", body), body, tmp_path, identity=identity)
    if declared:
        args[args.index("--sha256") + 1] = declared
    result, receipt, envelope, lines = invoke(args)
    assert (result.outcome, result.error_code, receipt) == ("FAILED", code, None)
    assert "staging_receipt" not in envelope and result.measurements is None
    assert not any("ACCELERATOR_REPORT" in x for x in lines)


def test_oversized_input_is_rejected_before_transfer(source, fixture_root, tmp_path):
    args = argv(fixture_root, source.put("big.npy", STAGED), STAGED, tmp_path)
    args[args.index("--size-bytes") + 1] = str(32 * 1024**2 + 1)
    result, receipt, _, _ = invoke(args)
    assert result.error_code == "STAGING_SIZE_EXCEEDS_LIMIT" and receipt is None


def test_native_and_identity_failures_never_report_success(source, fixture_root, tmp_path):
    url = source.put("d.npy", STAGED)
    FakeRuntime.fail_infer = True
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "a"))
    assert (result.error_code, receipt) == ("NATIVE_INFERENCE_FAILED", None)
    assert FakeRuntime.closed == 1
    FakeRuntime.fail_infer = False

    def broken(config, root):
        raise RuntimeError("no CUDA provider")

    result, receipt, _, _ = invoke(
        argv(fixture_root, url, STAGED, tmp_path / "b"), resnet_runtime=broken
    )
    assert (result.error_code, receipt) == ("NATIVE_INITIALIZATION_FAILED", None)
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "c", model="ef" * 32))
    assert (result.error_code, receipt) == ("MODEL_IDENTITY_MISMATCH", None)
    assert (tmp_path / "c").exists()  # staged, then rejected by the runtime's own identity
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "d", runtime="hailo"))
    assert (result.error_code, receipt) == ("MODEL_IDENTITY_MISMATCH", None)
    (fixture_root[0] / "manifest.json").write_text("{}")
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "e"))
    assert (result.error_code, receipt) == ("QUALIFICATION_MANIFEST_MISMATCH", None)


def test_expected_config_and_file_failures_emit_failed_envelope(source, fixture_root, tmp_path):
    url = source.put("d.npy", STAGED)
    root, config = fixture_root
    args = argv(fixture_root, url, STAGED, tmp_path / "a")
    args[args.index("--config") + 1] = str(tmp_path / "missing-model.json")
    result, receipt, envelope, _ = invoke(args)  # staged bytes valid, config absent
    assert (result.error_code, receipt) == ("MODEL_CONFIG_INVALID", None)
    assert envelope["result"]["outcome"] == "FAILED" and "staging_receipt" not in envelope
    config.write_text("{not json")
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "b"))
    assert (result.error_code, receipt) == ("MODEL_CONFIG_INVALID", None)
    config.write_text("[]")
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "c"))
    assert (result.error_code, receipt) == ("MODEL_IDENTITY_MISMATCH", None)


def test_missing_or_malformed_manifest_emits_failed_envelope(source, fixture_root, tmp_path):
    url = source.put("d.npy", STAGED)
    manifest = fixture_root[0] / "manifest.json"
    raw = manifest.read_bytes()
    manifest.unlink()
    result, receipt, envelope, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "a"))
    assert (result.error_code, receipt) == ("QUALIFICATION_MANIFEST_INVALID", None)
    assert "staging_receipt" not in envelope
    assert FakeRuntime.closed >= 1
    manifest.write_bytes(raw)


def test_inputs_must_be_the_accepted_workload_contract(source, fixture_root, tmp_path):
    url = source.put("d.npy", STAGED)
    args = argv(fixture_root, url, STAGED, tmp_path / "never", identity=workload(npy(FIXTURE[:2])))
    result, receipt, _, _ = invoke(args)  # signature matches, dataset version does not
    assert (result.error_code, receipt) == ("WORKLOAD_CONTRACT_MISMATCH", None)
    args = argv(fixture_root, url, STAGED, tmp_path / "never", identity=workload(STAGED, units=3))
    result, receipt, _, _ = invoke(args, {**job_env(args), "RA_WORK_UNITS": "2"})
    assert (result.error_code, receipt) == ("WORKLOAD_CONTRACT_MISMATCH", None)
    args = argv(fixture_root, url, STAGED, tmp_path / "never")
    forged = {**job_env(args), "RA_WORKLOAD_SIGNATURE": signature("another workload")}
    result, receipt, _, _ = invoke(args, forged)
    assert (result.error_code, receipt) == ("WORKLOAD_CONTRACT_MISMATCH", None)
    assert not (tmp_path / "never").exists()
    shape = workload(STAGED).model_copy(update={"input_shape": (2, 224, 224, 3)})
    result, receipt, _, _ = invoke(argv(fixture_root, url, STAGED, tmp_path / "s", identity=shape))
    assert (result.error_code, receipt) == ("STAGED_INPUT_CONTRACT_MISMATCH", None)


def test_missing_plan_binding_for_node_fails_without_staging(source, fixture_root, tmp_path):
    args = argv(fixture_root, source.put("d.npy", STAGED), STAGED, tmp_path / "never")
    env = {**job_env(args), "RA_STAGING_BINDINGS_JSON": json.dumps({"other": {"ref": "c"}})}
    result, receipt, _, _ = invoke(args, env)
    assert (result.error_code, receipt) == ("STAGING_BINDING_MISSING_FOR_NODE", None)
    assert not (tmp_path / "never").exists()


class FakeQb:
    """qbruntime-shaped module; the 'model' output is a function of its input."""

    calls = []

    class Cluster:
        Cluster0 = "c0"

    class Core:
        Core0 = "k0"

    class CoreId:
        def __init__(self, cluster, core):
            self.value = f"{cluster}/{core}"

        def __str__(self):
            return self.value

    class ModelConfig:
        def set_single_core_mode(self, core_ids):
            self.cores = core_ids
            return True

    class Accelerator:
        def __init__(self, index):
            pass

    class Model:
        def __init__(self, path, config):
            self.config = config

        def launch(self, accelerator):
            time.sleep(INIT_SECONDS)

        def get_target_cores(self):
            return self.config.cores

        def get_model_input_shape(self):
            return [(224, 344, 3)]

        def infer(self, inputs):
            FakeQb.calls.append(hashlib.sha256(inputs[0].tobytes()).hexdigest())
            return [inputs[0] * 0.5]

        def dispose(self):
            pass


@pytest.fixture
def fake_qb(monkeypatch):
    import importlib.metadata

    FakeQb.calls = []
    module = types.ModuleType("qbruntime")
    for name in ("Cluster", "Core", "CoreId", "ModelConfig", "Accelerator", "Model"):
        setattr(module, name, getattr(FakeQb, name))
    monkeypatch.setitem(sys.modules, "qbruntime", module)
    real_version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "1.4.0" if name == "mobilint-qb-runtime" else real_version(name),
    )
    monkeypatch.setattr(npu_probe, "file_digest", lambda path: "sha256:" + npu_probe.MOBILINT_MODEL)


def test_mobilint_no_arg_run_keeps_its_generated_input_and_ten_rounds(fake_qb):
    report = npu_probe.mobilint()
    data = np.random.default_rng(npu_probe.SEED).uniform(-1, 1, (224, 344, 3)).astype(np.float32)
    assert report["input_digest"] == "sha256:" + hashlib.sha256(data.tobytes()).hexdigest()
    assert len(report["samples_seconds"]) == npu_probe.ROUNDS == 10
    assert report["quality_scope"] == "runtime-repeatability-only"
    assert report["reference_output_digest"] == (
        "sha256:" + hashlib.sha256((data * 0.5).tobytes()).hexdigest()
    )
    assert len(FakeQb.calls) == 11


def test_mobilint_staged_input_is_the_inferred_input(fake_qb, source, fixture_root, tmp_path):
    data = np.random.default_rng(3).uniform(-1, 1, (2, 224, 344, 3)).astype(np.float32)
    body = npy(data)
    args = argv(
        fixture_root, source.put("m.npy", body), body, tmp_path,
        runtime="mobilint-candy", model=npu_probe.MOBILINT_MODEL,
        identity=workload(body, npu_probe.MOBILINT_MODEL, units=3),
    )  # fmt: skip
    result, receipt, _, lines = invoke(args)
    assert result.outcome == "COMPLETED" and receipt.cache_state == "cold"
    assert FakeQb.calls == [hashlib.sha256(t.tobytes()).hexdigest() for t in data] * 4
    assert result.measurements.elapsed_seconds < INIT_SECONDS
    assert result.measurements.quality_value == 1.0
    [report] = [json.loads(x.split(" ", 1)[1]) for x in lines if "ACCELERATOR_REPORT" in x]
    assert report["quality_scope"] == "runtime-repeatability-only"
    assert report["task_accuracy"] is None and report["preparation_seconds"] >= INIT_SECONDS
    resnet = argv(
        fixture_root, source.put("m.npy", body), body, tmp_path / "r", runtime="mobilint-candy"
    )
    result, receipt, _, _ = invoke(resnet)
    assert (result.error_code, receipt) == ("MODEL_IDENTITY_MISMATCH", None)
