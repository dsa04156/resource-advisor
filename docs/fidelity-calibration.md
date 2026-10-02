# Paired measurement calibration before R2

This increment implements a prerequisite to R2: a registered, randomized paired
measurement experiment whose executions use the normal Compute API, Kueue,
ledger and MLflow path. It is **fixed paired replication calibration**, not an
adaptive replication policy and not multi-fidelity Bayesian optimization.
`mfkg` remains disabled until its separate bias model, qualification and joint
configuration/fidelity acquisition path are implemented and validated.

## Question and registered design

Can a short measurement preserve the ordering of two resource configurations
when checked against a longer measurement and an independent target repeat?

- Experimental unit: one independently submitted Job. Ten or one hundred timed
  operations inside that Job are subsamples, not independent trials.
- Configurations: CPU requests 1 and 2, one fixed physical RTX 5080, 2Gi memory.
  The runner also sets PyTorch CPU threads to the CPU count. These are linked
  configurations, not a clean causal experiment on CPU quota alone.
- F1: 10 timed repetitions after three warmups. F2: 100. F3: an independent Job
  repeating the same F2 contract. Input matrices, seed, shape 256×256, fp32,
  numerical correctness check, code and qualified runtime remain unchanged.
- Three complete blocks contain all six configuration/phase cells. The API
  freezes the seeded order before any comparison Job is submitted and enforces
  sequential completion. F3 is a reserved independent calibration repeat; it
  does not replace a later selected recommendation's final confirmation.
- Three blocks are an exploratory engineering pilot, not a powered study or a
  claim of generalization. A preregistered 5% relative tie band is descriptive,
  not a confidence interval or a statistically significant difference.
- Seed 20261002; total wall budget 900 seconds; device reservation allowance
  600 physical GPU-seconds, covering 18 × 30-second execution reservations.
  Scheduler disconnection/termination overhead can exceed these nominal limits;
  the allowance is not proof of a physical resource-release bound.

Randomized block order reduces simple phase/order confounding. It cannot remove
unobserved background interference or establish stability on other workloads,
days, accelerators or temperature regimes. This small matmul is a numerical
fixture, not trained-model accuracy or a saturated inference service benchmark.

## API and evidence binding

`POST /api/v1/compute/fidelity-plans` requires the operator role and accepts a
`FidelityPlan`. Each arm has exactly one F1/F2/F3 cell identifying a registered
workload and candidate. Every cell requires profiling consent, its own approved
baseline, current compatibility and an accelerator reservation. The first
implementation allows only repetition-count and CPU/thread-configuration
differences within one node/runtime group; training is rejected.

Use `GET /fidelity-plans/{ref}` under the same API prefix to retrieve the immutable
plan and generated schedule. For each slot in order, POST
`/fidelity-plans/{ref}/slots/{slot}`. The server derives a stable idempotency key,
so retrying a slot recovers the same Job. A later slot cannot start until the
previous one is terminal. Expired plans cannot create new jobs; existing slot
lookups still work. Each submission reuses normal compatibility checks and queue
admission rather than binding a Pod directly to a node.

`GET /fidelity-plans/{ref}/assessment` joins the registered cells to actual jobs
and validated result records. A different workload under the reserved key,
pre-registration execution, broken order, missing cell, synthetic result or
failed quality check cannot supply performance evidence. A complete assessment
is stored immutably with result digests and separate attempt IDs. Cross-project
reads and submissions are rejected.

The response reports elapsed seconds **per measured work unit**. Raw elapsed
time from 10 operations is never compared directly with time from 100 operations.
Within each block, phase-wise configuration order is compared using the fixed
tie band; reversals and unresolved comparisons remain explicit. F0 qualification
runs are excluded from this performance comparison.

## Costs and admission decision

The assessment includes observed allocation, queue, preparation, collection and
end-to-end intervals from each attempt. Per-device units are reported separately;
unknown allocation counts/durations and coarse timestamp uncertainty remain
visible. Planning reservations are not presented as actual utilization. Model
fitting, data transfer and energy are not measured in this calibration.

The runner repeats identical inputs. Rank agreement, if observed, would still
not establish a fidelity-bias model. Every such assessment therefore includes
`REPLICATION_ONLY_NOT_MULTI_FIDELITY`, with `multi_fidelity_eligible=false` and
`early_pruning_allowed=false`. Missing thermal evidence is also reported; the
absence of a temperature measurement is not proof of no throttling. The next
MF-KG implementation needs a qualified meaningful fidelity axis, explicit
measurement cost model, target-level validation and independent confirmation.

See [raw plan and observations](evidence/fidelity-calibration.json) and the
[per-execution CSV](evidence/fidelity-calibration.csv). API and contract tests
use explicit doubles; only the separately recorded lab runs are hardware evidence.

## Actual outcome — 2026-10-02

Four F0 Jobs requalified both CPU/thread settings at 10 and 100 repetitions.
The initial comparison plan then failed at its first slot **before submission**:
a copied route binding contained a different environment fingerprint from the
fresh F0 results. The backend refused it, recorded zero allocation and retained
the failure. No performance observations were collected under that plan. Only
the new variant bindings were corrected; a new immutable plan was registered
before the complete comparison began.

All 18 Jobs in the new plan succeeded. Every CPU1/CPU2 comparison in all three
phases and blocks fell within the preregistered 5% tie band. No rank reversal was
detected, but neither was a resolved ordering. This does not establish statistical
equivalence, cross-fidelity predictive accuracy or a faster configuration.
Temperature/throttling attribution remained unavailable. The assessment therefore
kept MF-KG and early pruning ineligible.

The independent usage ledger recorded **38 physical GPU reservation seconds**
for comparison jobs, and scheduler timestamps recorded **8 further GPU reservation
seconds** for the four standalone F0 checks. These scopes are separate in the
report; F0 is not discarded from cost reporting or counted as a performance
replicate. Coarse timestamps and unmeasured energy/transfer costs remain explicit.
All 18 results had one FINISHED MLflow run and byte-identical API/S3/MLflow artifact
readbacks. The final LocalQueue had no pending, admitted or reserving workloads.
