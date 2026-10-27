# 증거 색인

[SOURCE-MATERIAL.md](SOURCE-MATERIAL.md)에 쓴 수치와 주장을 원본에 연결한다. 경로는 이 폴더 기준 상대경로.
증거별 시점은 다르며 현재 live 상태를 뜻하지 않는다. 날짜가 없는 원본도 있다.

| 주장 | 수치·조건 | 원본 |
|---|---|---|
| Slurm 폐루프 5/5 성공 | native Job 77–81, GPU1/CPU1/1GiB, 120초 한도 | [right-sizing-slurm-feedback.md](../reference/right-sizing-slurm-feedback.md) |
| GPU 예약 421초 | 89+85+83+84+80, startup·verification 포함 | [attempts CSV](../evidence/right-sizing-slurm-attempts-v1.csv), 사본 [evidence/slurm-closed-loop-20261008.csv](evidence/slurm-closed-loop-20261008.csv) |
| CPU 421 core·초, 431,104 MiB·초, wall 549.050608초, residual −0.686787% | audit 재계산 | [audit JSON](../evidence/right-sizing-slurm-audit-v1.json), [raw feedback JSON](../evidence/right-sizing-slurm-feedback-v1.json), [고정 계획](../evidence/right-sizing-slurm-plan-v1.json) |
| TTL 후 `NEEDS_RECONFIRMATION` | 추천 TTL 15분 | [pool-resume-readback-v1.json](../evidence/pool-resume-readback-v1.json) |
| K8s 5노드 본 실행 10/10 | Baseline 5 + Reuse 5, quality 1.0, 정확도 98.046875% | [결과 문서](../profile-pool-20261008-v2-results.md), [main.csv](../evidence/profile-pool-20261008-v2-results/main.csv), [summary.json](../evidence/profile-pool-20261008-v2-results/summary.json), [manifest.json](../evidence/profile-pool-20261008-v2-results/manifest.json) |
| 162작업, K8s 143 / Slurm 19, 평균 JCT 개선 없음, 감사 중단 | 3 부하 × 3 정책 × 3회 × 6작업 | [기록 문서](../jct-profile-queue-20261008-short-v1-recorded-results.md), [작업별 CSV](../evidence/jct-profile-queue-20261008-short-v1-recorded/recorded-main.csv), [중단 기록](../evidence/jct-profile-queue-20261008-short-v1-recorded/verification-stopped.json) |
| BO 선택 이점 없음 | 동일 예산 S0/S1/S2 | [policy-comparison-v2.md](../reference/policy-comparison-v2.md) |
| Slurm high 우선순위 경로 | QOS ra-high, GPU 예약 72초, 1 Job | [slurm-priority-gateway.md](../reference/slurm-priority-gateway.md) |
| Slurm container 미지원, native runtime만 | container variant preflight 거절 | [slurm-runtime.md](../reference/slurm-runtime.md), [backends.py](../../src/resource_advisor/backends.py) |
| 운영 한계·미검증 복구 | E0–E7 전체, Slurm 복구 등 | [architecture.md](../architecture.md), [claim audit](../right-sizing-claim-audit.md) |

## 구현 코드 (아키텍처 근거)

| 파일 | 확인한 내용 |
|---|---|
| [contracts.py](../../src/resource_advisor/contracts.py) | `State` 상태기계, `WorkloadSpec`/`RuntimeVariant`(이미지 `@sha256:` 고정), `ExecutionResult` |
| [configuration.py](../../src/resource_advisor/configuration.py) | `routes` {project, cluster, backend, options}, `DUPLICATE_WORKER_ROUTE` |
| [backends.py](../../src/resource_advisor/backends.py) | `KubernetesBackend`(`suspend: true`, Kueue queue/priority 라벨), `SlurmBackend`(`sbatch --parsable`, `squeue`, `sacct`, `scancel`, `SubmissionUnknown`, container 거절) |
| [worker.py](../../src/resource_advisor/worker.py) | 원격 호출 전 intent 커밋, `SUBMISSION_UNKNOWN`, reconcile, `COLLECTING` 기한, `MLflowDelivery` |
| [service.py](../../src/resource_advisor/service.py) | `ingest`: attempt/epoch, digest, signature, work units, quality → `RESULT_INVALID` |
| [runtime_guard.py](../../src/resource_advisor/runtime_guard.py) | manifest/environment digest, architecture, 허용 command, 실행 파일 hash |

## 공식 출처 (2026-10-27 접근)

- NAIS 소개: https://www.nst.re.kr/www/contents.do?key=358
- KTV K-문샷 (2026-05-06): https://www.ktv.go.kr/content/view?content_id=753896
- 직무기술서 ZIP (연구기술직-3 플랫폼 PDF): https://www.nst.re.kr/www/downloadBbsFile.do?atchmnflNo=11753
- NAIS Seed형 지원 보도자료: https://www.nst.re.kr/www/selectBbsNttView.do?bbsNo=16&key=57&nttNo=52260

## 추가 운영 증거

| 주제 | 원본 |
|---|---|
| Slurm CPU/메모리/시간 한도와 QOS 순서 | [정책 JSON](../evidence/slurm-policy.json) |
| Slurm API 장치 권한 실패와 수정 | [실행 JSON](../evidence/slurm-api-v1.json) |
| DB 5개 테이블 지문·46개 객체·52개 추적 링크 | [복원 JSON](../evidence/persistent-postgres.json) |
| 서비스 Pod 교체·SIGTERM·6.78초 관측 | [재시작 JSON](../evidence/service-deployment.json) |
| Hailo 100장 품질·4 API 실행·20 NPU 예약초 | [플랫폼 JSON](../evidence/hailo-resnet50-platform.json), [qualification JSON](../evidence/hailo-resnet50-qualification.json) |

## 공개 저장소 원본 목록

[DATA-FILES.csv](DATA-FILES.csv)는 이 자료를 작성할 때 origin/main에 보존돼 있던
`docs/evidence/` 파일을 열거한다. 경로·형식·크기·CSV 레코드 수·SHA-256을 담는다.
경로는 이 문서 폴더 기준이다. 레코드 수가 독립 표본 수를 뜻하지 않는다.
본문의 주요 수치를 대조했고, 전체 파일을 독립 감사했다는 뜻은 아니다.
별도 로컬 작업공간의 비공개 실습·실행 상태·미커밋 파일은 이 공개 목록에 포함하지 않는다.

## 발표용 이미지

[그림 9종 목록](visuals/README.md) · [한눈에 보기](visuals/CONTACT-SHEET.png) · [전체 ZIP](NAIS-visual-assets.zip)

고해상도 PNG와 편집용 SVG, 차트 수치 CSV, 출처 목록을 제공한다.
