"""Thermal protocol and NVML ABI fault doubles; never claims observed GPU telemetry."""

import ctypes as C
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_sampling import consume
from test_sampling import sampled as shared_sampled_fixture  # noqa: F401
from test_tracking import TrackingServer, delivery
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation, identity_environment
from resource_advisor.contracts import (
    ExecutionResult,
    JobRequest,
    RuntimeVariant,
    State,
    StudyRequest,
    ThermalPolicy,
    signature,
)
from resource_advisor.policy import context_signature
from resource_advisor.sampling import SamplingBindingRequest, SamplingPolicies
from resource_advisor.search import device_unit
from resource_advisor.store import Conflict
from resource_advisor.study import Studies
from resource_advisor.thermal import (
    NvmlReader,
    Reading,
    ThermalTrace,
    Window,
    assess,
)
from resource_advisor.worker import Worker


@pytest.fixture
def thermal_sampled(service, request, bundle):
    sampling_policy, spec, root = request.getfixturevalue("shared_sampled_fixture")
    _, _, old_variant, old_cap = bundle
    policy = ThermalPolicy(
        driver_version="fixture-driver",
        device_uuid_digest=signature("fixture-device"),
        maximum_temperature_c=80,
        maximum_gap_seconds=0.5,
        maximum_query_seconds=0.1,
    )
    versions = {**old_variant.runtime_versions, "driver": policy.driver_version}
    cap = old_cap.model_copy(update={"ref": "thermal-cap", "runtime_versions": versions})
    variant = old_variant.model_copy(
        update={
            "ref": "thermal-variant",
            "workload_ref": "thermal-workload",
            "workload_signature": signature(spec.identity),
            "supported_shapes": (spec.identity.input_shape,),
            "runtime_versions": versions,
            "thermal_policy": policy,
            "pilot_command": old_variant.command,
        }
    )
    candidate = spec.candidates[0].model_copy(
        update={
            "variant_ref": variant.ref,
            "capability_ref": cap.ref,
            "context": spec.candidates[0].context.model_copy(update={"runtime_versions": versions}),
        }
    )
    spec = spec.model_copy(
        update={
            "ref": "thermal-workload",
            "candidates": (candidate,),
            "profiling": spec.profiling.model_copy(
                update={"consent": True, "device_seconds": {device_unit(candidate, variant): 600}}
            ),
        }
    )
    for kind, value in [("capability", cap), ("variant", variant), ("workload", spec)]:
        service.register(kind, value, "team-a")
    SamplingPolicies(service).bind(
        "team-a", SamplingBindingRequest(workload_ref=spec.ref, policy_ref=sampling_policy.ref)
    )
    return sampling_policy, spec, root


def start(service, thermal_sampled):
    job = service.submit(
        "team-a", JobRequest(workload_ref=thermal_sampled[1].ref, candidate_ref="base"), "thermal"
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    return backend, worker, row


def reading(start):
    return Reading(
        started=start,
        finished=start + 0.001,
        temperature_c=45,
        power_w=30,
        sm_clock_mhz=1500,
        event_reasons=0,
        supported_reasons=0x1FF,
    )


def envelope(sampled, row, backend):
    result = consume(sampled, row, backend)
    policy = ThermalPolicy.model_validate(row["body"]["variant"]["thermal_policy"])
    windows = []
    for i, sample in enumerate(row["body"]["sampling_binding"]["plan"]["samples"]):
        t = i * 0.11
        windows.append(
            Window(
                sample_ref=sample["ref"],
                before=reading(t),
                forward_started=t + 0.002,
                forward_finished=t + 0.102,
                after=reading(t + 0.103),
            )
        )
    result["thermal_trace"] = ThermalTrace(
        job_id=row["id"],
        attempt_id=row["body"]["attempt_id"],
        result_digest=result["digest"],
        policy_digest=signature(policy),
        driver_version=policy.driver_version,
        device_uuid_digest=policy.device_uuid_digest,
        windows=tuple(windows),
    ).model_dump(mode="json")
    return result


def test_old_variant_and_context_signatures_remain_unchanged(bundle):
    _, candidate, variant, _ = bundle
    raw = variant.model_dump(mode="json")
    assert "thermal_policy" not in raw
    assert (
        RuntimeVariant.model_validate({**raw, "thermal_policy": None}).model_dump(mode="json")
        == raw
    )
    assert context_signature(candidate, variant) == signature(
        {
            "context": candidate.context.model_dump(mode="json"),
            "image": variant.image,
            "compiled_artifact_digest": variant.compiled_artifact_digest,
            "command": variant.command,
            "pilot_command": variant.pilot_command,
            "backend": candidate.backend,
        }
    )


def test_trace_acceptance_provenance_permissions_and_terminal_immutability(
    service, thermal_sampled
):
    from test_artifacts import MemoryS3

    from resource_advisor.artifacts import ArtifactDelivery, S3Artifacts

    backend, worker, row = start(service, thermal_sampled)
    e = envelope(thermal_sampled, row, backend)
    assert (
        json.loads(identity_environment(row)["RA_THERMAL_POLICY_JSON"])
        == row["body"]["variant"]["thermal_policy"]
    )
    backend.result = lambda _: e
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", row["id"])["state"] == State.SUCCEEDED
    with service.store.transaction() as conn:
        trace = service.store.get(conn, "thermal_trace", row["body"]["attempt_id"])["body"]
        job = service.store.job(conn, row["id"])
        assert job["body"]["thermal_assessment"]["status"] == "ELIGIBLE_TRACE"
        assert len(service.store.list(conn, "profile", "team-a")) == 1
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    tags = {t["key"]: t["value"] for t in server.batches[0]["tags"]}
    assert (
        tags["thermal.trace_digest"] == signature(trace)
        and tags["thermal.status"] == "ELIGIBLE_TRACE"
    )
    metrics = {m["key"]: m["value"] for m in server.batches[0]["metrics"]}
    assert metrics["thermal.maximum_observed_temperature_c"] == 45
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    assert ArtifactDelivery(service.store, storage).deliver_one()
    with service.store.transaction() as conn:
        artifact = service.store.list(conn, "artifact", "team-a")[0]["body"]
    payload = json.loads(storage.read(artifact))
    assert (
        payload["thermal_trace"] == trace
        and payload["thermal_assessment"] == job["body"]["thermal_assessment"]
    )
    tokens = {hashlib.sha256(k.encode()).hexdigest(): Principal(k) for k in ["team-a", "team-b"]}
    with TestClient(create_app(service, tokens)) as client:
        path = f"/api/v1/compute/jobs/{row['id']}/thermal-trace"
        assert client.get(path, headers={"Authorization": "Bearer team-a"}).json()["trace"] == trace
        assert client.get(path, headers={"Authorization": "Bearer team-b"}).status_code == 404
    altered = {**trace, "driver_version": "changed"}
    r = ExecutionResult.model_validate(e["result"])
    with pytest.raises(Conflict, match="immutable"):
        service.ingest("team-a", r, signature(r), thermal_trace=altered)


@pytest.mark.parametrize(
    "fault", ["missing", "driver", "device", "result", "attempt", "order", "duration"]
)
def test_missing_or_mismatched_traces_roll_back_all_evidence(service, thermal_sampled, fault):
    backend, worker, row = start(service, thermal_sampled)
    e = envelope(thermal_sampled, row, backend)
    t = e["thermal_trace"]
    if fault == "missing":
        e.pop("thermal_trace")
    elif fault == "driver":
        t["driver_version"] = "changed"
    elif fault == "device":
        t["device_uuid_digest"] = signature("wrong-device")
    elif fault == "result":
        t["result_digest"] = signature("other-result")
    elif fault == "attempt":
        t["attempt_id"] = "other-attempt"
    elif fault == "order":
        t["windows"].reverse()
    else:
        t["windows"][0]["forward_finished"] -= 0.01
    backend.result = lambda _: e
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", row["id"])["state"] == State.COLLECTING
    with service.store.transaction() as conn:
        for kind in ["result", "sampling_receipt", "thermal_trace", "profile"]:
            assert service.store.get(conn, kind, row["body"]["attempt_id"]) is None


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("temperature", "TEMPERATURE_LIMIT_EXCEEDED"),
        ("sw_thermal", "THERMAL_SLOWDOWN_OBSERVED"),
        ("hw_thermal", "THERMAL_SLOWDOWN_OBSERVED"),
        ("hardware", "HARDWARE_SLOWDOWN_OBSERVED"),
        ("unavailable", "SENSOR_UNAVAILABLE"),
        ("unsupported", "THERMAL_REASONS_UNSUPPORTED"),
    ],
)
def test_ineligible_trace_retained_without_recommendation_history(
    service, thermal_sampled, fault, reason
):
    backend, worker, row = start(service, thermal_sampled)
    e = envelope(thermal_sampled, row, backend)
    r = e["thermal_trace"]["windows"][0]["before"]
    if fault == "temperature":
        r["temperature_c"] = 90
    elif fault == "unavailable":
        r["temperature_c"] = None
        r["errors"] = {"temperature_c": 3}
    elif fault == "unsupported":
        r["supported_reasons"] = 0
    else:
        r["event_reasons"] = {"sw_thermal": 0x20, "hw_thermal": 0x40, "hardware": 0x8}[fault]
    backend.result = lambda _: e
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    with service.store.transaction() as conn:
        job = service.store.job(conn, row["id"])
        assert job["state"] == State.SUCCEEDED and job["body"]["quality_passed"]
        assert reason in job["body"]["thermal_assessment"]["reasons"]
        assert service.store.get(conn, "thermal_trace", row["body"]["attempt_id"])
        assert service.store.list(conn, "profile", "team-a") == []


def test_ineligible_pilot_stops_study_without_next_gpu_job(service, thermal_sampled):
    engine = Studies(service)
    study = engine.create(
        "team-a",
        StudyRequest(workload_ref=thermal_sampled[1].ref, strategy="random"),
        "thermal-study",
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    original = backend.result

    def hot(row):
        e = envelope(thermal_sampled, row, SimpleNamespace(result=original))
        e["thermal_trace"]["windows"][0]["before"]["event_reasons"] = 0x40
        return e

    backend.result = hot
    backend.observation = Observation(State.COLLECTING)
    for _ in range(8):
        engine.tick(study["ref"])
        worker.submit_one()
        worker.reconcile_all()
        current = engine.get("team-a", study["ref"])
        if current["state"] == "ABSTAINED":
            break
    assert (
        current["state"] == "ABSTAINED"
        and current["stop_reason"] == "THERMAL_OBSERVATION_INELIGIBLE"
    )
    assert backend.submissions == 1 and len(current["observations"]) == 1


def test_sampling_gaps_and_slow_sensor_queries_never_qualify(service, thermal_sampled):
    backend, _, row = start(service, thermal_sampled)
    e = envelope(thermal_sampled, row, backend)
    policy = ThermalPolicy.model_validate(row["body"]["variant"]["thermal_policy"])
    for field, value, reason in [
        ("maximum_gap_seconds", 0.00001, "OBSERVATION_GAP_TOO_LARGE"),
        ("maximum_query_seconds", 0.00001, "SENSOR_QUERY_TOO_SLOW"),
    ]:
        changed = policy.model_copy(update={field: value})
        trace = ThermalTrace.model_validate(
            {**e["thermal_trace"], "policy_digest": signature(changed)}
        )
        assert reason in assess(trace, changed)["reasons"]
    bad = e["thermal_trace"]["windows"][0]["before"]
    bad["temperature_c"] = None
    with pytest.raises(ValueError, match="explicit error"):
        Reading.model_validate(bad)


class NvmlDouble:
    def __init__(self, uuid, *, power_error=0):
        self.uuid = uuid
        self.power_error = power_error
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            if name == "nvmlDeviceGetHandleByUUID":
                assert args[0] == self.uuid.encode()
                args[-1]._obj.value = 123
            elif name == "nvmlDeviceGetUUID":
                args[-2].value = self.uuid.encode()
            elif name == "nvmlSystemGetDriverVersion":
                args[-2].value = b"fixture-driver"
            elif name == "nvmlDeviceGetPowerUsage" and self.power_error:
                return self.power_error
            elif name in [
                "nvmlDeviceGetTemperature",
                "nvmlDeviceGetPowerUsage",
                "nvmlDeviceGetClockInfo",
                "nvmlDeviceGetCurrentClocksThrottleReasons",
                "nvmlDeviceGetSupportedClocksThrottleReasons",
            ]:
                args[-1]._obj.value = {
                    "nvmlDeviceGetTemperature": 42,
                    "nvmlDeviceGetPowerUsage": 32100,
                    "nvmlDeviceGetClockInfo": 1400,
                    "nvmlDeviceGetCurrentClocksThrottleReasons": 0x20,
                    "nvmlDeviceGetSupportedClocksThrottleReasons": 0x1FF,
                }[name]
            return 0

        return call


def test_nvml_uses_cuda_uuid_and_preserves_unsupported_sensors():
    uuid = "GPU-" + str(UUID(int=1))
    lib = NvmlDouble(uuid, power_error=3)
    with NvmlReader(uuid, library=lib) as reader:
        r = reader.read()
        assert r.power_w is None and r.errors == {"power_w": 3}
        assert r.temperature_c == 42 and r.event_reasons == 0x20
        assert (
            reader.device_uuid_digest == signature(uuid.lower())
            and reader.driver_version == "fixture-driver"
        )
        assert uuid not in r.model_dump_json()
    assert lib.calls[-1] == "nvmlShutdown" and not any("ByIndex" in c for c in lib.calls)
    with pytest.raises(RuntimeError, match="closed"):
        reader.read()


def test_nvml_c_abi_types_and_power_units():
    uuid = "GPU-" + str(UUID(int=1))
    lib = NvmlDouble(uuid)
    # Preserve callable instances so argtypes set by the adapter can be inspected.
    lib.nvmlDeviceGetPowerUsage = lib.__getattr__("nvmlDeviceGetPowerUsage")
    with NvmlReader(uuid, library=lib) as reader:
        assert reader.read().power_w == 32.1
    assert lib.nvmlDeviceGetPowerUsage.argtypes == [C.c_void_p, C.POINTER(C.c_uint)]


def test_runtime_mismatches_do_not_leak_raw_uuid():
    with pytest.raises(ValueError, match="physical CUDA GPU UUID"):
        NvmlReader("0")


def test_cuda_uuid_without_nvml_prefix_is_resolved_and_confirmed():
    cuda_uuid = str(UUID(int=1))
    lib = NvmlDouble("GPU-" + cuda_uuid)
    with NvmlReader(cuda_uuid, library=lib) as reader:
        assert reader.device_uuid_digest == signature("GPU-".lower() + cuda_uuid)
    with pytest.raises(ValueError):
        NvmlReader("MIG-" + cuda_uuid, library=lib)


def test_bounded_trace_and_sampling_receipt_fit_one_collector_envelope(service, thermal_sampled):
    backend, _, row = start(service, thermal_sampled)
    e = envelope(thermal_sampled, row, backend)
    window = e["thermal_trace"]["windows"][0]
    e["thermal_trace"]["windows"] = [
        {**window, "sample_ref": "s" + str(i).zfill(3) + "x" * 92} for i in range(32)
    ]
    # A size bound, deliberately not claimed as a valid chronological receipt.
    e["sampling_receipt"]["measured_samples"] = [
        {"ref": w["sample_ref"], "content_digest": signature(i)}
        for i, w in enumerate(e["thermal_trace"]["windows"])
    ]
    assert len(json.dumps(e)) < 65536


def test_thermal_policy_changes_execution_context_signature(service, thermal_sampled):
    _, spec, _ = thermal_sampled
    with service.store.transaction() as conn:
        variant = RuntimeVariant.model_validate(
            service.store.get(conn, "variant", spec.candidates[0].variant_ref)["body"]
        )
    modified = variant.model_copy(
        update={
            "thermal_policy": variant.thermal_policy.model_copy(
                update={"maximum_temperature_c": 75}
            )
        }
    )
    assert context_signature(spec.candidates[0], variant) != context_signature(
        spec.candidates[0], modified
    )


def test_fixed_workload_thermal_trace_ingests_without_sampling_binding(service, thermal_sampled):
    original = thermal_sampled[1]
    identity = original.identity.model_copy(update={"sampling_policy_digest": None})
    with service.store.transaction() as conn:
        variant = RuntimeVariant.model_validate(
            service.store.get(conn, "variant", original.candidates[0].variant_ref)["body"]
        )
    variant = variant.model_copy(
        update={
            "ref": "fixed-thermal-variant",
            "workload_ref": "fixed-thermal",
            "workload_signature": signature(identity),
        }
    )
    spec = original.model_copy(
        update={
            "ref": "fixed-thermal",
            "identity": identity,
            "candidates": (original.candidates[0].model_copy(update={"variant_ref": variant.ref}),),
        }
    )
    service.register("variant", variant, "team-a")
    service.register("workload", spec, "team-a")
    job = service.submit(
        "team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "fixed-thermal-job"
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    e = backend.result(row)
    e["result"]["measurements"].update(
        elapsed_seconds=identity.work_units * 0.1,
        sample_count=identity.work_units,
        work_units=identity.work_units,
    )
    e["digest"] = signature(e["result"])
    e["thermal_trace"] = ThermalTrace(
        job_id=row["id"],
        attempt_id=row["body"]["attempt_id"],
        result_digest=e["digest"],
        policy_digest=signature(variant.thermal_policy),
        driver_version=variant.thermal_policy.driver_version,
        device_uuid_digest=variant.thermal_policy.device_uuid_digest,
        windows=tuple(
            Window(
                sample_ref=f"iteration-{i:04d}",
                before=reading(i * 0.11),
                forward_started=i * 0.11 + 0.002,
                forward_finished=i * 0.11 + 0.102,
                after=reading(i * 0.11 + 0.103),
            )
            for i in range(identity.work_units)
        ),
    ).model_dump(mode="json")
    backend.result = lambda _: e
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    with service.store.transaction() as conn:
        saved = service.store.job(conn, job["job_id"])
        assert saved["state"] == "SUCCEEDED"
        assert saved["body"]["thermal_assessment"]["status"] == "ELIGIBLE_TRACE"
        assert not saved["body"].get("sampling_binding")
    from resource_advisor.thermal import validate_trace

    changed = {**e["thermal_trace"], "windows": list(reversed(e["thermal_trace"]["windows"]))}
    with pytest.raises(ValueError):
        validate_trace(changed, ExecutionResult.model_validate(e["result"]), saved["body"])
