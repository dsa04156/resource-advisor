"""Restore verification must detect corruption even with unchanged row counts."""

import json

from sqlalchemy import insert, update

from resource_advisor.backup import compare, fingerprint
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
