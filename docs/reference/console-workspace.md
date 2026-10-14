# 작업 중심 Console

첫 사용자는 검증된 작업을 고르고 실행·결과로 이어집니다. 주 메뉴는 시작하기/작업/모델 서비스/자원/실험입니다. 기존 URL hash14개는 유지하며 관련 기능은 하위 탭과 운영·고급 도구로 연결합니다.

## 작업 흐름

홈은 첫 행동과 최근 작업을 보여줍니다. 새 작업은 서버의 curated catalog만 기본 카드에 표시하고, 정책/고정 자원/사용자 템플릿/원시 입력은 고급 영역으로 분리합니다. 제출은 기존 idempotency key와 서버 권한·큐 규칙을 그대로 사용합니다. 자동 선택은 호환성/관측된 예약 여유/정책을 따르며 실행시간 예측이나 예약 확정으로 표현하지 않습니다.

작업 목록은 표가 기본이며 검색·상태·백엔드 필터·페이지를 제공합니다. 상세는 요약/결과/실행 기록/자원·전체 기록으로 나뉩니다. 취소·재시도·MLflow 기록은 기존 API를 사용합니다. 실제 lifecycle_events를 보여주며 없는 단계 시각은 미확인으로 표시합니다.

## 이미지 체험

GET /api/v1/compute/inference-pools?page=0은 principal.project에 등록된 풀을12개씩 나눠 반환합니다. 내부 Service URL이나 자격증명은 반환하지 않습니다. 변경된 endpoint binding은 configured=false로 표시해 재등록이 필요한 풀의 실행을 막습니다. 목록은 readiness를 주장하지 않으며 선택한 풀의 health/identity를 기존 GET으로 별도 확인합니다.

현재 lab 풀은 ResNet50/CUDA ONNX/Hailo HEF의 검증된 입력/출력 계약을 따릅니다. 브라우저는 JPG/PNG/WebP 한 장≤8MiB·25MP을 RGB224×224로 stretch resize하며 투명 배경은 흰색입니다. 입력은 canvas pixels 0–255이고 서버가 네이티브 전처리를 적용합니다. 원본 이미지와 결과를 DB/스토리지에 저장하지 않습니다. 클라이언트에서 파일 선택과 결과를 유지하되 연결 해제/pagehide는 메모리·blob을 지우고 진행 요청을 abort합니다.

결과는 ImageNet 상위5개 분류/원시 모델 점수/실제 GPU 또는 Hailo 경로/라우터 구간 시간/fallback을 보여줍니다. 점수를 정확도나 정답 확률로 해석하지 않습니다. 분류명은 TensorFlow가 배포한 imagenet_class_index.json의 영어 라벨입니다. 선택한 이미지의 렌더링 예시는 새 품질 보장의 근거가 아닙니다.

풀 등록은 여전히 operator 전용이며 화면은 임의 URL/모델/이미지를 배포하지 않습니다. 기존 승인 후 모델 릴리스·staging·production 보호를 유지합니다. 새 이미지 체험은 그 릴리스와 별도 lab 기능입니다. 새 모델 lifecycle/릴리스 승인 편집 UI나 자동 재학습은 이번 범위에 포함하지 않습니다.

## 디자인 근거

- [SkyPilot 공개 작업 화면](https://github.com/skypilot-org/skypilot/tree/master/sky/dashboard): 검색/상태 필터/목록→상세
- [dstack 공개 프론트](https://github.com/dstackai/dstack/tree/master/frontend): 상세의 요약·지표·실행 기록 분리
- [ZenML 첫 안내 공식 소개](https://www.zenml.io/blog/oss-dashboard-release): 첫 행동을 단계로 안내 (2024 공식 예시)
- [GPUStack 공개 프론트](https://github.com/gpustack/gpustack-ui): 자원·모델서비스 영역만 참고

코드와 브랜드/서버는 우리 구현을 유지하며 참조 제품 코드를 이식하지 않았습니다. library icons는 lucide-static1.57.0에서 복사했고 static/icons/LICENSE에 MIT 고지를 포함합니다. 기본 스타일 토큰과 의도된 참조 차이는 .interface-design/system.md에 기록합니다.

전체 회귀·부하·장기·native 장애 주입은 사용자의 요청대로 후속입니다. 기본 API/브라우저/실제 추론·현재 배포 상태 확인 결과는 완료 시 evidence에 기록합니다.
