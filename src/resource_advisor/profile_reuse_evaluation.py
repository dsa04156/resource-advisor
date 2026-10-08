"""Audit real profile-reuse captures. Never impute unobserved utilization/cost."""

import math
import statistics


def summarize(capture):
    groups = {arm: [] for arm in ("baseline", "reuse")}
    sources = set(capture["profiling"]["source_attempts"])
    seen = set()
    blocks = set()
    comparable = set()
    for key in ("gpu_seconds", "wall_seconds"):
        cost = capture["profiling"][key]
        if cost is None or not math.isfinite(cost) or cost < 0:
            raise ValueError("unknown or negative upfront cost")
    for cohort in capture["cohorts"]:
        block = (cohort["arm"], cohort["block"])
        if block in blocks or cohort["arm"] not in groups:
            raise ValueError("duplicate block or unknown arm")
        blocks.add(block)
        if not cohort["jobs"] or cohort["wall_seconds"] <= 0:
            raise ValueError("incomplete cohort")
        for job in cohort["jobs"]:
            ref = job["attempt_id"]
            if ref in seen or ref in sources:
                raise ValueError("duplicate or leaked main attempt")
            seen.add(ref)
            if job["outcome"] != "SUCCEEDED" or job["quality"] != 1:
                raise ValueError("incomplete/failed cohort; retain cost, do not claim success")
            if not job["accepted_at"] <= job["started_at"] <= job["finished_at"]:
                raise ValueError("reversed clock boundaries")
            if job["gpu_seconds"] is None or job["gpu_seconds"] < 0:
                raise ValueError("unknown reservation cost")
            detail = job.get("details", {})
            comparable.add((job["units"], detail.get("input_digest"), detail.get("model_digest")))
        groups[cohort["arm"]].append(cohort)
    if len(comparable) != 1:
        raise ValueError("mismatched model, input or fixed work")
    if "plan" in capture:
        plan = capture["plan"]
        for cohorts in groups.values():
            if len(cohorts) != plan["paired_blocks"] or any(
                len(c["jobs"]) != plan["jobs_per_cohort"] for c in cohorts
            ):
                raise ValueError("incomplete preregistered trace")
    arms = {}
    for arm, cohorts in groups.items():
        rows = [j for c in cohorts for j in c["jobs"]]
        if not rows:
            raise ValueError("both complete arms required")
        jcts = sorted(j["finished_at"] - j["accepted_at"] for j in rows)
        wall = sum(c["wall_seconds"] for c in cohorts)
        gpu = sum(j["gpu_seconds"] for j in rows)
        integral, sampled = 0, 0
        pool_integral, pool_sampled = 0, 0
        pool = capture.get("pool_sensor", []) if capture.get("pool_sensor_qualified", True) else []
        for cohort in cohorts:
            if "started_at" not in cohort:
                continue
            begin = cohort["started_at"]
            end = begin + cohort["wall_seconds"]
            for a, b in zip(pool, pool[1:], strict=False):
                dt = b["at"] - a["at"]
                lo, hi = max(begin, a["at"]), min(end, b["at"])
                if not (0 < dt <= 6 and hi > lo):
                    continue
                ua, ub = a.get("utilization_percent"), b.get("utilization_percent")
                if ua is None or ub is None or not (0 <= ua <= 100 and 0 <= ub <= 100):
                    continue
                left = ua + (ub - ua) * (lo - a["at"]) / dt
                right = ua + (ub - ua) * (hi - a["at"]) / dt
                pool_integral += (hi - lo) * (left + right) / 2
                pool_sampled += hi - lo
        for job in rows:
            for a, b in zip(job["sensor"], job["sensor"][1:], strict=False):
                dt = b["at"] - a["at"]
                if 0 < dt <= 1 and all(s.get("utilization_percent") is not None for s in (a, b)):
                    integral += dt * (a["utilization_percent"] + b["utilization_percent"]) / 2
                    sampled += dt
        arms[arm] = {
            "jobs": len(rows),
            "mean_jct_seconds": statistics.mean(jcts),
            "p95_jct_seconds": jcts[math.ceil(0.95 * len(jcts)) - 1],
            "mean_wait_seconds": statistics.mean(j["started_at"] - j["accepted_at"] for j in rows),
            "gpu_hours": gpu / 3600,
            "gpu_seconds": gpu,
            "wall_seconds": wall,
            "throughput_jobs_per_hour": len(rows) / wall * 3600,
            "throughput_images_per_second": sum(j["units"] for j in rows) / wall,
            "reservation_occupancy_percent": gpu / wall * 100,
            "nvml_utilization_percent": integral / sampled if sampled else None,
            "nvml_observed_seconds": sampled,
            "nvml_reservation_coverage_percent": sampled / gpu * 100 if gpu else None,
            "dcgm_utilization_percent": pool_integral / pool_sampled if pool_sampled else None,
            "dcgm_observed_seconds": pool_sampled,
            "dcgm_coverage_percent": pool_sampled / wall * 100,
        }
    if len(groups["baseline"]) != len(groups["reuse"]):
        raise ValueError("unpaired cohorts")
    curve, gpu_payback, wall_payback = [], None, None
    baseline_gpu = baseline_wall = reuse_gpu = reuse_wall = jobs = 0
    for b, r in zip(
        sorted(groups["baseline"], key=lambda x: x["block"]),
        sorted(groups["reuse"], key=lambda x: x["block"]),
        strict=True,
    ):
        if b["block"] != r["block"] or len(b["jobs"]) != len(r["jobs"]):
            raise ValueError("mismatched paired trace")
        jobs += len(b["jobs"])
        baseline_gpu += sum(j["gpu_seconds"] for j in b["jobs"])
        reuse_gpu += sum(j["gpu_seconds"] for j in r["jobs"])
        baseline_wall += b["wall_seconds"]
        reuse_wall += r["wall_seconds"]
        charged_gpu = reuse_gpu + capture["profiling"]["gpu_seconds"]
        charged_wall = reuse_wall + capture["profiling"]["wall_seconds"]
        if charged_gpu <= baseline_gpu and gpu_payback is None:
            gpu_payback = jobs
        if charged_wall <= baseline_wall and wall_payback is None:
            wall_payback = jobs
        curve.append(
            dict(
                jobs=jobs,
                baseline_gpu_seconds=baseline_gpu,
                reuse_with_profile_gpu_seconds=charged_gpu,
                baseline_wall_seconds=baseline_wall,
                reuse_with_profile_wall_seconds=charged_wall,
            )
        )
    improvements = {}
    for key in (
        "mean_jct_seconds",
        "p95_jct_seconds",
        "mean_wait_seconds",
        "gpu_hours",
        "throughput_jobs_per_hour",
        "throughput_images_per_second",
        "reservation_occupancy_percent",
        "nvml_utilization_percent",
        "dcgm_utilization_percent",
    ):
        a, b = arms["baseline"][key], arms["reuse"][key]
        sign = 1 if key.startswith(("throughput", "reservation", "nvml", "dcgm")) else -1
        improvements[key] = sign * (b - a) / a * 100 if a and b is not None else None
    final = curve[-1]
    return dict(
        arms=arms,
        improvements_percent=improvements,
        cumulative=curve,
        observed_gpu_payback_jobs=gpu_payback,
        observed_wall_payback_jobs=wall_payback,
        net_gpu_improvement_percent=(
            1 - final["reuse_with_profile_gpu_seconds"] / final["baseline_gpu_seconds"]
        )
        * 100,
        net_wall_improvement_percent=(
            1 - final["reuse_with_profile_wall_seconds"] / final["baseline_wall_seconds"]
        )
        * 100,
        p95_method="nearest rank; pooled independent native main jobs",
        utilization_scope="NVML physical GPU, sampled execution windows; gaps unknown",
    )
