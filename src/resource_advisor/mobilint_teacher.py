"""Mobilint ARIES teacher logits over HTTP for GPU knowledge-distillation training.

The NPU only runs the frozen teacher forward pass (qbruntime ``Model.infer``); the
GPU student is the only model whose weights change. One endpoint accepts a batch
of float32 NHWC images that are already ImageNet-normalized exactly like the
student's preprocessing, so the teacher never re-normalizes, and returns the
1000 pre-softmax ImageNet logits with the native identity and timings.

Bounds: 1 <= N <= MAX_BATCH, 224x224x3, body <= MAX_BODY bytes, the body's sha256
must match its header. There are no paths, commands or model choices in a request.
The file needs only the standard library and numpy at import time so the qbruntime
image can run it with ``python -I``; qbruntime is imported by the server only.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA = "ra-teacher-logits-v1"
SHAPE = (224, 224, 3)
CLASSES = 1000
MAX_BODY = 32 * 1024 * 1024
IMAGE_BYTES = 224 * 224 * 3 * 4
MAX_BATCH = MAX_BODY // IMAGE_BYTES  # 55 float32 images fit the body bound
QBRUNTIME_VERSION = "1.4.0"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class QbBackend:
    """qbruntime on the allocated ARIES device, pinned to one core."""

    def __init__(self, model_path, model_sha256):
        import importlib.metadata

        import qbruntime as qb

        with open(model_path, "rb") as f:
            actual = sha256(f.read())
        if actual != model_sha256:
            raise RuntimeError(f"MXQ sha256 {actual} != pinned {model_sha256}")
        version = importlib.metadata.version("mobilint-qb-runtime")
        if version != QBRUNTIME_VERSION:
            raise RuntimeError(f"qbruntime {version} != {QBRUNTIME_VERSION}")
        target = qb.CoreId(qb.Cluster.Cluster0, qb.Core.Core0)
        config = qb.ModelConfig()
        if not config.set_single_core_mode(core_ids=[target]):
            raise RuntimeError("single-core request refused")
        self.accelerator = qb.Accelerator(0)
        self.model = qb.Model(str(model_path), config)
        self.model.launch(self.accelerator)
        cores = [str(c) for c in self.model.get_target_cores()]
        shape = tuple(self.model.get_model_input_shape()[0])
        if shape != SHAPE:
            raise RuntimeError(f"MXQ input shape {shape} != {SHAPE}")
        self.identity = {
            "runtime": "qbruntime",
            "runtime_version": version,
            "target_cores": cores,
            "model_sha256": actual,
            "model_file": os.path.basename(model_path),
        }
        try:
            self.identity["device_name"] = str(self.accelerator.get_device_name())
        except Exception as error:  # noqa: BLE001 - optional getter: recorded, never substituted
            self.identity["device_name_error"] = repr(error)[:200]

    def infer(self, image):
        import numpy as np

        # qbruntime writes into its input buffer: pass a private writeable copy.
        out = self.model.infer([np.array(image, dtype=np.float32, order="C", copy=True)])
        if not out:
            raise RuntimeError("qbruntime returned no output")
        logits = np.asarray(out[0], dtype=np.float32).reshape(-1)
        if logits.shape != (CLASSES,):
            raise RuntimeError(f"teacher output shape {logits.shape} != ({CLASSES},)")
        return logits


def decode_request(headers, body):
    """Validate one request; returns an (N,224,224,3) float32 array or raises ValueError."""
    import numpy as np

    try:
        shape = tuple(int(v) for v in headers.get("X-Teacher-Shape", "").split(","))
    except ValueError:
        raise ValueError("X-Teacher-Shape must be N,224,224,3") from None
    if len(shape) != 4 or shape[1:] != SHAPE or not 1 <= shape[0] <= MAX_BATCH:
        raise ValueError(f"shape must be N,224,224,3 with 1<=N<={MAX_BATCH}")
    expected = shape[0] * 224 * 224 * 3 * 4
    if len(body) != expected:
        raise ValueError(f"body is {len(body)} bytes, expected {expected}")
    if headers.get("X-Input-Sha256") != sha256(body):
        raise ValueError("X-Input-Sha256 does not match the body")
    array = np.frombuffer(body, dtype="<f4").reshape(shape)
    if not np.isfinite(array).all():
        raise ValueError("input contains non-finite values")
    return array


def make_server(backend, host, port, identity):
    state = {"requests": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/v1/teacher/health":
                return self.reply(404, {"error": "not found"})
            self.reply(
                200,
                {
                    "schema": SCHEMA,
                    "ready": True,
                    "teacher": identity,
                    "requests": state["requests"],
                },
            )

        def do_POST(self):
            started = time.perf_counter()
            if self.path != "/v1/teacher/logits":
                return self.reply(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                self.close_connection = True
                return self.reply(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            body = self.rfile.read(length)
            received = time.perf_counter()
            try:
                batch = decode_request(self.headers, body)
            except ValueError as error:
                return self.reply(400, {"error": str(error)})
            decoded = time.perf_counter()
            import numpy as np

            per_sample, rows = [], []
            try:
                for image in batch:
                    t0 = time.perf_counter()
                    rows.append(backend.infer(image))
                    per_sample.append(time.perf_counter() - t0)
            except Exception as error:  # noqa: BLE001 - native failure is reported, never hidden
                return self.reply(500, {"error": "teacher inference failed: " + repr(error)[:300]})
            logits = np.stack(rows).astype("<f4")
            if not np.isfinite(logits).all():
                return self.reply(500, {"error": "teacher produced non-finite logits"})
            inferred = time.perf_counter()
            raw = logits.tobytes()
            state["requests"] += 1
            value = {
                "schema": SCHEMA,
                "shape": list(logits.shape),
                "logits_b64": base64.b64encode(raw).decode(),
                "logits_sha256": sha256(raw),
                "input_sha256": self.headers.get("X-Input-Sha256"),
                "request_index": state["requests"],
                "teacher": identity,
                "server": {
                    "request_bytes": length,
                    "read_seconds": received - started,
                    "decode_seconds": decoded - received,
                    "infer_seconds": inferred - decoded,
                    "infer_seconds_per_sample": per_sample,
                    "encode_seconds": time.perf_counter() - inferred,
                    "clock": "perf_counter within the teacher process",
                },
            }
            value["server"]["handler_seconds"] = time.perf_counter() - started
            self.reply(200, value)

    return ThreadingHTTPServer((host, port), Handler)


def teacher_logits(url, batch, timeout=30.0, expect=None):
    """POST one NHWC float32 batch; returns (logits [N,1000] float32, receipt).

    ``expect`` pins teacher identity fields (e.g. model_sha256, runtime_version,
    pod_uid): a content digest alone does not identify which teacher answered.
    """
    import numpy as np

    array = np.ascontiguousarray(batch, dtype="<f4")
    if array.ndim != 4 or array.shape[1:] != SHAPE or not 1 <= array.shape[0] <= MAX_BATCH:
        raise ValueError(f"batch must be N,224,224,3 with 1<=N<={MAX_BATCH}")
    body = array.tobytes()
    parts = urllib.parse.urlsplit(url)
    started = time.perf_counter()
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/v1/teacher/logits",
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Teacher-Shape": ",".join(str(n) for n in array.shape),
                "X-Input-Sha256": sha256(body),
            },
        )
        response = connection.getresponse()
        limit = array.shape[0] * CLASSES * 4 * 2 + 64 * 1024  # base64 logits + metadata
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise RuntimeError("teacher response exceeds its bound")
    finally:
        connection.close()
    elapsed = time.perf_counter() - started
    value = json.loads(raw)
    if response.status != 200:
        raise RuntimeError(f"teacher HTTP {response.status}: {value.get('error')}")
    data = base64.b64decode(value["logits_b64"])
    if sha256(data) != value["logits_sha256"] or value["input_sha256"] != sha256(body):
        raise RuntimeError("teacher response digest mismatch")
    if value["shape"] != [array.shape[0], CLASSES] or len(data) != array.shape[0] * CLASSES * 4:
        raise RuntimeError("teacher response shape mismatch")
    mismatch = {k: v for k, v in (expect or {}).items() if value["teacher"].get(k) != v}
    if mismatch:
        raise RuntimeError(f"teacher identity differs from the pinned teacher: {sorted(mismatch)}")
    logits = np.frombuffer(data, dtype="<f4").reshape(value["shape"]).copy()
    if not np.isfinite(logits).all():
        raise RuntimeError("teacher logits are not finite")
    receipt = {k: v for k, v in value.items() if k != "logits_b64"}
    receipt["client"] = {
        "request_bytes": len(body),
        "response_bytes": len(raw),
        "elapsed_seconds": elapsed,
        "clock": "perf_counter within the client process (includes network and server)",
    }
    return logits, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    started = time.perf_counter()
    backend = QbBackend(args.model, args.model_sha256)
    identity = {
        **backend.identity,
        "node_name": os.environ.get("NODE_NAME"),
        "pod_uid": os.environ.get("POD_UID"),
        "launch_seconds": time.perf_counter() - started,
        "role": "frozen teacher forward only; no weight updates on the NPU",
    }
    print(json.dumps({"event": "teacher_ready", **identity}), flush=True)
    make_server(backend, args.host, args.port, identity).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
