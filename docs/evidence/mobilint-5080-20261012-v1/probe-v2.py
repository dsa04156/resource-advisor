"""Candy deployment timing; model equivalence is deliberately not assumed."""
import argparse
import base64
import collections
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import time
import zlib

import numpy as np


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=["mobilint", "cuda"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 1:
        raise ValueError("positive sample and warmup counts required")
    if sha(Path(args.model).read_bytes()) != args.model_sha256:
        raise ValueError("model artifact mismatch")
    data = np.random.default_rng(20261008).uniform(-1, 1, (224, 344, 3)).astype(np.float32)
    meta = {}
    model = None
    setup = time.perf_counter()
    if args.runtime == "mobilint":
        import qbruntime as qb

        version = importlib.metadata.version("mobilint-qb-runtime")
        if version != "1.4.0":
            raise ValueError("qualified runtime 1.4.0 required")
        target = qb.CoreId(qb.Cluster.Cluster0, qb.Core.Core0)
        cfg = qb.ModelConfig()
        if not cfg.set_single_core_mode(core_ids=[target]):
            raise ValueError("single-core request refused")
        accel = qb.Accelerator(0)
        model = qb.Model(args.model, cfg)
        model.launch(accel)
        if tuple(model.get_model_input_shape()[0]) != data.shape:
            raise ValueError("NPU input shape mismatch")
        if [str(c) for c in model.get_target_cores()] != [str(target)]:
            raise ValueError("NPU target core mismatch")
        meta = {"runtime_version": version, "target_cores": [str(target)],
                "precision": "compiled-mxq-float32-io-internal-precision-unverified"}

        def infer():
            return np.array(model.infer([data])[0], dtype=np.float32, copy=True)
    else:
        import onnxruntime as ort

        ort.preload_dlls(directory="")
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise ValueError("CUDA provider unavailable")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
        providers = [("CUDAExecutionProvider", {"device_id": 0, "use_tf32": 0})]
        session = ort.InferenceSession(args.model, options, providers=providers)
        session.disable_fallback()
        registered = session.get_providers()
        if not registered or registered[0] != "CUDAExecutionProvider" or any(
            p not in ["CUDAExecutionProvider", "CPUExecutionProvider"] for p in registered
        ):
            raise ValueError("unexpected execution providers")
        cuda_input = np.ascontiguousarray(data.transpose(2, 0, 1)[None])
        key = session.get_inputs()[0].name
        meta = {"runtime_version": ort.__version__, "providers": session.get_providers(),
                "provider_options": session.get_provider_options(), "precision": "fp32-tf32-disabled"}

        def infer():
            return np.array(session.run(None, {key: cuda_input})[0][0].transpose(1, 2, 0),
                            dtype=np.float32, copy=True)

    setup_seconds = time.perf_counter() - setup
    try:
        warmup_seconds = []
        for _ in range(args.warmups):
            start = time.perf_counter()
            reference = infer()
            warmup_seconds.append(time.perf_counter() - start)
        if reference.shape != data.shape or not np.isfinite(reference).all():
            raise ValueError("finite HWC output required")
        samples, checksums, agreements = [], [], []
        for _ in range(args.samples):
            start = time.perf_counter()
            output = infer()
            elapsed = time.perf_counter() - start
            if output.shape != data.shape or not np.isfinite(output).all():
                raise ValueError("invalid output")
            samples.append(elapsed)
            checksums.append(sha(output.tobytes()))
            agreements.append(float(np.mean(output == reference)))
        if args.runtime == "cuda":
            # A separate session verifies placement without profiling the timed calls.
            options.enable_profiling = True
            options.profile_file_prefix = "/tmp/candy-placement"
            audit = ort.InferenceSession(args.model, options, providers=providers)
            audit.disable_fallback()
            audit.run(None, {key: cuda_input})
            events = json.loads(Path(audit.end_profiling()).read_text())
            counts = collections.Counter((e.get("args", {}).get("provider"),
                                          e.get("args", {}).get("op_name"))
                                         for e in events if e.get("cat") == "Node"
                                         and e.get("args", {}).get("provider"))
            if not counts or any(p != "CUDAExecutionProvider" for p, _ in counts):
                raise ValueError("non-CUDA model operator detected")
            meta["placement_counts"] = [{"provider": p, "op": op, "count": n}
                                        for (p, op), n in sorted(counts.items())]
            meta["placement_trace_zlib_base64"] = base64.b64encode(
                zlib.compress(json.dumps(events).encode())).decode()
        result = {"schema": 1, "runtime": args.runtime, "model_sha256": args.model_sha256,
                  "model_equivalence": "not-established", "seed": 20261008,
                  "input_shape": list(data.shape), "batch_size": 1,
                  "input_sha256": sha(data.tobytes()), "numpy": np.__version__,
                  "python": platform.python_version(), "metadata": meta,
                  "setup_seconds": setup_seconds, "warmup_seconds": warmup_seconds,
                  "samples_seconds": samples, "output_sha256": checksums,
                  "repeat_element_agreement": min(agreements),
                  "output_shape": list(reference.shape), "output_dtype": "float32",
                  "output_range": [float(reference.min()), float(reference.max())],
                  "output_zlib_base64": base64.b64encode(zlib.compress(reference.tobytes())).decode(),
                  "boundary": "prepared-host-input-to-contiguous-host-HWC-output; blocking"}
        print("CANDY_RESULT " + json.dumps(result, separators=(",", ":")), flush=True)
    finally:
        if model is not None:
            model.dispose()


if __name__ == "__main__":
    main()
