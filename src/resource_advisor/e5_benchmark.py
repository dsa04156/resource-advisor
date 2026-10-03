"""Fixed E5 input-reuse versus CUDA-path trial; profiler runs are diagnostics only."""

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import tempfile
from contextlib import nullcontext

from .contracts import ExecutionResult, Measurements, ThermalPolicy, signature
from .diagnostics import PhaseProfile, PhaseSample
from .kernel_diagnostics import PREFIX, summarize_trace, trace_evidence
from .thermal import NvmlReader, ThermalTrace, Window

BOUNDARY = "cnn-batch32-serial-input-transfer-4-forwards-v1"
MODEL_RECIPE = "conv3-32-relu-conv32-32-relu-conv32-32-relu-globalavg-v1"
SHAPE = (32, 3, 128, 128)
FORWARDS = 4
BLOCKS = 12


def build_model(torch, seed):
    torch.manual_seed(seed)
    return torch.nn.Sequential(
        torch.nn.Conv2d(3, 32, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.Conv2d(32, 32, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.Conv2d(32, 32, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.AdaptiveAvgPool2d(1),
    ).eval()


def validate(context, shape, units, precision):
    if tuple(shape) != SHAPE or units != BLOCKS or precision != "fp32":
        raise ValueError("E5 requires fixed batch32 fp32 shape and twelve blocks")
    if context["parameters"] not in ({"input_strategy": "recompute"}, {"input_strategy": "cache"}):
        raise ValueError("only the two approved input strategies are supported")
    if context["allocation_mode"] != "physical_device" or context["resources"] != {
        "host_cpu": 1,
        "host_memory_mib": 2048,
        "accelerator_count": 1,
    }:
        raise ValueError("E5 requires one physical GPU, one CPU and 2048 MiB host memory")


def run(*, kernel_profile=False):
    import torch

    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    shape = json.loads(os.environ["RA_INPUT_SHAPE"])
    units = int(os.environ["RA_WORK_UNITS"])
    policy = ThermalPolicy.model_validate_json(os.environ["RA_THERMAL_POLICY_JSON"])
    validate(context, shape, units, os.environ["RA_PRECISION"])
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("one actual CUDA GPU required; no CPU fallback")
    strategy = context["parameters"]["input_strategy"]
    with NvmlReader(str(torch.cuda.get_device_properties(0).uuid)) as reader:
        versions = {
            "pytorch": torch.__version__,
            "cuda": torch.version.cuda,
            "driver": reader.driver_version,
        }
        if (
            {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine()) != context["arch"]
            or torch.cuda.get_device_name(0) != context["accelerator_model"]
            or versions != context["runtime_versions"]
            or reader.driver_version != policy.driver_version
            or reader.device_uuid_digest != policy.device_uuid_digest
        ):
            raise RuntimeError("runtime/device changed since qualification")
        torch.set_num_threads(1)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        model_cpu = build_model(torch, int(os.environ["RA_SEED"]))
        raw = torch.randn(*SHAPE)

        def preprocess():
            return torch.nn.functional.avg_pool2d(raw, 3, stride=1, padding=1).contiguous()

        fixture = {
            "model_recipe": MODEL_RECIPE,
            "model_weights_digest": "sha256:"
            + hashlib.sha256(
                b"".join(t.contiguous().numpy().tobytes() for t in model_cpu.state_dict().values())
            ).hexdigest(),
            "input_digest": "sha256:" + hashlib.sha256(raw.numpy().tobytes()).hexdigest(),
            "prepared_input_digest": "sha256:"
            + hashlib.sha256(preprocess().numpy().tobytes()).hexdigest(),
            "shape": list(SHAPE),
            "seed": int(os.environ["RA_SEED"]),
            "blocks": BLOCKS,
            "forwards_per_block": FORWARDS,
            "profiler_enabled": kernel_profile,
            "input_strategy": strategy,
            "quality_scope": "generated deterministic CNN numerical agreement, not label accuracy",
        }
        print("RA_E5_FIXTURE " + json.dumps(fixture), flush=True)
        with torch.inference_mode():
            reference = model_cpu(preprocess()).double()
            model = copy.deepcopy(model_cpu).cuda()
            warm = preprocess().cuda()
            for _ in range(3):
                model(warm)
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            profiler = (
                torch.profiler.profile(
                    activities=[
                        torch.profiler.ProfilerActivity.CPU,
                        torch.profiler.ProfilerActivity.CUDA,
                    ],
                    record_shapes=False,
                    with_stack=False,
                    profile_memory=False,
                )
                if kernel_profile
                else nullcontext()
            )
            windows, samples, qualities = [], [], []
            cached = None
            with profiler:
                for i in range(BLOCKS):
                    before = reader.read()
                    start = reader.elapsed()
                    if strategy == "recompute":
                        prepared = [preprocess() for _ in range(FORWARDS)]
                    else:
                        if cached is None:
                            cached = preprocess()
                        prepared = [cached] * FORWARDS
                    input_done = reader.elapsed()
                    data = [p.cuda() for p in prepared]
                    torch.cuda.synchronize()
                    transfer_done = reader.elapsed()
                    marker = (
                        torch.profiler.record_function(f"{PREFIX}{i:04d}")
                        if kernel_profile
                        else nullcontext()
                    )
                    with marker:
                        outputs = [model(d) for d in data]
                        torch.cuda.synchronize()
                    finish = reader.elapsed()
                    after = reader.read()
                    windows.append(
                        Window(
                            sample_ref=f"iteration-{i:04d}",
                            before=before,
                            forward_started=start,
                            forward_finished=finish,
                            after=after,
                        )
                    )
                    samples.append(
                        PhaseSample(
                            wall_seconds=finish - start,
                            phases_seconds={
                                "cpu_processing": input_done - start,
                                "host_to_device": transfer_done - input_done,
                                "accelerator_compute": finish - transfer_done,
                            },
                        )
                    )
                    # Validate every output outside the measured objective, within Job cost.
                    qualities.extend(
                        torch.isclose(o.cpu().double(), reference, atol=1e-4, rtol=1e-4)
                        .double()
                        .mean()
                        .item()
                        for o in outputs
                    )
            measurements = Measurements(
                elapsed_seconds=math.fsum(s.wall_seconds for s in samples),
                peak_memory_mib=torch.cuda.max_memory_allocated() / 1024**2,
                quality_value=min(qualities),
                sample_count=BLOCKS,
                work_units=BLOCKS,
                throughput=BLOCKS / math.fsum(s.wall_seconds for s in samples),
            )
        result = ExecutionResult(
            job_id=os.environ["RA_JOB_ID"],
            attempt_id=os.environ["RA_ATTEMPT_ID"],
            epoch=int(os.environ["RA_EPOCH"]),
            workload_signature=os.environ["RA_WORKLOAD_SIGNATURE"],
            context_signature=os.environ["RA_CONTEXT_SIGNATURE"],
            outcome="COMPLETED",
            evidence_kind="hardware",
            measurements=measurements,
        )
        boundary = BOUNDARY + ("-profiler" if kernel_profile else "")
        phase = PhaseProfile(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            measurement_boundary=boundary,
            samples=tuple(samples),
        )
        trace = ThermalTrace(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            policy_digest=signature(policy),
            device_uuid_digest=reader.device_uuid_digest,
            driver_version=reader.driver_version,
            windows=tuple(windows),
        )
        envelope = {
            "result": result.model_dump(mode="json"),
            "digest": signature(result),
            "phase_profile": phase.model_dump(mode="json"),
            "thermal_trace": trace.model_dump(mode="json"),
        }
        if kernel_profile:
            with tempfile.TemporaryDirectory(prefix="ra-e5-") as directory:
                path = os.path.join(directory, "trace.json")
                profiler.export_chrome_trace(path)
                with open(path) as stream:
                    raw_trace = json.load(stream)
                # Persist a bounded, whitelisted input before parsing so an
                # exception does not erase the only replayable trace evidence.
                print(
                    "RA_E5_TRACE_INPUT " + json.dumps(trace_evidence(raw_trace), allow_nan=False),
                    flush=True,
                )
                envelope["kernel_diagnostics"] = summarize_trace(raw_trace, BLOCKS)
            # Deliberately no normal result envelope: diagnostic timings cannot
            # accidentally enter a plain execution's profile/MLflow/result path.
            print("RA_E5_KERNEL_DIAGNOSTIC " + json.dumps(envelope, allow_nan=False), flush=True)
        else:
            print("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-profile", action="store_true")
    run(kernel_profile=parser.parse_args().kernel_profile)


if __name__ == "__main__":
    main()
