"""Read-only, content-level verification for an independently restored database."""

import hashlib
import json
import re
from contextlib import contextmanager

from sqlalchemy import select

from . import scheduler_lab  # noqa: F401 — register experiment tables in fresh backup CLI processes
from .store import metadata


def connection_fingerprint(conn):
    result = {}
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        records = [
            json.dumps(dict(row), sort_keys=True, separators=(",", ":"), allow_nan=False)
            for row in conn.execute(select(table)).mappings()
        ]
        digest = hashlib.sha256()
        for record in sorted(records):
            encoded = record.encode()
            digest.update(str(len(encoded)).encode() + b":" + encoded)
        result[table.name] = {"rows": len(records), "sha256": digest.hexdigest()}
    return result


def fingerprint(store):
    """No credentials, row bodies or site identities appear in the output."""
    with store.engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            return connection_fingerprint(conn)


@contextmanager
def exported_snapshot(store):
    """Keep the source transaction open until pg_dump has consumed its snapshot."""
    if store.engine.dialect.name != "postgresql":
        raise ValueError("exported snapshots require PostgreSQL")
    with store.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        with conn.begin():
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            snapshot = conn.exec_driver_sql("SELECT pg_export_snapshot()").scalar_one()
            version = conn.exec_driver_sql("SHOW server_version").scalar_one()
            captured_at = conn.exec_driver_sql("SELECT transaction_timestamp()").scalar_one()
            yield {
                "snapshot": snapshot,
                "server_version": version,
                "captured_at": captured_at.isoformat(),
                "tables": connection_fingerprint(conn),
            }


def file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def verify_snapshot(restored, manifest, archive):
    """Verify a trusted saved snapshot manifest, not a newer live source state."""
    tables = manifest.get("tables", {})
    expected_archive = manifest.get("archive", {})
    if (
        manifest.get("schema_version") != "postgres-snapshot-v1"
        or set(tables) != set(metadata.tables)
        or any(
            not isinstance(value, dict)
            or type(value.get("rows")) is not int
            or value["rows"] < 0
            or not isinstance(value.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
            for value in tables.values()
        )
        or type(expected_archive.get("size_bytes")) is not int
        or expected_archive["size_bytes"] <= 0
        or not isinstance(expected_archive.get("sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_archive["sha256"])
    ):
        raise ValueError("invalid complete snapshot manifest")
    actual_archive = file_digest(archive)
    after = fingerprint(restored)
    return {
        "schema_version": "v1",
        "matches": tables == after and expected_archive == actual_archive,
        "archive_matches": expected_archive == actual_archive,
        "source_snapshot": tables,
        "restored": after,
        "mismatched_tables": [name for name in tables if tables[name] != after[name]],
        "scope": "Saved transaction snapshot and archive bytes; external artifacts checked separately",
    }


def compare(source, restored):
    before, after = fingerprint(source), fingerprint(restored)
    return {
        "schema_version": "v1",
        "matches": before == after,
        "source": before,
        "restored": after,
        "mismatched_tables": [name for name in before if before[name] != after[name]],
        "scope": "All registered platform tables, exact canonical contents; writers must be quiesced",
    }
