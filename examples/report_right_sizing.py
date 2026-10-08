"""Render descriptive comparison tables directly from strictly audited evidence."""

import argparse
import json
from pathlib import Path

from audit_right_sizing_cost import audit_cost
from audit_right_sizing_reference import audit as audit_extension
from audit_right_sizing_trial import audit, compare_reference


def render(root, cost_capture):
    def read(name):
        return json.loads((root / name).read_text())

    primary, plan = read("right-sizing-gpu-v1.json"), read("right-sizing-trial-plan-v1.json")
    reference, reference_plan = (
        read("right-sizing-reference-recovery-v2.json"),
        read("right-sizing-reference-recovery-plan-v2.json"),
    )
    extension, extension_plan = (
        read("right-sizing-reference-v3.json"),
        read("right-sizing-reference-plan-v3.json"),
    )
    checked = audit(primary, plan)
    earlier = compare_reference(primary, plan, reference, reference_plan)
    fresh = audit_extension(primary, plan, extension, extension_plan)
    costs = audit_cost(json.loads(cost_capture.read_text()), root)
    comparisons = [c for c in earlier["comparison"] if c["workload"] == "W1"] + fresh["comparison"]
    lines = [
        "# 실제 right-sizing 비교 결과",
        "",
        "이 표는 raw evidence와 auditor에서 자동 생성한다. 전체 완료 또는 최적화 성공을 선언하지 않는다.",
        "새 Slurm approved-feedback 실험은 controller 연결 복구가 필요하다.",
        "",
        "## Static / Random / qLogNEI",
        "",
        "각 arm의 main 실행은 독립 native Job 3개다. 두 탐색은 각각 같은 5-probe 예산을 사용했다.",
        "시간은 W1의 100회 matmul 합계, W2의 12개 CNN block 합계다.",
        "",
        "| 작업 | 방법 | 선택 CPU / memory | main 평균 (s) | Static 대비 평균 차이 |",
        "|---|---|---|---:|---:|",
    ]
    for row in checked["main_execution"]:
        selected = next(
            (
                s["selected_candidate"]
                for s in checked["search_comparison"]
                if s["workload"] == row["workload"] and s["strategy"] == row["arm"]
            ),
            "cpu1-mem2048",
        )
        mean = row["mean_objective_seconds"]
        relative = row["relative_to_static_mean"]
        lines.append(
            f"| {row['workload']} | {row['arm']} | {selected} | {mean:.9f} | {relative * 100:+.4f}% |"
        )
    lines += [
        "",
        "Random과 BO가 두 작업에서 같은 구성을 골랐다. 평균 차이를 BO의 인과적 성능 우위나 통계적 유의성으로 해석하지 않는다.",
        "",
        "| 작업 | 방법 | probes / 서로 다른 후보 | 독립 확인 Jobs | study elapsed (s) | model/planning (s) | 실제 BO 선택 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in checked["search_comparison"]:
        lines.append(
            f"| {row['workload']} | {row['strategy']} | {row['probes']} / {row['unique_probed_candidates']} | {row['confirmation_jobs']} | {row['study_elapsed_seconds']:.3f} | {row['planning_seconds']:.6f} | {row['actual_bo_choices']} |"
        )
    lines += [
        "",
        "## 나중에 측정한 전체 후보 reference",
        "",
        "Grid는 전체 6개 후보의 3회 독립 확인이다. Equal-budget 탐색 경쟁자가 아니며 search/main 이후에 측정했다.",
        "W1은 v2, W2는 다음 날의 v3이다. 날짜별 환경 변동을 통제한 동시 oracle이 아니다.",
        "",
        "| 작업 | reference 완료 | 후보 | 독립 확인 평균 (s) |",
        "|---|---|---|---:|",
    ]
    for name in ("W1", "W2"):
        row = next(c for c in comparisons if c["workload"] == name)
        if not row["reference_complete"]:
            lines.append(f"| {name} | 아니오 | 미완료, distance unknown | — |")
        for candidate, mean in sorted(row["reference_means_seconds"].items()):
            lines.append(f"| {name} | 예 | {candidate} | {mean:.9f} |")
    lines += ["", "| 작업 | 방법 | 선택 | reference 최소 평균과의 거리 |", "|---|---|---|---:|"]
    for row in comparisons:
        value = row["relative_distance_to_measured_reference"]
        distance = f"{100 * value:.4f}%" if value is not None else "unknown"
        lines.append(
            f"| {row['workload']} | {row['strategy']} | {row['selected_candidate']} | {distance} |"
        )
    lines += [
        "",
        "## 초기 비용을 포함한 실제 N=3",
        "",
        "Qualification + profiling/confirmation + 실제 main 실행을 합한다. Model/planning은 study elapsed에 이미 들어 있어 중복 가산하지 않는다.",
        "Latency cost는 개별 Job/API latency 합이며 전체 연구 wall-clock과 다르다.",
        "",
        "| 작업 | 방법 | 누적 latency cost (s) | GPU 예약초 | CPU core초 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in checked["cumulative_measured_cost"]:
        if row["N"] == 3:
            lines.append(
                f"| {row['workload']} | {row['arm']} | {row['wall_seconds']:.6f} | {row['device_seconds']:g} | {row['cpu_core_seconds']:g} |"
            )
    lines += [
        "",
        "**Random과 BO 모두 실제 N=1..3에서 profiling 비용을 회수하지 못했다.** 측정하지 않은 반복 횟수까지 손익분기를 외삽하지 않는다.",
        "",
        "## 추가 reference 비용과 보존한 실패",
        "",
        f"W2 v3은 새 qualification을 포함해 {fresh['fresh_native_jobs']} native Jobs, {sum(fresh['supplementary_reservation_by_unit'].values()):g} GPU 예약초, {fresh['cpu_core_reservation_seconds']:g} CPU core초, 실제 protocol {fresh['protocol_wall_seconds']:.3f}초다.",
        "전체 연구에는 실패한 v1/v2 reference와 v3의 모든 예약 비용을 함께 포함한다.",
        "",
        f"- GPU: {costs['native_gpu_jobs']} Jobs / {costs['gpu_reservation_seconds']:g} 예약초",
        f"- NPU: {costs['native_npu_jobs']} Jobs / {costs['npu_reservation_seconds']:g} 예약초",
        f"- CPU: {costs['cpu_reservation_core_seconds']:g} core초",
        f"- GPU 연구 Jobs의 host memory: {costs['gpu_attempts_host_memory_reservation_mib_seconds']:g} MiB초",
        f"- 실패한 GPU Jobs: {costs['failed_gpu_jobs']}개, 비용 포함",
        f"- GPU protocol wall 합계: {costs['measured_protocol_wall_sum_seconds']:.3f}초; calendar envelope {costs['observed_research_wall_envelope_seconds']:.3f}초는 실험 사이 중단 시간도 포함",
        "",
        "GPU/NPU 예약초는 같은 경제적 비용이 아니며 GPU busy time도 아니다. Image/fixture 준비, 에너지, shared service CPU 비용은 이번 합계에서 unknown이다.",
        "Hailo qualification/승인 feedback은 별도 qualified ResNet50 계약이다. W1/W2와 NOT_COMPARABLE이며 GPU↔NPU 속도 순위를 주장하지 않는다.",
        "",
        "## 원시 근거와 그림",
        "",
        "- [Primary raw](evidence/right-sizing-gpu-v1.json), [원래 계획](evidence/right-sizing-trial-plan-v1.json)",
        "- [W1 v2 / 실패한 W2 v2](evidence/right-sizing-reference-recovery-v2.json)",
        "- [W2 v3 raw](evidence/right-sizing-reference-v3.json), [v3 계획](evidence/right-sizing-reference-plan-v3.json), [v3 audit](evidence/right-sizing-reference-audit-v3.json)",
        "- [전체 비용 입력](evidence/right-sizing-total-cost-capture-v2.json), [전체 비용 audit](evidence/right-sizing-total-cost-audit-v2.json)",
        "- [누적 비용 그림](evidence/right-sizing-figures-v3/right-sizing-cost-v3.png), [전체 후보 그림](evidence/right-sizing-figures-v3/right-sizing-reference-v3.png)",
        "- [Claim audit](right-sizing-claim-audit.md): 새 Slurm loop, 같은-task GPU/NPU ranking, BO 우위, calibrated forecast, net benefit은 미증명",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cost_capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    text = render(args.cost_capture.parent, args.cost_capture)
    with args.output.open("x") as stream:
        stream.write(text)
    print(f"Wrote audited descriptive report: {args.output}")
