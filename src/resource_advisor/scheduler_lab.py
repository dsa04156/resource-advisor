"""Bounded native scheduling experiments. No shell or hardware selector in user input."""

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, Column, Integer, String, Table, select, update

from .contracts import now
from .service import NotFound, Rejected
from .store import Conflict, metadata

runs = Table(
    "ra_scheduler_labs",
    metadata,
    Column("ref", String(96), primary_key=True),
    Column("project", String(96), nullable=False),
    Column("scenario", String(32), nullable=False),
    Column("state", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("body", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)
agent = Table(
    "ra_scheduler_lab_agent",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("seen_at", String(40), nullable=False),
    Column("body", JSON, nullable=False),
)
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}


class LabRequest(BaseModel):
    scenario: Literal["backfill", "gang", "topology", "multi_gpu"]
    gpu_count: int | None = Field(default=None, ge=1, le=8, strict=True)

    @model_validator(mode="after")
    def valid_count(self):
        if self.scenario == "multi_gpu" and self.gpu_count is None:
            raise ValueError("multi_gpu requires gpu_count")
        if self.scenario != "multi_gpu" and self.gpu_count is not None:
            raise ValueError("gpu_count is only configurable for multi_gpu")
        return self


class LabReport(BaseModel):
    state: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELED"]
    snapshot: dict = Field(default_factory=dict)
    event: dict | None = None


class SchedulerLab:
    def __init__(self, store):
        self.store = store

    def listing(self, project):
        with self.store.transaction() as conn:
            a = conn.execute(select(agent)).mappings().first()
            fresh = a and (now() - datetime.fromisoformat(a["seen_at"])).total_seconds() < 40
            items = conn.execute(
                select(runs)
                .where(runs.c.project == project)
                .order_by(runs.c.created_at.desc())
                .limit(12)
            ).mappings()
            return {
                "agent": {
                    "online": bool(fresh),
                    "seen_at": a["seen_at"] if a else None,
                    **(a["body"] if a else {}),
                },
                "items": [dict(r) for r in items],
            }

    def start(self, project, request, key):
        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key must contain 1–128 characters")
        ref = "lab-" + hashlib.sha256(f"{project}:{key}".encode()).hexdigest()[:24]
        with self.store.transaction() as conn:
            # Lock the single runner capability row: one hardware experiment at a time.
            q = select(agent)
            if conn.dialect.name == "postgresql":
                q = q.with_for_update()
            a = conn.execute(q).mappings().first()
            old = conn.execute(select(runs).where(runs.c.ref == ref)).mappings().first()
            if old:
                if (
                    old["scenario"] != request.scenario
                    or old["body"].get("gpu_count") != request.gpu_count
                ):
                    raise Conflict("idempotency key already used for another scenario")
                return dict(old)
            if not a or (now() - datetime.fromisoformat(a["seen_at"])).total_seconds() > 40:
                raise Rejected("실험 실행기 연결을 확인해 주세요")
            if request.scenario not in a["body"].get("scenarios", []):
                raise Rejected("이 환경에 준비되지 않은 실험입니다")
            if request.scenario == "multi_gpu" and request.gpu_count > a["body"].get(
                "multi_gpu", {}
            ).get("max_gpus", 0):
                raise Rejected("요청 GPU 수가 이 PoC에 등록된 물리 GPU 수를 초과합니다")
            if conn.execute(select(runs.c.ref).where(~runs.c.state.in_(TERMINAL))).first():
                raise Conflict("다른 스케줄링 실험이 실행 중입니다. 종료 후 시작해 주세요")
            conn.execute(
                runs.insert().values(
                    ref=ref,
                    project=project,
                    scenario=request.scenario,
                    state="REQUESTED",
                    version=0,
                    body={
                        "events": [],
                        "snapshot": {},
                        **({"gpu_count": request.gpu_count} if request.gpu_count else {}),
                    },
                    created_at=now().isoformat(),
                )
            )
            return dict(conn.execute(select(runs).where(runs.c.ref == ref)).mappings().one())

    def cancel(self, project, ref):
        with self.store.transaction() as conn:
            r = self._get(conn, ref, project)
            if r["state"] not in TERMINAL:
                self._change(conn, r, "CANCEL_REQUESTED", r["body"])
            return {"ref": ref, "cancel_requested": r["state"] not in TERMINAL}

    def heartbeat(self, body):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        with self.store.transaction() as conn:
            ins = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            conn.execute(
                ins(agent)
                .values(id="native-lab", seen_at=now().isoformat(), body=body)
                .on_conflict_do_update(
                    index_elements=[agent.c.id], set_={"seen_at": now().isoformat(), "body": body}
                )
            )
            return {
                "items": [
                    dict(r)
                    for r in conn.execute(
                        select(runs).where(~runs.c.state.in_(TERMINAL))
                    ).mappings()
                ]
            }

    def report(self, ref, report):
        with self.store.transaction() as conn:
            r = self._get(conn, ref)
            if r["state"] in TERMINAL:
                return dict(r)
            body = {**r["body"], "snapshot": report.snapshot, "observed_at": now().isoformat()}
            if report.event:
                body["events"] = (
                    body.get("events", [])
                    + [
                        {
                            **report.event,
                            "observed_at": now().isoformat(),
                            "snapshot": report.snapshot,
                        }
                    ]
                )[-100:]
            state = (
                r["state"]
                if r["state"] == "CANCEL_REQUESTED" and report.state == "RUNNING"
                else report.state
            )
            self._change(conn, r, state, body)
            return {"ref": ref, "state": state}

    def _get(self, conn, ref, project=None):
        q = select(runs).where(runs.c.ref == ref)
        if conn.dialect.name == "postgresql":
            q = q.with_for_update()
        r = conn.execute(q).mappings().first()
        if not r or (project is not None and r["project"] != project):
            raise NotFound("scheduler experiment not found")
        return r

    def _change(self, conn, row, state, body):
        n = conn.execute(
            update(runs)
            .where(runs.c.ref == row["ref"], runs.c.version == row["version"])
            .values(state=state, body=body, version=row["version"] + 1)
        )
        if n.rowcount != 1:
            raise Conflict("concurrent experiment update; retry")
