const $ = (id) => document.getElementById(id);
const API = "/api/v1/compute";
let token = "",
  generation = 0,
  controller = null,
  pending = false,
  data = null;
let receivedAt = 0,
  expiryTimer = null,
  active = "execution";
const pages = { jobs: 0, compatibility: 0, history: 0, recommendations: 0, qualifications: 0 };
const views = {
  execution: [
    "OBSERVATION",
    "실행 현황",
    "실측 사용량과 스케줄러 예약량을 나란히 확인합니다.",
  ],
  compatibility: [
    "QUALIFICATION",
    "가속기 호환성",
    "장비 등록과 모델 검증은 별개입니다. 후보별 실행 계약을 다시 확인합니다.",
  ],
  history: [
    "ALLOCATION LEDGER",
    "대기 · 할당 이력",
    "현재 프로젝트의 큐 상태와 완료·실패·취소를 포함한 사용 이력입니다.",
  ],
  recommendations: [
    "EVIDENCE",
    "추천 근거 · 실제 결과",
    "추천에 사용한 근거 실행과 승인 후 독립 실행의 결과를 비교합니다.",
  ],
};
const labels = {
  ok: "측정됨",
  unavailable: "확인 불가",
  stale: "오래된 값",
  invalid_time: "시각 오류",
  collector_unhealthy: "수집기 이상",
  missing: "미측정",
};
const states = {
  SUCCEEDED: "완료",
  FAILED: "실패",
  CANCELED: "취소",
  RESULT_INVALID: "결과 검증 실패",
  RUNNING: "실행 중",
  QUEUED: "대기",
  VALIDATED: "제출 준비",
  SUBMITTING: "제출 중",
  SUBMISSION_UNKNOWN: "제출 확인 필요",
  COLLECTING: "결과 수집",
  CANCEL_REQUESTED: "취소 요청",
};
const modes = {
  physical_device: "물리 장치",
  virtual_slot: "공유 슬롯",
  cpu_only: "CPU",
  unknown: "단위 미확인",
};
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function add(parent, ...children) {
  children.filter(Boolean).forEach((c) => parent.append(c));
  return parent;
}
function fmt(v, digits = 2) {
  return typeof v === "number" && Number.isFinite(v)
    ? v.toLocaleString("ko-KR", { maximumFractionDigits: digits })
    : "—";
}
function duration(v) {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  if (v === 0) return "0 s";
  const magnitude = Math.abs(v);
  if (magnitude < 1e-12) return v.toExponential(3) + " s";
  if (magnitude < 1e-6) return fmt(v * 1e9, 3) + " ns";
  if (magnitude < 1e-3) return fmt(v * 1e6, 3) + " μs";
  if (magnitude < 1) return fmt(v * 1e3, 3) + " ms";
  return fmt(v, 3) + " s";
}
function stamp(v) {
  return v && Number.isFinite(Date.parse(v))
    ? new Date(v).toLocaleString("ko-KR", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      })
    : "시각 미확인";
}
function badge(text, tone = "") {
  return el("span", text, "badge " + tone);
}
function stateBadge(state) {
  return badge(
    states[state] || state,
    ["FAILED", "RESULT_INVALID"].includes(state)
      ? "bad"
      : state === "SUCCEEDED"
        ? "good"
        : "",
  );
}
function code(value) {
  const n = el("code", value || "—");
  return n;
}
function details(title, value) {
  return add(
    el("details"),
    el("summary", title),
    el("pre", JSON.stringify(value, null, 2)),
  );
}
function effectiveNow() {
  return Date.parse(data?.generated_at) + (performance.now() - receivedAt);
}
function observed(s, snapshot) {
  if (!s) return { value: null, status: "missing" };
  if (s.status !== "ok") return { ...s, value: null };
  const age = effectiveNow() - Date.parse(s.observed_at),
    collectionAge = effectiveNow() - Date.parse(snapshot.collected_at);
  if (!Number.isFinite(age) || age < -5000)
    return { ...s, value: null, status: "invalid_time" };
  if (
    age > snapshot.stale_after_seconds * 1000 ||
    collectionAge > snapshot.stale_after_seconds * 1000
  )
    return { ...s, value: null, status: "stale" };
  return s;
}
function value(s, snapshot) {
  return observed(s, snapshot).value;
}
function measure(label, sample, snapshot, maximum, unit, scale = 1) {
  const s = observed(sample, snapshot),
    valid = typeof s.value === "number" && Number.isFinite(s.value);
  const div = el("div", null, "measure");
  add(
    div,
    add(
      el("div", null, "measure-label"),
      el("span", label),
      el(
        "b",
        valid
          ? fmt(s.value / scale) + " " + unit
          : labels[s.status] || "확인 불가",
      ),
    ),
  );
  if (valid && typeof maximum === "number" && maximum > 0) {
    const m = el("meter");
    m.min = 0;
    m.max = maximum;
    m.value = Math.min(s.value, maximum);
    m.setAttribute("aria-label", label);
    m.setAttribute(
      "aria-valuetext",
      `${fmt(s.value / scale)} / ${fmt(maximum / scale)} ${unit}`,
    );
    div.append(m);
  }
  add(
    div,
    el(
      "small",
      `${s.source || "수집원 없음"} · ${s.observed_at ? stamp(s.observed_at) : "관측 시각 없음"}`,
      "source-age",
    ),
  );
  return div;
}
function table(headers, rows) {
  const t = el("table"),
    head = el("tr");
  headers.forEach((h) => {
    const th = el("th", h);
    th.scope = "col";
    head.append(th);
  });
  add(t, add(el("thead"), head));
  const body = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((c) =>
      tr.append(
        c instanceof HTMLElement && c.tagName === "TD"
          ? c
          : add(el("td"), c instanceof HTMLElement ? c : el("span", c ?? "—")),
      ),
    );
    body.append(tr);
  });
  add(t, body);
  return add(el("div", null, "table-wrap"), t);
}
function panel(title, description, content) {
  const box = el("section", null, "panel");
  add(
    box,
    add(
      el("div", null, "panel-head"),
      add(
        el("div"),
        el("h2", title),
        description ? el("p", description) : null,
      ),
    ),
    content,
  );
  return box;
}
function empty(text) {
  return el("div", text, "empty");
}
function pager(kind, info) {
  const div = el("div", null, "pager"),
    previous = el("button", "이전"),
    next = el("button", "다음");
  previous.disabled = info.page === 0;
  next.disabled = !info.has_next;
  previous.onclick = () => {
    pages[kind] = Math.max(0, info.page - 1);
    load();
  };
  next.onclick = () => {
    pages[kind] = info.page + 1;
    load();
  };
  const from = info.total ? info.page * info.size + 1 : 0,
    to = Math.min((info.page + 1) * info.size, info.total);
  return add(
    div,
    el("span", `${from}–${to} / ${info.total}건`),
    previous,
    next,
  );
}
function resourcePanel(snapshot) {
  const rows = snapshot.nodes.map((n) => {
    const cpu = n.resources.cpu || {},
      mem = n.resources.memory || {};
    const ready = observed(n.ready, snapshot);
    const node = add(
      el("td", null, "node"),
      code(n.node_ref),
      el("small", n.hardware?.architecture || "구조 미확인"),
      badge(
        ready.status !== "ok"
          ? labels[ready.status]
          : ready.value
            ? "Ready 보고됨"
            : "NotReady",
        ready.status === "ok" && ready.value ? "" : "warn",
      ),
    );
    if (n.scheduling_blockers.length)
      node.append(el("small", n.scheduling_blockers.join(" · ")));
    const cpuCell = add(
      el("td", null, "metric"),
      measure(
        "실측 CPU 사용",
        n.telemetry.cpu_usage_cores,
        snapshot,
        value(cpu.capacity, snapshot),
        "cores",
      ),
      measure(
        "CPU 예약 여유",
        cpu.request_headroom,
        snapshot,
        value(cpu.allocatable, snapshot),
        "cores",
      ),
    );
    const memCell = add(
      el("td", null, "metric"),
      measure(
        "실측 메모리 여유",
        n.telemetry["node:memory_available_bytes"],
        snapshot,
        value(mem.capacity, snapshot),
        "GiB",
        2 ** 30,
      ),
      measure(
        "메모리 예약 여유",
        mem.request_headroom,
        snapshot,
        value(mem.allocatable, snapshot),
        "GiB",
        2 ** 30,
      ),
    );
    const deviceCell = el("td", null, "metric");
    const resources = Object.entries(n.resources).filter(([key]) =>
      key.includes("/"),
    );
    for (const [key, r] of resources) {
      add(
        deviceCell,
        el("div", key, "resource-key"),
        el(
          "div",
          `${fmt(value(r.requested, snapshot))} 예약 / ${fmt(value(r.allocatable, snapshot))} · ${modes[r.allocation_mode] || r.allocation_mode}`,
        ),
      );
    }
    const utils = Object.entries(n.telemetry).filter(([key]) =>
      key.endsWith(":utilization"),
    );
    for (const [key, s] of utils)
      deviceCell.append(
        measure(key.split(":")[0] + " 사용률", s, snapshot, 100, "%"),
      );
    if (!utils.length)
      deviceCell.append(
        el("small", resources.length ? "사용률 미측정" : "등록된 가속기 없음"),
      );
    const extras = Object.fromEntries(
      Object.entries(n.telemetry)
        .filter(([k]) => k.includes(":") && !k.startsWith("node:"))
        .map(([k, s]) => [k, observed(s, snapshot)]),
    );
    if (Object.keys(extras).length)
      deviceCell.append(details("메모리 · 전력 · 온도 근거", extras));
    return [node, cpuCell, memCell, deviceCell];
  });
  const box = panel(
    snapshot.cluster_ref + ` · ${snapshot.nodes.length}개 노드`,
    `수집 ${stamp(snapshot.collected_at)} · 예약 여유는 즉시 실행 승인이나 쿼터 잔량을 뜻하지 않습니다.`,
    rows.length
      ? table(["노드 / 상태", "CPU", "메모리", "GPU · NPU"], rows)
      : empty("허용된 노드가 없습니다."),
  );
  box.append(
    el(
      "div",
      "공유 슬롯은 물리 GPU 개수가 아닙니다. 미측정·오래된 값은 막대를 표시하지 않습니다.",
      "footnote",
    ),
  );
  return box;
}
function jobRows() {
  return data.jobs.items.map((j) => [
    add(
      el("div"),
      code(j.job_id),
      el("small", j.workload_ref),
      details("실행 상세 · 결과 · MLflow", j),
    ),
    add(
      el("div"),
      stateBadge(j.state),
      j.error ? el("small", j.error) : null,
      j.last_observation_error
        ? el("small", "최근 관측 오류: " + j.last_observation_error)
        : null,
    ),
    add(
      el("div"),
      el("span", j.backend),
      el("small", j.node_ref),
      el("small", j.mode),
    ),
    add(
      el("div"),
      el("span", stamp(j.created_at)),
      el("small", "백엔드 확인: " + stamp(j.backend_observed_at)),
    ),
    j.result?.measurements
      ? add(
          el("div"),
          el("span", duration(j.result.measurements.elapsed_seconds)),
          badge(
            j.result.evidence_kind === "hardware" ? "실장비" : "합성 데이터",
            j.result.evidence_kind === "hardware" ? "" : "warn",
          ),
          el(
            "small",
            "품질 " +
              (j.quality_passed === true
                ? "통과"
                : j.quality_passed === false
                  ? "미통과"
                  : "미확인"),
          ),
          diagnosticView(j.diagnostics),
        )
      : "유효 측정 없음",
  ]);
}
function diagnosticView(d) {
  if (!d || d.reasons?.includes("NO_PHASE_PROFILE"))
    return el("small", "병목 진단: 구간 계측 없음");
  const names = {
    POSSIBLE_INPUT_SUPPLY_BOUND: "입력 공급 병목 가능성",
    POSSIBLE_IO_BOUND: "파일 읽기 병목 가능성",
    POSSIBLE_TRANSFER_BOUND: "데이터 전송 병목 가능성",
    POSSIBLE_ACCELERATOR_PATH_BOUND: "GPU 계산 경로 병목 가능성",
    POSSIBLE_SYNCHRONIZATION_BOUND: "동기화 대기 병목 가능성",
  };
  const phases = {
    input_wait: "입력 대기",
    cpu_processing: "CPU 처리",
    file_read: "파일 읽기",
    host_to_device: "GPU로 전송",
    accelerator_compute: "GPU 계산 경로",
    device_to_host: "CPU로 전송",
    synchronization: "동기화 대기",
  };
  const box = el("details", null, "phase-diagnostic");
  box.append(el("summary", names[d.hypothesis] || "병목 진단 보류"));
  for (const [key, share] of Object.entries(d.phase_shares || {}))
    box.append(el("small", `${phases[key] || key}: ${fmt(share * 100, 1)}%`));
  box.append(
    el("small", "이번 실행의 구간별 경과시간입니다. GPU 사용률이 아닙니다."),
  );
  box.append(
    el(
      "small",
      "원인 확정에는 한 변수씩 바꾼 재시험이 필요합니다. 자원은 자동 변경하지 않습니다.",
    ),
  );
  box.append(details("진단 근거 · 한계", d));
  return box;
}
function execution() {
  const root = el("div"),
    stats = el("div", null, "stats");
  for (const [label, count] of [
    [
      "프로젝트 전체 기록",
      Object.values(data.job_counts).reduce((a, b) => a + b, 0),
    ],
    ["실행 중으로 기록", data.job_counts.RUNNING || 0],
    ["대기 기록", data.job_counts.QUEUED || 0],
    ["실행 완료", data.job_counts.SUCCEEDED || 0],
  ])
    stats.append(
      add(el("div", null, "stat"), el("strong", count), el("span", label)),
    );
  root.append(stats);
  if (!data.inventory.length)
    root.append(
      panel(
        "자원 관측",
        "",
        empty(
          "이 프로젝트에 수집된 자원 스냅샷이 없습니다. 수집기 연결을 확인하세요.",
        ),
      ),
    );
  data.inventory.forEach((s) => root.append(resourcePanel(s)));
  const attempts = panel(
    "실행 기록",
    "표는 저장된 작업 상태입니다. 백엔드 확인 시각이 없으면 현재 실행 여부를 보장하지 않습니다.",
    data.jobs.items.length
      ? table(["작업", "상태", "실행 대상", "시각", "실측 결과"], jobRows())
      : empty("아직 제출된 작업이 없습니다."),
  );
  attempts.append(pager("jobs", data.jobs));
  root.append(attempts);
  return root;
}
function compatibilityView() {
  const root = el("div");
  const observedNodes = data.inventory
    .flatMap((s) => s.nodes)
    .filter((n) =>
      Object.values(n.resources).some((r) =>
        ["gpu", "npu"].includes(r.device_class),
      ),
    );
  root.append(
    panel(
      "장비 보유와 검증 범위",
      "현재 수집된 목록의 등록 상태입니다. 작업별 실행 계약·자원 승인과는 다릅니다.",
      empty(
        `${observedNodes.length}개 노드에 가속기 자원이 보고되어 있습니다. 아래에 없는 장비·모델 조합은 검증되지 않았습니다.`,
      ),
    ),
  );
  root.append(qualificationView());
  for (const w of data.compatibility.items) {
    const rows = w.candidates.map((c) => [
      add(el("div"), code(c.candidate_ref), el("small", c.variant_ref)),
      add(
        el("div"),
        el("span", c.model || "장비 미확인"),
        el("small", c.node_ref || "노드 미확인"),
        el(
          "small",
          c.backend + " · " + (modes[c.allocation_mode] || "단위 미확인"),
        ),
      ),
      add(
        el("div"),
        badge(c.verification || "미등록"),
        details("검증 근거 · 런타임", {
          validation_refs: c.validation_refs,
          runtime_versions: c.runtime_versions,
          capability_observed_at: c.capability_observed_at,
        }),
      ),
      add(
        el("div"),
        badge(
          c.contract_compatible_now ? "계약 검사 통과" : "현재 계약 불충족",
          c.contract_compatible_now ? "good" : "warn",
        ),
        ...c.reasons.map((reason) => el("div", reason, "reason")),
      ),
    ]);
    root.append(
      panel(
        w.workload_ref,
        w.task_type +
          " · " +
          w.precision +
          " · 최종 자원 승인은 Kueue / Slurm이 결정합니다.",
        table(["실행 후보", "장비 / 백엔드", "검증 단계", "현재 검사"], rows),
      ),
    );
  }
  if (!data.compatibility.items.length)
    root.append(
      empty(
        "등록된 작업별 실행 후보가 없습니다. 장비 등록만으로 모델 호환성을 추정하지 않습니다.",
      ),
    );
  root.append(pager("compatibility", data.compatibility));
  return root;
}
function qualificationView() {
  const page = data.qualifications;
  if (!page)
    return panel("모델 검증 이력", "", empty("검증 이력 조회를 지원하지 않는 서버입니다."));
  const gateNames = {
    minimum_accuracy: "정확도 미달",
    minimum_reference_agreement: "원본 일치율 미달",
    maximum_accuracy_loss: "정확도 감소 초과",
  };
  const percent = (v) => fmt(v * 100, 2) + "%";
  const rows = page.items.map((record) => {
    const a = record.assessment, g = record.gates;
    return [
      add(el("div"), el("b", record.model_name), el("small", record.accelerator_model),
        code(record.ref)),
      add(el("div"), badge(a.inference_completed ? "추론 완료" : "실행 미확인"),
        badge(a.quality_passed ? "품질 기준 통과" : "품질 기준 미달", a.quality_passed ? "" : "bad"),
        ...a.failed_checks.map((key) => el("small", gateNames[key] || key)),
        el("small", "자동 추천 등록 없음")),
      add(el("div"), el("b", percent(a.accuracy)),
        el("small", "기준 ≥ " + percent(g.minimum_accuracy))),
      add(el("div"), el("b", percent(a.reference_agreement)),
        el("small", "기준 ≥ " + percent(g.minimum_reference_agreement))),
      add(el("div"), el("b", fmt(a.accuracy_loss * 100, 2) + "%p"),
        el("small", "한도 ≤ " + fmt(g.maximum_accuracy_loss * 100, 2) + "%p")),
      add(el("div"), badge(record.evidence_kind === "hardware" ? "외부 실측 가져옴" : "합성 테스트", "warn"),
        el("small", stamp(record.finished_at) + " · " + a.sample_count + "장"),
        details("실험 조건 · 근거", record)),
    ];
  });
  return panel("모델 검증 이력",
    "운영자가 가져온 외부 실험입니다. 서버가 이미지별 결과에서 품질 지표를 재계산합니다. 기록만으로 실행 후보나 사용량 원장에 추가되지 않습니다.",
    add(el("div"), page.items.length
      ? table(["모델 / 장비", "실행 · 품질", "정확도", "원본 일치율", "정확도 감소", "출처 · 조건"], rows)
      : empty("가져온 검증 기록이 없습니다. 미검증을 성공으로 표시하지 않습니다."),
    pager("qualifications", page)));
}
function queuePolicyView(q, title) {
  const format = (value, unit) =>
    value == null
      ? "미보고"
      : unit === "bytes"
        ? fmt(value / 1024 ** 3) + " GiB"
        : fmt(value) + (unit === "cores" ? " cores" : " units");
  const content = add(
    el("div", null, "evidence"),
    el(
      "p",
      `대기 ${fmt(q.pending_workloads, 0)} · 예약 ${fmt(q.reserving_workloads, 0)} · 승인 ${fmt(q.admitted_workloads, 0)} 작업`,
    ),
    badge(
      q.active === true
        ? "큐 활성"
        : q.active === false
          ? "큐 비활성"
          : "큐 활성 여부 미확인",
      q.active === true ? "" : "warn",
    ),
    q.resources.length
      ? table(
          ["Flavor / 자원", "기본 한도", "예약", "승인", "빌린 예약"],
          q.resources.map((r) => [
            add(
              el("div"),
              code(r.flavor),
              el("div", r.resource),
              el(
                "small",
                r.unit === "cores"
                  ? "호스트 CPU"
                  : r.unit === "bytes"
                    ? "메모리 / 저장 용량"
                    : modes[r.allocation_mode] || "단위 미확인",
              ),
            ),
            format(r.nominal_quota, r.unit),
            format(r.reserved, r.unit),
            format(r.admitted, r.unit),
            format(r.borrowed_reservation, r.unit),
          ]),
        )
      : empty("자원별 할당량이 보고되지 않았습니다."),
    details("정책 · 상태 사유", q),
  );
  return panel(
    title,
    q.scope === "cluster_queue_all_namespaces"
      ? "ClusterQueue 전체 네임스페이스의 합계입니다. 예약·승인량은 실제 사용률이나 즉시 가용량이 아닙니다."
      : "이 네임스페이스의 LocalQueue 사용량입니다. 공유 ClusterQueue의 한도는 프로젝트 전용 한도가 아닙니다.",
    content,
  );
}
function historyView() {
  const root = el("div");
  let queues = 0;
  for (const s of data.inventory)
    for (const q of s.queues) {
      queues++;
      const w = observed(q.workloads, s);
      root.append(
        panel(
          s.cluster_ref + " / " + q.namespace,
          `Kueue 관측 ${stamp(w.observed_at)}`,
          w.status !== "ok"
            ? empty("큐 상태 " + (labels[w.status] || "확인 불가"))
            : w.value.length
              ? table(
                  [
                    "Workload",
                    "큐",
                    "Admission",
                    "우선순위",
                    "상태 사유",
                    "생성 시각",
                  ],
                  w.value.map((x) => [
                    code(x.ref),
                    x.queue,
                    badge(
                      x.admitted === true
                        ? "승인됨"
                        : x.admitted === false
                          ? "미승인"
                          : "미확인",
                      x.admitted === true ? "good" : "warn",
                    ),
                    add(
                      el("div"),
                      el("span", fmt(x.priority, 0)),
                      el("small", x.priority_class || "클래스 미보고"),
                    ),
                    details("예약 · 승인 검사", {
                      quota_reserved: x.quota_reserved,
                      conditions: x.conditions,
                      admission_checks: x.admission_checks,
                    }),
                    stamp(x.created_at),
                  ]),
                )
              : empty("이번 관측에서 진행 중인 Kueue Workload가 없습니다."),
        ),
      );
      const local = observed(q.local_queues, s);
      if (local.status === "ok")
        for (const item of local.value)
          root.append(queuePolicyView(item, q.namespace + " / " + item.ref));
      else
        root.append(
          empty("LocalQueue 정책 " + (labels[local.status] || "확인 불가")),
        );
    }
  for (const s of data.inventory)
    for (const q of s.cluster_queues || []) {
      const current = observed(q.observation, s);
      root.append(
        current.status === "ok"
          ? queuePolicyView(current.value, "ClusterQueue / " + q.ref)
          : panel(
              "ClusterQueue / " + q.ref,
              "",
              empty(labels[current.status] || "확인 불가"),
            ),
      );
    }
  if (!queues)
    root.append(
      panel(
        "현재 큐",
        "",
        empty("연결된 큐 관측이 없습니다. 빈 큐로 판단하지 않습니다."),
      ),
    );
  const rows = data.history.items.map((r) => {
    const b = r.body,
      known = b.schema_version === "v2";
    return [
      add(
        el("div"),
        code(b.job_id || r.attempt_id),
        details("시간 경계 · 할당 근거", r),
      ),
      stateBadge(b.outcome || "과거 기록"),
      add(
        el("div"),
        el("span", r.backend + " / " + r.device_class),
        el("small", modes[r.allocation_mode] || r.allocation_mode),
        el("small", b.accelerator_model),
      ),
      fmt(r.queue_seconds) + " s",
      known ? fmt(r.allocated_device_seconds) + " 장치·s" : "과거 미검증 값",
      duration(r.measured_compute_seconds),
      add(
        el("div"),
        el("span", stamp(b.terminal_observed_at)),
        ...(b.uncertainty || []).map((x) => el("small", x)),
      ),
    ];
  });
  const box = panel(
    "할당 원장",
    "할당 시간과 실제 계산 시간은 다릅니다. GPU·NPU, 물리 장치·공유 슬롯을 합산하지 않습니다.",
    rows.length
      ? table(
          [
            "작업 / 근거",
            "종료 상태",
            "자원 단위",
            "큐 대기",
            "관측된 할당",
            "실측 계산",
            "관측 / 누락",
          ],
          rows,
        )
      : empty("종료된 작업의 할당 이력이 없습니다."),
  );
  box.append(pager("history", data.history));
  root.append(box);
  return root;
}
function transferEvidenceView(transfer) {
  const root = el("div", null, "evidence");
  const rgpe = transfer.strategy === "rgpe";
  add(
    root,
    el("h3", "이전 작업의 이력을 어떻게 사용했나요?"),
    badge(
      rgpe ? "RGPE · 모델 가중 결합" : "이력 기반 시작 · 이후 현재 작업 BO",
    ),
    el(
      "p",
      rgpe
        ? "현재 작업에서 다시 측정한 순위로 이전 모델의 가중치를 정합니다. 가중치는 혼합 비중이며 정확도나 성공 확률이 아닙니다."
        : "이전 작업의 순위로 처음 시험할 후보를 정하고, 이후에는 현재 작업의 측정값으로만 BO를 수행합니다.",
      "muted",
    ),
    el(
      "small",
      `이력 고정 ${stamp(transfer.source_snapshot_at)} · ${transfer.study_ref}`,
    ),
    table(
      ["이전 작업", "독립 근거 실행", "실행 ID"],
      transfer.sources.map((s) => [
        s.workload_refs.join(", "),
        s.attempt_ids.length,
        details("근거 확인", s.attempt_ids),
      ]),
    ),
    table(
      ["비용 구분", "시간"],
      [
        [
          "선택된 이전 근거 작업의 시간",
          duration(transfer.historical_source_wall_seconds),
        ],
        [
          "이번 탐색 · 검증의 경과 시간",
          duration(transfer.target_cost?.wall_seconds),
        ],
        [
          "이번 모델 계획 시간 (경과 시간에 포함)",
          duration(transfer.target_cost?.planning_seconds),
        ],
      ],
    ),
    el(
      "p",
      "이전 근거 시간에는 전체 이력 구축 비용이 포함되지 않습니다. 이번 작업 비용과 따로 해석하세요.",
      "muted",
    ),
    badge(
      transfer.historical_source_cost_recharged
        ? "이전 비용 재청구됨"
        : "이전 비용 별도 기록",
      transfer.historical_source_cost_recharged ? "warn" : "",
    ),
  );
  const choices = transfer.choices || [];
  if (!choices.length) root.append(empty("저장된 탐색 선택이 없습니다."));
  for (const [index, choice] of choices.entries()) {
    const section = el("details");
    add(
      section,
      el(
        "summary",
        `${index + 1}. ${choice.candidate_ref} · ${choice.fallback_reason ? "현재 작업 모델로 전환" : choice.weights ? "모델 가중치 계산" : choice.warm_start_order ? "이전 순위 참고" : "현재 작업 사전 검증"}`,
      ),
    );
    if (choice.weights)
      section.append(
        table(
          ["모델", "혼합 비중"],
          Object.entries(choice.weights).map(([ref, weight]) => [
            ref === "target"
              ? "현재 작업"
              : transfer.sources
                  .find((s) => s.workload_signature === ref)
                  ?.workload_refs.join(", ") || ref,
            typeof weight === "number"
              ? fmt(weight * 100, 2) + "%"
              : "확인 불가",
          ]),
        ),
      );
    if (choice.fallback_reason)
      add(section, badge("전환 사유", "warn"), code(choice.fallback_reason));
    if (choice.warm_start_order)
      section.append(
        el("p", "시작 순서: " + choice.warm_start_order.join(" → ")),
      );
    add(
      section,
      el(
        "p",
        `이전 실행 ${choice.source_run_ids.length}개 · 현재 탐색 실행 ${choice.target_run_ids.length}개`,
      ),
      details("실행 ID · 순위 진단 · 선택 근거", choice),
    );
    root.append(section);
  }
  if (transfer.plans_truncated)
    root.append(el("p", "최근 200개 계획의 탐색 선택만 표시합니다.", "muted"));
  return root;
}
function evidenceView(payload) {
  const root = el("div", null, "evidence"),
    r = payload.recommendation;
  const validity = payload.current_validity;
  add(
    root,
    el("h3", "지금 다시 사용할 수 있나요?"),
    badge(
      validity?.reusable ? "조회 시점 검사 통과" : "재확인 필요",
      validity?.reusable ? "" : "warn",
    ),
    el(
      "p",
      validity?.reusable
        ? "조회한 시점의 근거 검사 결과입니다. 승인·제출 시 다시 검사하며, 실행 성능을 보장하지 않습니다."
        : "이전 추천의 만료·근거 유효기간·후속 실행 변화를 확인해야 합니다.",
      "muted",
    ),
  );
  if (validity)
    root.append(el("small", `검사 시각 ${stamp(validity.assessed_at)}`));
  if (validity)
    root.append(details("재사용 검사 사유 · 후속 실측 근거", validity));
  if (payload.transfer) root.append(transferEvidenceView(payload.transfer));
  add(
    root,
    el("h3", "추천에 사용한 측정 요약"),
    el(
      "p",
      "이 값은 과거 측정의 요약입니다. 앞으로 실행할 작업의 보장값이 아닙니다.",
      "muted",
    ),
  );
  const ranks = r.ranking || [];
  root.append(
    ranks.length
      ? table(
          ["후보", "평균", "기술 통계 구간", "독립 실행"],
          ranks.map((x) => [
            x.candidate_ref,
            duration(x.mean_seconds),
            x.interval_seconds.map(duration).join(" – "),
            x.independent_runs,
          ]),
        )
      : empty("추천 가능한 측정 근거가 없습니다."),
  );
  add(
    root,
    el("h3", "근거 실행"),
    payload.evidence_runs.length
      ? table(
          ["Attempt", "결과", "실측 시간", "MLflow run"],
          payload.evidence_runs.map((e) => [
            code(e.attempt_id),
            e.result
              ? e.result.outcome + " / " + e.result.evidence_kind
              : "근거 레코드 없음",
            duration(e.result?.measurements?.elapsed_seconds),
            code(e.tracking?.run_id),
          ]),
        )
      : empty("연결된 근거 실행이 없습니다."),
  );
  add(
    root,
    el("h3", "승인 후 독립 실행과 비교"),
    payload.approved_executions.length
      ? table(
          ["작업", "상태", "과거 평균", "새 실측", "실측 − 과거 평균"],
          payload.approved_executions.map((c) => [
            code(c.job.job_id),
            c.comparison_status === "independent_measured"
              ? "독립 실장비 측정"
              : "비교 불가 / 대기",
            duration(c.historical_mean_seconds),
            duration(c.actual_seconds),
            duration(c.signed_error_seconds),
          ]),
        )
      : empty(
          "이 추천을 승인해 새로 실행한 결과가 없습니다. 추천의 근거 실행을 새 실행으로 재사용하지 않습니다.",
        ),
  );
  if (payload.approved_executions_truncated)
    root.append(
      el("p", "승인 후 실행은 처음 200건까지만 표시합니다.", "muted"),
    );
  root.append(details("제외 사유 · 추가 시험 비용 · 전체 추천 근거", r));
  return root;
}
function recommendationsView() {
  const root = el("div");
  for (const r of data.recommendations.items) {
    const content = el("div", null, "evidence"),
      header = add(
        el("div", null, "state-line"),
        badge(r.status),
        badge(
          Date.parse(r.expires_at) < effectiveNow()
            ? "승인 기한 만료"
            : "승인 기한 내",
          "warn",
        ),
      );
    add(
      content,
      header,
      el("p", "선택 후보: " + (r.candidate_ref || "추천 보류")),
      el("small", `작성 ${stamp(r.created_at)} · ${r.ref}`),
    );
    if (r.selected_context) {
      const c = r.selected_context;
      content.append(
        el(
          "p",
          `${c.accelerator_model} · ${c.device_class.toUpperCase()} ${c.resources.accelerator_count} · CPU ${c.resources.host_cpu} cores`,
          "muted",
        ),
      );
    }
    const b = el("button", "근거와 실제 결과 보기", "details-action"),
      target = el("div");
    b.onclick = async () => {
      $("auto").checked = false;
      const session = generation;
      b.disabled = true;
      target.replaceChildren(empty("근거를 불러오는 중…"));
      try {
        const response = await fetch(
          API + "/recommendations/" + encodeURIComponent(r.ref) + "/evidence",
          {
            headers: { Authorization: "Bearer " + token },
            signal: controller.signal,
            cache: "no-store",
          },
        );
        if (!response.ok)
          throw new Error(
            response.status === 401
              ? "인증이 만료되었습니다."
              : "근거 조회에 실패했습니다.",
          );
        const result = await response.json();
        if (session === generation)
          target.replaceChildren(evidenceView(result));
      } catch (e) {
        if (session === generation && e.name !== "AbortError")
          target.replaceChildren(empty(e.message));
      } finally {
        if (session === generation) b.disabled = false;
      }
    };
    add(content, b, target);
    root.append(
      panel(
        r.workload_ref,
        "추천 결과는 승인이 있어야 다음 실행에 사용됩니다.",
        content,
      ),
    );
  }
  if (!data.recommendations.items.length)
    root.append(
      panel(
        "추천 근거",
        "",
        empty(
          "저장된 추천이 없습니다. 근거 없는 성능이나 추천값을 생성하지 않습니다.",
        ),
      ),
    );
  root.append(pager("recommendations", data.recommendations));
  return root;
}
function render() {
  if (!data) return;
  const [label, title, description] = views[active];
  $("section-label").textContent = label;
  $("view-title").textContent = title;
  $("view-description").textContent = description;
  $("identity").textContent = data.project_ref;
  $("updated").textContent = "조회 " + stamp(data.generated_at);
  $("content").replaceChildren(
    {
      execution,
      compatibility: compatibilityView,
      history: historyView,
      recommendations: recommendationsView,
    }[active](),
  );
  $("workspace").hidden = false;
  $("connect").hidden = true;
  $("logout").hidden = false;
  clearTimeout(expiryTimer);
  const expirations = data.inventory
    .flatMap((s) => [
      Date.parse(s.collected_at) + s.stale_after_seconds * 1000,
      ...s.nodes.flatMap((n) =>
        Object.values(n.telemetry).map(
          (v) => Date.parse(v.observed_at) + s.stale_after_seconds * 1000,
        ),
      ),
    ])
    .filter((t) => Number.isFinite(t) && t > effectiveNow());
  if (expirations.length)
    expiryTimer = setTimeout(
      () => {
        if (data && ["execution", "history"].includes(active)) render();
      },
      Math.max(1, Math.min(...expirations) - effectiveNow() + 10),
    );
}
function reset(message = "") {
  generation++;
  controller?.abort();
  controller = null;
  pending = false;
  token = "";
  data = null;
  clearTimeout(expiryTimer);
  $("content").replaceChildren();
  $("workspace").hidden = true;
  $("connect").hidden = false;
  $("logout").hidden = true;
  $("identity").textContent = "프로젝트 연결 전";
  $("notice").textContent = message;
  $("token").value = "";
  Object.keys(pages).forEach((k) => (pages[k] = 0));
}
async function load() {
  if (!token || pending) return;
  pending = true;
  const session = generation;
  $("refresh").disabled = true;
  const query = new URLSearchParams(
    Object.entries(pages).map(([key, v]) => [key + "_page", v]),
  );
  try {
    const response = await fetch(API + "/overview?" + query, {
      headers: { Authorization: "Bearer " + token },
      signal: controller.signal,
      cache: "no-store",
    });
    if (session !== generation) return;
    if (response.status === 401) {
      reset("프로젝트 토큰을 확인해 주세요.");
      return;
    }
    if (!response.ok)
      throw new Error("자원 API에 연결할 수 없습니다. 다시 갱신해 주세요.");
    const result = await response.json();
    if (session !== generation) return;
    data = result;
    receivedAt = performance.now();
    $("notice").textContent = "";
    render();
  } catch (e) {
    if (session === generation && e.name !== "AbortError") {
      data = null;
      clearTimeout(expiryTimer);
      $("content").replaceChildren(
        empty("새 상태를 확인할 수 없어 이전 자원 막대를 숨겼습니다."),
      );
      $("notice").textContent =
        "자원 API에 연결할 수 없습니다. 다시 갱신해 주세요.";
    }
  } finally {
    if (session === generation) {
      pending = false;
      $("refresh").disabled = false;
    }
  }
}
$("login").onsubmit = (e) => {
  e.preventDefault();
  const entered = $("token").value.trim();
  if (!entered) return;
  reset();
  token = entered;
  controller = new AbortController();
  $("notice").textContent = "프로젝트에 연결하는 중…";
  load();
};
$("content").addEventListener(
  "toggle",
  (e) => {
    if (e.target.tagName === "DETAILS" && e.target.open)
      $("auto").checked = false;
  },
  true,
);
$("logout").onclick = () => {
  reset("연결을 해제했습니다.");
  $("token").focus();
};
$("refresh").onclick = () => load();
function navigate(event) {
  const view = new URL(event?.newURL || location.href).hash.slice(1);
  if (view === "main") return; // The skip link must not change the selected view.
  active = views[view] ? view : "execution";
  document.querySelectorAll("nav a").forEach((a) => {
    if (a.dataset.view === active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  render();
}
window.addEventListener("hashchange", navigate);
navigate();
setInterval(() => {
  if ($("auto").checked && token) load();
}, 15000);
window.addEventListener("pagehide", () => reset());
