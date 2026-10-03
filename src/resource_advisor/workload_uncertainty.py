"""Offline, source-only shape holdout model; never authorizes execution.

Callers bind a family signature to audited model, runtime, measurement and
constraint identities. Target descriptors intentionally cannot contain results.
This shadow model does not replace workload-scoped BO or target-adapted RGPE.
"""

import math
import statistics
import time
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass

from .contracts import signature

POLICY = "source-only-shape-gp-v1"
SEED = 20261003
NOISE_FLOOR = 1e-6
NORMAL_MULTIPLIER = 1.959963984540054


@dataclass(frozen=True)
class Target:
    workload_signature: str
    family_signature: str
    candidate_ref: str
    input_area: int
    host_cpu: float


@dataclass(frozen=True)
class SourceCell:
    descriptor: Target
    attempt_ids: tuple[str, ...]
    elapsed_seconds: tuple[float, ...]


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def prepare(sources, targets):
    """Validate complete, disjoint source grids and derive source-only scaling."""
    _require(sources and targets, "sources and targets required")
    descriptors = [s.descriptor for s in sources] + list(targets)
    for d in descriptors:
        _require(
            all(
                isinstance(v, str) and v
                for v in (d.workload_signature, d.family_signature, d.candidate_ref)
            )
            and type(d.input_area) is int
            and d.input_area > 0
            and _positive(d.host_cpu),
            "invalid workload descriptor",
        )
    source_tasks = {s.descriptor.workload_signature for s in sources}
    _require(len(source_tasks) >= 2, "at least two source workloads required")
    _require(
        source_tasks.isdisjoint(d.workload_signature for d in targets),
        "target workload leaked into source training",
    )
    _require(len({s.descriptor.family_signature for s in sources}) == 1, "mixed source families")
    _require(
        len({(s.descriptor.workload_signature, s.descriptor.candidate_ref) for s in sources})
        == len(sources),
        "duplicate source cell",
    )
    _require(
        len({(d.workload_signature, d.candidate_ref) for d in targets}) == len(targets),
        "duplicate target descriptor",
    )
    ids = [ref for cell in sources for ref in cell.attempt_ids]
    _require(
        len(ids) == len(set(ids)) and all(isinstance(ref, str) and ref for ref in ids),
        "duplicate or invalid source attempt",
    )
    tasks = defaultdict(list)
    for s in sources:
        _require(
            len(s.elapsed_seconds) == len(s.attempt_ids) >= 3
            and all(_positive(t) for t in s.elapsed_seconds),
            "at least three finite positive timings per source cell required",
        )
        tasks[s.descriptor.workload_signature].append(s)
    grids, areas = [], []
    for cells in tasks.values():
        values = {s.descriptor.input_area for s in cells}
        _require(len(values) == 1, "workload descriptor changed within source grid")
        areas.append(next(iter(values)))
        grid = {(s.descriptor.candidate_ref, s.descriptor.host_cpu) for s in cells}
        _require(len({cpu for _, cpu in grid}) == len(cells), "aliased source CPU cells")
        grids.append(grid)
    _require(len(set(areas)) == len(areas), "aliases are not distinct source workloads")
    _require(all(g == grids[0] for g in grids), "incomplete source grid")
    for target in targets:
        _require(
            target.family_signature != sources[0].descriptor.family_signature
            or target.input_area not in areas,
            "target shape already present in source workloads",
        )
    for workload in {t.workload_signature for t in targets}:
        _require(
            len(
                {
                    (t.family_signature, t.input_area)
                    for t in targets
                    if t.workload_signature == workload
                }
            )
            == 1,
            "target workload descriptor changed within grid",
        )
    bounds = {
        "input_area": [min(areas), max(areas)],
        "host_cpu": [min(cpu for _, cpu in grids[0]), max(cpu for _, cpu in grids[0])],
    }
    prepared = []
    for cell in sorted(sources, key=lambda s: (s.descriptor.input_area, s.descriptor.host_cpu)):
        logs = [math.log(t) for t in cell.elapsed_seconds]
        variance = statistics.variance(logs)
        prepared.append(
            {
                "descriptor": vars(cell.descriptor),
                "attempt_ids": list(cell.attempt_ids),
                "log_mean": statistics.mean(logs),
                "log_sample_variance": variance,
                "repeat_count": len(logs),
                "training_variance": max(variance / len(logs), NOISE_FLOOR),
            }
        )
    return bounds, prepared, grids[0]


def forecast(sources: list[SourceCell], targets: list[Target]):
    start = time.monotonic()
    bounds, cells, grid = prepare(sources, targets)
    family = sources[0].descriptor.family_signature
    rows, eligible = [], []
    for d in targets:
        reasons = []
        if d.family_signature != family:
            reasons.append("UNSEEN_FAMILY_OR_RUNTIME")
        if any(not lo <= getattr(d, key) <= hi for key, (lo, hi) in bounds.items()):
            reasons.append("OUTSIDE_SOURCE_RANGE")
        if (d.candidate_ref, d.host_cpu) not in grid:
            reasons.append("UNSEEN_CONFIGURATION")
        row = {
            "descriptor": vars(d),
            "status": "ABSTAIN" if reasons else "PENDING",
            "reasons": reasons,
            "prediction": None,
        }
        rows.append(row)
        if not reasons:
            eligible.append(row)
    result = {
        "policy": POLICY,
        "seed": SEED,
        "measured": False,
        "execution_authorized": False,
        "source_bounds": bounds,
        "source_cells": cells,
        "training_digest": signature(cells),
        "targets": rows,
        "versions": {},
        "diagnostics": [],
        "fit_count": 0,
        "interval_semantics": "Uncalibrated log-GP plus source pooled within-CPU Job variance",
    }
    if eligible:
        try:
            _fit_predict(cells, bounds, eligible, result)
        except (ImportError, RuntimeError, ValueError, ArithmeticError) as exc:
            result["model_failure"] = type(exc).__name__
            for row in eligible:
                row.update(status="ABSTAIN", reasons=["MODEL_FAILURE"], prediction=None)
    result["status_counts"] = dict(Counter(row["status"] for row in rows))
    result["modeling_wall_seconds"] = time.monotonic() - start
    return result


def _fit_predict(cells, bounds, eligible, result):
    import botorch
    import gpytorch
    import torch
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from botorch.models.transforms.outcome import Standardize
    from gpytorch.mlls import ExactMarginalLogLikelihood

    def coordinates(d):
        return [(d[key] - lo) / (hi - lo) if hi != lo else 0 for key, (lo, hi) in bounds.items()]

    result["versions"] = {
        "botorch": botorch.__version__,
        "torch": torch.__version__,
        "gpytorch": gpytorch.__version__,
    }
    thread_count = torch.get_num_threads()
    diagnostics = []
    try:
        torch.set_num_threads(1)
        with warnings.catch_warnings(record=True) as diagnostics, torch.random.fork_rng(devices=[]):
            warnings.simplefilter("always")
            torch.manual_seed(SEED)
            x = torch.tensor([coordinates(c["descriptor"]) for c in cells], dtype=torch.double)
            y = torch.tensor([[c["log_mean"]] for c in cells], dtype=torch.double)
            v = torch.tensor([[c["training_variance"]] for c in cells], dtype=torch.double)
            model = SingleTaskGP(x, y, train_Yvar=v, outcome_transform=Standardize(m=1))
            result["fit_count"] = 1
            fit_gpytorch_mll(
                ExactMarginalLogLikelihood(model.likelihood, model),
                optimizer_kwargs={"options": {"maxiter": 60}},
            )
            with torch.no_grad():
                post = model.posterior(
                    torch.tensor(
                        [coordinates(r["descriptor"]) for r in eligible], dtype=torch.double
                    )
                )
                means = post.mean.flatten().tolist()
                variances = post.variance.clamp_min(0).flatten().tolist()
            for row, mean, variance in zip(eligible, means, variances, strict=True):
                same_cpu = [
                    c for c in cells if c["descriptor"]["host_cpu"] == row["descriptor"]["host_cpu"]
                ]
                noise = max(
                    NOISE_FLOOR,
                    math.fsum(c["log_sample_variance"] * (c["repeat_count"] - 1) for c in same_cpu)
                    / sum(c["repeat_count"] - 1 for c in same_cpu),
                )

                def interval(v, center=mean):
                    delta = NORMAL_MULTIPLIER * math.sqrt(v)
                    return [math.exp(center - delta), math.exp(center + delta)]

                prediction = {
                    "median_seconds": math.exp(mean),
                    "log_mean": mean,
                    "latent_log_variance": variance,
                    "individual_noise_log_variance": noise,
                    "latent_interval_seconds": interval(variance),
                    "job_interval_seconds": interval(variance + noise),
                }
                _require(
                    all(
                        _positive(v)
                        for v in [
                            prediction["median_seconds"],
                            *prediction["latent_interval_seconds"],
                            *prediction["job_interval_seconds"],
                        ]
                    ),
                    "invalid numerical prediction",
                )
                row.update(status="SHADOW_PREDICTION", prediction=prediction)
    finally:
        result["diagnostics"] = [
            {"category": w.category.__name__, "message": str(w.message)[:512]} for w in diagnostics
        ]
        torch.set_num_threads(thread_count)
