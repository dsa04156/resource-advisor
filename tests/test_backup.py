"""Restore verification must detect corruption even with unchanged row counts."""

import json

import pytest
from sqlalchemy import insert, update

from resource_advisor.backup import (
    compare,
    exported_snapshot,
    file_digest,
    fingerprint,
    verify_snapshot,
)
from resource_advisor.store import Store, entities, outbox


def test_copy_comparison_detects_changed_json_with_equal_counts(database_store):
    restored = Store("sqlite://")
    restored.initialize()
    try:
        row = dict(
            kind="fixture",
            ref="private-ref",
            project="private-project",
            digest="fixture",
            body={"nested": {"metric": 1, "label": "private-value"}},
            created_at="fixture-time",
        )
        with database_store.transaction() as c:
            c.execute(insert(entities).values(**row))
        # JSON key/insertion ordering must not cause false mismatches.
        row["body"] = {"nested": {"label": "private-value", "metric": 1}}
        with restored.transaction() as c:
            c.execute(insert(entities).values(**row))
        report = compare(database_store, restored)
        assert report["matches"]
        assert len(report["source"]) == 5
        assert "private-" not in json.dumps(report)
        with restored.transaction() as c:
            c.execute(update(entities).values(body={"nested": {"metric": 2}}))
        report = compare(database_store, restored)
        assert not report["matches"]
        assert report["mismatched_tables"] == ["ra_entities"]
        assert report["source"]["ra_entities"]["rows"] == 1
        assert report["restored"]["ra_entities"]["rows"] == 1
        assert fingerprint(database_store) == report["source"]
    finally:
        restored.engine.dispose()


def test_copy_detects_missing_outbox_event(database_store):
    restored = Store("sqlite://")
    restored.initialize()
    try:
        with database_store.transaction() as c:
            c.execute(
                insert(outbox).values(
                    id="event",
                    kind="mlflow",
                    status="PENDING",
                    tries=0,
                    body={"job_id": "job"},
                )
            )
        report = compare(database_store, restored)
        assert not report["matches"]
        assert report["mismatched_tables"] == ["ra_outbox"]
        assert fingerprint(restored)["ra_outbox"]["rows"] == 0
    finally:
        restored.engine.dispose()


def test_snapshot_archive_and_complete_table_manifest_required(database_store, tmp_path):
    archive = tmp_path / "archive"
    archive.write_bytes(b"synthetic archive bytes for integrity tests only")
    manifest = {
        "schema_version": "postgres-snapshot-v1",
        "tables": fingerprint(database_store),
        "archive": file_digest(archive),
    }
    assert verify_snapshot(database_store, manifest, archive)["matches"]
    archive.write_bytes(b"corrupt archive with unchanged restored database")
    report = verify_snapshot(database_store, manifest, archive)
    assert not report["matches"] and not report["archive_matches"]
    assert report["mismatched_tables"] == []
    del manifest["tables"]["ra_jobs"]
    with pytest.raises(ValueError, match="complete snapshot"):
        verify_snapshot(database_store, manifest, archive)


def test_snapshot_verification_detects_equal_count_corruption(database_store, tmp_path):
    with database_store.transaction() as conn:
        conn.execute(
            insert(outbox).values(
                id="e1", kind="artifact", status="DONE", tries=1, body={"job_id": "j1"}
            )
        )
    archive = tmp_path / "archive"
    archive.write_bytes(b"test archive")
    manifest = {
        "schema_version": "postgres-snapshot-v1",
        "tables": fingerprint(database_store),
        "archive": file_digest(archive),
    }
    with database_store.transaction() as conn:
        conn.execute(update(outbox).values(status="PENDING"))
    report = verify_snapshot(database_store, manifest, archive)
    assert not report["matches"] and report["archive_matches"]
    assert report["mismatched_tables"] == ["ra_outbox"]


def test_exported_snapshot_excludes_concurrent_source_write(database_store):
    if database_store.engine.dialect.name != "postgresql":
        with pytest.raises(ValueError, match="PostgreSQL"):
            with exported_snapshot(database_store):
                pytest.fail("SQLite cannot export a PostgreSQL snapshot")
        return
    with exported_snapshot(database_store) as snapshot:
        assert snapshot["tables"]["ra_outbox"]["rows"] == 0
        with database_store.transaction() as writer:
            writer.execute(
                insert(outbox).values(id="after", kind="artifact", status="DONE", tries=0, body={})
            )
        # A second PostgreSQL connection imports exactly what pg_dump consumes.
        with database_store.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as reader:
            with reader.begin():
                from psycopg import sql

                reader.exec_driver_sql("SET TRANSACTION READ ONLY")
                # SET requires a SQL literal rather than a bind parameter.
                command = sql.SQL("SET TRANSACTION SNAPSHOT {}").format(
                    sql.Literal(snapshot["snapshot"])
                )
                reader.connection.driver_connection.execute(command)
                assert reader.exec_driver_sql("SELECT count(*) FROM ra_outbox").scalar_one() == 0
    assert fingerprint(database_store)["ra_outbox"]["rows"] == 1


def test_capture_failure_preserves_partial_archive_and_closes_snapshot(tmp_path, monkeypatch):
    import runpy
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace

    capture = runpy.run_path(str(Path(__file__).parents[1] / "examples/backup_metadata.py"))[
        "capture"
    ]
    closed = []

    @contextmanager
    def snapshot(_):
        try:
            yield {"snapshot": "test-only"}
        finally:
            closed.append(True)

    def failed(command, *, stdout, stderr, timeout):
        assert "--snapshot=test-only" in command
        stdout.write(b"partial private archive")
        stderr.write(b"private failure detail")
        return SimpleNamespace(returncode=1)

    monkeypatch.setitem(capture.__globals__, "exported_snapshot", snapshot)
    monkeypatch.setattr(capture.__globals__["subprocess"], "run", failed)
    store = SimpleNamespace(
        engine=SimpleNamespace(url=SimpleNamespace(username="app", database="app"))
    )
    directory = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="pg_dump failed"):
        capture(store, directory, ["test-transport"])
    assert closed == [True]
    assert not (directory / "manifest.json").exists()
    assert (directory / "metadata.dump").read_bytes() == b"partial private archive"
    assert json.loads((directory / "capture-status.json").read_text())["status"] == "INCOMPLETE"
    with pytest.raises(FileExistsError):
        capture(store, directory, ["test-transport"])
    assert closed == [True], "existing evidence must not be replaced by another capture"
