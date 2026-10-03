# Actual GPU transfer comparison

This is a completed, preregistered functional comparison on one RTX 5080.
It verifies actual random search, fixed-fidelity qLogNEI, history-guided warm start
and RGPE through admission, execution, collection and independent confirmation.
Two study blocks do not establish statistically powered method superiority.

The [frozen plan](evidence/transfer-gpu-plan-v2.json),
[complete raw evidence](evidence/transfer-gpu.json),
[per-Job CSV](evidence/transfer-gpu.csv) and
[audited summary](evidence/transfer-gpu-summary.json) retain all attempts.
The [earlier quota stop](evidence/transfer-gpu-quota-stop.json) is separate and
includes its unsuccessful admission requests and successful F0 costs.

## Workload and design

Every Job uses the same generated fixed-weight three-convolution CNN and one
physical GPU allocation. PyTorch 2.8.0+cu128, CUDA 12.8, driver 595.84 and the
immutable runtime source were held fixed. The source inputs are 128×128 and
192×192; the held-out target input is 256×256. Seed is 20261003.

The approved configurations request 0.5, 1 or 2 host CPU cores and 2 GiB memory.
PyTorch threads are `max(1, int(requested_cpu))`; the fractional CPU limit is
enforced by the container quota. This tunes the CPU support for a CUDA workload,
not GPU count, GPU sharing or cross-accelerator placement.

Reported objective time is the sum of 12 measured blocks, each containing CPU
preprocessing, host-to-device transfer, 32 CUDA forwards and synchronization.
It is not individual inference latency. CPU reference generation and output
validation are outside the objective and inside Job cost. Numerical agreement
with the deterministic CPU reference is 1.0 for every Job; this is not
trained-model accuracy on a research dataset.

Each source receives two balanced three-configuration probe blocks and three
independent confirmation Jobs per configuration. All 18 source confirmation
profiles are frozen before target tuning; source pilot timings and F0 timings
are not substituted as target observations. RGPE first spends six target Jobs
on balanced checks. Warm start uses source rank for first coverage and then
target-only BO. All methods have the same caps: eight probes, 1,200 seconds
study wall time, 900 physical GPU seconds and 360 seconds reserved for final
validation. Final baseline/finalist confirmation uses three new Jobs each.

The two method blocks follow the preregistered randomized sequence. Study is
the independent unit for policy comparison; Job is the within-configuration
repeat. The 12 forward blocks are subsamples, not 12 independent experiments.

## Observed selections and cost

| Block | Policy | Selected CPU | Confirmation total (ms) | GPU reserved (s) | Study wall (s) | Post-hoc regret |
|---|---|---|---:|---:|---:|---:|
| 1 | qlognei | cpu2 | 261.051 | 38 | 342.407 | 0.000% |
| 1 | rgpe | cpu2 | 261.146 | 37 | 345.615 | 0.000% |
| 1 | history_warm_start | cpu2 | 260.653 | 39 | 339.907 | 0.000% |
| 1 | random | cpu2 | 260.671 | 36 | 338.035 | 0.000% |
| 2 | rgpe | cpu2 | 260.755 | 37 | 341.568 | 0.000% |
| 2 | qlognei | cpu2 | 261.088 | 43 | 353.028 | 0.000% |
| 2 | random | cpu2 | 260.883 | 41 | 336.474 | 0.000% |
| 2 | history_warm_start | cpu2 | 260.870 | 39 | 359.888 | 0.000% |

The post-hoc oracle ran only after every target study finished. Its independent
confirmation means are cpu0-5: 397.774 ms, cpu1: 263.624 ms, cpu2: 261.235 ms. The displayed regret is the selected
configuration's later oracle mean divided by the smallest later oracle mean,
minus one. It describes this finite comparison; it is not true-regret estimation
under a stationary population. Drift and measurement uncertainty remain possible.

The trial contains 157 API-controlled GPU Jobs: 30 source,
112 target
and 15 post-hoc oracle Jobs. Full source characterization cost is
82 reserved GPU
seconds and 729.912 study
wall seconds. The selected source profiles alone account for
150.031 Job
wall seconds; that smaller figure must not stand in for the whole source search.

Target studies reserve 310 GPU seconds;
the oracle reserves another 45.
Qualification reserves 14 seconds under the
stopped first protocol and 24 under the executed
second protocol. Total recorded physical GPU reservation is
475 seconds. Reservation is not GPU
utilization, objective time or energy. The three initially unadmitted CPU-4
requests had no Pod and reserved no GPU. The first report's 9 container seconds
were corrected to 14 reservation seconds using PodScheduled as the start;
both boundaries remain recorded.

Study wall time includes control-plane polling and collection gaps between
short Jobs. No attempt was made to tune worker polling mid-protocol. Source
construction, initial discovery, image building and external setup should be
included when deciding whether this experiment is economical; only measured
costs above are quantified here. No production break-even count is claimed.

All eight target studies selected `cpu2`, so this fixture demonstrates no
configuration-selection advantage for transfer over cold-start BO or random
search. Each still spent eight probes and six confirmations. Smaller observed
cost differences do not establish a method advantage with only two blocks and
scheduler-time granularity. The full historical cost further weakens any
cold-start economy claim. Improvement over the CPU-1 baseline is a different
question from improvement over another tuning method.

## Recorded transfer behavior

| Target block / update | Target weight | Source weights | Fallback |
|---|---:|---|---|
| target-0-rgpe / 6 observations | 0.140625 | 0.859375, 0.000000 | none |
| target-0-rgpe / 7 observations | 0.140625 | 0.859375, 0.000000 | none |
| target-1-rgpe / 6 observations | 0.156250 | 0.843750, 0.000000 | none |
| target-1-rgpe / 7 observations | 0.156250 | 0.843750, 0.000000 | none |

Source columns follow the immutable source order in the raw model evidence;
workload signatures and every contributing attempt ID remain available there.
Weights are mixture contributions, not confidence or accuracy. A zero weight
alone does not prove harmful transfer. Wrong-ranking, expired-source and model
failure fallback are separately tested with explicit synthetic/scheduler-double
fixtures; this successful hardware comparison must not be relabeled as a live
negative-transfer failure injection.

## Integrity checks and reproduction

All 157 API results were matched to one successful Kubernetes Pod and Kueue admission,
a validated PostgreSQL result, one usage row, one MLflow run and matching
S3/API/MLflow artifact bytes. Thermal and phase traces match the actual Job log
and result digest. Only independent confirmations produced historical profiles.
Every recorded trace passed its fixed policy; maximum
observed temperature was 54°C.
Total NVML query overhead was 0.439204
seconds. Bracketing samples are not continuous telemetry or long-duration
thermal qualification. No integrated energy figure is inferred from them.

The offline audit checks the full source cohort, study order/seeds/budgets,
attempt ownership, complete plan/accounting coverage, no future/source/
confirmation/oracle leakage into target GP fits, and feasible post-hoc repeats:

```sh
uv sync --locked --extra optimizer --extra artifacts
uv run python examples/evaluate_transfer.py \
  --input docs/evidence/transfer-gpu.json \
  --output /tmp/transfer-audit.json
```

The output path must not already exist. This reruns the audit, not the GPU trial.
To run a new hardware trial, freshly qualify your runtime and each candidate,
register immutable workload/variant/capability and TransferSpace contracts,
preregister budgets and sequence, and submit source `grid_characterization`
studies through the profiling API. Bind all source confirmation Job IDs through
the transfer-evidence endpoint, then submit the planned target strategies with
stable idempotency keys. Run a separate target grid only after tuning completes.
The published plan, workload specifications, exact model/input digests, strategy
seeds and per-plan choices define this trial; site credentials and routing are
external. No existing node or SDK is qualified merely by replaying these JSONs.

## Interpretation and remaining scope

This establishes a working single-device transfer loop and a bounded cost
comparison. Configuration coverage is deliberately small, so exhaustive
measurement remains a credible baseline. The oracle is an evaluation expense,
not a hidden source of optimizer training data. A separately randomized,
equal-budget grid policy would be needed for a direct policy comparison with
exhaustive search. More models, datasets, runtimes and method blocks are needed
before generalizing effectiveness. Live harmful-transfer injection, source
amortization over repeated production work, real task accuracy and different
accelerators remain unverified. GPU sharing and NPU training are not covered.

A subsequent [offline workload/forecast audit](workload-holdout.md) reuses these
results without new Jobs. It separates source-only whole-shape rank holdout
from target-adapted chronological intervals; all four saved RGPE intervals missed
their subsequent observations. No operational uncertainty guarantee is established.
