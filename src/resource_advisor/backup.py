"""Read-only, content-level verification for an independently restored database."""

import hashlib
import json

from sqlalchemy import select

from .store import metadata


def fingerprint(store):
    """No credentials, row bodies or site identities appear in the output."""
    result = {}
    with store.engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            for table in sorted(metadata.tables.values(), key=lambda t: t.name):
                records = [
                    json.dumps(dict(row), sort_keys=True, separators=(",", ":"), allow_nan=False)
                    for row in conn.execute(select(table)).mappings()
                ]
                # Canonical ordering also covers append-only tables with compound keys.
                digest = hashlib.sha256()
                for record in sorted(records):
                    encoded = record.encode()
                    digest.update(str(len(encoded)).encode() + b":" + encoded)
                result[table.name] = {"rows": len(records), "sha256": digest.hexdigest()}
    return result


def compare(source, restored):
    before, after = fingerprint(source), fingerprint(restored)
    return {
        "schema_version": "v1",
        "matches": before == after,
        "source": before,
        "restored": after,
        "mismatched_tables": [name for name in before if before[name] != after[name]],
        "scope": "All five platform tables, exact canonical contents; writers must be quiesced",
    }
