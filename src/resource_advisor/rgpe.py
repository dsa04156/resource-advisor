"""Rank transfer over an explicit finite space, without Job submission authority.

Source times remain source evidence. Each task is normalized separately and
target-only measurements determine constraints and the final time scale.
"""

import math
import statistics
import time
import warnings
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, signature
from .mfkg import _snapshot_value


class TransferOption(Contract):
    candidate_ref: Ref
    coordinates: tuple[Annotated[float, Field(ge=0, le=1)], ...] = Field(min_length=1, max_length=4)


class TransferObservation(Contract):
    attempt_id: Ref
    candidate_ref: Ref
    elapsed_seconds: float = Field(gt=0)
    evaluation_wall_seconds: float = Field(gt=0)
    peak_memory_mib: float = Field(ge=0)
    quality_value: float
    quality_passed: Literal[True]
    memory_passed: Literal[True]


class TransferTask(Contract):
    workload_signature: Digest
    runtime_group_signature: Digest
    observations: tuple[TransferObservation, ...] = Field(max_length=128)


class RGPEInput(Contract):
    runtime_group_signature: Digest
    options: tuple[TransferOption, ...] = Field(min_length=3, max_length=12)
    feature_names: tuple[Ref, ...] = Field(min_length=1, max_length=4)
    sources: tuple[TransferTask, ...] = Field(min_length=1, max_length=4)
    target: TransferTask
    minimum_quality: float
    maximum_peak_memory_mib: float = Field(gt=0)
    rank_tie_fraction: float = Field(default=0.05, ge=0, le=0.25)
    maximum_source_rank_loss: float = Field(default=0.25, ge=0, le=0.5)
    posterior_samples: int = Field(default=64, ge=32, le=256)
    seed: int = 0

    @model_validator(mode="after")
    def comparable_independent_tasks(self):
        refs = {o.candidate_ref for o in self.options}
        if len(refs) != len(self.options) or len({o.coordinates for o in self.options}) != len(
            self.options
        ):
            raise ValueError("candidate refs and numeric coordinates must be distinct")
        if len(set(self.feature_names)) != len(self.feature_names) or any(
            len(o.coordinates) != len(self.feature_names) for o in self.options
        ):
            raise ValueError("fixed named numeric dimensions required")
        tasks = (*self.sources, self.target)
        if len({t.workload_signature for t in tasks}) != len(tasks):
            raise ValueError("source and target workloads must be distinct")
        if any(t.runtime_group_signature != self.runtime_group_signature for t in tasks):
            raise ValueError("transfer requires one approved runtime group")
        ids = [r.attempt_id for t in tasks for r in t.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("source and target independent attempts cannot overlap")
        for task in tasks:
            if any(r.candidate_ref not in refs for r in task.observations):
                raise ValueError("observation outside the approved space")
        for task in self.sources:
            if any(sum(r.candidate_ref == ref for r in task.observations) < 2 for ref in refs):
                raise ValueError("source needs independent repeats at every configuration")
        if any(
            r.quality_value < self.minimum_quality
            or r.peak_memory_mib > self.maximum_peak_memory_mib
            for r in self.target.observations
        ):
            raise ValueError("target quality/memory flags contradict measurements")
        return self


def history_order(problem: RGPEInput):
    """Equal-source rank voting only: no GP fit, transferred seconds or RGPE claim."""
    problem = RGPEInput.model_validate(problem.model_dump(mode="json"))
    refs = sorted(o.candidate_ref for o in problem.options)
    ranks = {ref: [] for ref in refs}
    for task in problem.sources:
        means = {
            ref: statistics.mean(
                math.log(r.elapsed_seconds) for r in task.observations if r.candidate_ref == ref
            )
            for ref in refs
        }
        # Near-ties receive the same number of decisively better configurations.
        for ref in refs:
            ranks[ref].append(
                sum(
                    means[other] + math.log1p(problem.rank_tie_fraction) < means[ref]
                    for other in refs
                )
            )
    scores = {ref: statistics.mean(values) for ref, values in ranks.items()}
    return {
        "method": "history_guided_warm_start",
        "candidate_order": sorted(refs, key=lambda ref: (scores[ref], ref)),
        "mean_source_ranks": scores,
        "source_run_ids": [r.attempt_id for t in problem.sources for r in t.observations],
        "target_run_ids": [r.attempt_id for r in problem.target.observations],
        "measured": False,
        "execution_authorized": False,
    }


def _aggregate(task, options):
    import torch

    present = [
        o for o in options if any(r.candidate_ref == o.candidate_ref for r in task.observations)
    ]
    groups = {
        o.candidate_ref: sorted(
            (r for r in task.observations if r.candidate_ref == o.candidate_ref),
            key=lambda r: r.attempt_id,
        )
        for o in present
    }
    ys, variances = [], []
    for option in present:
        values = torch.tensor(
            [
                [-math.log(r.elapsed_seconds), r.peak_memory_mib, r.quality_value]
                for r in groups[option.candidate_ref]
            ],
            dtype=torch.double,
        )
        ys.append(values.mean(0))
        variances.append((values.var(0, unbiased=True) / len(values)).clamp_min(1e-6))
    return (
        present,
        torch.tensor([o.coordinates for o in present], dtype=torch.double),
        torch.stack(ys),
        torch.stack(variances),
        groups,
    )


def _fit(x, y, variance):
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from gpytorch.mlls import ExactMarginalLogLikelihood

    # Explicit task normalization. Source absolute elapsed values never define
    # target units. LOO calls recompute these statistics without the held-out row.
    center = y.mean(0, keepdim=True)
    scale = y.std(0, unbiased=True, keepdim=True).clamp_min(1e-6)
    model = SingleTaskGP(
        x, (y - center) / scale, train_Yvar=variance / scale.square(), outcome_transform=None
    )
    fit_gpytorch_mll(
        ExactMarginalLogLikelihood(model.likelihood, model),
        optimizer_kwargs={"options": {"maxiter": 60}},
    )
    return model, center.squeeze().item(), scale.squeeze().item()


def _ensemble(models, weights, constraints, center, scale):
    import torch
    from botorch.models.model import Model
    from botorch.posteriors.gpytorch import GPyTorchPosterior
    from gpytorch.distributions import MultitaskMultivariateNormal, MultivariateNormal

    class RankEnsemble(Model):
        def __init__(self):
            super().__init__()
            self.components = torch.nn.ModuleList(models)
            self.constraint_models = torch.nn.ModuleList([c[0] for c in constraints])
            self.register_buffer("weights", weights)

        @property
        def num_outputs(self):
            return 3

        @property
        def batch_shape(self):
            return torch.Size([])

        def posterior(
            self, X, output_indices=None, observation_noise=False, posterior_transform=None
        ):
            if observation_noise is not False:
                raise NotImplementedError("ensemble exposes latent posterior only")
            posteriors = [m.posterior(X).mvn for m in self.components]
            mean = sum(w * p.mean for w, p in zip(self.weights, posteriors, strict=True))
            covariance = sum(
                w.square() * p.lazy_covariance_matrix
                for w, p in zip(self.weights, posteriors, strict=True)
            )
            outputs = [MultivariateNormal(center + scale * mean, scale**2 * covariance)]
            for model, (_, offset, factor) in zip(self.constraint_models, constraints, strict=True):
                p = model.posterior(X).mvn
                outputs.append(
                    MultivariateNormal(
                        offset + factor * p.mean, factor**2 * p.lazy_covariance_matrix
                    )
                )
            if output_indices is not None:
                outputs = [outputs[i] for i in output_indices]
            distribution = (
                outputs[0]
                if len(outputs) == 1
                else MultitaskMultivariateNormal.from_independent_mvns(outputs)
            )
            posterior = GPyTorchPosterior(distribution)
            return posterior_transform(posterior) if posterior_transform else posterior

    return RankEnsemble()


def ask_rgpe(problem: RGPEInput):
    """Fit actual source/target GPs and rank-weighted constrained qLogNEI.

    This pure numerical boundary cannot prove input provenance. Execution callers
    must supply database-derived qualified source evidence and fresh target Jobs.
    """
    import botorch
    import torch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective
    from botorch.optim import optimize_acqf_discrete
    from botorch.sampling.normal import SobolQMCNormalSampler

    problem = RGPEInput.model_validate(problem.model_dump(mode="json"))
    started = time.monotonic()
    options = sorted(problem.options, key=lambda o: o.candidate_ref)
    counts = {
        o.candidate_ref: sum(
            r.candidate_ref == o.candidate_ref for r in problem.target.observations
        )
        for o in options
    }
    # Balanced, source-independent target checks before any transfer weighting.
    if any(count < 2 for count in counts.values()):
        return {
            "candidate_ref": min(counts, key=lambda ref: (counts[ref], ref)),
            "reason": "TARGET_CHECKS_REQUIRED",
            "method": "source_independent_initial_design",
            "weights": None,
            "measured": False,
            "execution_authorized": False,
            "planning_seconds": time.monotonic() - started,
        }
    previous_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(1)
        with warnings.catch_warnings(record=True) as diagnostics, torch.random.fork_rng():
            warnings.simplefilter("always")
            torch.manual_seed(problem.seed)
            present, tx, ty, tv, groups = _aggregate(problem.target, options)
            target_model, center, scale = _fit(tx, ty[:, :1], tv[:, :1])
            constraints = [_fit(tx, ty[:, i : i + 1], tv[:, i : i + 1]) for i in (1, 2)]
            source_models, source_snapshots = [], []
            for task in problem.sources:
                _, x, y, variance, _ = _aggregate(task, options)
                model, offset, factor = _fit(x, y[:, :1], variance[:, :1])
                source_models.append(model)
                source_snapshots.append(
                    {
                        "workload_signature": task.workload_signature,
                        "center": offset,
                        "scale": factor,
                    }
                )
            values = ty[:, 0]
            differences = values[:, None] - values[None, :]
            informative = differences.abs() > math.log1p(problem.rank_tie_fraction)
            informative.fill_diagonal_(False)
            pairs = int(informative.sum().item())
            truth = differences < 0

            def draw(model, seed):
                return SobolQMCNormalSampler(torch.Size([problem.posterior_samples]), seed=seed)(
                    model.posterior(tx)
                ).squeeze(-1)

            with torch.no_grad():
                losses = [
                    (((samples[:, :, None] < samples[:, None, :]) != truth) & informative).sum(
                        (-1, -2)
                    )
                    for samples in [draw(m, problem.seed + i) for i, m in enumerate(source_models)]
                ]
            target_loss = torch.zeros(problem.posterior_samples, dtype=torch.long)
            folds = []
            for i, option in enumerate(present):
                keep = torch.arange(len(present)) != i
                fold, _, _ = _fit(tx[keep], ty[keep, :1], tv[keep, :1])
                with torch.no_grad():
                    samples = draw(fold, problem.seed + 100 + i)
                    target_loss += (
                        ((samples[:, i : i + 1] < samples) != truth[i]) & informative[i]
                    ).sum(-1)
                folds.append(
                    {
                        "held_out_candidate_ref": option.candidate_ref,
                        "held_out_run_ids": [r.attempt_id for r in groups[option.candidate_ref]],
                        "training_run_ids": [
                            r.attempt_id
                            for ref, rows in groups.items()
                            if ref != option.candidate_ref
                            for r in rows
                        ],
                    }
                )
            # Target is first so equal posterior ranking losses prefer target.
            all_losses = torch.stack([target_loss, *losses]).double()
            average = all_losses.mean(-1) / max(pairs, 1)
            eligible = [
                bool(pairs and loss <= problem.maximum_source_rank_loss and loss <= average[0])
                for loss in average[1:]
            ]
            for i, allowed in enumerate(eligible, start=1):
                if not allowed:
                    all_losses[i] = float("inf")
            weights = (
                torch.bincount(all_losses.argmin(0), minlength=len(problem.sources) + 1).double()
                / problem.posterior_samples
            )
            model = _ensemble([target_model, *source_models], weights, constraints, center, scale)
            acquisition = qLogNoisyExpectedImprovement(
                model=model,
                X_baseline=tx,
                sampler=SobolQMCNormalSampler(torch.Size([64]), seed=problem.seed),
                objective=GenericMCObjective(lambda samples, X=None: samples[..., 0]),
                constraints=[
                    lambda samples: samples[..., 1] - problem.maximum_peak_memory_mib,
                    lambda samples: problem.minimum_quality - samples[..., 2],
                ],
                prune_baseline=False,
                cache_root=False,
            )
            choices = torch.tensor([o.coordinates for o in options], dtype=torch.double)
            selected, acquisition_value = optimize_acqf_discrete(
                acquisition, q=1, choices=choices, max_batch_size=64
            )
            index = ((choices - selected[0]) ** 2).sum(-1).argmin().item()
            with torch.no_grad():
                posterior = model.posterior(choices)
                means, sd = posterior.mean, posterior.variance.clamp_min(0).sqrt()
                predictions = [
                    {
                        "candidate_ref": o.candidate_ref,
                        "measured": False,
                        "predicted_elapsed_seconds": math.exp(-means[i, 0].item()),
                        "posterior_interval_seconds": [
                            math.exp(-means[i, 0].item() - 1.96 * sd[i, 0].item()),
                            math.exp(-means[i, 0].item() + 1.96 * sd[i, 0].item()),
                        ],
                        "predicted_memory_mib": means[i, 1].item(),
                        "predicted_quality": means[i, 2].item(),
                    }
                    for i, o in enumerate(options)
                ]
            snapshot = {
                key: _snapshot_value(key, value.detach().tolist())
                for key, value in model.state_dict().items()
            }
    finally:
        torch.set_num_threads(previous_threads)
    transferred = bool(weights[1:].sum().item())
    result = {
        "method": "rank_weighted_gp_ensemble_qLogNEI",
        "candidate_ref": options[index].candidate_ref,
        "reason": "RGPE_NUMERICAL_SUGGESTION" if transferred else "TARGET_ONLY_QLOGNEI_FALLBACK",
        "fallback_reason": None
        if transferred
        else (
            "NO_INFORMATIVE_TARGET_RANKS" if not pairs else "NO_SOURCE_OUTPERFORMS_TARGET_RANKING"
        ),
        "execution_authorized": False,
        "measured": False,
        "input_digest": signature(problem),
        "weights": {
            "target": weights[0].item(),
            **{t.workload_signature: weights[i + 1].item() for i, t in enumerate(problem.sources)},
        },
        "rank_diagnostics": {
            "informative_ordered_pairs": pairs,
            "tie_fraction": problem.rank_tie_fraction,
            "maximum_source_rank_loss": problem.maximum_source_rank_loss,
            "target_loo_mean_loss": average[0].item(),
            "source_mean_losses": average[1:].tolist(),
            "eligible_sources": eligible,
            "tie_rule": "target wins equal posterior ranking losses",
        },
        "acquisition_value": acquisition_value.item(),
        "predictions": predictions,
        "planning_seconds": time.monotonic() - started,
        "historical_source_wall_seconds": sum(
            r.evaluation_wall_seconds for t in problem.sources for r in t.observations
        ),
        "new_target_wall_seconds": sum(
            r.evaluation_wall_seconds for r in problem.target.observations
        ),
        "surrogate": {
            "training_run_ids": [r.attempt_id for r in problem.target.observations],
            "source_run_ids": [r.attempt_id for t in problem.sources for r in t.observations],
            "source_normalization": source_snapshots,
            "target_center": center,
            "target_scale": scale,
            "feature_names": list(problem.feature_names),
            "target_loo": folds,
            "state_dict": snapshot,
            "snapshot_digest": signature(snapshot),
            "botorch_version": botorch.__version__,
            "torch_version": torch.__version__,
            "seed": problem.seed,
            "posterior_samples": problem.posterior_samples,
            "numerical_diagnostics": [
                {"category": w.category.__name__, "message": str(w.message)[:512]}
                for w in diagnostics
            ],
            "validation_status": "uncalibrated posterior; source eligibility is an operational heuristic",
        },
    }
    signature(result)  # Reject nonfinite outputs instead of emitting unusable evidence.
    return result
