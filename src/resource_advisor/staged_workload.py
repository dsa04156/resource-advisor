"""Bounded CPU workload that stages an exact dataset object, then computes over it.

Staging (copy into the destination cache, or a verified cache read) is timed and
reported as a ``StagingReceipt``; ``elapsed_seconds`` covers only the compute
passes that follow, so staging and compute are separate, non-overlapping
boundaries. Compute is a real SHA-256 pass over the staged bytes per work unit,
with quality = fraction of passes whose digest matches the declared content.
This is CPU file-processing evidence, not accelerator or model evidence.

The dataset is named by the (signed) variant command:
``python -m resource_advisor.staged_workload --source URL --sha256 HEX
--size-bytes N --source-endpoint-ref REF --cache-dir DIR``; RA_DATASET_* env
values are accepted as a fallback for local runs. When the accepted plan carries
cache bindings (RA_STAGING_BINDINGS_JSON), the binding for this node replaces
``--cache-dir`` and the receipt records the binding and the directory identity
the job actually observed, so ingest can confirm or refute the plan's cache.
The receipt also records the Kubernetes Downward API node/Pod identity when the
adapter supplies it; a collector only trusts it after matching the adapter's
own Pod observation.
"""

import argparse
import hashlib
import json
import os
import resource
import statistics
import sys
import time

from .contracts import ExecutionResult, Measurements, signature
from .network_profile import (
    CHUNK,
    StagingReceipt,
    Unavailable,
    execution_identity,
    stage,
    storage_identity,
)


def _arguments(argv, env):
    p = argparse.ArgumentParser(prog="python -m resource_advisor.staged_workload")
    p.add_argument("--source", default=env.get("RA_DATASET_SOURCE"))
    p.add_argument("--sha256", default=env.get("RA_DATASET_SHA256"))
    p.add_argument("--size-bytes", type=int, default=env.get("RA_DATASET_SIZE_BYTES"))
    p.add_argument("--source-endpoint-ref", default=env.get("RA_DATASET_SOURCE_ENDPOINT"))
    p.add_argument("--cache-dir", default=env.get("RA_DATASET_CACHE_DIR"))
    args = p.parse_args(argv)
    if None in (args.source, args.sha256, args.size_bytes, args.source_endpoint_ref):
        p.error("dataset source, sha256, size and source endpoint are required")
    return args


def _emit(write, result, receipt=None):
    envelope = {"result": result.model_dump(mode="json"), "digest": signature(result)}
    if receipt is not None:
        envelope["staging_receipt"] = receipt.model_dump(mode="json")
    write("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope, allow_nan=False))


def run(env=None, *, argv=None, write=print):
    env = os.environ if env is None else env
    args = _arguments([] if argv is None else argv, env)
    digest, size = args.sha256, int(args.size_bytes)
    iterations = int(env["RA_WORK_UNITS"])
    if not 1 <= iterations <= 20:
        raise ValueError("staged workload requires 1–20 work units")
    identity = {
        "job_id": env["RA_JOB_ID"],
        "attempt_id": env["RA_ATTEMPT_ID"],
        "epoch": int(env["RA_EPOCH"]),
        "workload_signature": env["RA_WORKLOAD_SIGNATURE"],
        "context_signature": env["RA_CONTEXT_SIGNATURE"],
    }
    who = execution_identity(env)
    cache_dir, binding_ref = args.cache_dir, None
    if env.get("RA_STAGING_BINDINGS_JSON"):
        binding = json.loads(env["RA_STAGING_BINDINGS_JSON"]).get(who["node_name"])
        if binding is None:
            result = ExecutionResult(
                **identity,
                outcome="FAILED",
                evidence_kind="hardware",
                error_code="STAGING_BINDING_MISSING_FOR_NODE",
            )
            _emit(write, result)
            return result, None
        cache_dir, binding_ref = binding["cache_path"], binding["ref"]
    if not cache_dir:
        raise ValueError("a cache directory or plan cache binding is required")
    try:
        path, sample, state, kind, locator = stage(args.source, digest, size, cache_dir)
    except Unavailable as exc:
        result = ExecutionResult(
            **identity, outcome="FAILED", evidence_kind="hardware", error_code=f"STAGING_{exc}"
        )
        _emit(write, result)
        return result, None
    samples, matches = [], 0
    for _ in range(iterations):
        started = time.perf_counter()
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK):
                hasher.update(chunk)
        samples.append(time.perf_counter() - started)
        matches += hasher.hexdigest() == digest
    elapsed = sum(samples)
    result = ExecutionResult(
        **identity,
        outcome="COMPLETED",
        evidence_kind="hardware",
        measurements=Measurements(
            elapsed_seconds=elapsed,
            peak_memory_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            quality_value=matches / iterations,
            sample_count=iterations,
            work_units=iterations,
            latency_p50_ms=statistics.median(samples) * 1000,
            throughput=iterations / elapsed,
        ),
    )
    receipt = StagingReceipt(
        job_id=identity["job_id"],
        attempt_id=identity["attempt_id"],
        content_digest="sha256:" + digest,
        size_bytes=size,
        source_endpoint_ref=args.source_endpoint_ref,
        source_kind=kind,
        source_locator=locator,
        observer_host=who["observer_host"],
        identity_source=who["identity_source"],
        node_name=who["node_name"],
        pod_name=who.get("pod_name"),
        pod_uid=who.get("pod_uid"),
        cache_state=state,
        cache_locator=str(path.parent),
        cache_storage_identity=storage_identity(path.parent),
        cache_binding_ref=binding_ref,
        sample=sample,
    )
    _emit(write, result, receipt)
    return result, receipt


if __name__ == "__main__":
    run(argv=sys.argv[1:])
