"""Scheduling inventory reads: latest snapshot per cluster, without a full window scan."""

from datetime import timedelta

from sqlalchemy import event
from test_inventory import config, source

from resource_advisor.contracts import now
from resource_advisor.inventory import InventoryCollector, save_inventory
from resource_advisor.scheduling import node_observations


def snapshot(cluster, node, age):
    value = InventoryCollector(config(), execute=source()).collect()
    value["cluster_ref"] = cluster
    value["nodes"][0]["node_ref"] = node
    value["collected_at"] = (now() - age).isoformat()
    return value


def test_latest_snapshot_per_cluster_without_a_window_over_all_snapshots(service):
    store = service.store
    for cluster, node, minutes in [
        ("lab", "old-node", 9),
        ("lab", "new-node", 1),
        ("edge", "edge-node", 2),
    ]:
        save_inventory(store, "team-a", snapshot(cluster, node, timedelta(minutes=minutes)))
    save_inventory(store, "team-b", snapshot("lab", "foreign-node", timedelta(minutes=0)))
    statements = []
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(store.engine, "before_cursor_execute", listener)
    try:
        with store.transaction() as conn:
            observed = node_observations(conn, "team-a")
    finally:
        event.remove(store.engine, "before_cursor_execute", listener)
    assert set(observed) == {("kubernetes", "new-node"), ("kubernetes", "edge-node")}
    assert not any("row_number" in s.lower() for s in statements)
