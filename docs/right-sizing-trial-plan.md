# Prospective right-sizing lifecycle trial v1

The [frozen JSON plan](evidence/right-sizing-trial-plan-v1.json) fixes new
experiment `right-sizing-20261007-v1` before any qualification or performance
observation. Existing trials remain separate. This is a bounded functional and
descriptive comparison, not a powered optimizer-superiority study.

W1 is a2048×2048 FP32 matrix benchmark with100 synchronized operations after
three warmups. Its numerical tolerance agreement minimum is0.99. W2 is the
existing generated three-convolution CNN with a new seed/model digest, twelve
preprocess/transfer/32-forward/synchronize blocks and agreement1.0. W3 retains
the exact qualified Hailo ResNet50 artifact/100-image/quality contract. Its
observe/lookup/approve/independent execution path is separate: no arbitrary pilot
entrypoint or model conversion is introduced. No qualified CUDA equivalent of
that compiled-model precision and input contract currently exists, so GPU/NPU
performance pooling is explicitly `NOT_COMPARABLE`.

Each GPU workload has six host-resource configurations: CPU0.5/1/2 crossed with
host memory1024/2048MiB, one physical GPU, fixed model/data/seed/precision/work.
The reasonable baseline is CPU1,2048MiB. Threads remain the documented
`max(1,int(CPU))` runner policy, so CPU request and derived threads are a joint
support configuration. They are not independently identifiable effects. One
qualified RuntimeVariant per workload is shared across its CPU/memory coordinates.

Random and qLogNEI receive identical five-probe/900s/900GPU-s caps, with360s
protected for new three-repeat baseline/finalist confirmation. Five probes cannot
measure all six configurations. Candidate validity and actual coverage are
reported; selecting a candidate is not finding the unobserved global optimum.
Model failures remain named fallbacks, and aborted studies are not replaced.

After search, three seeded randomized main-execution blocks compare valid static
baseline, explicitly approved random recommendation and approved BO recommendation.
An abstaining arm receives no invented execution or replacement recommendation.
Only afterward does a separate grid characterize all six configurations, with
two probes and three new confirmations each. This is a later finite reference,
not an equal-budget grid competitor or data available to earlier decisions.

The experimental-design skill's seeded block randomizer generated the archived
study and main sequences; its script hash and NumPy/Pandas versions are recorded.
Study is the search comparison unit; separately submitted Job is the main
execution repeat. Inner forward samples are subsamples. One study per strategy
and three main blocks are structural functional checks, not statistically powered
replication. No hypothesis-test superiority or general effectiveness conclusion
will be drawn from this trial.

Qualification, profiling, model planning, confirmation, failed/canceled allocation,
main execution and later-reference costs are kept separate. Actual cumulative
cost curves stop at the three measured main uses; no break-even extrapolation.
Setup/image/preparation values not measured remain unknown. GPU tensor allocation
is not host RSS; a host OOM is censored failure and still incurs reservation cost.
All source/result/native/run IDs and signatures must be captured by the auditor.

The maximum is131 native Jobs across the declared GPU qualification/search/main/
reference and five Hailo Jobs, within7200 protocol seconds. Stop a workload on
failed fresh qualification; keep the result/cost and do not relax its quality
threshold. Existing quotas, drivers, scheduler policies and pressure guards stay
unchanged. Slurm controller access currently times out; new Slurm profiling and
feedback are `BLOCKED` until authenticated controller connectivity and normal
status/accounting are confirmed. Existing Slurm success/recovery evidence remains.
