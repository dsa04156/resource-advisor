"""Fixed-weight CUDA CNN transfer fixture with per-block phase/thermal evidence.

Generated inputs and weights check numerical consistency, not trained-model
accuracy. Every work unit preprocesses one input and executes 32 GPU forwards.
The elapsed objective is block time, not an individual inference latency.
"""

import copy
import hashlib
import json
import os
import platform
import statistics

from .contracts import ExecutionResult, Measurements, ThermalPolicy, signature
from .diagnostics import PhaseProfile, PhaseSample
from .thermal import NvmlReader, ThermalTrace, Window

BOUNDARY = "cnn-preprocess-transfer-32-forwards-synchronize-per-block-v1"
MODEL_RECIPE = "conv3-64-relu-conv64-64-relu-conv64-64-relu-globalavg-v1"
INNER_FORWARDS = 32
SHAPES = ((1, 3, 128, 128), (1, 3, 192, 192), (1, 3, 256, 256))


def build_model(torch, seed):
    torch.manual_seed(seed)
    return torch.nn.Sequential(
        torch.nn.Conv2d(3, 64, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.Conv2d(64, 64, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.Conv2d(64, 64, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.AdaptiveAvgPool2d(1),
    ).eval()


def validate(context, shape, units, precision):
    if tuple(shape) not in SHAPES or not 3 <= units <= 16 or precision != "fp32":
        raise ValueError("transfer fixture requires an approved fp32 shape and 3–16 blocks")
    if context["parameters"] or context["resources"]["host_cpu"] not in (1, 2, 4):
        raise ValueError("only fixed 1/2/4 CPU requests without extra parameters are supported")
    if (
        context["allocation_mode"] != "physical_device"
        or context["resources"]["accelerator_count"] != 1
    ):
        raise ValueError("one exclusive physical GPU is required")


def run():
    import torch

    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    shape = json.loads(os.environ["RA_INPUT_SHAPE"])
    units = int(os.environ["RA_WORK_UNITS"])
    policy = ThermalPolicy.model_validate_json(os.environ["RA_THERMAL_POLICY_JSON"])
    validate(context, shape, units, os.environ["RA_PRECISION"])
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("exactly one CUDA GPU required; no CPU fallback")
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
            raise RuntimeError("runtime or physical GPU differs from qualification")
        torch.set_num_threads(int(context["resources"]["host_cpu"]))
        torch.manual_seed(int(os.environ["RA_SEED"]))
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        model_cpu = build_model(torch, int(os.environ["RA_SEED"]))
        raw = torch.randn(*shape)

        def preprocess():
            return torch.nn.functional.avg_pool2d(raw, 3, stride=1, padding=1).contiguous()

        print(
            "RA_TRANSFER_FIXTURE "
            + json.dumps(
                {
                    "model_recipe": MODEL_RECIPE,
                    "model_weights_digest": "sha256:"
                    + hashlib.sha256(
                        b"".join(
                            t.contiguous().numpy().tobytes()
                            for t in model_cpu.state_dict().values()
                        )
                    ).hexdigest(),
                    "input_digest": "sha256:" + hashlib.sha256(raw.numpy().tobytes()).hexdigest(),
                    "shape": shape,
                    "seed": int(os.environ["RA_SEED"]),
                    "blocks": units,
                    "forwards_per_block": INNER_FORWARDS,
                    "quality_scope": "generated deterministic numerical fixture, not task accuracy",
                }
            ),
            flush=True,
        )
        with torch.inference_mode():
            reference = model_cpu(preprocess()).double()
            model = copy.deepcopy(model_cpu).cuda()
            warm = preprocess().cuda()
            for _ in range(3):
                model(warm)
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            windows, samples, qualities = [], [], []
            for i in range(units):
                before = reader.read()
                start = reader.elapsed()
                prepared = preprocess()
                input_done = reader.elapsed()
                data = prepared.cuda()
                torch.cuda.synchronize()
                transfer_done = reader.elapsed()
                for _ in range(INNER_FORWARDS):
                    output = model(data)
                torch.cuda.synchronize()
                finished = reader.elapsed()
                after = reader.read()
                windows.append(
                    Window(
                        sample_ref=f"iteration-{i:04d}",
                        before=before,
                        forward_started=start,
                        forward_finished=finished,
                        after=after,
                    )
                )
                samples.append(
                    PhaseSample(
                        wall_seconds=finished - start,
                        phases_seconds={
                            "cpu_processing": input_done - start,
                            "host_to_device": transfer_done - input_done,
                            "accelerator_compute": finished - transfer_done,
                        },
                    )
                )
                # Validate each block outside the objective, inside Job wall cost.
                qualities.append(
                    torch.isclose(output.cpu().double(), reference, atol=1e-4, rtol=1e-4)
                    .double()
                    .mean()
                    .item()
                )
        elapsed = sum(s.wall_seconds for s in samples)
        result = ExecutionResult(
            job_id=os.environ["RA_JOB_ID"],
            attempt_id=os.environ["RA_ATTEMPT_ID"],
            epoch=int(os.environ["RA_EPOCH"]),
            workload_signature=os.environ["RA_WORKLOAD_SIGNATURE"],
            context_signature=os.environ["RA_CONTEXT_SIGNATURE"],
            outcome="COMPLETED",
            evidence_kind="hardware",
            measurements=Measurements(
                elapsed_seconds=elapsed,
                peak_memory_mib=torch.cuda.max_memory_allocated() / 1024**2,
                quality_value=min(qualities),
                sample_count=units,
                work_units=units,
                throughput=units / elapsed,
            ),
        )
        trace = ThermalTrace(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            policy_digest=signature(policy),
            driver_version=reader.driver_version,
            device_uuid_digest=reader.device_uuid_digest,
            windows=tuple(windows),
        )
        phases = PhaseProfile(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            measurement_boundary=BOUNDARY,
            samples=tuple(samples),
        )
        # No p95 inference latency: block service time has a different meaning.
        print(
            "RA_TRANSFER_BLOCK_SUMMARY "
            + json.dumps(
                {"median_block_seconds": statistics.median(s.wall_seconds for s in samples)}
            ),
            flush=True,
        )
        print(
            "RESOURCE_ADVISOR_RESULT "
            + json.dumps(
                {
                    "result": result.model_dump(mode="json"),
                    "digest": signature(result),
                    "thermal_trace": trace.model_dump(mode="json"),
                    "phase_profile": phases.model_dump(mode="json"),
                },
                allow_nan=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    run()
