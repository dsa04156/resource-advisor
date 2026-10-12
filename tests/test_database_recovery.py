"""PostgreSQL fault tests run only against the dedicated ra_test_* test database."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool

from resource_advisor.health import heartbeat_fresh


@pytest.fixture
def postgres_store(database_store):
    if database_store.engine.dialect.name != "postgresql":
        pytest.skip("requires RA_TEST_DATABASE_URL pointing to a dedicated PostgreSQL database")
    return database_store


def terminate_connection(store, pid):
    admin = create_engine(store.engine.url, poolclass=NullPool)
    try:
        with admin.begin() as conn:
            assert conn.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    finally:
        admin.dispose()


def test_store_recovers_stale_pooled_connection_before_next_transaction(postgres_store):
    with postgres_store.transaction() as conn:
        pid = conn.scalar(text("SELECT pg_backend_pid()"))
    terminate_connection(postgres_store, pid)

    with postgres_store.transaction() as conn:
        assert conn.scalar(text("SELECT 42")) == 42


@pytest.mark.parametrize("action", ["worker", "collect-inventory"])
@pytest.mark.parametrize("once", [False, True])
def test_cli_recovers_mid_cycle_disconnect_and_only_then_becomes_ready(
    postgres_store, tmp_path, action, once
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"routes": [], "coordinate_studies": False}
            if action == "worker"
            else {"project_ref": "test", "cluster_ref": "lab", "node_refs": ["n"]}
        )
    )
    # Replace only the external Kubernetes executable, not the collector/worker.
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\nprint('{{\"items\": []}}')\n")
    kubectl.chmod(0o700)
    heartbeat = tmp_path / "heartbeat"
    fired = tmp_path / "fault-fired"
    script = """
import os, signal, sys, threading, time
from pathlib import Path
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from resource_advisor import cli
from resource_advisor.health import heartbeat_fresh

heartbeat, fired = Path(sys.argv[1]), Path(sys.argv[2])
def disconnect(conn, cursor, statement, parameters, context, executemany):
    if fired.exists() or not any(t in statement for t in ('ra_jobs', 'ra_entities')):
        return
    fired.write_text('in-flight connection terminated')
    pid = conn.connection.driver_connection.info.backend_pid
    admin = create_engine(os.environ['RA_DATABASE_URL'], poolclass=NullPool)
    try:
        with admin.begin() as other:
            other.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': pid})
    finally:
        admin.dispose()
event.listen(Engine, 'before_cursor_execute', disconnect)
def stop_after_recovery():
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if fired.exists() and heartbeat_fresh(heartbeat):
            os.kill(os.getpid(), signal.SIGTERM)
            return
        time.sleep(0.02)
    os._exit(99)
threading.Thread(target=stop_after_recovery, daemon=True).start()
sys.argv = ['resource-advisor', *sys.argv[3:]]
cli.main()
"""
    args = [action, "--config", str(config), "--heartbeat-path", str(heartbeat)]
    if action == "collect-inventory":
        args += ["--interval-seconds", "5"]
    if once:
        args += ["--once"]
    result = subprocess.run(
        [sys.executable, "-c", script, str(heartbeat), str(fired), *args],
        env={
            **os.environ,
            "RA_DATABASE_URL": postgres_store.engine.url.render_as_string(hide_password=False),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert fired.exists(), "the PostgreSQL fault must actually be injected"
    if once:
        assert result.returncode == 1, "one-shot failure must not be reported as success"
        assert not heartbeat_fresh(heartbeat)
        return
    assert result.returncode == 0, result.stderr
    assert heartbeat_fresh(heartbeat), "readiness requires a completed post-outage cycle"
    assert '"status": "database_unavailable"' in result.stdout


def test_disconnect_after_scheduler_acceptance_recovers_same_attempt_without_resubmission(
    service, postgres_store, tmp_path, monkeypatch
):
    from test_worker import SchedulerDouble

    from resource_advisor import store
    from resource_advisor.backends import Observation
    from resource_advisor.contracts import JobRequest, State, now
    from resource_advisor.database_recovery import database_cycle
    from resource_advisor.health import write_heartbeat
    from resource_advisor.worker import Worker

    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "db-outage"
    )
    heartbeat = tmp_path / "heartbeat"
    write_heartbeat(heartbeat)
    fired = False

    def disconnect(conn, cursor, statement, parameters, context, executemany):
        nonlocal fired
        if fired or not backend.submissions:
            return
        fired = True
        terminate_connection(postgres_store, conn.connection.driver_connection.info.backend_pid)

    event.listen(postgres_store.engine, "before_cursor_execute", disconnect)
    try:
        with database_cycle(postgres_store, heartbeat):
            worker.submit_one()
    finally:
        event.remove(postgres_store.engine, "before_cursor_execute", disconnect)
    assert fired
    assert not heartbeat_fresh(heartbeat)
    interrupted = service.get_job("team-a", job["job_id"])
    assert interrupted["state"] == "SUBMITTING"

    # Move only the clock boundary past the existing outbox lease, avoiding a
    # two-minute sleep or direct mutation of persisted job/outbox state.
    resumed_at = now() + timedelta(seconds=121)
    monkeypatch.setattr(store, "now", lambda: resumed_at)
    with database_cycle(postgres_store, heartbeat):
        worker.submit_one()
    recovered = service.get_job("team-a", job["job_id"])
    assert recovered["state"] == "QUEUED"
    assert recovered["external_id"] == "42"
    assert recovered["attempt_id"] == interrupted["attempt_id"]
    assert backend.submissions == 1
    assert heartbeat_fresh(heartbeat)

    backend.observation = Observation(State.COLLECTING)
    with database_cycle(postgres_store, heartbeat):
        worker.reconcile_all()
    completed = service.get_job("team-a", job["job_id"])
    assert completed["state"] == "SUCCEEDED"
    assert completed["attempt_id"] == interrupted["attempt_id"]
    assert (
        service.submit(
            "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "db-outage"
        )["job_id"]
        == job["job_id"]
    )
    assert backend.submissions == 1


def test_unresponsive_postgres_connection_returns_within_connect_deadline(postgres_store):
    import socket
    from threading import Event, Thread

    stopping, accepted = Event(), Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)

        def accept_without_responding():
            while not stopping.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                with conn:
                    accepted.set()
                    stopping.wait(10)

        thread = Thread(target=accept_without_responding)
        thread.start()
        url = (
            postgres_store.engine.url.set(host="127.0.0.1", port=listener.getsockname()[1])
            .difference_update_query(["host", "hostaddr", "port", "service"])
            .update_query_dict({"sslmode": "disable", "gssencmode": "disable"})
        )
        script = """
import os
from sqlalchemy.exc import OperationalError
from resource_advisor.store import Store
try:
    with Store(os.environ['RA_FAULT_DATABASE_URL']).transaction():
        raise AssertionError('the fake endpoint must never establish a DB connection')
except OperationalError:
    print('connection deadline enforced')
"""
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                env={
                    **os.environ,
                    "RA_FAULT_DATABASE_URL": url.render_as_string(hide_password=False),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                },
                capture_output=True,
                text=True,
                timeout=8,
            )
        finally:
            stopping.set()
            thread.join(timeout=2)
        assert result.returncode == 0, result.stderr
        assert accepted.is_set(), "the deadline must exercise the unresponsive TCP endpoint"
        assert result.stdout.strip() == "connection deadline enforced"


def test_fatal_cycle_error_propagates_and_clears_previous_heartbeat(database_store, tmp_path):
    from resource_advisor.database_recovery import database_cycle
    from resource_advisor.health import write_heartbeat

    heartbeat = tmp_path / "heartbeat"
    write_heartbeat(heartbeat)
    with pytest.raises(ValueError, match="invalid cycle"):
        with database_cycle(database_store, heartbeat):
            raise ValueError("invalid cycle")
    assert not heartbeat_fresh(heartbeat)
