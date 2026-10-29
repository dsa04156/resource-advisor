"""Measured dataset access from a source to the destination node that runs the job.

``measure`` runs *on the destination*: it streams the exact dataset object (HTTP(S)
or a local/mounted file) into an explicit cache directory, or reads an already
cached copy, with a monotonic clock and full SHA-256 verification. Only a
complete, verified transfer becomes a ``NetworkProfile``; a partial, capped,
mismatched or unreachable read is reported as UNAVAILABLE with reasons and is
never turned into evidence. Link bandwidth and cache hits are never assumed.

The profile labels the node it was measured on with this host's name, so a
coordinator cannot record its own loopback reads as a remote node's evidence.
Credentials and query tokens in the source are never persisted or printed.
"""

import argparse
import hashlib
import json
import math
import os
import socket
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AwareDatetime, Field, model_serializer, model_validator

from .contracts import Backend, Contract, Digest, Ref, now

MAX_BYTES = 2**30
MAX_SECONDS = 120
MAX_REPEATS = 10
CHUNK = 1024 * 1024
# The total deadline is enforced by a watchdog that closes the response; the
# abort lands within this scheduling/close tolerance after the deadline.
DEADLINE_TOLERANCE_SECONDS = 0.25


def _omit_none(value, keys):
    for key in keys:
        if value.get(key) is None:
            value.pop(key, None)
    return value


def storage_identity(path):
    """Observed identity of a cache directory: device and inode, not a declared name."""
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def execution_identity(env=None, hostname=None):
    """Who is executing: native Kubernetes Pod identity (Downward API) or this host.

    The Downward API values are only a claim until a collector compares them with
    the adapter's own observation of the Pod (UID and node); see
    ``recommendation.network_profile_from_receipts``.
    """
    env = os.environ if env is None else env
    host = hostname or socket.gethostname()
    if env.get("RA_POD_UID") and env.get("RA_NODE_NAME"):
        return {
            "identity_source": "kubernetes_downward_api",
            "node_name": env["RA_NODE_NAME"],
            "pod_name": env.get("RA_POD_NAME"),
            "pod_uid": env["RA_POD_UID"],
            "observer_host": host,
        }
    return {"identity_source": "host", "node_name": host, "observer_host": host}


class CacheBinding(Contract):
    """Operator-declared cache destination of one execution route.

    ``persistent`` storage keeps entries between jobs and carries the observed
    device:inode identity of the directory as seen *by the job*; ``ephemeral``
    storage (container scratch) is empty for every job, so hot evidence never applies.
    """

    ref: Ref
    project_ref: Ref
    backend: Backend
    cluster_ref: Ref
    node_ref: Ref
    cache_path: str = Field(min_length=2, max_length=512, pattern=r"^/")
    storage_mode: Literal["persistent", "ephemeral"]
    storage_identity: str | None = Field(default=None, pattern=r"^\d+:\d+$")
    evidence_ref: Ref

    @model_validator(mode="after")
    def persistent_needs_identity(self):
        if (self.storage_mode == "persistent") != (self.storage_identity is not None):
            raise ValueError("only persistent storage has, and it requires, an observed identity")
        return self


class AccessSample(Contract):
    elapsed_seconds: float = Field(gt=0)
    bytes: int = Field(ge=0)
    content_verified: bool
    first_byte_seconds: float | None = Field(default=None, ge=0)


class NetworkProfile(Contract):
    """Immutable, project-scoped evidence for one dataset object on one node."""

    schema_version: Literal["v1"] = "v1"
    ref: Ref
    project_ref: Ref
    workload_ref: Ref
    dataset_version: str = Field(min_length=1, max_length=256)
    content_digest: Digest
    size_bytes: int = Field(ge=1, le=MAX_BYTES)
    source_endpoint_ref: Ref
    source_kind: Literal["http", "file"]
    source_locator: str = Field(min_length=1, max_length=512)
    backend: Backend
    cluster_ref: Ref
    node_ref: Ref
    observer_host: str = Field(min_length=1, max_length=253)
    cache_state: Literal["cold", "hot"]
    cache_locator: str = Field(min_length=1, max_length=512)
    observed_at: AwareDatetime
    valid_for_seconds: int = Field(ge=1, le=7 * 86400)
    samples: tuple[AccessSample, ...] = Field(min_length=1, max_length=MAX_REPEATS)
    limits: dict[str, int]
    method: str = Field(min_length=1, max_length=256)
    # Optional (omitted from the digest when unset): observed cache directory
    # identity, and native RA job receipts when built by the receipt collector.
    cache_storage_identity: str | None = Field(default=None, pattern=r"^\d+:\d+$")
    identity_source: Literal["observer_host", "ra_job_receipts"] | None = None
    native_job_ids: tuple[Ref, ...] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_signature(self, handler):
        return _omit_none(
            handler(self), ("cache_storage_identity", "identity_source", "native_job_ids")
        )

    @model_validator(mode="after")
    def complete_verified_destination_evidence(self):
        if self.identity_source == "ra_job_receipts":
            # node_ref is the adapter-observed node; observer_host may be a Pod name.
            if not self.native_job_ids or len(self.native_job_ids) != len(self.samples):
                raise ValueError("receipt evidence names one native RA job per sample")
        elif self.native_job_ids is not None:
            raise ValueError("native job ids belong to receipt-derived evidence")
        elif self.node_ref != self.observer_host:
            raise ValueError("network evidence must be measured on its destination node")
        if any(
            s.bytes != self.size_bytes
            or not s.content_verified
            or not math.isfinite(s.elapsed_seconds)
            for s in self.samples
        ):
            raise ValueError("only complete, content-verified transfers are dataset evidence")
        if "@" in self.source_locator or "?" in self.source_locator or "#" in self.source_locator:
            raise ValueError("source locator must not carry credentials or query tokens")
        return self


class StagingReceipt(Contract):
    """One attempt's own dataset staging, timed apart from its measured compute span.

    The workload stages before compute and reports compute without staging, so a
    collector can bound ``data_staging`` separately instead of inside compute.
    """

    schema_version: Literal["v1"] = "v1"
    job_id: Ref
    attempt_id: Ref
    content_digest: Digest
    size_bytes: int = Field(ge=1, le=MAX_BYTES)
    source_endpoint_ref: Ref
    source_locator: str = Field(min_length=1, max_length=512)
    observer_host: str = Field(min_length=1, max_length=253)
    cache_state: Literal["cold", "hot"]
    cache_locator: str = Field(min_length=1, max_length=512)
    sample: AccessSample
    # Optional (omitted when unset). Native identity is checked against the
    # adapter's observed placement; the binding ref is the plan's cache binding.
    source_kind: Literal["http", "file"] | None = None
    identity_source: Literal["host", "kubernetes_downward_api"] | None = None
    node_name: str | None = Field(default=None, max_length=253)
    pod_name: str | None = Field(default=None, max_length=253)
    pod_uid: str | None = Field(default=None, max_length=64)
    cache_storage_identity: str | None = Field(default=None, pattern=r"^\d+:\d+$")
    cache_binding_ref: Ref | None = None
    # When staging finished (before compute); a collector's freshness basis.
    staged_at: AwareDatetime | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_signature(self, handler):
        return _omit_none(
            handler(self),
            (
                "staged_at",
                "source_kind",
                "identity_source",
                "node_name",
                "pod_name",
                "pod_uid",
                "cache_storage_identity",
                "cache_binding_ref",
            ),
        )

    @model_validator(mode="after")
    def complete_verified_staging(self):
        s = self.sample
        if (
            s.bytes != self.size_bytes
            or not s.content_verified
            or not math.isfinite(s.elapsed_seconds)
        ):
            raise ValueError("only complete, content-verified staging is a staging boundary")
        if "@" in self.source_locator or "?" in self.source_locator or "#" in self.source_locator:
            raise ValueError("source locator must not carry credentials or query tokens")
        return self


def stage(source, sha256, size, cache_dir, *, timeout_seconds=MAX_SECONDS, max_bytes=MAX_BYTES):
    """Make the verified dataset available in the cache; returns (path, sample, state).

    A verified cache entry is read (hot); otherwise the source is copied in (cold).
    A corrupt shared entry is not deleted or trusted: staging fails with CACHE_CORRUPT.
    """
    if size > max_bytes or size > MAX_BYTES or not 0 < timeout_seconds <= MAX_SECONDS:
        raise Unavailable("SIZE_EXCEEDS_LIMIT" if size > max_bytes else "LIMITS_INVALID")
    kind, locator = sanitize(source)
    cache = Path(cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if (cache / sha256).is_file():
        return cache / sha256, _hot(cache, sha256, size, timeout_seconds), "hot", kind, locator
    sample = _cold(
        source if kind == "http" else locator, kind, cache, sha256, size, timeout_seconds
    )
    return cache / sha256, sample, "cold", kind, locator


def summary(profile):
    """Observed descriptive spread; no calibrated interval is implied."""
    values = [s.elapsed_seconds for s in profile.samples]
    return {
        "median_seconds": statistics.median(values),
        "min_seconds": min(values),
        "max_seconds": max(values),
        "samples": len(values),
    }


def sanitize(source):
    """Location without userinfo, query or fragment (they may carry secrets)."""
    parts = urlsplit(source)
    if parts.scheme in {"http", "https"}:
        host = parts.hostname or ""
        netloc = host + (f":{parts.port}" if parts.port else "")
        return "http", urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))
    if parts.scheme in {"", "file"}:
        return "file", str(Path(parts.path if parts.scheme else source).resolve())
    raise ValueError("unsupported source scheme")


class Unavailable(Exception):
    pass


def _http(source, sink, hasher, size, deadline, timeout):
    """Stream with a total wall-clock bound.

    Per-read socket timeouts alone let a slow drip run indefinitely, and a
    buffered chunk iterator only checks time after a full chunk. A watchdog
    therefore closes the client at the deadline, and every piece is checked as it
    arrives; the abort lands within ``DEADLINE_TOLERANCE_SECONDS``.
    """
    import threading

    import httpx

    started = time.perf_counter()
    first = None
    received = 0
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    expired = threading.Event()

    def expire():
        expired.set()
        client.close()

    watchdog = threading.Timer(max(0.0, deadline - started), expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        with client.stream("GET", source) as response:
            if response.status_code != 200:
                raise Unavailable(f"SOURCE_HTTP_{response.status_code}")
            for chunk in response.iter_bytes():
                if expired.is_set() or time.perf_counter() > deadline:
                    raise Unavailable("TRANSFER_TIMEOUT")
                if first is None:
                    first = time.perf_counter() - started
                received += len(chunk)
                if received > size:
                    raise Unavailable("SIZE_MISMATCH")
                hasher.update(chunk)
                sink.write(chunk)
        if expired.is_set():
            raise Unavailable("TRANSFER_TIMEOUT")
    except Unavailable:
        raise
    except httpx.TimeoutException as exc:
        raise Unavailable("TRANSFER_TIMEOUT") from exc
    except httpx.ConnectError as exc:
        raise Unavailable("TRANSFER_TIMEOUT" if expired.is_set() else "SOURCE_UNREACHABLE") from exc
    except (httpx.HTTPError, OSError, RuntimeError) as exc:
        # A peer closing early, or the watchdog closing the client: never full evidence.
        raise Unavailable(
            "TRANSFER_TIMEOUT" if expired.is_set() else "TRANSFER_INCOMPLETE"
        ) from exc
    finally:
        watchdog.cancel()
        client.close()
    return received, first


def _file(path, sink, hasher, size, deadline):
    received = 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK):
                received += len(chunk)
                if received > size:
                    raise Unavailable("SIZE_MISMATCH")
                if time.perf_counter() > deadline:
                    raise Unavailable("TRANSFER_TIMEOUT")
                hasher.update(chunk)
                if sink is not None:
                    sink.write(chunk)
    except FileNotFoundError as exc:
        raise Unavailable("SOURCE_UNREACHABLE") from exc
    return received


def _cold(source, kind, cache, digest, size, timeout):
    """Copy source→destination cache; install atomically only if absent."""
    hasher = hashlib.sha256()
    handle = tempfile.NamedTemporaryFile(dir=cache, prefix=".partial-", delete=False)
    temp = Path(handle.name)
    try:
        started = time.perf_counter()
        deadline = started + timeout
        with handle:
            if kind == "http":
                received, first = _http(source, handle, hasher, size, deadline, timeout)
            else:
                received, first = _file(source, handle, hasher, size, deadline), None
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = time.perf_counter() - started
        if received < size:
            raise Unavailable("TRANSFER_INCOMPLETE")
        if hasher.hexdigest() != digest:
            raise Unavailable("CONTENT_DIGEST_MISMATCH")
        try:
            # link() never replaces an existing (shared) cache entry.
            os.link(temp, cache / digest)
        except FileExistsError:
            pass
        return AccessSample(
            elapsed_seconds=elapsed, bytes=received, content_verified=True, first_byte_seconds=first
        )
    finally:
        temp.unlink(missing_ok=True)


def _hot(cache, digest, size, timeout):
    path = cache / digest
    if not path.is_file():
        raise Unavailable("CACHE_MISSING")
    hasher = hashlib.sha256()
    started = time.perf_counter()
    received = _file(path, None, hasher, size, started + timeout)
    elapsed = time.perf_counter() - started
    if received != size or hasher.hexdigest() != digest:
        raise Unavailable("CACHE_CORRUPT")  # reported, never deleted
    return AccessSample(elapsed_seconds=elapsed, bytes=received, content_verified=True)


def measure(args, *, hostname=None):
    """Returns (profile body | None, reasons)."""
    hostname = hostname or socket.gethostname()
    node = args.node_ref or hostname
    reasons = []
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        # Inside a Pod the hostname is the Pod name and nothing native vouches for
        # the node: measure through an RA job and build evidence from its receipts.
        reasons.append("KUBERNETES_POD_REQUIRES_NATIVE_RECEIPT")
    if node != hostname:
        reasons.append("OBSERVER_NOT_TARGET")
    if not (0 < args.max_bytes <= MAX_BYTES and 0 < args.timeout_seconds <= MAX_SECONDS):
        reasons.append("LIMITS_INVALID")
    if not 1 <= args.repeats <= MAX_REPEATS:
        reasons.append("REPEATS_INVALID")
    if args.size_bytes > args.max_bytes:
        reasons.append("SIZE_EXCEEDS_LIMIT")
    digest = args.sha256.lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        reasons.append("DIGEST_INVALID")
    try:
        kind, locator = sanitize(args.source)
    except ValueError:
        kind, locator = None, None
        reasons.append("SOURCE_SCHEME_UNSUPPORTED")
    if reasons:
        return None, reasons
    cache = Path(args.cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    source = args.source if kind == "http" else locator
    samples = []
    try:
        for _ in range(args.repeats):
            samples.append(
                _cold(source, kind, cache, digest, args.size_bytes, args.timeout_seconds)
                if args.mode == "cold"
                else _hot(cache, digest, args.size_bytes, args.timeout_seconds)
            )
    except Unavailable as exc:
        return None, [str(exc)]
    except OSError as exc:
        return None, ["IO_ERROR:" + type(exc).__name__]
    body = NetworkProfile(
        ref=args.ref,
        project_ref=args.project,
        workload_ref=args.workload_ref,
        dataset_version=args.dataset_version,
        content_digest="sha256:" + digest,
        size_bytes=args.size_bytes,
        source_endpoint_ref=args.source_endpoint_ref,
        source_kind=kind,
        source_locator=locator,
        backend=args.backend,
        cluster_ref=args.cluster_ref,
        node_ref=node,
        observer_host=hostname,
        cache_state=args.mode,
        cache_locator=str(cache),
        observed_at=now(),
        valid_for_seconds=args.valid_for_seconds,
        cache_storage_identity=storage_identity(cache),
        samples=tuple(samples),
        limits={"max_bytes": args.max_bytes, "timeout_seconds": args.timeout_seconds},
        method=(
            "single-stream copy into destination cache, fsync, sha256 verified"
            if args.mode == "cold"
            else "read of verified destination cache entry, sha256 verified"
        ),
    )
    return body.model_dump(mode="json"), []


def parser():
    p = argparse.ArgumentParser(prog="python -m resource_advisor.network_profile")
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure", help="measure dataset access on this (destination) host")
    m.add_argument("--source", required=True, help="http(s) URL or local/mounted file path")
    m.add_argument("--sha256", required=True)
    m.add_argument("--size-bytes", type=int, required=True)
    m.add_argument("--cache-dir", required=True)
    m.add_argument("--mode", choices=("cold", "hot"), required=True)
    m.add_argument("--ref", required=True)
    m.add_argument("--project", required=True)
    m.add_argument("--workload-ref", required=True)
    m.add_argument("--dataset-version", required=True)
    m.add_argument("--source-endpoint-ref", required=True)
    m.add_argument("--backend", choices=("kubernetes", "slurm"), required=True)
    m.add_argument("--cluster-ref", required=True)
    m.add_argument("--node-ref", help="must equal this host's name (default)")
    m.add_argument("--repeats", type=int, default=3)
    m.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    m.add_argument("--timeout-seconds", type=int, default=30)
    m.add_argument("--valid-for-seconds", type=int, default=3600)
    m.add_argument("--out")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    body, reasons = measure(args)
    output = body if body else {"status": "UNAVAILABLE", "reasons": reasons}
    text = json.dumps(output, indent=2, sort_keys=True)
    if body and args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if body else 2


if __name__ == "__main__":
    sys.exit(main())
