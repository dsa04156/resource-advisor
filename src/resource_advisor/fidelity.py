"""Preregistered paired calibration. Repetition alone is not multi-fidelity BO."""

import itertools
import random
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from .accounting import interval
from .contracts import TERMINAL, Contract, JobRequest, Ref, now, signature
from .policy import compatibility
from .search import device_unit
from .service import Rejected, required
from .store import jobs, usage

PHASES = ("F1", "F2", "F3")


class CalibrationCell(Contract):
    arm: Ref
    phase: Literal["F1", "F2", "F3"]
    workload_ref: Ref
    candidate_ref: Ref


class FidelityPlan(Contract):
    ref: Ref
    project_ref: Ref
    cells: tuple[CalibrationCell, ...] = Field(min_length=6, max_length=12)
    blocks: int = Field(default=3, ge=3, le=8)
    seed: int = 0
    total_wall_seconds: int = Field(default=900, ge=60, le=3600)
    reserved_device_seconds: float = Field(gt=0)
    # A preregistered descriptive tie band, not a confidence/significance test.
    relative_tie_band: float = Field(default=0.05, gt=0, le=0.5)
    sampling_policy: Literal["identical_input_repetition"] = "identical_input_repetition"

    @model_validator(mode="after")
    def complete_cells(self):
        arms = {c.arm for c in self.cells}
        if len({(c.arm, c.phase) for c in self.cells}) != len(self.cells) or any(
            {c.phase for c in self.cells if c.arm == arm} != set(PHASES) for arm in arms
        ):
            raise ValueError("each arm needs exactly one F1, F2 and independent F3 cell")
        return self


class FidelityCalibration:
    def __init__(self, service):
        self.service, self.store = service, service.store

    @staticmethod
    def key(ref, slot):
        return f"fidelity:{ref}:{slot}"

    @staticmethod
    def matches(job, cell, plan):
        return bool(
            job
            and signature(job["body"]["spec"]) == cell["workload_digest"]
            and job["body"]["candidate"]["ref"] == cell["candidate_ref"]
            and datetime.fromisoformat(job["body"]["created_at"])
            >= datetime.fromisoformat(plan["registered_at"])
        )

    def create(self, project, plan):
        if plan.project_ref != project:
            raise Rejected("plan project differs from principal")
        with self.store.transaction() as conn:
            old = self.store.get(conn, "fidelity_plan", plan.ref)
            if old:
                # Reuse the immutable schedule, never reset its registration time.
                if old["project"] != project or old["body"]["plan"] != plan.model_dump(mode="json"):
                    raise Rejected("immutable calibration plan already exists")
                return old["body"]
            group, contexts, units = None, {}, {}
            reservations = 0.0
            cells = []
            for cell in plan.cells:
                spec, candidate, variant, cap = self.service.bundle(
                    conn, project, cell.workload_ref, cell.candidate_ref
                )
                errors = compatibility(spec, candidate, variant, cap)
                if errors:
                    raise Rejected("calibration compatibility: " + ",".join(errors))
                if not spec.profiling.consent or cell.candidate_ref != spec.baseline_candidate_ref:
                    raise Rejected(
                        "each calibration cell needs explicit consent and its own baseline"
                    )
                if spec.identity.task_type not in {"inference", "benchmark"}:
                    raise Rejected(
                        "training fidelity needs separate checkpoint/quality qualification"
                    )
                if variant.device_class not in {"gpu", "npu"}:
                    raise Rejected("calibration requires a qualified accelerator")
                identity = spec.identity.model_dump(mode="json", exclude={"work_units"})
                context = candidate.context.model_dump(mode="json")
                shared_context = candidate.context.model_dump(mode="json")
                shared_context["resources"].pop("host_cpu")
                invariant = {
                    "identity": identity,
                    "quality": spec.quality.model_dump(mode="json"),
                    "context": shared_context,
                    "image": variant.image,
                    "command": variant.command,
                    "node": cap.node_ref,
                    "cluster": cap.backend_cluster_id,
                    "backend": candidate.backend,
                    "compiled_artifact": variant.compiled_artifact_digest,
                }
                current = signature(invariant)
                if variant.thermal_policy:
                    invariant["thermal_policy"] = variant.thermal_policy.model_dump(mode="json")
                    current = signature(invariant)
                if group is not None and group != current:
                    raise Rejected("paired cells differ beyond repetition count and CPU allocation")
                group = current
                if cell.arm in contexts and contexts[cell.arm] != context:
                    raise Rejected("an arm must keep the same configuration across phases")
                contexts[cell.arm] = context
                units.setdefault(cell.arm, {})[cell.phase] = spec.identity.work_units
                count = candidate.context.resources.accelerator_count
                if count < 1:
                    raise Rejected("each calibration cell must reserve an accelerator")
                reservations += count * spec.execution.max_run_seconds * plan.blocks
                cells.append(
                    dict(
                        cell.model_dump(),
                        work_units=spec.identity.work_units,
                        workload_digest=signature(spec),
                        device_unit=device_unit(candidate, variant),
                    )
                )
            if any(not v["F1"] < v["F2"] == v["F3"] for v in units.values()):
                raise Rejected("F1 must be shorter; F3 independently repeats the F2 work budget")
            if len({tuple(v[p] for p in PHASES) for v in units.values()}) != 1:
                raise Rejected("all arms require equal phase work budgets")
            if reservations > plan.reserved_device_seconds:
                raise Rejected("device reservation budget cannot cover all planned jobs")
            if len({c["resources"]["host_cpu"] for c in contexts.values()}) != len(contexts):
                raise Rejected("CPU allocation must distinguish the registered arms")
            rng, schedule = random.Random(plan.seed), []
            for block in range(plan.blocks):
                ordered = sorted(cells, key=lambda c: (c["arm"], c["phase"]))
                rng.shuffle(ordered)
                offset = len(schedule)
                schedule.extend(
                    dict(c, slot=offset + i, block=block) for i, c in enumerate(ordered)
                )
            body = {
                "plan": plan.model_dump(mode="json"),
                "registered_at": now().isoformat(),
                "group_signature": group,
                "schedule": schedule,
                "planned_device_seconds": reservations,
                "design": "randomized complete blocks; independent Job per cell",
                "method": "paired_replication_calibration",
            }
            return self.store.put(conn, "fidelity_plan", plan.ref, project, body)["body"]

    def get(self, project, ref):
        with self.store.transaction() as conn:
            return required(self.store, conn, "fidelity_plan", ref, project)

    def submit_slot(self, project, ref, slot):
        with self.store.transaction() as conn:
            plan = required(self.store, conn, "fidelity_plan", ref, project)
            if not 0 <= slot < len(plan["schedule"]):
                raise Rejected("slot outside registered schedule")
            existing = (
                conn.execute(
                    select(jobs).where(
                        jobs.c.project == project, jobs.c.idempotency_key == self.key(ref, slot)
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if not self.matches(existing, plan["schedule"][slot], plan):
                    raise Rejected("slot idempotency key belongs to a different execution")
                return self.service.public_job(existing)
            if (now() - datetime.fromisoformat(plan["registered_at"])).total_seconds() >= plan[
                "plan"
            ]["total_wall_seconds"]:
                raise Rejected("calibration wall budget exhausted")
            if slot:
                previous = (
                    conn.execute(
                        select(jobs).where(
                            jobs.c.project == project,
                            jobs.c.idempotency_key == self.key(ref, slot - 1),
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not self.matches(previous, plan["schedule"][slot - 1], plan)
                    or previous["state"] not in TERMINAL
                ):
                    raise Rejected("previous registered slot must finish first")
            cell = plan["schedule"][slot]
        # Normal API transaction/outbox and scheduler admission; no direct node binding.
        return self.service.submit(
            project,
            JobRequest(
                workload_ref=cell["workload_ref"],
                candidate_ref=cell["candidate_ref"],
                mode="observe",
            ),
            self.key(ref, slot),
        )

    def assess(self, project, ref):
        with self.store.transaction() as conn:
            plan = required(self.store, conn, "fidelity_plan", ref, project)
            rows, reasons = [], set()
            for cell in plan["schedule"]:
                job = (
                    conn.execute(
                        select(jobs).where(
                            jobs.c.project == project,
                            jobs.c.idempotency_key == self.key(ref, cell["slot"]),
                        )
                    )
                    .mappings()
                    .first()
                )
                record = self.store.get(conn, "result", job["body"]["attempt_id"]) if job else None
                contract_matches = self.matches(job, cell, plan)
                if (
                    job
                    and rows
                    and (
                        not rows[-1]["finished_at"]
                        or datetime.fromisoformat(job["body"]["created_at"])
                        < datetime.fromisoformat(rows[-1]["finished_at"])
                    )
                ):
                    reasons.add("REGISTERED_ORDER_NOT_FOLLOWED")
                    contract_matches = False
                if job and not contract_matches:
                    reasons.add("SLOT_EXECUTION_CONTRACT_MISMATCH")
                valid = bool(
                    job
                    and contract_matches
                    and job["state"] == "SUCCEEDED"
                    and job["body"].get("quality_passed")
                    and record
                    and record["body"]["evidence_kind"] == "hardware"
                )
                if not valid:
                    reasons.add("MISSING_OR_UNQUALIFIED_CELL")
                m = record["body"]["measurements"] if valid else None
                ledger = (
                    conn.execute(
                        select(usage).where(usage.c.attempt_id == job["body"]["attempt_id"])
                    )
                    .mappings()
                    .first()
                    if job
                    else None
                )
                if ledger and ledger["body"].get("schema_version") != "v2":
                    ledger = None
                rows.append(
                    dict(
                        cell,
                        job_id=job["id"] if job else None,
                        attempt_id=job["body"]["attempt_id"] if job else None,
                        state=job["state"] if job else "NOT_SUBMITTED",
                        result_digest=job["body"].get("result_digest") if job else None,
                        created_at=job["body"]["created_at"] if job else None,
                        finished_at=job["body"].get("finished_at") if job else None,
                        seconds_per_work_unit=m["elapsed_seconds"] / m["work_units"] if m else None,
                        temperature_celsius=m["temperature_celsius"] if m else None,
                        allocated_device_seconds=ledger["allocated_device_seconds"]
                        if ledger
                        else None,
                        queue_seconds=ledger["queue_seconds"] if ledger else None,
                        preparation_seconds=ledger["body"].get("preparation_seconds")
                        if ledger
                        else None,
                        collection_seconds=ledger["body"].get("collection_seconds")
                        if ledger
                        else None,
                        cost_uncertainty=ledger["body"].get("uncertainty")
                        if ledger
                        else ["NO_TERMINAL_LEDGER"],
                        end_to_end_seconds=interval(
                            job["body"].get("created_at"), job["body"].get("finished_at")
                        )
                        if job
                        else None,
                    )
                )
            pairs = []
            for block in range(plan["plan"]["blocks"]):
                by_phase = {
                    p: {
                        r["arm"]: r["seconds_per_work_unit"]
                        for r in rows
                        if r["block"] == block and r["phase"] == p
                    }
                    for p in PHASES
                }
                for a, b in itertools.combinations(sorted(by_phase["F1"]), 2):
                    signs = {}
                    for phase in PHASES:
                        x, y = by_phase[phase][a], by_phase[phase][b]
                        signs[phase] = (
                            None
                            if x is None or y is None
                            else (
                                0
                                if abs(x - y) / max(x, y) <= plan["plan"]["relative_tie_band"]
                                else (1 if x > y else -1)
                            )
                        )
                    known = {v for v in signs.values() if v is not None and v != 0}
                    reversal = len(known) > 1
                    if reversal:
                        reasons.add("RANK_REVERSAL")
                    if 0 in signs.values():
                        reasons.add("RANK_UNRESOLVED_WITHIN_TIE_BAND")
                    pairs.append(
                        {
                            "block": block,
                            "arms": [a, b],
                            "phase_order_sign": signs,
                            "rank_reversal": reversal,
                        }
                    )
            if any(r["temperature_celsius"] is None for r in rows):
                reasons.add("THERMAL_BEHAVIOR_UNVERIFIED")
            # This experiment varies repetitions only. It cannot establish a
            # fidelity-bias model or authorize MF-KG, regardless of rank agreement.
            reasons.add("REPLICATION_ONLY_NOT_MULTI_FIDELITY")
            costs = []
            for unit in sorted({r["device_unit"] for r in rows}):
                completed = [r for r in rows if r["device_unit"] == unit and r["state"] in TERMINAL]
                known = [
                    r["allocated_device_seconds"]
                    for r in completed
                    if r["allocated_device_seconds"] is not None
                ]
                costs.append(
                    {
                        "device_unit": unit,
                        "completed_attempts": len(completed),
                        "known_allocated_device_seconds": sum(known),
                        "unknown_allocation_attempts": len(completed) - len(known),
                    }
                )
            assessment = {
                "plan_ref": ref,
                "plan_digest": signature(plan),
                "rows": rows,
                "rank_checks": pairs,
                "reasons": sorted(reasons),
                "complete": all(r["state"] in TERMINAL for r in rows),
                "multi_fidelity_eligible": False,
                "early_pruning_allowed": False,
                "method": "paired_replication_calibration; descriptive ranks only",
                "independent_unit": "one separate Job, not inner benchmark samples",
                "costs": costs,
                "cost_semantics": "scheduler reservations and control-plane intervals; model fitting, transfer and energy not measured here",
            }
            if assessment["complete"]:
                assessment["ref"] = "fidelity-" + signature(assessment)[7:39]
                self.store.put(conn, "fidelity_assessment", assessment["ref"], project, assessment)
            return assessment
