"""Finite approved-space strategies. Predictions never become observations."""

import math
import random
import time
import warnings
from collections import defaultdict

from .contracts import signature


def device_unit(candidate, variant):
    ctx = candidate.context
    return ":".join(
        [candidate.backend, variant.device_class, ctx.accelerator_model, ctx.allocation_mode]
    )


def features(candidates):
    """One-hot unordered runtime categories, normalized numerical resources."""
    categorical = [
        signature(
            {
                "variant": c.variant_ref,
                "backend": c.backend,
                "model": c.context.accelerator_model,
                "environment": c.context.environment_digest,
                "power_mode": c.context.power_mode,
                "allocation_mode": c.context.allocation_mode,
            }
        )
        for c in candidates
    ]
    categories = sorted(set(categorical))
    numeric_keys = sorted(
        {
            key
            for c in candidates
            for key, value in c.context.parameters.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    )
    # A conditional parameter missing from one variant is distinct from numeric zero.
    raw = [
        [
            c.context.resources.host_cpu,
            c.context.resources.host_memory_mib,
            *[float(c.context.parameters.get(key, 0)) for key in numeric_keys],
            *[float(key in c.context.parameters) for key in numeric_keys],
        ]
        for c in candidates
    ]
    columns = list(zip(*raw, strict=True))
    bounds = [(min(col), max(col)) for col in columns]
    encoded = [
        [(v - lo) / (hi - lo) if hi != lo else 0 for v, (lo, hi) in zip(row, bounds, strict=True)]
        + [float(category == current) for category in categories]
        for row, current in zip(raw, categorical, strict=True)
    ]
    # String-valued parameters are also categorical, never arbitrarily ordinal.
    strings = sorted(
        {
            (key, str(value))
            for c in candidates
            for key, value in c.context.parameters.items()
            if isinstance(value, str)
        }
    )
    for row, candidate in zip(encoded, candidates, strict=True):
        row.extend(float(candidate.context.parameters.get(key) == value) for key, value in strings)
    return encoded, {
        "numeric": [
            "host_cpu",
            "host_memory_mib",
            *numeric_keys,
            *[key + ".present" for key in numeric_keys],
        ],
        "bounds": bounds,
        "runtime_categories": categories,
        "string_categories": strings,
    }


def ask(strategy, candidates, observations, quality, seed):
    """Suggest a probe, not an approved final configuration."""
    start = time.monotonic()
    completed = [o for o in observations if o.get("measurements") is not None]
    failed = {o["candidate_ref"] for o in observations if o.get("outcome") in {"OOM", "TIMEOUT"}}
    feasible = [c for c in candidates if c.ref not in failed]
    if not feasible:
        return {
            "candidate_ref": None,
            "reason": "NO_UNFAILED_CANDIDATE",
            "planning_seconds": time.monotonic() - start,
        }
    counts = defaultdict(int)
    for obs in observations:
        counts[obs["candidate_ref"]] += 1
    rng = random.Random(seed + len(observations))
    unseen = [c for c in feasible if counts[c.ref] == 0]
    fallback = None
    if strategy == "lookup":
        choice = min(feasible, key=lambda c: counts[c.ref])
        reason = "SPACE_COVERAGE_LOOKUP_BASELINE"
    elif strategy == "random":
        choice, reason = rng.choice(feasible), "SEEDED_RANDOM"
    elif len(completed) < 3 or len({o["candidate_ref"] for o in completed}) < 2:
        choice = rng.choice(unseen or feasible)
        reason = "INITIAL_DESIGN_INSUFFICIENT_OBSERVATIONS"
    else:
        try:
            answer = qlognei(feasible, completed, quality, seed)
            answer["planning_seconds"] = time.monotonic() - start
            return answer
        except (ImportError, RuntimeError, ValueError, ArithmeticError) as exc:
            # A model exception is an explicit fallback, never fake acquisition output.
            choice, reason = rng.choice(unseen or feasible), "MODEL_FAILURE_RANDOM_FALLBACK"
            fallback = type(exc).__name__
    return {
        "candidate_ref": choice.ref,
        "reason": reason,
        "fallback": fallback,
        "measured": False,
        "planning_seconds": time.monotonic() - start,
    }


def qlognei(candidates, observations, quality, seed):
    import botorch
    import torch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import ModelListGP, SingleTaskGP
    from botorch.models.transforms.outcome import Standardize
    from botorch.optim import optimize_acqf_discrete
    from gpytorch.mlls import SumMarginalLogLikelihood

    torch.set_num_threads(1)
    rows, schema = features(candidates)
    lookup = {c.ref: row for c, row in zip(candidates, rows, strict=True)}
    observations = [o for o in observations if o["candidate_ref"] in lookup]
    if len(observations) < 3:
        raise ValueError("not enough uncensored measurements")
    grouped = defaultdict(list)
    for obs in observations:
        m = obs["measurements"]
        grouped[obs["candidate_ref"]].append(
            [-math.log(m["elapsed_seconds"]), m["peak_memory_mib"], m["quality_value"]]
        )
    # Repeated independent runs estimate observation noise; use a floor for singletons.
    xs, ys, variances = [], [], []
    for ref, values in grouped.items():
        tensor = torch.tensor(values, dtype=torch.double)
        xs.append(lookup[ref])
        ys.append(tensor.mean(dim=0))
        variance = (
            tensor.var(dim=0, unbiased=True) / len(values)
            if len(values) > 1
            else torch.full((3,), 0.01, dtype=torch.double)
        )
        variances.append(variance.clamp_min(1e-6))
    train_x = torch.tensor(xs, dtype=torch.double)
    train_y, train_var = torch.stack(ys), torch.stack(variances)
    choices = torch.tensor(rows, dtype=torch.double)
    with warnings.catch_warnings(record=True) as diagnostics, torch.random.fork_rng():
        warnings.simplefilter("always")
        torch.manual_seed(seed)
        model = ModelListGP(
            *[
                SingleTaskGP(
                    train_x,
                    train_y[:, i : i + 1],
                    train_Yvar=train_var[:, i : i + 1],
                    outcome_transform=Standardize(m=1),
                )
                for i in range(3)
            ]
        )
        fit_started = time.monotonic()
        fit_gpytorch_mll(
            SumMarginalLogLikelihood(model.likelihood, model),
            optimizer_kwargs={"options": {"maxiter": 60}},
        )
        model_fitting_seconds = time.monotonic() - fit_started
        acquisition_started = time.monotonic()
        acquisition = qLogNoisyExpectedImprovement(
            model=model,
            X_baseline=train_x,
            objective=GenericMCObjective(lambda samples, X=None: samples[..., 0]),
            constraints=[
                lambda samples: samples[..., 1] - quality.maximum_peak_memory_mib,
                lambda samples: quality.minimum - samples[..., 2],
            ],
            prune_baseline=False,
        )
        selected, value = optimize_acqf_discrete(
            acquisition, q=1, choices=choices, unique=True, max_batch_size=64
        )
        acquisition_seconds = time.monotonic() - acquisition_started
        index = torch.argmin(torch.sum((choices - selected[0]) ** 2, dim=-1)).item()
        posterior = model.posterior(choices)
        means, std = posterior.mean.detach(), posterior.variance.clamp_min(0).sqrt().detach()
    predictions = [
        {
            "candidate_ref": c.ref,
            "measured": False,
            "predicted_elapsed_seconds": math.exp(-means[i, 0].item()),
            "posterior_interval_seconds": [
                math.exp(-means[i, 0].item() - 1.96 * std[i, 0].item()),
                math.exp(-means[i, 0].item() + 1.96 * std[i, 0].item()),
            ],
            "predicted_memory_mib": means[i, 1].item(),
            "predicted_quality": means[i, 2].item(),
        }
        for i, c in enumerate(candidates)
    ]
    return {
        "candidate_ref": candidates[index].ref,
        "reason": "CONSTRAINED_QLOGNEI",
        "measured": False,
        "acquisition_value": value.item(),
        "predictions": predictions,
        "surrogate": {
            "feature_schema": schema,
            "training_run_ids": [o["attempt_id"] for o in observations],
            "botorch_version": botorch.__version__,
            "model_fitting_seconds": model_fitting_seconds,
            "acquisition_seconds": acquisition_seconds,
            "torch_version": torch.__version__,
            "outputs": ["negative_log_elapsed", "peak_memory_mib", "quality"],
            "validation_error": None,
            "validation_status": "not calibrated; posterior is not an operational guarantee",
            "numerical_diagnostics": [
                {"category": warning.category.__name__, "message": str(warning.message)[:512]}
                for warning in diagnostics
            ],
        },
    }
