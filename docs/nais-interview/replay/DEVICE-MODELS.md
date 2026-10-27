# 기록된 장치 모델 표시

노드 별칭은 기존 exporter의 매핑을 유지한다. 실제 노드가 해당 프레임에 관측된 뒤에만 장치 모델을 표시하며, 배치 이전에는 장치 배치 미관측으로 표시한다.

| 공개 별칭 | 장치 모델 |
|---|---|
| GPU-A | NVIDIA RTX 5060 Ti |
| GPU-B | NVIDIA RTX 5080 |
| GPU-C | NVIDIA GB10 |
| JET-A | Jetson Orin Nano |
| JET-B | Jetson AGX Orin |
| NPU-A | Hailo-8 |
| NPU-B | Mobilint ARIES2 |
| SLURM-A | Jetson Orin (Slurm) |

Mobilint ARIES2는 별도의 2026-10-08 성공 카드이며 기존 10건에 합산하지 않는다.

## 근거

비공개 실행 inventory의 node_ref를 exporter 별칭과 대조하고, 등록된 CUDA capability의 장치 모델·Jetson variant 및 GPU source job의 모델을 교차 확인했다. 원본 호스트 이름·접속 정보는 게시하지 않는다. Slurm 장치는 기록에서 확인된 Jetson Orin 수준으로 표시한다.

| 로컬 원본 역할 | SHA-256 |
|---|---|
| fleet_batch-latest.json | `2449294738f393ca1c435a8931eed6ce209e57b2c26c2f881096326f60db9927` |
| latest.json | `cdd8e56d22d3dcbfdd25beea7f0daa4fda84c66f3ed15dedacf4feb35d19d91f` |
| cuda-smoke-auto-v1.json | `6817e98b49c75c1fdf0381f9e8040de6e48bf5695208fc1a82990a40f58a435d` |
| gpu-source-job.json | `cbbcb9894e3926dcd9101b0bf8102cc1e04e6f449a3961af55de68eb5dd5d710` |

NPU-B는 2026-10-08 Mobilint 성공 기록의 node_alias npu-1에 부여한 공개용 표시 별칭이다. 요청 11은 이 합본 표의 표시 번호이며 10월 7일 재생 원본의 요청 번호가 아니다. 완료 표의 합계는 두 날짜의 11건을 집계한다.
