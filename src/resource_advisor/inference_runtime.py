"""Persistent ResNet50 CUDA/Hailo lab worker, executable without the API dependencies."""

import argparse
import contextlib
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Runtime:
    def __init__(self, config, root):
        import numpy as np

        self.np = np
        self.config = config
        self.identity = {
            key: config[key]
            for key in (
                "model_ref",
                "source_model_sha256",
                "runtime",
                "artifact_sha256",
                "qualification_digest",
            )
        }
        self.identity["runtime_image_digest"] = os.environ["RA_RUNTIME_IMAGE_DIGEST"]
        self.identity.update(
            input_contract="rgb-f32-nhwc-224x224x3-v1",
            output_contract="imagenet-scores-f32-1000-v1",
        )
        manifest = json.loads((root / "manifest.json").read_text())
        assert "sha256:" + sha(root / "manifest.json") == config["qualification_digest"]
        assert manifest["model_sha256"] == config["source_model_sha256"]
        for name, key in [("inputs.npy", "inputs_sha256"), ("reference.npy", "reference_sha256")]:
            assert sha(root / name) == manifest[key]
        self.stack = contextlib.ExitStack()
        if config["runtime"] == "cuda":
            import onnxruntime as ort

            assert ort.__version__ == "1.23.2"
            ort.preload_dlls(directory="")
            artifact = root / "resnet50.onnx"
            assert sha(artifact) == config["artifact_sha256"]
            options = ort.SessionOptions()
            options.intra_op_num_threads = options.inter_op_num_threads = 1
            options.log_severity_level = 3
            options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
            session = ort.InferenceSession(
                str(artifact),
                options,
                providers=[("CUDAExecutionProvider", {"device_id": 0, "use_tf32": 0})],
            )
            session.disable_fallback()
            assert session.get_providers()[0] == "CUDAExecutionProvider"
            key = session.get_inputs()[0].name
            mean = np.asarray([123.68, 116.78, 103.94], dtype=np.float32)
            self.infer = lambda tensor: session.run(None, {key: (tensor - mean)[None]})[0].reshape(
                -1
            )
            self.metadata = {"onnxruntime": ort.__version__, "providers": session.get_providers()}
        else:
            import hailo_platform as hailo

            assert hailo.__version__ == "4.23.0"
            assert Path("/sys/module/hailo_pci/version").read_text().strip() == "4.23.0"
            ids = hailo.Device.scan()
            assert len(ids) == 1
            artifact = root / "model.hef"
            assert sha(artifact) == config["artifact_sha256"]
            params = hailo.VDevice.create_params()
            params.scheduling_algorithm = hailo.HailoSchedulingAlgorithm.NONE
            target = self.stack.enter_context(hailo.VDevice(params, device_ids=ids))
            hef = hailo.HEF(str(artifact))
            group = target.configure(
                hef,
                hailo.ConfigureParams.create_from_hef(
                    hef, interface=hailo.HailoStreamInterface.PCIe
                ),
            )[0]
            ip = hailo.InputVStreamParams.make_from_network_group(
                group, quantized=False, format_type=hailo.FormatType.FLOAT32
            )
            op = hailo.OutputVStreamParams.make_from_network_group(
                group, quantized=False, format_type=hailo.FormatType.FLOAT32
            )
            pipeline = self.stack.enter_context(hailo.InferVStreams(group, ip, op))
            self.stack.enter_context(group.activate(group.create_params()))
            key, output = (
                hef.get_input_vstream_infos()[0].name,
                hef.get_output_vstream_infos()[0].name,
            )
            self.infer = lambda tensor: pipeline.infer({key: tensor[None]})[output].reshape(-1)
            self.metadata = {"hailort": hailo.__version__, "driver": "4.23.0"}
        data = np.load(root / "inputs.npy", allow_pickle=False)
        assert data.shape == (100, 224, 224, 3) and data.dtype == np.float32
        for sample, tensor in zip(manifest["samples"], data, strict=True):
            assert hashlib.sha256(tensor.tobytes()).hexdigest() == sample["tensor_sha256"]
        for tensor in data[:4]:
            self._scores(tensor)
        started = time.perf_counter()
        predictions = [int(np.argmax(self._scores(tensor))) for tensor in data]
        self.service_ms = (time.perf_counter() - started) * 10
        accuracy = (
            sum(p == s["label"] for p, s in zip(predictions, manifest["samples"], strict=True))
            / 100
        )
        agreement = (
            sum(
                p == s["reference_top1"]
                for p, s in zip(predictions, manifest["samples"], strict=True)
            )
            / 100
        )
        ref_accuracy = sum(s["label"] == s["reference_top1"] for s in manifest["samples"]) / 100
        assert accuracy >= 0.75 and agreement >= 0.90 and ref_accuracy - accuracy <= 0.05 + 1e-12
        self.qualification = {
            "accuracy": accuracy,
            "reference_agreement": agreement,
            "distinct_images": 100,
            "manifest_digest": config["qualification_digest"],
        }
        self.lock, self.execution = threading.Lock(), threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.inflight = 0
        self.ready = True

    def _scores(self, tensor):
        scores = self.infer(tensor)
        assert scores.shape == (1000,) and self.np.isfinite(scores).all()
        return scores

    def health(self):
        with self.lock:
            return {
                "ready": self.ready,
                "identity": self.identity,
                "inflight": self.inflight,
                "max_inflight": 8,
                "service_ms": self.service_ms,
                "qualification": self.qualification,
                "runtime_metadata": self.metadata,
            }

    def predict(self, payload):
        instances = payload.get("instances") if isinstance(payload, dict) else None
        if (
            not isinstance(instances, list)
            or not 1 <= len(instances) <= 4
            or any(
                not isinstance(row, list)
                or len(row) != 150528
                or any(type(x) not in (int, float) for x in row)
                for row in instances
            )
        ):
            raise ValueError("expected 1-4 flattened RGB224 images")
        data = self.np.asarray(instances, dtype=self.np.float32).reshape(-1, 224, 224, 3)
        if not self.np.isfinite(data).all() or data.min() < 0 or data.max() > 255:
            raise ValueError("pixels must be finite numbers in [0,255]")
        if not self.slots.acquire(blocking=False):
            return None
        with self.lock:
            self.inflight += 1
        queued = time.perf_counter()
        try:
            with self.execution:
                with self.lock:
                    if not self.ready:
                        return None
                queue_seconds = time.perf_counter() - queued
                started = time.perf_counter()
                try:
                    predictions = [self._scores(tensor).tolist() for tensor in data]
                except Exception:  # noqa: BLE001 -- vendor SDK exceptions differ; invalidate under execution lock
                    with self.lock:
                        self.ready = False
                    raise RuntimeError("native inference failed") from None
                service_ms = (time.perf_counter() - started) * 1000 / len(instances)
                with self.lock:
                    self.service_ms = 0.8 * self.service_ms + 0.2 * service_ms
                return {
                    "identity": self.identity,
                    "predictions": predictions,
                    "queue_seconds": queue_seconds,
                }
        finally:
            with self.lock:
                self.inflight -= 1
            self.slots.release()


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args):
        self.connections = threading.BoundedSemaphore(16)
        super().__init__(*args)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except (OSError, RuntimeError):
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


def serve(runtime, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path != "/health":
                self.reply(404, {"detail": "not found"})
            else:
                self.reply(200 if runtime.ready else 503, runtime.health())

        def do_POST(self):
            if self.path != "/predict":
                self.reply(404, {"detail": "not found"})
                return
            self.connection.settimeout(5)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 12 * 1024**2:
                    raise ValueError("request body exceeds bounded image contract")
                result = runtime.predict(json.loads(self.rfile.read(length)))
                self.reply(
                    200 if result is not None else 503, result or {"detail": "worker queue full"}
                )
            except (ValueError, TypeError, OverflowError):
                self.reply(422, {"detail": "invalid image input"})
            except (AssertionError, RuntimeError):
                with runtime.lock:
                    runtime.ready = False
                self.reply(503, {"detail": "native inference failed"})
            except (TimeoutError, OSError):
                return

    server = BoundedServer(("0.0.0.0", port), Handler)
    print("INFERENCE_READY " + json.dumps(runtime.health()), flush=True)
    try:
        server.serve_forever()
    finally:
        runtime.stack.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=Path("/opt/fixture"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if config["runtime"] not in {"cuda", "hailo"}:
        raise ValueError("unsupported runtime")
    serve(Runtime(config, args.fixture), args.port)


if __name__ == "__main__":
    main()
