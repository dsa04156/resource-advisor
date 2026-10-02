"""Finite-space MF-GP / MF-KG numerical kernel, without execution authority.

Inputs must represent one homogeneous runtime group. Qualification and durable
mixed-fidelity execution are separate gates; this module cannot submit Jobs.
"""

import math
import statistics
import time
import warnings
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, signature

UnitCoordinate = Annotated[float, Field(ge=0, le=1)]


def _snapshot_value(key, value):
    if isinstance(value, list):
        return [_snapshot_value(key, item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isinf(value) and key.endswith((".upper_bound", ".lower_bound")):
            return "+infinity" if value > 0 else "-infinity"
        raise ArithmeticError("nonfinite fitted model state")
    return value


class MFOption(Contract):
    ref: Ref
    candidate_ref: Ref
    coordinates: tuple[UnitCoordinate, ...] = Field(min_length=1, max_length=4)
    fidelity: float = Field(gt=0, le=1)


class MFObservation(Contract):
    attempt_id: Ref
    option_ref: Ref
    runtime_group_signature: Digest
    seconds_per_work_unit: float = Field(gt=0)
    # Entire independently measured evaluation interval, not accelerator busy time.
    evaluation_wall_seconds: float = Field(gt=0)
    quality_passed: Literal[True]
    memory_passed: Literal[True]


class MFKernelInput(Contract):
    runtime_group_signature: Digest
    # A declared numerical axis is not proof of qualified representative sampling.
    fidelity_axis: Literal["representative_sampling"]
    options: tuple[MFOption, ...] = Field(min_length=4, max_length=32)
    observations: tuple[MFObservation, ...] = Field(min_length=8, max_length=192)
    feature_names: tuple[Ref, ...] = Field(min_length=1, max_length=4)
    seed: int = 0
    num_fantasies: int = Field(default=16, ge=8, le=64)

    @model_validator(mode="after")
    def bounded_comparable_data(self):
        options = {o.ref: o for o in self.options}
        if len(options) != len(self.options):
            raise ValueError("option refs must be unique")
        if len(set(self.feature_names)) != len(self.feature_names) or any(
            len(o.coordinates) != len(self.feature_names) for o in self.options
        ):
            raise ValueError("all options need the same named numeric dimensions")
        if len({(*o.coordinates, o.fidelity) for o in self.options}) != len(self.options):
            raise ValueError("duplicate configuration/fidelity coordinates")
        candidates = {o.candidate_ref for o in self.options}
        if len(candidates) < 2:
            raise ValueError("at least two distinct configurations required")
        for ref in candidates:
            cells = [o for o in self.options if o.candidate_ref == ref]
            if len({o.coordinates for o in cells}) != 1 or sum(o.fidelity == 1 for o in cells) != 1:
                raise ValueError("each configuration must have fixed coordinates and one target")
            if not any(o.fidelity < 1 for o in cells):
                raise ValueError("each configuration needs a lower fidelity")
        if len({o.coordinates for o in self.options}) != len(candidates):
            raise ValueError("configuration names cannot alias numeric coordinates")
        ids = [o.attempt_id for o in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("independent attempt IDs must be unique")
        if any(o.option_ref not in options for o in self.observations):
            raise ValueError("observation outside the explicit finite space")
        if any(
            o.runtime_group_signature != self.runtime_group_signature for o in self.observations
        ):
            raise ValueError("mixed runtime groups cannot share one MF surrogate")
        if any(sum(r.option_ref == key for r in self.observations) < 2 for key in options):
            raise ValueError("every option needs independent repeated performance and cost data")
        return self


def ask_mfkg(problem: MFKernelInput):
    """Joint configuration/fidelity suggestion for analysis, never a ProbePlan.

    Enumerate the approved finite target choices inside each fantasy instead of
    continuously optimizing into unregistered resource configurations.
    """
    import botorch
    import torch
    from botorch.acquisition.cost_aware import InverseCostWeightedUtility
    from botorch.acquisition.knowledge_gradient import qMultiFidelityKnowledgeGradient
    from botorch.acquisition.utils import project_to_target_fidelity
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskMultiFidelityGP
    from botorch.models.deterministic import GenericDeterministicModel
    from botorch.models.transforms.outcome import Standardize
    from botorch.sampling.normal import SobolQMCNormalSampler
    from gpytorch.mlls import ExactMarginalLogLikelihood

    # Revalidate even if a caller used model_copy(update=...) without validation.
    problem = MFKernelInput.model_validate(problem.model_dump(mode="json"))
    started = time.monotonic()
    options = sorted(problem.options, key=lambda o: o.ref)
    groups = {
        o.ref: sorted(
            (r for r in problem.observations if r.option_ref == o.ref),
            key=lambda r: r.attempt_id,
        )
        for o in options
    }
    values = [[-math.log(r.seconds_per_work_unit) for r in groups[o.ref]] for o in options]
    means = [statistics.mean(v) for v in values]
    variances = [max(statistics.variance(v) / len(v), 1e-6) for v in values]
    costs = {
        o.ref: statistics.mean(r.evaluation_wall_seconds for r in groups[o.ref]) for o in options
    }
    targets = [o for o in options if o.fidelity == 1]
    # BoTorch's utility treats negative fantasy gains differently. Dimensionless
    # costs prevent seconds-vs-minutes units from changing that piecewise rule.
    reference_cost = statistics.mean(costs[o.ref] for o in targets)
    train_x = torch.tensor([(*o.coordinates, o.fidelity) for o in options], dtype=torch.double)
    target_x = torch.tensor([(*o.coordinates, 1.0) for o in targets], dtype=torch.double)
    train_y = torch.tensor(means, dtype=torch.double).unsqueeze(-1)
    train_var = torch.tensor(variances, dtype=torch.double).unsqueeze(-1)
    dimension = train_x.shape[-1] - 1
    previous_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(1)
        with warnings.catch_warnings(record=True) as diagnostics, torch.random.fork_rng():
            warnings.simplefilter("always")
            torch.manual_seed(problem.seed)
            model = SingleTaskMultiFidelityGP(
                train_x,
                train_y,
                train_Yvar=train_var,
                data_fidelities=[dimension],
                outcome_transform=Standardize(m=1),
            )
            fit_gpytorch_mll(
                ExactMarginalLogLikelihood(model.likelihood, model),
                optimizer_kwargs={"options": {"maxiter": 60}},
            )
            fitted_at = time.monotonic()
            project = lambda x: project_to_target_fidelity(x, {dimension: 1.0})  # noqa: E731
            with torch.no_grad():
                posterior = model.posterior(target_x)
                target_mean = posterior.mean.squeeze(-1)
                target_sd = posterior.variance.clamp_min(0).sqrt().squeeze(-1)
                current = target_mean.max()
                scores = []
                for i, option in enumerate(options):
                    normalized_cost = costs[option.ref] / reference_cost
                    cost_model = GenericDeterministicModel(
                        lambda x, cost=normalized_cost: torch.full_like(x[..., :1], cost)
                    )
                    acquisition = qMultiFidelityKnowledgeGradient(
                        model=model,
                        num_fantasies=problem.num_fantasies,
                        sampler=SobolQMCNormalSampler(
                            torch.Size([problem.num_fantasies]), seed=problem.seed
                        ),
                        current_value=current,
                        cost_aware_utility=InverseCostWeightedUtility(cost_model=cost_model),
                        project=project,
                    )
                    actual = train_x[i : i + 1].unsqueeze(0)
                    fantasy = model.fantasize(X=actual, sampler=acquisition.sampler)
                    fantasy_means = fantasy.posterior(target_x).mean.reshape(
                        problem.num_fantasies, len(targets)
                    )
                    best_values, indices = fantasy_means.max(dim=-1)
                    inner = target_x[indices]
                    augmented = torch.cat([train_x[i : i + 1], inner]).unsqueeze(0)
                    score = acquisition(augmented).item()
                    if not math.isfinite(score):
                        raise ArithmeticError("MF-KG produced a nonfinite acquisition value")
                    scores.append(
                        {
                            "option_ref": option.ref,
                            "candidate_ref": option.candidate_ref,
                            "fidelity": option.fidelity,
                            "acquisition_value": score,
                            "estimated_evaluation_wall_seconds": costs[option.ref],
                            "normalized_cost": normalized_cost,
                            "fantasy_gains": (best_values - current).tolist(),
                            "fantasy_target_options": [targets[j].ref for j in indices.tolist()],
                        }
                    )
                predictions = [
                    {
                        "candidate_ref": option.candidate_ref,
                        "fidelity": 1.0,
                        "measured": False,
                        "median_seconds_per_work_unit": math.exp(-target_mean[i].item()),
                        "posterior_interval_seconds_per_work_unit": [
                            math.exp(-target_mean[i].item() - 1.96 * target_sd[i].item()),
                            math.exp(-target_mean[i].item() + 1.96 * target_sd[i].item()),
                        ],
                    }
                    for i, option in enumerate(targets)
                ]
            snapshot = {
                key: _snapshot_value(key, value.detach().tolist())
                for key, value in model.state_dict().items()
            }
            numerical_warnings = [
                {"category": w.category.__name__, "message": str(w.message)[:512]}
                for w in diagnostics
            ]
    finally:
        torch.set_num_threads(previous_threads)
    best = max(scores, key=lambda row: row["acquisition_value"])
    selected = best if best["acquisition_value"] > 0 else None
    finished = time.monotonic()
    return {
        "method": "finite_space_cost_aware_qMultiFidelityKnowledgeGradient",
        "option_ref": selected["option_ref"] if selected else None,
        "candidate_ref": selected["candidate_ref"] if selected else None,
        "fidelity": selected["fidelity"] if selected else None,
        "reason": "MF_KG_NUMERICAL_SUGGESTION" if selected else "NO_POSITIVE_MF_KG_ESTIMATE",
        "execution_authorized": False,
        "measured": False,
        "input_digest": signature(problem),
        "runtime_group_signature": problem.runtime_group_signature,
        "scores": scores,
        "predictions": predictions,
        "current_target_value": current.item(),
        "cost_reference_seconds": reference_cost,
        "cost_method": "per-option observed mean evaluation wall time; dimensionless target normalization",
        "cost_uncertainty": "not modeled; no unit conversion to GPU-hours or energy",
        "planning_seconds": finished - started,
        "fit_seconds": fitted_at - started,
        "acquisition_seconds": finished - fitted_at,
        "surrogate": {
            "class": "SingleTaskMultiFidelityGP",
            "fidelity_dimension": dimension,
            "feature_names": [*problem.feature_names, "fidelity"],
            "training_run_ids": [r.attempt_id for o in options for r in groups[o.ref]],
            "independent_runs_per_option": {o.ref: len(groups[o.ref]) for o in options},
            "train_x": train_x.tolist(),
            "train_y_negative_log_seconds_per_work_unit": train_y.tolist(),
            "train_variance_of_mean": train_var.tolist(),
            "variance_floor": 1e-6,
            "state_dict": snapshot,
            "state_dict_encoding": "JSON numbers; infinite constraint bounds encoded as signed strings",
            "snapshot_digest": signature(snapshot),
            "botorch_version": botorch.__version__,
            "torch_version": torch.__version__,
            "num_fantasies": problem.num_fantasies,
            "seed": problem.seed,
            "numerical_diagnostics": numerical_warnings,
            "validation_status": "uncalibrated posterior; numerical kernel is not hardware qualification",
        },
    }
