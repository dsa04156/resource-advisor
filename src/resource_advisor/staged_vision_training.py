"""Stage the pinned vision dataset bundle, then run unchanged vision_training on it.

The dataset (one tar of the manifest-pinned ImageNetV2 subset directory) is
copied from its declared source into the destination cache (cold), or an existing
cache entry is fully re-read (hot), with a monotonic clock and SHA-256
verification of the whole object. That span alone is the ``StagingReceipt``
sample. The bundle is then unpacked (timed separately, never data staging) and
``vision_training`` runs with ``--data`` set to the unpacked root, where the
manifest digest and every image hash are verified again. Training clocks start
after all of this, so staging is never inside the measured compute span.

vision_training calls the stage hook on every rank between two process-group
barriers (DDP), so the staging critical path is the slowest member's, observed
rather than assumed across skewed launches. Each rank prints its own receipt
(``RESOURCE_ADVISOR_STAGING_RECEIPT``) and returns it to vision_training, whose
rank 0 binds its own (``staging_receipt``) and every rank's
(``staging_receipts``) receipt into the result envelope. A plan's cache binding
for this node (``RA_STAGING_BINDINGS_JSON``) replaces ``--cache-dir``. Native
Pod/node identity comes from the Downward API values and counts only after a
collector matches them with the adapter's own observation of each rank Pod.

Standard library only, so the image runs it beside vision_training.py with
``python -I``:

    python3 -I staged_vision_training.py --source URL --sha256 HEX --size-bytes N
        --source-endpoint-ref REF [--cache-dir DIR] -- VISION_TRAINING_ARGS (no --data)
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit

try:
    from . import vision_training as vt
except ImportError:  # copied beside vision_training.py into the image, run with python -I
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
    import vision_training as vt

RECEIPT = "RESOURCE_ADVISOR_STAGING_RECEIPT "
REPORT = "RESOURCE_ADVISOR_STAGED_VISION_REPORT "
MAX_BYTES = 2**30  # network_profile.MAX_BYTES
MAX_SECONDS = 120  # network_profile.MAX_SECONDS
MAX_MEMBERS = 10000
CHUNK = 1024 * 1024
UNSTAGED = "/nonexistent/ra-unstaged-dataset"  # replaced by the stage hook before any read


class Unavailable(Exception):
    pass


def storage_identity(path):
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def sanitize(source):
    """Location without userinfo, query or fragment (they may carry secrets)."""
    parts = urlsplit(source)
    if parts.scheme in {"http", "https"}:
        netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
        return "http", urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))
    if parts.scheme in {"", "file"}:
        return "file", str(Path(parts.path if parts.scheme else source).resolve())
    raise Unavailable("SOURCE_SCHEME_UNSUPPORTED")


def _copy(read, sink, hasher, size, deadline):
    received = 0
    while chunk := read(CHUNK):
        received += len(chunk)
        if received > size:
            raise Unavailable("SIZE_MISMATCH")
        if time.perf_counter() > deadline:
            raise Unavailable("TRANSFER_TIMEOUT")
        hasher.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return received


def _request(source):
    """urllib does not take userinfo from the URL: send it as basic auth instead."""
    parts = urlsplit(source)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    request = urllib.request.Request(urlunsplit(parts._replace(netloc=netloc, fragment="")))
    if parts.username is not None:
        token = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        request.add_header("Authorization", "Basic " + base64.b64encode(token).decode())
    return request


def _cold(source, kind, cache, digest, size, timeout):
    """Source → destination cache (fsync), verified, installed only if absent."""
    hasher = hashlib.sha256()
    handle = tempfile.NamedTemporaryFile(dir=cache, prefix=".partial-", delete=False)
    temp = Path(handle.name)
    try:
        started = time.perf_counter()
        deadline, first = started + timeout, None
        with handle:
            if kind == "http":
                request = _request(source)
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        if response.status != 200:
                            raise Unavailable(f"SOURCE_HTTP_{response.status}")
                        first = time.perf_counter() - started
                        received = _copy(response.read, handle, hasher, size, deadline)
                except urllib.error.HTTPError as exc:
                    raise Unavailable(f"SOURCE_HTTP_{exc.code}") from exc
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    raise Unavailable("SOURCE_UNREACHABLE") from exc
            else:
                try:
                    with open(source, "rb") as file:
                        received = _copy(file.read, handle, hasher, size, deadline)
                except FileNotFoundError as exc:
                    raise Unavailable("SOURCE_UNREACHABLE") from exc
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = time.perf_counter() - started
        if received < size:
            raise Unavailable("TRANSFER_INCOMPLETE")
        if hasher.hexdigest() != digest:
            raise Unavailable("CONTENT_DIGEST_MISMATCH")
        try:
            os.link(temp, cache / digest)  # never replaces an existing (shared) entry
        except FileExistsError:
            pass
        return {"elapsed_seconds": elapsed, "bytes": received, "content_verified": True,
                "first_byte_seconds": first}  # fmt: skip
    finally:
        temp.unlink(missing_ok=True)


def _hot(cache, digest, size, timeout):
    hasher = hashlib.sha256()
    started = time.perf_counter()
    with open(cache / digest, "rb") as file:
        received = _copy(file.read, None, hasher, size + 1, started + timeout)
    elapsed = time.perf_counter() - started
    if received != size or hasher.hexdigest() != digest:
        raise Unavailable("CACHE_CORRUPT")  # reported, never deleted
    return {"elapsed_seconds": elapsed, "bytes": received, "content_verified": True,
            "first_byte_seconds": None}  # fmt: skip


def stage_bundle(source, digest, size, cache_dir, timeout=MAX_SECONDS):
    """(bundle path, sample, cache state, source kind, sanitized locator)."""
    if not 0 < size <= MAX_BYTES:
        raise Unavailable("SIZE_EXCEEDS_LIMIT")
    kind, locator = sanitize(source)
    cache = Path(cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if (cache / digest).is_file():
        return cache / digest, _hot(cache, digest, size, timeout), "hot", kind, locator
    sample = _cold(source if kind == "http" else locator, kind, cache, digest, size, timeout)
    return cache / digest, sample, "cold", kind, locator


def unpack(bundle, target, size):
    """Regular files and directories only, relative and contained, bounded in total."""
    with tarfile.open(bundle, mode="r:") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS or sum(m.size for m in members) > 4 * size + CHUNK:
            raise Unavailable("ARCHIVE_UNSAFE")
        for m in members:
            name = PurePosixPath(m.name)
            if name.is_absolute() or ".." in name.parts or not (m.isfile() or m.isdir()):
                raise Unavailable("ARCHIVE_UNSAFE")
        for m in members:
            path = target.joinpath(*PurePosixPath(m.name).parts)
            if m.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(path, "xb") as dst:
                shutil.copyfileobj(src, dst)
    if (target / "manifest.json").is_file():
        return target
    tops = list(target.iterdir())
    if len(tops) == 1 and (tops[0] / "manifest.json").is_file():
        return tops[0]
    raise Unavailable("ARCHIVE_MANIFEST_MISSING")


def identity(environ):
    host = socket.gethostname()
    if environ.get("RA_POD_UID") and environ.get("RA_NODE_NAME"):
        return {"identity_source": "kubernetes_downward_api", "node_name": environ["RA_NODE_NAME"],
                "pod_name": environ.get("RA_POD_NAME"), "pod_uid": environ["RA_POD_UID"],
                "observer_host": host}  # fmt: skip
    return {"identity_source": "host", "node_name": host, "observer_host": host}


class Failed(Exception):
    """A staging failure; ``code`` becomes the job's error code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def train(train_argv, environ, stage):
    """vision_training runs ``stage`` between two process-group barriers (DDP)."""
    vt.run(vt.parser().parse_args([*train_argv, "--data", UNSTAGED]), environ, stage=stage)


def parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--size-bytes", type=int, required=True)
    p.add_argument("--source-endpoint-ref", required=True)
    p.add_argument("--cache-dir")
    p.add_argument("--timeout-seconds", type=int, default=MAX_SECONDS)
    p.add_argument("train", nargs=argparse.REMAINDER, help="-- vision_training arguments")
    return p


def _print(line):
    print(line, flush=True)


def run(argv, environ=os.environ, *, train=train, write=_print):
    p = parser()
    args = p.parse_args(argv)
    passthrough = args.train[1:] if args.train[:1] == ["--"] else args.train
    if any(a == "--data" or a.startswith("--data=") for a in passthrough):
        p.error("--data is the staged bundle; do not pass it")
    if not 0 < args.timeout_seconds <= MAX_SECONDS:
        p.error(f"--timeout-seconds must be 1..{MAX_SECONDS}")
    if not args.cache_dir and not environ.get("RA_STAGING_BINDINGS_JSON"):
        p.error("a cache directory or plan cache binding is required")
    job, attempt = environ["RA_JOB_ID"], environ["RA_ATTEMPT_ID"]
    digest = args.sha256.lower()

    def stage(train_args):
        """Stage, verify and unpack on this rank; sets ``train_args.data``; returns the receipt.

        Every failure (including a missing plan binding) raises ``Failed`` from
        inside the hook, so vision_training can fail all ranks together.
        """
        who = identity(environ)
        cache_dir, binding_ref = args.cache_dir, None
        if environ.get("RA_STAGING_BINDINGS_JSON"):
            binding = json.loads(environ["RA_STAGING_BINDINGS_JSON"]).get(who["node_name"])
            if binding is None:
                raise Failed("STAGING_BINDING_MISSING_FOR_NODE")
            cache_dir, binding_ref = binding["cache_path"], binding["ref"]
        try:
            bundle, sample, state, kind, locator = stage_bundle(
                args.source, digest, args.size_bytes, cache_dir, args.timeout_seconds
            )
            staged_at = datetime.now(UTC).isoformat()
            started = time.perf_counter()
            target = Path(tempfile.mkdtemp(prefix=f"extract-{attempt}-", dir=bundle.parent))
            root = unpack(bundle, target, args.size_bytes)
            unpacked = time.perf_counter() - started
        except Unavailable as exc:
            raise Failed(f"STAGING_{exc}") from exc
        except (OSError, tarfile.TarError) as exc:
            raise Failed("STAGING_IO_ERROR:" + type(exc).__name__) from exc
        receipt = {
            "schema_version": "v1", "job_id": job, "attempt_id": attempt,
            "content_digest": "sha256:" + digest, "size_bytes": args.size_bytes,
            "source_endpoint_ref": args.source_endpoint_ref, "source_kind": kind,
            "source_locator": locator, "observer_host": who["observer_host"],
            "identity_source": who["identity_source"], "node_name": who["node_name"],
            "pod_name": who.get("pod_name"), "pod_uid": who.get("pod_uid"),
            "cache_state": state, "cache_locator": str(bundle.parent),
            "cache_storage_identity": storage_identity(bundle.parent),
            "cache_binding_ref": binding_ref, "staged_at": staged_at, "sample": sample,
        }  # fmt: skip
        receipt = {k: v for k, v in receipt.items() if v is not None}
        write(RECEIPT + json.dumps(receipt, allow_nan=False))
        write(REPORT + json.dumps({"staging_seconds": sample["elapsed_seconds"],
                                   "unpack_seconds": unpacked, "data_root": str(root)}))  # fmt: skip
        train_args.data = str(root)
        return receipt

    try:
        train(passthrough, environ, stage)
    except Failed as exc:
        if int(environ.get("RANK", "0")) == 0:  # only rank 0 reports the job result
            result = {
                "schema_version": "v1", "job_id": job, "attempt_id": attempt,
                "epoch": int(environ["RA_EPOCH"]),
                "workload_signature": environ["RA_WORKLOAD_SIGNATURE"],
                "context_signature": environ["RA_CONTEXT_SIGNATURE"],
                "outcome": "FAILED", "measured": True, "evidence_kind": "hardware",
                "measurements": None, "error_code": exc.code,
            }  # fmt: skip
            write(vt.RESULT + json.dumps({"result": result, "digest": vt.signature(result)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
