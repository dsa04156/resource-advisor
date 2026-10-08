# NPU lab integration

[plugins.example.json](plugins.example.json)은 Intel NPU와 RK3399Pro USB device
registration 예시다. node selector의 `REPLACE_*`를 운영자가 private configuration에서
치환한다. 그대로 적용하면 실제 lab node를 선택하지 않는다. 고정 upstream image
digest를 사용하며 기존 cluster나 driver를 업그레이드하지 않는다.

등록 count는 모델 지원이 아니다. 별도로 compiled model·입력 digest·runtime·quality
qualification과 `build_npu_target.py`의 immutable contract가 필요하다. MXQ/RKNN/HEF,
공급사 SDK, private image registry와 node 이름은 이 저장소에 넣지 않는다.

## Rockchip experimental host bridge

RKNN1.7.1의 pod-owned proxy에서 재실행 hang을 관측했다. 제한된 lab에서는 단일
host proxy2.1 + host-network client의 연속 실행을 검증했다. 공급사의 공식 Docker
recipe와 다른 local adaptation이며 multi-tenant isolation을 제공하지 않는다.

1. 실행 중인 해당 NPU 작업이 없는지 native queue에서 확인한다.
2. proxy2.1 binary SHA256이 `c6961f392030a71272e53263246f688ad74ea7410d302c9ba9988cd5e5d30b68`인지 확인한다.
3. 기존 binary/boot 설정을 백업한다. `/usr/bin/npu_transfer_proxy`를 덮어쓰지 않는다.
4. 별도 `/usr/local/libexec/resource-advisor/npu_transfer_proxy-2.1.0`와
   [service 예시](resource-advisor-rockchip-transport.service)를 사용한다.
   기존 proxy autostart 한 줄만 중복 실행되지 않게 조정한다.
5. 실행 이미지의 `/opt/rockchip/transport.json`에는
   `mode:host-persistent`와 위 digest를 운영자가 attestation한다.
6. 전용 `KubernetesBackend` route의 `host_network_variants`에 정확한 variant ref와
   environment digest를 private worker configuration으로 넣는다. 이 전용 route는
   legacy pod-owned variant를 거절한다. 일반 backend의 Pod network 설정은 유지한다.
7. 새 Pod와 API 연속 실행을 모두 확인한다. driver/firmware fingerprint가 완전하지
   않으면 manual observe만 사용하며 자동 performance reuse는 차단한다.

USB re-enumeration 뒤 stale device path/registration을 관측했다. idle 시 plugin
재등록이 필요할 수 있다. USB reset을 자동 retry 기능으로 넣지 않았다. reset이나
proxy 소유권 변경은 활성 업무 작업에 수행하면 안 된다.

Rollback: 먼저 해당 lab route 제출을 중단하고 활성 NPU 작업이 없는지 확인한다.
새 service를 disable/stop한 뒤 변경한 autostart 한 줄을 백업과 대조해 복구한다.
전체 rc.local을 덮어써 다른 변경을 잃지 않는다. 기존 vendor binary는 그대로다.
worker allowlist도 제거하며 기존 실패·Job/result/accounting은 삭제하지 않는다.

## Hailo recovery

두 번째 Hailo의 resource0 원인은 물리 부재가 아니라 probe helper가 있는 emptyDir
손실이었다. idle plugin Pod를 재시작해 registration1을 복구했다. 후속 local
plugin image에는 고정 static BusyBox를 `/probe/busybox`에 넣고 probes를 그 경로로
연결했다. `/health`의 writable generation state와 기존 socket recovery는 유지한다.
공급사 driver/firmware는 변경하지 않았다. 실제 deployment는 private operator overlay에 있다.

[공식 조사와 local evidence 구분](../../docs/npu-runtime-research.md),
[실행·비용·한계](../../docs/all-accelerators.md)를 먼저 읽는다.
