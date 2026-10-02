"""Preregistered, single-trial qualification of a bounded sampled fidelity space.

Operational acceptance of observed pairs, not a population-level confidence claim
or proof of sustained thermal stability. No caller-provided performance values.
"""

import math
import statistics
from datetime import datetime, timedelta
from itertools import combinations

from pydantic import Field

from .contracts import Contract, Ref, now, signature
from .fidelity_space import FidelityEvidenceRequest, FidelitySpaces
from .service import Rejected, required


class FidelityQualificationPlan(Contract):
    ref: Ref
    project_ref: Ref
    space_ref: Ref
    blocks: int = Field(default=3, ge=3, le=12)
    tie_fraction: float = Field(default=0.05, gt=0, le=0.2)
    maximum_relative_bias: float = Field(default=0.2, gt=0, le=0.5)
    minimum_target_elapsed_seconds: float = Field(default=0.05, ge=0.001, le=300)
    maximum_age_seconds: int = Field(default=3600, ge=60, le=86400)


def paired_assessment(plan, space, observations):
    """Check every preregistered block; inputs are joined from validated stored jobs."""
    plan = FidelityQualificationPlan.model_validate(plan)
    options = {o["ref"]: o for o in space["options"]}
    cells = {(o["block"], o["option_ref"]): o for o in observations}
    expected = {(b, ref) for b in range(plan.blocks) for ref in options}
    if len(cells) != len(observations) or set(cells) != expected:
        raise Rejected("complete independent preregistered blocks required")
    configs = sorted({o["candidate_ref"] for o in options.values()})
    levels = sorted({o["fidelity"] for o in options.values()})
    by_option = {(o["candidate_ref"], o["fidelity"]): o["ref"] for o in options.values()}
    if len(by_option) != len(configs) * len(levels):
        raise Rejected("qualification requires the same fidelity levels for every configuration")
    reasons, bias, ranks = set(), [], []
    for block in range(plan.blocks):
        for candidate in configs:
            target = cells[block, by_option[candidate, 1.0]]
            if target["elapsed_seconds"] < plan.minimum_target_elapsed_seconds:
                reasons.add("TARGET_MEASUREMENT_TOO_SHORT")
            for level in levels[:-1]:
                ratio = (
                    cells[block, by_option[candidate, level]]["seconds_per_work_unit"]
                    / target["seconds_per_work_unit"]
                )
                bias.append(
                    {
                        "block": block,
                        "candidate_ref": candidate,
                        "fidelity": level,
                        "low_to_target_ratio": ratio,
                    }
                )
                if (
                    not 1 / (1 + plan.maximum_relative_bias)
                    <= ratio
                    <= 1 + plan.maximum_relative_bias
                ):
                    reasons.add("PAIRED_BIAS_EXCEEDS_LIMIT")

    def order(value):
        return (
            -1
            if value < -math.log1p(plan.tie_fraction)
            else (1 if value > math.log1p(plan.tie_fraction) else 0)
        )

    resolved = 0
    for first, second in combinations(configs, 2):
        signs = []
        for block in range(plan.blocks):
            target = math.log(
                cells[block, by_option[first, 1.0]]["seconds_per_work_unit"]
                / cells[block, by_option[second, 1.0]]["seconds_per_work_unit"]
            )
            for level in levels[:-1]:
                low = math.log(
                    cells[block, by_option[first, level]]["seconds_per_work_unit"]
                    / cells[block, by_option[second, level]]["seconds_per_work_unit"]
                )
                low_sign, target_sign = order(low), order(target)
                signs.extend((low_sign, target_sign))
                ranks.append(
                    {
                        "block": block,
                        "candidates": [first, second],
                        "fidelity": level,
                        "low_log_ratio": low,
                        "target_log_ratio": target,
                        "low_order": low_sign,
                        "target_order": target_sign,
                    }
                )
                if low_sign * target_sign == -1:
                    reasons.add("PAIRED_RANK_REVERSAL")
                elif low_sign != target_sign:
                    reasons.add("PAIRED_RANK_UNRESOLVED")
        if len(set(signs)) > 1:
            reasons.add("RANK_UNSTABLE_ACROSS_BLOCKS")
        elif signs[0] != 0:
            resolved += 1
    if resolved == 0:
        reasons.add("NO_RESOLVED_CONFIGURATION_ORDERING")
    return {
        "status": "REJECTED" if reasons else "QUALIFIED",
        "reasons": sorted(reasons),
        "paired_bias": bias,
        "paired_ranks": ranks,
        "resolved_pairs": resolved,
        "experimental_unit": "independent Job within preregistered randomized block",
        "scope": "observed finite pairs and bracketed temperatures only; no calibrated confidence or continuous/sustained thermal guarantee",
        "early_pruning_authorized": False,
    }


class FidelityQualifications:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def create(self, project, request: FidelityQualificationPlan):
        request = FidelityQualificationPlan.model_validate(request.model_dump(mode="json"))
        if project != request.project_ref:
            raise Rejected("qualification project differs from principal")
        with self.store.transaction() as conn:
            old = self.store.get(conn, "fidelity_qualification", request.ref)
            if old:
                if old["project"] != project or old["body"]["request"] != request.model_dump(
                    mode="json"
                ):
                    raise Rejected("immutable qualification plan already exists")
                return old["body"]
            space = required(self.store, conn, "fidelity_space", request.space_ref, project)
            FidelitySpaces(self.service)._check_bindings(conn, project, space, current=True)
            if space["request"]["fidelity_axis"] != "representative_sampling":
                raise Rejected("replication-only spaces cannot qualify for MF-KG")
            if len(space["options"]) * request.blocks > 128:
                raise Rejected("qualification exceeds bounded independent evidence budget")
            for binding in space["bindings"]:
                variant = self.service.bundle(
                    conn, project, binding["workload_ref"], binding["candidate_ref"]
                )[2]
                if variant.thermal_policy is None:
                    raise Rejected("execution-bound thermal policy required for every option")
            body = {
                "request": request.model_dump(mode="json"),
                "space_digest": signature(space),
                "registered_at": now().isoformat(),
                "execution_authorized": False,
            }
            return self.store.put(conn, "fidelity_qualification", request.ref, project, body)[
                "body"
            ]

    def get(self, project, ref):
        with self.store.transaction() as conn:
            return required(self.store, conn, "fidelity_qualification", ref, project)

    def status(self, project, ref):
        with self.store.transaction() as conn:
            plan = required(self.store, conn, "fidelity_qualification", ref, project)
            assessment = required(self.store, conn, "fidelity_assessment", ref, project)
            invalidation = self.store.get(conn, "fidelity_invalidation", ref)
            space = required(
                self.store, conn, "fidelity_space", plan["request"]["space_ref"], project
            )
            reason = None
            try:
                self.checked(conn, project, ref, space)
            except Rejected as exc:
                reason = str(exc)
            return {
                "assessment": assessment,
                "invalidation": invalidation["body"] if invalidation else None,
                "currently_eligible": reason is None,
                "ineligibility_reason": reason,
            }

    def assess(self, project, ref):
        with self.store.transaction() as conn:
            old = self.store.get(conn, "fidelity_assessment", ref)
            if old:
                return required(self.store, conn, "fidelity_assessment", ref, project)
            plan = required(self.store, conn, "fidelity_qualification", ref, project)
            binding = required(self.store, conn, "fidelity_qualification_study", ref, project)
            row = self.store.study(conn, binding["study_ref"])
            if not row or row["project"] != project or row["state"] != "COMPLETED":
                raise Rejected("bound calibration study must be completed")
            study = row["body"]
            if (
                study["request"].get("fidelity_qualification_ref") != ref
                or study["request"]["strategy"] != "fidelity_calibration"
                or datetime.fromisoformat(study["created_at"])
                < datetime.fromisoformat(plan["registered_at"])
            ):
                raise Rejected("qualification must precede the single bound calibration study")
            space = required(
                self.store, conn, "fidelity_space", plan["request"]["space_ref"], project
            )
            if (
                signature(space) != plan["space_digest"]
                or signature(space) != study["fidelity_space_digest"]
            ):
                raise Rejected("qualification space changed")
            probes = [o for o in study["observations"] if o["mode"] == "pilot"]
            maximum_age = min(
                plan["request"]["maximum_age_seconds"],
                *(
                    required(self.store, conn, "workload", b["workload_ref"], project)["quality"][
                        "max_profile_age_seconds"
                    ]
                    for b in space["bindings"]
                ),
            )
            cells = []
            expected_schedule = study["fidelity_schedule"]
            if len(probes) != len(expected_schedule):
                raise Rejected("incomplete qualification schedule")
            for obs, slot in zip(probes, expected_schedule, strict=True):
                saved = required(self.store, conn, "probe_plan", obs["plan_ref"], project)
                job = self.store.job(conn, obs["job_id"])
                if (
                    saved["study_ref"] != row["id"]
                    or saved["option_ref"] != slot["option_ref"]
                    or saved["block"] != slot["block"]
                    or obs["option_ref"] != slot["option_ref"]
                    or obs["block"] != slot["block"]
                    or not job
                    or job["project"] != project
                    or job["body"]["request"].get("probe_plan_ref") != saved["ref"]
                    or job["body"]["attempt_id"] != obs["attempt_id"]
                ):
                    raise Rejected("qualification observation differs from its reserved schedule")
                cells.append(
                    {
                        "block": slot["block"],
                        "option_ref": slot["option_ref"],
                        "attempt_id": obs["attempt_id"],
                    }
                )
        evidence = FidelitySpaces(self.service).evidence(
            project,
            plan["request"]["space_ref"],
            FidelityEvidenceRequest(
                job_ids=tuple(o["job_id"] for o in probes), seed=study["request"]["seed"]
            ),
        )
        if not evidence["thermal_brackets_verified"]:
            raise Rejected("all calibration observations require verified thermal brackets")
        if any(
            datetime.fromisoformat(p["created_at"]) < datetime.fromisoformat(plan["registered_at"])
            for p in evidence["provenance"]
        ):
            raise Rejected("calibration jobs must follow preregistration")
        units = {b["option_ref"]: b["work_units"] for b in space["bindings"]}
        measured = {o["attempt_id"]: o for o in evidence["observations"]}
        for cell in cells:
            o = measured[cell["attempt_id"]]
            cell.update(
                seconds_per_work_unit=o["seconds_per_work_unit"],
                elapsed_seconds=o["seconds_per_work_unit"] * units[cell["option_ref"]],
            )
        assessment = paired_assessment(plan["request"], space, cells)
        oldest = min(datetime.fromisoformat(p["finished_at"]) for p in evidence["provenance"])
        body = {
            **assessment,
            "ref": ref,
            "plan_digest": signature(plan),
            "space_digest": signature(space),
            "study_ref": row["id"],
            "evidence_ref": evidence["ref"],
            "evidence_digest": signature(evidence),
            "assessed_at": now().isoformat(),
            "valid_until": (oldest + timedelta(seconds=maximum_age)).isoformat(),
            "execution_authorized": assessment["status"] == "QUALIFIED",
            "calibration_cost": study["recommendation"]["cost"]
            if "recommendation" in study
            else {
                "planning_seconds": study["planning_seconds"],
                "device_seconds": study["charged_device_seconds"],
            },
        }
        with self.store.transaction() as conn:
            return self.store.put(conn, "fidelity_assessment", ref, project, body)["body"]

    def checked(self, conn, project, ref, space):
        plan = required(self.store, conn, "fidelity_qualification", ref, project)
        result = required(self.store, conn, "fidelity_assessment", ref, project)
        if self.store.get(conn, "fidelity_invalidation", ref):
            raise Rejected("MF_KG_DISABLED: qualification invalidated by a later observation")
        if (
            result["status"] != "QUALIFIED"
            or not result["execution_authorized"]
            or result["plan_digest"] != signature(plan)
            or result["space_digest"] != signature(space)
            or now() >= datetime.fromisoformat(result["valid_until"])
        ):
            raise Rejected("MF_KG_DISABLED: qualification rejected, changed or expired")
        evidence = required(self.store, conn, "mf_evidence", result["evidence_ref"], project)
        if signature(evidence) != result["evidence_digest"]:
            raise Rejected("MF_KG_DISABLED: qualification evidence changed")
        FidelitySpaces(self.service)._check_bindings(conn, project, space, current=True)
        return result, evidence


def adaptive_ranks_unchanged(space, assessment, seed, observations, policy):
    """Conservative latest-observation guard, not a second inferential experiment."""
    rates = {
        o["ref"]: statistics.mean(
            r["seconds_per_work_unit"] for r in seed["observations"] if r["option_ref"] == o["ref"]
        )
        for o in space["options"]
    }
    for obs in observations:
        if obs["outcome"] == "COMPLETED" and obs["measurements"]:
            rates[obs["option_ref"]] = (
                obs["measurements"]["elapsed_seconds"] / obs["measurements"]["work_units"]
            )
    options = {(o["candidate_ref"], o["fidelity"]): o["ref"] for o in space["options"]}
    bound = math.log1p(policy["tie_fraction"])
    for pair in assessment["paired_ranks"]:
        a, b = pair["candidates"]
        for level, expected in [(pair["fidelity"], pair["low_order"]), (1.0, pair["target_order"])]:
            ratio = math.log(rates[options[a, level]] / rates[options[b, level]])
            actual = -1 if ratio < -bound else (1 if ratio > bound else 0)
            if actual != expected:
                return False
    return True
