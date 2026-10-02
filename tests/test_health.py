from resource_advisor.health import heartbeat_fresh, write_heartbeat


def test_heartbeat_requires_recent_completed_cycle(tmp_path):
    path = tmp_path / "heartbeat"
    assert not heartbeat_fresh(path)
    write_heartbeat(path)
    assert heartbeat_fresh(path)
    for value in ("0", "nan", "inf", "corrupt", "1001"):
        path.write_text(value)
        assert not heartbeat_fresh(path, 120, timestamp=1000)
    path.write_text("950")
    assert heartbeat_fresh(path, 120, timestamp=1000)
    assert not heartbeat_fresh(path, 40, timestamp=1000)
    assert not heartbeat_fresh(path, -1)


def test_collector_sigterm_finishes_snapshot_and_exits_without_sleep(tmp_path, monkeypatch):
    import json
    import signal
    import sys

    from resource_advisor import cli, inventory

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"project_ref": "test", "cluster_ref": "lab", "node_refs": ["n"]}))
    heartbeat = tmp_path / "heartbeat"
    snapshot = {"ref": "inv-test", "status": "ok", "nodes": []}
    calls = []

    class Collector:
        def __init__(self, config):
            pass

        def collect(self):
            assert not calls, "a second collection must not start during shutdown"
            return snapshot

    def save(store, project, observed):
        calls.append(observed)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(inventory, "InventoryCollector", Collector)
    monkeypatch.setattr(inventory, "save_inventory", save)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resource-advisor",
            "--database",
            "sqlite://",
            "collect-inventory",
            "--config",
            str(config),
            "--interval-seconds",
            "300",
            "--heartbeat-path",
            str(heartbeat),
        ],
    )
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        cli.main()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    assert calls == [snapshot]
    assert heartbeat_fresh(heartbeat)


def test_worker_finishes_cycle_before_graceful_shutdown(tmp_path, monkeypatch):
    import json
    import signal
    import sys
    from types import SimpleNamespace

    from resource_advisor import cli, study

    config = tmp_path / "worker.json"
    config.write_text(json.dumps({"routes": []}))
    heartbeat = tmp_path / "heartbeat"
    calls = []

    def submit():
        calls.append("submit")
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(
        cli,
        "Worker",
        lambda *_: SimpleNamespace(
            submit_one=submit,
            cancel_one=lambda: calls.append("cancel"),
            reconcile_all=lambda: calls.append("reconcile"),
        ),
    )
    monkeypatch.setattr(
        study,
        "Studies",
        lambda *_: SimpleNamespace(
            tick_all=lambda: calls.append("studies"),
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resource-advisor",
            "--database",
            "sqlite://",
            "worker",
            "--config",
            str(config),
            "--heartbeat-path",
            str(heartbeat),
        ],
    )
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        cli.main()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    assert calls == ["studies", "submit", "cancel", "reconcile"]
    assert heartbeat_fresh(heartbeat)
