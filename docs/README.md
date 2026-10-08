# 문서 안내

처음에는 아래 **5개만** 읽으면 된다.

| 내용 | 문서 |
|---|---|
| 작업 제출과 결과 확인 | [사용법](quickstart-ko.md) |
| 구성요소와 직접 만든 기능 | [플랫폼 설명](platform-explained-ko.md) |
| 지금 사용하는 GPU/NPU와 실행 범위 | [현재 장비](all-accelerators.md) |
| Static·Random·BO와 전체 비용 비교 | [비교 결과](right-sizing-comparison-results.md) |
| 연구 기여와 한계 | [기여·한계](right-sizing-contribution.md) |

구조를 더 보려면 [아키텍처](architecture.md), 주장을 검토하려면
[claim audit](right-sizing-claim-audit.md), 최초 설계와 대조하려면
[HAIRP 범위](hairp-original-scope-ko.md)를 읽는다.

중복 계획과 과거 실패·재시도 보고서를 정리했다. 세부 기술 문서는 `reference/`에 있다.
실험 문서는 작성 당시의 상태를 담으므로 현재 장비 상태는 위의 현재 장비 문서를 기준으로 본다.
필요한 세부 항목만 펼치면 된다.

<details>
<summary>실행·장비·모델 · 16개</summary>

- [실제 CPU → GPU 계산 파이프라인 결과](reference/cpu-gpu-pipeline-results.md)
- [E5 input supply versus CUDA kernel path: measured result](reference/e5-kernel-results.md)
- [Actual GPU smoke experiment — 2026-10-02](reference/gpu-experiment.md)
- [Real Hailo compiled-artifact rejection](reference/hailo-artifact.md)
- [EfficientFormer-L1: actual inference, reference agreement gate failed](reference/hailo-efficientformer.md)
- [Actual Hailo device allocation and quota checks](reference/hailo-reservation.md)
- [ResNet-v1-50 on Hailo-8](reference/hailo-resnet50.md)
- [Read-only inventory and telemetry](reference/inventory.md)
- [Explicit bindings for mounted Kubernetes runtimes](reference/kubernetes-runtime-bundles.md)
- [NPU runtime integration: primary-source notes](reference/npu-runtime-research.md)
- [코드 변경 없는 실제 CUDA 작업 관측](reference/passive-gpu-observation-results.md)
- [Observation without changing researcher code](reference/passive-observation.md)
- [Classification qualification records](reference/qualification-registry.md)
- [Right-sizing hardware evidence matrix](reference/right-sizing-hardware-matrix.md)
- [GPU thermal observations bound to an execution](reference/thermal-evidence.md)
- [Isolated checkpoint trials on Kubernetes GPUs](reference/training-isolation.md)

</details>

<details>
<summary>큐·정책·워크플로 · 13개</summary>

- [Common heterogeneous GPU pool](reference/common-gpu-pool.md)
- [Kubeflow launches; the compute backend owns the GPU](reference/kubeflow-pipeline.md)
- [Live Kueue GPU queue acceptance](reference/kueue-policy.md)
- [Native accelerator scheduling lab](reference/native-scheduler-lab.md)
- [Interactive resource request scenario](reference/queue-scenario.md)
- [Browser → API → Slurm GPU result](reference/slurm-api-results.md)
- [Native CNN result contract — October 6](reference/slurm-cnn-contract-results.md)
- [Slurm host observations in the research console](reference/slurm-inventory.md)
- [Orin Jetson runtime: strict GPU CNN qualification passed](reference/slurm-jetson-runtime-results.md)
- [Dedicated controller observation identity](reference/slurm-observer-transport.md)
- [Lab quota and priority verification](reference/slurm-policy.md)
- [Common high-priority policy through the scoped Slurm gateway](reference/slurm-priority-gateway.md)
- [Slurm runtime and result boundary](reference/slurm-runtime.md)

</details>

<details>
<summary>추천·프로파일링·비교 · 24개</summary>

- [동일 MLP 프로파일 재사용: GPU·NPU 재실험](profile-all-20261008-v3-results.ko.md)
- [Noise-aware adaptive replication](reference/adaptive-replication.md)
- [Real GPU recommendation → approval → independent result](reference/approved-gpu-demo.md)
- [Cooperative phase diagnostics and the live CNN trial](reference/bottleneck-diagnostics.md)
- [Paired measurement calibration before R2](reference/fidelity-calibration.md)
- [Immutable fidelity spaces and mixed-workload calibration](reference/fidelity-spaces.md)
- [Actual CUDA load-context and stale-approval acceptance](reference/load-drift.md)
- [Frozen history for lookup studies](reference/lookup-history.md)
- [Multi-fidelity GP and cost-aware KG: numerical implementation](reference/mfkg-kernel.md)
- [Source-only numerical workload holdout](reference/numerical-workload-holdout.md)
- [Actual B0/B1/B2 operational GPU comparison](reference/operational-comparison.md)
- [Durable optimization studies](reference/optimization.md)
- [Completed policy comparison after the worker runtime repair](reference/policy-comparison-v2.md)
- [Preregistered fidelity qualification and the MF-KG executor](reference/qualified-mfkg.md)
- [Right-sizing lifecycle and feedback contracts](reference/right-sizing-api.md)
- [새 Hailo 실행 피드백 검증](reference/right-sizing-hailo.md)
- [Slurm 관측 → 추천 → 승인 실행 → 피드백](reference/right-sizing-slurm-feedback.md)
- [Approved input sampling and execution receipts](reference/sampling.md)
- [Actual GPU transfer comparison](reference/transfer-gpu.md)
- [R3 transfer: evidence and execution boundaries](reference/transfer.md)
- [Offline replay of the confirmation uncertainty gate](reference/uncertainty-ablation.md)
- [S5: what the recorded drift gate actually changes](reference/uncertainty-drift-ablation.md)
- [Recheck recommendation evidence and audit saved forecasts](reference/uncertainty.md)
- [Whole-workload rank holdout and saved forecast coverage](reference/workload-holdout.md)

</details>

<details>
<summary>배포·관측·운영 · 18개</summary>

- [Allocation accounting across outcomes](reference/accounting.md)
- [Actual ARM lab telemetry provisioning](reference/ansible-lab.md)
- [Result artifacts and recovery](reference/artifacts.md)
- [Verified central host telemetry](reference/central-telemetry.md)
- [Check project integrations before starting services](reference/configuration-checks.md)
- [Research resource console](reference/console.md)
- [Live E2/E3 contract boundary checks](reference/contract-boundaries.md)
- [Live adoption of static services into Argo CD](reference/gitops-adoption.md)
- [Container exit while Job completion is pending](reference/kubernetes-completion.md)
- [Container load context bound to execution results](reference/load-context.md)
- [공동 컴퓨팅 자원 운영 연습](reference/operations-ko.md)
- [Persistent metadata and verified cutover](reference/persistent-postgres.md)
- [CPU·GPU 단계와 실제 계산 작업 연결](reference/pipeline-stage-links.md)
- [Research operations console](reference/research-console.md)
- [E0–E7: design-to-evidence matrix](reference/scenario-acceptance.md)
- [Common scheduling profiles](reference/scheduling-profiles.md)
- [Supervised API and inventory](reference/service-deployment.md)
- [Worker ownership across Kubernetes and Slurm routes](reference/worker-route-ownership.md)

</details>
