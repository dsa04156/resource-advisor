# 05. 네트워크·데이터 위치

## 1. 원칙

- 링크 비용은 기존 [`placement.py`](../../../src/resource_advisor/placement.py)의 **측정된 방향 링크**만 쓴다. 미측정·역방향 전용·만료 링크는 unknown이며 0이 아니다. 같은 호스트도 측정 없이는 무료가 아니다.
- Kubernetes/Kueue TAS topology label, Slurm topology tree, NIC link speed는 측정 throughput이 아니다.
- 데이터 위치는 `DataLocation`(dataset version, 크기, 원천 endpoint, storage 종류, **접근 허용 노드**, provenance, 관측 시각)으로 operator가 등록한다. 같은 workload·dataset version에 둘 이상이면 `DATA_LOCATION_AMBIGUOUS`로 실패한다.

## 2. 추천에서의 사용

- hard filter: `DataLocation`이 있으면 허용 목록에 없는 노드는 `DATA_ACCESS_DENIED`(pool은 허용 member만 남김). 정책과 무관하게 B3–B5에 적용된다.
- B5 `profile_queue_network`: `TopologySnapshot`(정책에 ref 고정)에서 원천 endpoint → 후보 노드 endpoint 링크로 `overhead + bytes / bandwidth`를 계산하고, profile의 **별도 측정된 `data_staging` 값을 이 추정으로 대체**한다(더하지 않음). pool은 모든 member 링크가 측정돼 있어야 하며 가장 느린 값을 쓴다.
- `DataLocation` 없음, endpoint 미등록, 링크 미측정·만료 → `NETWORK_ESTIMATE_UNKNOWN` → `ESTIMATE_UNAVAILABLE`.
- `staging_boundary=not_required`(데이터를 읽지 않는 workload)만 전송 0을 쓰며 `DATA_NOT_REQUIRED`로 기록한다.

## 3. 측정 도구 (명시 허가된 경로만)

```bash
# dry-run(기본): 실행할 명령과 상한만 출력
rtk proxy uv run --locked python examples/hetero_collect.py link \
  --url http://<허가된 object> --source <endpoint> --target <endpoint> --evidence-ref <ref>
# 실제 측정: --execute (단일 stream, 동시성 1)
```

상한: 객체 ≤1 GiB(range read), ≤60초, rate limit ≤1 GiB/s, 동시성 1. rate cap에 닿으면 `rate_capped=true`이며 결과는 경로 용량이 아니라 **하한**이다. 출력의 `link`만 `TopologySnapshot.links`에 넣고 `provenance`는 evidence로 보관한다. 측정은 반드시 후보 노드에서 실행해야 그 노드 방향 링크가 된다(다른 호스트에서 잰 값으로 대체 금지).

## 4. 오늘 상태

측정된 링크와 `DataLocation`이 없다. 따라서 B5와 E3는 [07](07-EXPERIMENT-PLAN.md)에서 BLOCKED이다.
