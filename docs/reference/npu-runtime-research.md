# NPU runtime integration: primary-source notes

조사일: 2026-10-08. 이 문서는 SDK·device plugin의 계약과 진단 범위를 조사한
기록이다. 공급사의 지원 설명, 소스에서 확인한 동작, 실제 장비의 실행 성공을
구분한다. 장치 등록은 모델 실행이나 품질 검증을 대신하지 않는다.

## Mobilint ARIES / qb Runtime

공식 Linux 호환표는 ARIES driver 1.11 이상과 runtime 1.0.0 이상을 같은
지원 구간에 둔다. 따라서 현재 관측한 driver 1.14 / qbruntime 1.4.0 조합은
그 표의 범위 안이다. 실제 firmware 버전도 별도로 확인해야 한다.
[ARIES 호환표](https://docs.mobilint.com/aries/en/compatibility.html)

SDK 경로는 `Accelerator` → compiled MXQ를 읽는 `Model` → `launch()` →
`infer()`다. `ModelConfig.set_single_core_mode(core_ids=[CoreId(...)])`로
정확한 local core를 지정할 수 있다. 이를 근거로 현재 runner는
Cluster0/Core0와 고정 MXQ digest·입력 shape를 확인한다.
[Programming Guide](https://docs.mobilint.com/runtime/v1.4/en/programming_guide.html),
[ModelConfig API](https://docs.mobilint.com/runtime/v1.4/doxygen/html_en/classqbruntime_1_1type_1_1ModelConfig.html)

MXQ format/runtime 호환성은 driver 호환성과 별도 계약이다. 모델 이름만으로
다른 MXQ를 재사용하지 않는다. 현재 Candy 모델의 반복 출력 일치는 runtime
repeatability 검증이며, 스타일 품질이나 분류 정확도가 아니다.
[MXQ 호환표](https://docs.mobilint.com/runtime/v1.4/en/compatibility.html),
[실행 코드](../../src/resource_advisor/npu_probe.py)

공식 1.4 release notes는 별도 `mbltml` 관리 라이브러리의 온도·전력·메모리·
활용률 API를 설명한다. API의 존재가 해당 장비에서 센서 수집과 job attribution이
검증됐다는 뜻은 아니다. 수집되지 않은 값은 unknown으로 남긴다.
[1.4 release notes](https://docs.mobilint.com/runtime/v1.4/en/release_note.html#v1-4-0)

## Intel NPU / OpenVINO

공식 NPU 경로는 `core.compile_model(model, "NPU")`다. OpenVINO는 별도로
AUTO/HETERO 모드를 제공하므로 실제 NPU 검증에는 자동 CPU fallback을 허용하지
않는 직접 NPU 지정과 execution-device 확인을 사용한다. compilation 비용과
warmup, 반복 inference 비용을 분리한다. XML/BIN IR digest는 NPU compiled blob
digest가 아니다.
[NPU Device 문서](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html)

로컬 OpenVINO 2026.3.1 direct-NPU 실행에서는 `EXECUTION_DEVICES`가 문자열
`"NPU"`로 관측됐다. 이 관측을 모든 plugin의 반환 타입으로 일반화하지 않는다.
runner는 문자열 또는 리스트를 명시적으로 정규화하고 CPU·혼합·빈 결과를
거절한다. 생성 CNN의 NumPy 수치 기준 통과는 그 CNN의 검증이며 임의 모델의
정확도 보장이 아니다.
[runner 및 반환 타입 회귀검사](../../src/resource_advisor/npu_probe.py),
[software test](../../tests/test_npu_probe.py)

Intel NPU plugin v0.36.0은 `npu.intel.com/accel`을 등록하며 기본
`shared-dev-num=1`이다. 실행 이미지의 UMD, host KMD와 firmware가 모두 필요하다.
device plugin은 OpenVINO나 모델을 설치하지 않는다.
[고정 release의 NPU plugin 문서](https://github.com/intel/intel-device-plugins-for-kubernetes/blob/76f8fcf655957d0121e333c91b5125abe4d7bc52/cmd/npu_plugin/README.md)

공식 release matrix는 release-0.36을 Kubernetes 1.36 지원 구간으로 표시한다.
기존 cluster에서 standalone plugin 등록·실행이 성공해도 공식 지원 범위를
확장했다고 표현하지 않는다. 현재 cluster를 업그레이드할 근거도 아니다.
[v0.36.0 compatibility matrix](https://github.com/intel/intel-device-plugins-for-kubernetes/blob/76f8fcf655957d0121e333c91b5125abe4d7bc52/README.md#supported-kubernetes-versions)

## Rockchip RK3399Pro / RKNN Toolkit Lite 1.7.1

RK3399Pro는 legacy RKNN 계열 대상이며 RKNN Toolkit2로 바꿔 쓰는 대상이 아니다.
Lite 1.7.1 §4.3은 `init_runtime(target="rk3399pro")`를 지원한다.
여러 장치에서는 `list_devices()`로 얻은 `device_id`를 지정한다. 임의 ID를
만들지 않으며 단일 장치의 default None 자체를 오류라고 단정하지 않는다.
[플랫폼 구분](https://github.com/rockchip-linux/rknn-toolkit),
[고정 1.7.1 Lite guide](https://github.com/rockchip-linux/rknn-toolkit/blob/a990fb76f5567d6783fba2fd951ccc8243bfc285/doc/Rockchip_User_Guide_RKNN_Toolkit_Lite_V1.7.1_EN.pdf)

같은 guide §2.2.2의 공식 Docker 예시는 privileged와 USB bus mount를 사용하고,
한 번에 inference container 하나만 사용하며 host의 `npu_transfer_proxy`가
실행되지 않아야 한다고 명시한다. 이 예시는 특정 Linux capability 하나가
충분하다는 보장이 아니다. 플랫폼의 제한된 device allocation·보안 설정에서는
동일 경로를 별도로 검증해야 한다.
[Lite guide §2.2.2](https://github.com/rockchip-linux/rknn-toolkit/blob/a990fb76f5567d6783fba2fd951ccc8243bfc285/doc/Rockchip_User_Guide_RKNN_Toolkit_Lite_V1.7.1_EN.pdf)

초기화 실패에 대한 공식 점검은 host proxy 충돌, USB 읽기/쓰기 권한,
target/device ID, SDK/server/runtime/driver 조합이다. `get_sdk_version()`의
`DRV`는 **rknn_server 버전**이다. 관측된 API 1.7.1 / DRV 1.6.0을 kernel
driver 1.6.0이라고 쓰지 않는다. 공식 대응표는 SDK 1.7.1을 server/runtime
1.7.1과 연결한다. 로컬의 한 번 성공이 이 불일치 위험을 없애지는 않는다.
[고정 troubleshooting guide, printed pp.33–35](https://github.com/rockchip-linux/rknn-toolkit/blob/a990fb76f5567d6783fba2fd951ccc8243bfc285/doc/Rockchip_Trouble_Shooting_RKNN_Toolkit_V1.7.1_EN.pdf)

문서는 상세 로그를 위해 **device-side** serial/ADB 환경에서
`RKNN_LOG_LEVEL=5`와 `restart_rknn.sh`를 설명한다. host에서 이 명령을 임의로
실행하는 절차나, container 재시도만으로 USB/session 상태가 복구된다는 보장은
찾지 못했다. 과거 stuck-inference FAQ의 0.9.9 이상 권고는 현재 1.7.1 hang의
해결책을 증명하지 않는다. 펌웨어 교체·USB reset을 자동 복구로 추가할 근거로
삼지 않는다.
[troubleshooting guide, printed pp.33,38](https://github.com/rockchip-linux/rknn-toolkit/blob/a990fb76f5567d6783fba2fd951ccc8243bfc285/doc/Rockchip_Trouble_Shooting_RKNN_Toolkit_V1.7.1_EN.pdf)

공식 ResNet18 예제의 단일 입력은 top-1 class 812를 제시한다. 그 한 입력의
일치는 dataset accuracy가 아니다. 실제 runtime의 raw logit을 probability로
표시하지 않는다. 원래 성공한 F0 이미지도 새 Pod에서 다시 초기화 hang이
발생했으므로 현재 보안 설정만을 원인으로 지목할 수 없다. USB/session/server
상태는 추가 실험이 필요한 가설이다.
[공식 Lite 예제 결과](https://github.com/rockchip-linux/rknn-toolkit/blob/a990fb76f5567d6783fba2fd951ccc8243bfc285/doc/Rockchip_User_Guide_RKNN_Toolkit_Lite_V1.7.1_EN.pdf),
[현재 bounded runner](../../src/resource_advisor/npu_probe.py)

## Device registration과 socket recovery

generic-device-plugin 0.2.0은 USB vendor/product/optional serial로 장치를 찾아
매칭된 device node를 `rw`로 할당한다. `count=1`은 하나의 scheduler allocation이며
모델 초기화 성공이나 task accuracy가 아니다. 검토한 소스는 plugin socket 삭제를
검사해 process 종료로 복구를 유도한다.
[고정 USB allocation 소스](https://github.com/squat/generic-device-plugin/blob/a3d6f47ddde5cbb24cc274f4ffd2f2cdc90f7b92/deviceplugin/usb.go),
[socket lifecycle 소스](https://github.com/squat/generic-device-plugin/blob/a3d6f47ddde5cbb24cc274f4ffd2f2cdc90f7b92/deviceplugin/plugin.go)

Kubernetes 문서는 kubelet 재시작 시 plugin socket 삭제와 재등록 책임을 설명한다.
검토한 **community** Hailo plugin source revision은 Start에서 한 번 등록하고
device path health를 확인하지만 socket 재등록 loop는 없다. 이 소스와 설치된
image revision의 동일성은 별도 검증 대상이다. process가 살아 있는지만 검사하면
등록 손실을 놓칠 수 있다는 설계 위험으로 사용한다.
[Kubernetes device plugin lifecycle](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/),
[Hailo plugin source 3c2f8ca](https://github.com/gllm-dev/hailo-device-plugin/blob/3c2f8ca6b260a0f4c68815277222adf3de976efb/internal/plugin/plugin.go)

## 로컬 증거와 이 조사 문서의 경계

초기 조사 당시 Hailo 두 장치, Mobilint Candy와 Intel 생성 CNN의 native API 실행은
성공했고 Rockchip은 후속 초기화 hang으로 판정을 보류했다. 이후 별도
[실행 기록](../all-accelerators.md)에 host-persistent proxy2.1 + allowlisted hostNetwork
client의 fresh Pod2건과 연속 API2건 성공을 추가했다. 공식 Docker recipe가 아닌
trusted lab adaptation이며 firmware/isolation/장기간 안정성은 미검증이다.
SDK1.6.0 matched-version 실험은 ARM64/NTB 요구와 현재 USB 구성 불일치로 실패했다.
이 조사 문서는 장비에 직접
작업을 제출하지 않았으며 전체 비용·native UID·품질 evidence를 재감사한 결과를
대신하지 않는다. 실패와 timeout 비용은 성공 결과와 함께 보존해야 한다.

각 SDK가 서로 다른 model/input/precision/quality 계약을 실행하므로 현재
결과를 GPU↔NPU 성능 순위로 합치지 않는다. DEEPX의 실제 탐지·runtime·모델
증거가 없으면 blocked이며 AMD·다중 GPU·NPU 학습 지원을 이 조사로 추가하지
않는다. 모델 후보 생성은 기존 [immutable contract renderer](../../examples/build_npu_target.py)를
사용하되 native execution과 quality qualification은 별도 gate다.
