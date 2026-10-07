"""Independent SQL store. PostgreSQL in deployment; SQLite for local tests/demo."""

from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    false,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

from .contracts import TERMINAL, State, now, signature

metadata = MetaData()
entities = Table(
    "ra_entities",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("ref", String(96), primary_key=True),
    Column("project", String(96), nullable=False),
    Column("digest", String(80), nullable=False),
    Column("body", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)
jobs = Table(
    "ra_jobs",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("project", String(96), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_digest", String(80)),
    Column("state", String(32), nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("body", JSON, nullable=False),
    UniqueConstraint("project", "idempotency_key"),
)
outbox = Table(
    "ra_outbox",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("lease_until", String(40)),
    Column("lease_token", String(96)),
    Column("tries", Integer, nullable=False),
    Column("body", JSON, nullable=False),
    Column("last_error", String(256)),
)
usage = Table(
    "ra_usage",
    metadata,
    Column("attempt_id", String(96), primary_key=True),
    Column("project", String(96)),
    Column("backend", String(32)),
    Column("device_class", String(32)),
    Column("allocation_mode", String(32)),
    Column("allocated_device_seconds", Float),
    Column("measured_compute_seconds", Float),
    Column("queue_seconds", Float),
    Column("body", JSON, nullable=False),
)
studies = Table(
    "ra_studies",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("project", String(96), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_digest", String(80), nullable=False),
    Column("version", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("body", JSON, nullable=False),
    UniqueConstraint("project", "idempotency_key"),
)


class Conflict(ValueError):
    pass


def job_route_filter(routes):
    """Match complete project/cluster pairs, never their Cartesian product."""
    return or_(
        false(),
        *(
            (jobs.c.project == project) & (jobs.c.body["backend_cluster_id"].as_string() == cluster)
            for project, cluster in routes
        ),
    )


class Store:
    def __init__(self, url: str):
        options = {}
        if url == "sqlite://":
            options = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        elif url.startswith("sqlite:"):
            options = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        self.engine = create_engine(url, **options)

    def initialize(self):
        from . import scheduler_lab  # noqa: F401

        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            yield conn

    def put(self, conn, kind, ref, project, body):
        existing = self.get(conn, kind, ref)
        digest = signature(body)
        if existing:
            if existing["digest"] != digest or existing["project"] != project:
                raise Conflict("immutable reference already exists with different contents")
            return existing
        # Two result collectors can both observe absence before either commits.
        # Let the unique key arbitrate without aborting the transaction, then
        # check ownership/content. Never overwrite immutable evidence.
        make_insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        conn.execute(
            make_insert(entities)
            .values(
                kind=kind,
                ref=ref,
                project=project,
                digest=digest,
                body=body,
                created_at=now().isoformat(),
            )
            .on_conflict_do_nothing(index_elements=[entities.c.kind, entities.c.ref])
        )
        existing = self.get(conn, kind, ref)
        if existing["digest"] != digest or existing["project"] != project:
            raise Conflict("immutable reference already exists with different contents")
        return existing

    def get(self, conn, kind, ref):
        return (
            conn.execute(select(entities).where(entities.c.kind == kind, entities.c.ref == ref))
            .mappings()
            .first()
        )

    def list(self, conn, kind, project=None):
        query = select(entities).where(entities.c.kind == kind)
        if project is not None:
            query = query.where(entities.c.project == project)
        return list(conn.execute(query).mappings())

    def job(self, conn, job_id):
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()

    def change_job(self, conn, row, state, body):
        # Persist observed transitions, not browser polling samples. No invented
        # admission timestamp: a QUEUED observation may include admission/startup.
        body = dict(body)
        events = list(body.get("lifecycle_events", []))
        event = {"state": str(state), "reason": body.get("scheduler_reason")}
        if not events or any(events[-1].get(k) != v for k, v in event.items()):
            events.append({**event, "observed_at": now().isoformat()})
        body["lifecycle_events"] = events[-64:]
        if body.get("termination"):
            self.put(conn, "termination", body["attempt_id"], row["project"], body["termination"])
        if state == State.CANCEL_REQUESTED and row["state"] != State.CANCEL_REQUESTED:
            body = dict(body)
            body.setdefault("cancel_requested_at", now().isoformat())
        if state in TERMINAL:
            body = dict(body)
            body.setdefault("finished_at", now().isoformat())
        result = conn.execute(
            update(jobs)
            .where(jobs.c.id == row["id"], jobs.c.version == row["version"])
            .values(state=str(state), body=body, version=row["version"] + 1)
        )
        if result.rowcount != 1:
            raise Conflict("concurrent job transition; retry")
        if state in TERMINAL:
            self.record_usage(conn, row, state, body)
            from .right_sizing import record_feedback

            record_feedback(self, conn, row, state, body)
            self.enqueue_tracking(conn, row, body)
            if body.get("termination", {}).get("retention_finalizer"):
                self.enqueue(
                    conn,
                    "release-" + body["attempt_id"],
                    "release_termination",
                    {"job_id": row["id"]},
                )

    def enqueue_tracking(self, conn, row, body):
        event_id = "mlflow-" + body["attempt_id"]
        exists = conn.execute(select(outbox.c.id).where(outbox.c.id == event_id)).first()
        self.enqueue(conn, event_id, "mlflow", {"job_id": row["id"]})
        return not bool(exists)

    def backfill_tracking(self):
        """Queue missing terminal-attempt metadata, without inventing execution results."""
        with self.transaction() as conn:
            query = select(jobs).where(jobs.c.state.in_(list(TERMINAL)))
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update()
            return sum(
                self.enqueue_tracking(conn, row, row["body"])
                for row in conn.execute(query).mappings()
            )

    def record_usage(self, conn, row, state, body):
        from .accounting import ledger_record

        attempt = body["attempt_id"]
        if conn.execute(select(usage.c.attempt_id).where(usage.c.attempt_id == attempt)).first():
            return False
        result = self.get(conn, "result", attempt)
        conn.execute(
            insert(usage).values(
                **ledger_record(row, state, body, result["body"] if result else None)
            )
        )
        return True

    def backfill_usage(self):
        """Record missing terminal attempts only; never rewrite historical evidence."""
        with self.transaction() as conn:
            query = select(jobs).where(jobs.c.state.in_(list(TERMINAL)))
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update()
            return sum(
                self.record_usage(conn, row, row["state"], row["body"])
                for row in conn.execute(query).mappings()
            )

    def study(self, conn, study_id):
        return conn.execute(select(studies).where(studies.c.id == study_id)).mappings().first()

    def change_study(self, conn, row, state, body):
        result = conn.execute(
            update(studies)
            .where(studies.c.id == row["id"], studies.c.version == row["version"])
            .values(state=state, body=body, version=row["version"] + 1)
        )
        if result.rowcount != 1:
            raise Conflict("concurrent study transition; retry")

    def enqueue(self, conn, event_id, kind, body):
        if conn.execute(select(outbox.c.id).where(outbox.c.id == event_id)).first():
            return
        conn.execute(
            insert(outbox).values(id=event_id, kind=kind, status="PENDING", tries=0, body=body)
        )

    def claim(self, kind: str, seconds: int = 120, *, job_routes=None):
        with self.transaction() as conn:
            query = (
                select(outbox)
                .where(
                    outbox.c.kind == kind,
                    (
                        (outbox.c.status == "PENDING")
                        & (
                            (outbox.c.lease_until.is_(None))
                            | (outbox.c.lease_until < now().isoformat())
                        )
                    )
                    | ((outbox.c.status == "LEASED") & (outbox.c.lease_until < now().isoformat())),
                )
                .order_by(outbox.c.id)
            )
            if job_routes is not None:
                # Filter before LIMIT and leasing. An unrelated pending event
                # must not consume retries or starve work owned by this worker.
                query = query.where(
                    select(jobs.c.id)
                    .where(
                        jobs.c.id == outbox.c.body["job_id"].as_string(),
                        job_route_filter(job_routes),
                    )
                    .exists()
                )
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            row = conn.execute(query.limit(1)).mappings().first()
            if not row:
                return None
            token = uuid4().hex
            result = conn.execute(
                update(outbox)
                .where(outbox.c.id == row["id"], outbox.c.tries == row["tries"])
                .values(
                    status="LEASED",
                    lease_token=token,
                    tries=row["tries"] + 1,
                    lease_until=(now() + timedelta(seconds=seconds)).isoformat(),
                )
            )
            return (
                dict(row, lease_token=token, tries=row["tries"] + 1)
                if result.rowcount == 1
                else None
            )

    def finish(self, event, error: str | None = None):
        with self.transaction() as conn:
            conn.execute(
                update(outbox)
                .where(outbox.c.id == event["id"], outbox.c.lease_token == event["lease_token"])
                .values(
                    status="PENDING" if error else "DONE",
                    last_error=error,
                    lease_until=(
                        now() + timedelta(seconds=min(300, 2 ** min(event["tries"], 8)))
                    ).isoformat()
                    if error
                    else None,
                    lease_token=None,
                )
            )
