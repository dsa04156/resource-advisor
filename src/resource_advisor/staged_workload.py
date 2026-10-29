"""Bounded CPU workload that stages an exact dataset object, then computes over it.

Staging (copy into the destination cache, or a verified cache read) is timed and
reported as a ``StagingReceipt``; ``elapsed_seconds`` covers only the compute
passes that follow, so staging and compute are separate, non-overlapping
boundaries. Compute is a real SHA-256 pass over the staged bytes per work unit,
with quality = fraction of passes whose digest matches the declared content.
This is CPU file-processing evidence, not accelerator or model evidence.

Environment: the usual RA_* job identity plus RA_DATASET_SOURCE, RA_DATASET_SHA256,
RA_DATASET_SIZE_BYTES, RA_DATASET_CACHE_DIR and RA_DATASET_SOURCE_ENDPOINT.
"""

import hashlib
import json
import os
import resource
import socket
import statistics
import time

from .contracts import ExecutionResult, Measurements, signature
from .network_profile import CHUNK, StagingReceipt, Unavailable, stage


def run(env=None, *, write=print):
    env = env or os.environ
    digest, size = env["RA_DATASET_SHA256"], int(env["RA_DATASET_SIZE_BYTES"])
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
    try:
        path, sample, state, _, locator = stage(
            env["RA_DATASET_SOURCE"], digest, size, env["RA_DATASET_CACHE_DIR"]
        )
    except Unavailable as exc:
        result = ExecutionResult(
            **identity, outcome="FAILED", evidence_kind="hardware", error_code=f"STAGING_{exc}"
        )
        write(
            "RESOURCE_ADVISOR_RESULT "
            + json.dumps({"result": result.model_dump(mode="json"), "digest": signature(result)})
        )
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
        source_endpoint_ref=env["RA_DATASET_SOURCE_ENDPOINT"],
        source_locator=locator,
        observer_host=socket.gethostname(),
        cache_state=state,
        cache_locator=str(path.parent),
        sample=sample,
    )
    envelope = {
        "result": result.model_dump(mode="json"),
        "digest": signature(result),
        "staging_receipt": receipt.model_dump(mode="json"),
    }
    write("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope, allow_nan=False))
    return result, receipt


if __name__ == "__main__":
    run()
