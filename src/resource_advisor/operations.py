"""Scoped operational read model; it never changes scheduler policy or retries jobs."""

from datetime import timedelta

from sqlalchemy import func, select

from .accounting import interval, summarize
from .contracts import TERMINAL, now
from .store import jobs, usage

GUIDANCE = {
    "Resources": ("자원 확보 대기", "요청 장비의 예약량과 실행 중 작업을 확인하세요."),
    "Priority": ("앞선 우선순위 작업 대기", "큐 정책과 먼저 제출된 작업을 확인하세요."),
    "QOSMaxGRESPerUser": (
        "사용자 가속기 한도 대기",
        "Slurm 계정·QOS 한도와 현재 할당을 확인하세요.",
    ),
    "QOSGrpGRES": ("그룹 가속기 한도 대기", "팀의 Slurm QOS 사용량을 확인하세요."),
    "AssocGrpGRES": ("계정 가속기 한도 대기", "팀의 Slurm 계정 사용량을 확인하세요."),
    "AdmissionPending": (
        "큐 승인 대기",
        "Kueue의 예약·승인 조건을 확인하세요. 한도 초과로 단정하지 않습니다.",
    ),
    "Unschedulable": (
        "배치 가능한 노드 없음",
        "노드 상태, 자원 요청, 장비 선택 조건을 확인하세요.",
    ),
    "ImagePullBackOff": (
        "컨테이너 이미지 가져오기 실패",
        "이미지 주소·접근 권한·레지스트리 연결을 확인하세요.",
    ),
    "ErrImagePull": (
        "컨테이너 이미지 가져오기 실패",
        "이미지 주소·접근 권한·레지스트리 연결을 확인하세요.",
    ),
    "ContainerCreating": (
        "컨테이너 준비 중",
        "지속되면 볼륨 연결과 컨테이너 런타임 상태를 확인하세요.",
    ),
    "OUT_OF_MEMORY": (
        "메모리 부족으로 종료",
        "호스트·가속기 메모리 관측과 요청량을 확인한 뒤 실행 구성을 조정하세요.",
    ),
    "OOMKilled": ("메모리 부족으로 종료", "컨테이너 메모리 한도와 사용량을 확인하세요."),
    "TIMEOUT": ("실행 시간 한도 도달", "설정한 실행 시간과 실제 실행 구간을 확인하세요."),
    "DeadlineExceeded": ("실행 시간 한도 도달", "설정한 실행 시간과 실제 실행 구간을 확인하세요."),
    "NODE_FAIL": ("실행 노드 장애", "노드 연결과 장비 상태를 복구한 뒤 새 작업을 제출하세요."),
}


def job_attention(job):
    state = job["state"]
    reason = job.get("scheduler_reason") if state == "QUEUED" else job.get("error")
    if state == "SUBMISSION_UNKNOWN":
        title, action = (
            "제출 결과 확인 중",
            "백엔드에 기존 작업이 있는지 확인하세요. 중복 제출하지 마세요.",
        )
    elif job.get("last_observation_error_at") and (
        not job.get("backend_observed_at")
        or job["last_observation_error_at"] > job["backend_observed_at"]
    ):
        title, action = (
            "백엔드 상태 조회 실패",
            "연결과 작업 조회 권한을 확인하세요. 마지막 상태는 현재 상태와 다를 수 있습니다.",
        )
    elif reason in GUIDANCE:
        title, action = GUIDANCE[reason]
    elif state == "CANCEL_REQUESTED":
        title, action = (
            "취소·자원 반환 확인 중",
            "백엔드 종료 확인 전까지 자원이 반환됐다고 판단하지 않습니다.",
        )
    elif state == "QUEUED":
        title, action = (
            "실행 대기",
            "백엔드가 세부 사유를 아직 보고하지 않았습니다. 큐·쿼터 현황을 확인하세요.",
        )
    elif state in {"VALIDATED", "RECEIVED", "SUBMITTING"}:
        title, action = "백엔드 제출 준비", "워커 처리와 백엔드 연결 상태를 확인하세요."
    elif state == "COLLECTING":
        title, action = "실행 결과 수집 중", "결과 검증과 저장이 끝나면 완료로 표시됩니다."
    elif state in {"FAILED", "RESULT_INVALID"}:
        title, action = (
            "실행 실패" if state == "FAILED" else "결과 검증 실패",
            "작업 상세의 오류 기록과 실행 환경을 확인하세요.",
        )
    elif state in TERMINAL:
        title, action = "작업 종료", "실행 결과와 기록된 자원 할당 시간을 확인하세요."
    else:
        title, action = "실행 중", "실제 사용량과 예약량을 함께 확인하세요."
    return {
        "title": title,
        "action": action,
        "reason_code": reason,
        "backend_observed_at": job.get("backend_observed_at"),
    }


def operations_snapshot(service, conn, project, inventory, counts):
    from .console import job_view

    moment = now()
    active_query = select(jobs).where(
        jobs.c.project == project, jobs.c.state.not_in(list(TERMINAL))
    )
    active = [
        job_view(service, conn, row)
        for row in conn.execute(
            active_query.order_by(jobs.c.body["created_at"].as_string()).limit(50)
        ).mappings()
    ]
    failed_query = select(jobs).where(
        jobs.c.project == project,
        jobs.c.state.in_(["FAILED", "RESULT_INVALID"]),
        jobs.c.body["finished_at"].as_string() >= (moment - timedelta(hours=24)).isoformat(),
    )
    failures = [
        job_view(service, conn, row)
        for row in conn.execute(
            failed_query.order_by(jobs.c.body["finished_at"].as_string().desc()).limit(12)
        ).mappings()
    ]
    failed_total = conn.execute(
        select(func.count()).select_from(failed_query.subquery())
    ).scalar_one()
    for job in active + failures:
        job["attention"] = job_attention(job)
        job["since_submission_seconds"] = interval(job.get("created_at"), moment.isoformat())
    records = [
        dict(row)
        for row in conn.execute(select(usage).where(usage.c.project == project)).mappings()
    ]
    groups = summarize(records)
    known_wait = [
        r["queue_seconds"]
        for r in records
        if r["body"].get("schema_version") == "v2" and r["queue_seconds"] is not None
    ]
    issues = []
    for snapshot in inventory:
        backend = snapshot.get("backend", "kubernetes")
        for node in snapshot["nodes"]:
            ready = node.get("scheduler_state" if backend == "slurm" else "ready", {})
            known = ready.get("status") == "ok"
            blocked = node.get("scheduling_blockers", [])
            if not known or blocked or (backend == "kubernetes" and ready.get("value") is not True):
                issues.append(
                    {
                        "node_ref": node["node_ref"],
                        "backend": backend,
                        "kind": "node_unavailable" if known else "observation_unavailable",
                        "title": "노드 실행 불가" if known else "노드 상태 확인 필요",
                        "status": ready.get("status", "unknown"),
                        "blockers": blocked,
                        "observed_at": ready.get("observed_at"),
                        "action": "연결·노드 상태와 장비 런타임을 확인하세요."
                        if known
                        else "수집기 연결과 마지막 관측 시각을 확인하세요. 장애로 단정하지 않습니다.",
                    }
                )
    return {
        "project_ref": project,
        "scope": "current_project",
        "generated_at": moment.isoformat(),
        "active_total": sum(v for k, v in counts.items() if k not in TERMINAL),
        "waiting_total": sum(
            counts.get(k, 0)
            for k in ["RECEIVED", "VALIDATED", "SUBMITTING", "SUBMISSION_UNKNOWN", "QUEUED"]
        ),
        "running_total": counts.get("RUNNING", 0),
        "cancel_pending_total": counts.get("CANCEL_REQUESTED", 0),
        "active_jobs": active,
        "active_limit": 50,
        "recent_failures": failures,
        "failed_24h_total": failed_total,
        "failure_window_hours": 24,
        "node_issues": issues,
        "usage": {
            "groups": groups,
            "attempts": len(records),
            "scope": "current_project_all_recorded_terminal_attempts",
            "queue_mean_seconds": sum(known_wait) / len(known_wait) if known_wait else None,
            "queue_known_attempts": len(known_wait),
            "queue_unknown_attempts": len(records) - len(known_wait),
        },
    }
