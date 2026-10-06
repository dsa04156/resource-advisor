const $ = (id) => document.getElementById(id);
const API = "/api/v1/compute";
let anonymousConnected = false;
function authHeaders() { return token ? { Authorization: "Bearer " + token } : {}; }
let token = "",
  generation = 0,
  controller = null,
  pending = false,
  data = null;
let receivedAt = 0,
  expiryTimer = null,
  active = "operations";
const pages = { jobs: 0, compatibility: 0, history: 0, recommendations: 0, qualifications: 0, templates: 0 };
const submitting = new Set();
const requestKeys = new Map();
const jobFilters = { status: "all", backend: "all", search: "" };
let resourceBackend = "all", resourceSearch = "", searchTimer = null, filterRevision = 0;
const submissionDraft = { workload: "", candidate: "" };
let schedulingProfile = "", schedulingPreview = null, schedulingPreviewKey = "";
let templateEditorOpen = false, savingTemplate = false;
let templateDraft = {};
const selectionKey = w => w.template_ref ? "template:" + w.template_ref : w.workload_ref;
let showAllTemplates = false;
const canSubmit = (candidate) => candidate.submittable_now ?? candidate.contract_compatible_now;
function reasonText(reason) {
  return ({ CAPABILITY_STALE: "장비 확인 기록이 오래됨", NODE_UNAVAILABLE: "노드 연결 확인 필요",
    INSUFFICIENT_CAPACITY: "장비 용량보다 많은 자원을 요청함", APPROVAL_REQUIRED: "추천 구성 승인 필요",
    ARCH_MISMATCH: "CPU 아키텍처가 실행 환경과 다름", RUNTIME_MISMATCH: "런타임 버전이 다름",
    LOGICAL_WORKLOAD_MISMATCH: "모델·입력 조건이 등록된 실행 환경과 다름",
    PRECISION_MISMATCH: "모델 정밀도 설정이 다름", INPUT_SHAPE_UNVERIFIED: "입력 크기가 실행 환경과 다름",
    ENVIRONMENT_MISMATCH: "등록된 실행 환경이 다름" })[reason] || reason;
}

function submitButton(workload, candidate, approval = null, scheduling = null) {
  const label = approval ? "승인한 구성 실행" : "작업 제출";
  const key = `ra-submit:${data.project_ref}:${workload.workload_ref}:${candidate.candidate_ref}${approval ? ":" + approval.ref : ""}${workload.template_ref ? ":template:" + workload.template_ref : ""}${scheduling ? ":policy:" + scheduling.digest : ""}`;
  let saved = requestKeys.get(key);
  try { saved ||= sessionStorage.getItem(key); } catch (_) { /* Memory fallback. */ }
  if (saved) requestKeys.set(key, saved);
  const button = el("button", submitting.has(key) ? "제출 중…" : saved ? "제출 확인 재시도" : label, "primary");
  button.setAttribute("aria-label", `${label} · ${workload.workload_ref} · ${candidate.candidate_ref}`);
  button.disabled = !canSubmit(candidate) || submitting.has(key);
  button.onclick = async () => {
    if (submitting.has(key)) return;
    const session = generation;
    if (!requestKeys.has(key)) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      requestKeys.set(key, "console-" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join(""));
      try { sessionStorage.setItem(key, requestKeys.get(key)); } catch (_) { /* Memory fallback. */ }
    }
    submitting.add(key);
    button.disabled = true;
    button.textContent = "제출 중…";
    try {
      const response = await fetch(API + "/jobs", {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json", "Idempotency-Key": requestKeys.get(key) },
        body: JSON.stringify({ workload_ref: workload.workload_ref, candidate_ref: candidate.candidate_ref,
          ...(workload.template_ref ? { template_ref: workload.template_ref } : {}),
          ...(scheduling ? { scheduling_profile_ref: scheduling.plan.profile_ref, scheduling_plan_digest: scheduling.digest } : {}),
          mode: approval ? "fixed" : "observe", ...(approval ? { approval_ref: approval.ref } : {}) }),
      });
      if (!response.ok) {
        const failure = await response.json().catch(() => ({}));
        const message = typeof failure.detail === "string" ? failure.detail.split(",").map(reasonText).join(" · ") : "요청 조건과 프로젝트 권한을 확인해 주세요.";
        throw new Error("제출 실패: " + message);
      }
      const job = await response.json();
      requestKeys.delete(key);
      try { sessionStorage.removeItem(key); } catch (_) { /* No credential is stored. */ }
      if (session !== generation) return;
      pages.jobs = 0;
      jobFilters.status = "all"; jobFilters.backend = "all"; jobFilters.search = "";
      active = "jobs";
      location.hash = "jobs";
      await load();
      $("notice").textContent = "작업을 접수했습니다: " + job.job_id;
    } catch (error) {
      if (session === generation)
        $("notice").textContent = error.message + " 같은 버튼으로 재시도하면 동일 요청을 확인합니다.";
    } finally {
      submitting.delete(key);
      if (session === generation) render();
    }
  };
  const resources = candidate.resources;
  return add(el("div"), button, resources ? el("small",
    `가속기 ${resources.accelerator_count} · CPU ${resources.host_cpu} · ${resources.host_memory_mib} MiB · 최대 ${workload.max_run_seconds}초`) : null);
}
const recommending = new Set(), approving = new Set(), approvals = new Map();
function recommendButton(workload) {
  const key = `${data.project_ref}:${workload.workload_ref}`;
  const button = el("button", recommending.has(key) ? "추천 확인 중…" : "추천 받기");
  button.setAttribute("aria-label", `추천 받기 · ${workload.workload_ref}`);
  button.disabled = recommending.has(key);
  button.onclick = async () => {
    if (recommending.has(key)) return;
    const session = generation;
    recommending.add(key); button.disabled = true;
    try {
      const response = await fetch(API + "/recommendations", {
        method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ workload_ref: workload.workload_ref }),
      });
      if (!response.ok) throw new Error("추천을 확인하지 못했습니다. 작업 등록과 접근 권한을 확인해 주세요.");
      const recommendation = await response.json();
      if (session !== generation) return;
      pages.recommendations = 0; active = "recommendations"; location.hash = active;
      await load();
      $("notice").textContent = recommendation.candidate_ref
        ? "추천을 만들었습니다. 근거와 요청 자원을 확인한 뒤 승인해 주세요."
        : "지금은 추천을 보류했습니다. 근거 보기에서 부족한 조건을 확인해 주세요.";
    } catch (error) {
      if (session === generation) $("notice").textContent = error.message;
    } finally {
      recommending.delete(key);
      if (session === generation) render();
    }
  };
  return add(el("div"), button, el("small", "기록된 실행 이력으로 추천합니다. 새 계산 작업은 실행하지 않습니다."));
}
function approvalControls(recommendation) {
  const r = recommendation, c = r.selected_context;
  if (!r.measured || !r.candidate_ref || !c) return el("small", "추천 보류 상태에서는 승인·실행할 수 없습니다.");
  if (Date.parse(r.expires_at) <= effectiveNow()) return el("small", "승인 기한이 지났습니다. 가속기 호환성에서 추천을 다시 받아 주세요.");
  const key = `ra-approval:${data.project_ref}:${r.ref}`;
  if (!approvals.has(key)) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key));
      if (saved?.ref && saved.recommendation_digest === r.digest && saved.candidate_ref === r.candidate_ref)
        approvals.set(key, saved);
    } catch (_) { /* Memory fallback; server revalidates every approval. */ }
  }
  const approved = approvals.get(key);
  if (approved) return add(el("div"), el("small", "구성 승인됨 · 실행 버튼을 눌러야 새 작업이 생성됩니다."),
    submitButton({ workload_ref: r.workload_ref, max_run_seconds: c.max_run_seconds },
      { candidate_ref: r.candidate_ref, contract_compatible_now: true, resources: c.resources }, approved));
  const button = el("button", approving.has(key) ? "승인 확인 중…" : "이 구성 승인");
  button.setAttribute("aria-label", `이 구성 승인 · ${r.ref}`);
  button.disabled = approving.has(key);
  button.onclick = async () => {
    if (approving.has(key)) return;
    const session = generation;
    approving.add(key); button.disabled = true;
    try {
      const response = await fetch(API + "/recommendations/" + encodeURIComponent(r.ref) + "/approve", {
        method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ recommendation_digest: r.digest, candidate_ref: r.candidate_ref }),
      });
      if (!response.ok) throw new Error("승인하지 못했습니다. 추천의 유효기간·근거·실행 환경을 다시 확인해 주세요.");
      const approval = await response.json();
      approvals.set(key, approval);
      try { sessionStorage.setItem(key, JSON.stringify(approval)); } catch (_) { /* No credential stored. */ }
      if (session === generation) $("notice").textContent = "구성을 승인했습니다. 실행은 아직 시작하지 않았습니다.";
    } catch (error) {
      if (session === generation) $("notice").textContent = error.message;
    } finally {
      approving.delete(key);
      if (session === generation) render();
    }
  };
  return add(el("div"), button, el("small", "승인 시 현재 근거를 다시 검사합니다. 승인은 자원 예약이 아닙니다."));
}
const views = {
  operations: ["COMPUTE OPERATIONS", "운영 현황", "자원 배분, 대기 사유, 장애와 사용량을 확인합니다."],
  usage: ["PROJECT ACCOUNTING", "프로젝트 사용량", "물리 장치와 공유 슬롯을 구분한 할당 원장입니다."],
  submit: [
    "SUBMIT JOB",
    "작업 제출",
    "작업 템플릿과 실행 장비를 선택하면 Kubernetes 또는 Slurm에 새 작업을 제출합니다.",
  ],
  execution: [
    "OBSERVATION",
    "자원 현황",
    "실측 사용량과 스케줄러 예약량을 나란히 확인합니다.",
  ],
  jobs: ["EXECUTIONS", "작업 현황", "프로젝트의 전체 작업을 검색하고 실행 상태·결과를 확인합니다."],
  compatibility: [
    "QUALIFICATION",
    "가속기 호환성",
    "장비 등록과 모델 검증은 별개입니다. 후보별 실행 계약을 다시 확인합니다.",
  ],
  history: [
    "ALLOCATION LEDGER",
    "큐 · 쿼터",
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
  not_configured: "연결 미설정",
  unclassified: "자원 종류 미분류",
  partial: "일부 확인",
};
const states = {
  SUCCEEDED: "완료",
  FAILED: "실패",
  CANCELED: "취소",
  RESULT_INVALID: "결과 검증 실패",
  RUNNING: "실행 중",
  QUEUED: "대기",
  VALIDATED: "접수됨",
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
  const slurm = snapshot.backend === "slurm";
  const rows = snapshot.nodes.map((n) => {
    const cpu = n.resources.cpu || {},
      mem = n.resources.memory || {};
    const ready = observed(slurm ? n.scheduler_state : n.ready, snapshot);
    const node = add(
      el("td", null, "node"),
      code(n.node_ref),
      el("small", n.hardware?.architecture || "구조 미확인"),
      badge(
        slurm
          ? "Slurm: " + (ready.status === "ok" && Array.isArray(ready.value)
            ? ready.value.join(" + ") : labels[ready.status] || "확인 불가")
          : ready.status !== "ok" ? labels[ready.status] || "확인 불가"
            : ready.value ? "Ready 보고됨" : "NotReady",
        ready.status === "ok" && (slurm ? !n.scheduling_blockers.length : ready.value) ? "" : "warn",
      ),
    );
    if (!slurm && n.scheduling_blockers.length)
      node.append(el("small", n.scheduling_blockers.join(" · ")));
    const cpuCell = add(
      el("td", null, "metric"),
      measure(
        "실측 CPU 사용",
        slurm ? n.telemetry["node:cpu_non_idle_cores"] : n.telemetry.cpu_usage_cores,
        snapshot,
        value(slurm ? n.telemetry["node:cpu_count"] : cpu.capacity, snapshot),
        "cores",
      ),
      measure(
        slurm ? "Slurm 미예약 CPU" : "CPU 예약 여유",
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
        value(slurm ? n.telemetry["node:memory_total_bytes"] : mem.capacity, snapshot),
        "GiB",
        2 ** 30,
      ),
      measure(
        slurm ? "Slurm 미예약 메모리" : "메모리 예약 여유",
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
        el("small", resources.length ? "사용률 미측정"
          : slurm && observed(n.accelerator_inventory, snapshot).status !== "ok"
            ? "가속기 등록 상태 확인 불가" : "등록된 가속기 없음"),
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
    snapshot.cluster_ref + ` · ${slurm ? "Slurm" : "Kubernetes"} · ${snapshot.nodes.length}개 노드`,
    `수집 ${stamp(snapshot.collected_at)} · ${slurm ? "실측 막대는 호스트 총량 기준입니다. " : ""}예약 차감값은 즉시 실행 승인이나 쿼터 잔량을 뜻하지 않습니다.`,
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
const cancelling = new Set();
function cancelButton(job) {
  if (["SUCCEEDED", "FAILED", "CANCELED", "RESULT_INVALID", "REJECTED"].includes(job.state)) return null;
  if (job.state === "CANCEL_REQUESTED") return el("small", "백엔드 종료 확인 중");
  const button = el("button", cancelling.has(job.job_id) ? "취소 요청 중…" : "작업 취소");
  button.setAttribute("aria-label", `작업 취소 · ${job.job_id}`);
  button.disabled = cancelling.has(job.job_id);
  button.onclick = async () => {
    if (cancelling.has(job.job_id)) return;
    const session = generation;
    cancelling.add(job.job_id);
    button.disabled = true;
    button.textContent = "취소 요청 중…";
    try {
      const response = await fetch(API + "/jobs/" + encodeURIComponent(job.job_id) + "/cancel", {
        method: "POST",
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error("취소 요청을 확인하지 못했습니다. 같은 작업에서 다시 요청할 수 있습니다.");
      const current = await response.json();
      if (session !== generation) return;
      await load();
      $("notice").textContent = current.state === "CANCEL_REQUESTED"
        ? "취소를 요청했습니다. 백엔드 종료가 확인되면 상태가 바뀝니다."
        : "현재 작업 상태: " + (states[current.state] || current.state);
    } catch (error) {
      if (session === generation) $("notice").textContent = error.message;
    } finally {
      cancelling.delete(job.job_id);
      if (session === generation) render();
    }
  };
  return button;
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
      cancelButton(j),
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
function dashboardStats(entries) {
  return add(el("div", null, "dashboard-stats"), ...entries.map(([label, number, note, tone]) =>
    add(el("article", null, "dashboard-stat " + (tone || "")), el("small", label),
      el("strong", number), el("span", note))));
}
function actionLink(text, href) { const link = el("a", text, "action-link"); link.href = href; return link; }
function nodeStatus(n, s) {
  const slurm = s.backend === "slurm", sample = observed(slurm ? n.scheduler_state : n.ready, s);
  const known = sample.status === "ok";
  const ready = known && (slurm ? Array.isArray(sample.value) && !(n.scheduling_blockers || []).length : sample.value === true && !(n.scheduling_blockers || []).length);
  return { ready, known, label: !known ? labels[sample.status] || "확인 불가" : slurm ? sample.value.join(" · ") : ready ? "Ready" : "스케줄 불가" };
}
function nodeCard(n, s) {
  const slurm = s.backend === "slurm", status = nodeStatus(n, s), cpu = n.resources.cpu || {}, mem = n.resources.memory || {};
  const devices = Object.entries(n.resources).filter(([,r]) => ["gpu", "npu"].includes(r.device_class));
  const head = add(el("div", null, "node-card-head"),
    add(el("div"), el("small", `${slurm ? "SLURM" : "KUBERNETES"} · ${n.hardware?.architecture || "ARCH —"}`), el("h3", n.node_ref)),
    badge(status.label, status.ready ? "good" : "warn"));
  const card = add(el("article", null, "node-card"), head);
  const metrics = add(el("div", null, "node-metrics"),
    measure("CPU 사용", slurm ? n.telemetry["node:cpu_non_idle_cores"] : n.telemetry.cpu_usage_cores, s,
      value(slurm ? n.telemetry["node:cpu_count"] : cpu.capacity, s), "cores"),
    measure("메모리 여유", n.telemetry["node:memory_available_bytes"], s,
      value(slurm ? n.telemetry["node:memory_total_bytes"] : mem.capacity, s), "GiB", 2 ** 30));
  card.append(metrics);
  const allocation = el("div", null, "node-reservations");
  allocation.append(el("span", `미예약 CPU ${fmt(value(cpu.request_headroom, s))} cores`),
    el("span", `미예약 메모리 ${value(mem.request_headroom,s) == null ? "—" : fmt(value(mem.request_headroom,s) / 2 ** 30)} GiB`));
  card.append(allocation);
  for (const [key, r] of devices) card.append(add(el("div", null, "device-allocation"),
    add(el("div"), badge(r.device_class.toUpperCase()), el("strong", key)),
    el("span", `${fmt(value(r.requested,s))} 예약 / ${fmt(value(r.allocatable,s))} ${modes[r.allocation_mode] || "단위 미확인"}`)));
  const utils = Object.entries(n.telemetry).filter(([k]) => k.endsWith(":utilization"));
  for (const [key, sample] of utils) card.append(measure(key.split(":")[0] + " 사용률", sample, s, 100, "%"));
  if (!utils.length) card.append(el("small", devices.length ? "가속기 사용률 미측정" : "가속기 등록 정보 없음", "node-note"));
  const facts = Object.fromEntries(Object.entries(n.telemetry).filter(([k]) => /power|temperature|memory_used|memory_total/.test(k) && !k.startsWith("node:"))
    .map(([k,sample]) => [k, observed(sample,s)]));
  if (Object.keys(facts).length) card.append(details("가속기 메모리 · 전력 · 온도", facts));
  const linked = (data.submission_catalog || []).flatMap(w => w.candidates
    .filter(c => c.backend === (slurm ? "slurm" : "kubernetes") && c.node_ref === n.node_ref)
    .map(c => ({w,c})));
  if (linked.length) {
    card.append(el("small", "기본 실행 작업 · 선택 후 제출", "node-note"));
    for (const {w,c} of linked) card.append(selectTargetButton(w,c));
  } else card.append(add(el("div",null,"node-note"),
    el("span", slurm ? "Slurm 작업은 제출 목록에서 확인" : devices.length ? "기본 실행 작업 연결 필요" : "가속기 자원 등록 확인 필요"),
    actionLink("작업 목록 →", "#submit")));
  return card;
}
function workloadPurpose(w) {
  return w.measurement_boundary?.startsWith("cuda-squares-") ? "GPU 연산 점검 · AI 모델 아님"
    : ({inference:"모델 추론",training:"모델 학습",benchmark:"벤치마크",preprocessing:"전처리"}[w.task_type] || w.task_type);
}
function selectTargetButton(w,c) {
  const button=el("button",`${c.model || c.candidate_ref} · 작업 선택`);
  button.onclick=()=>{
    showAllTemplates=false;submissionDraft.workload=w.workload_ref;submissionDraft.candidate=c.candidate_ref;
    $("auto").checked=false;
    if(location.hash==="#submit")render();else location.hash="submit";
    $("submit-candidate")?.focus();
  };
  return button;
}
function executionTargets() {
  const rows=(data.submission_catalog || []).flatMap(w=>w.candidates.map(c=>[
    add(el("div"),el("strong",c.model || c.candidate_ref),el("small",c.node_ref || "노드 확인 필요")),
    add(el("div"),badge(c.backend),el("small",`${(c.device_class || "unknown").toUpperCase()} · ${modes[c.allocation_mode] || c.allocation_mode}`)),
    add(el("div"),el("strong",workloadPurpose(w)),el("small",w.workload_ref)),
    add(el("div"),badge(canSubmit(c)?"제출 가능":"조건 확인",canSubmit(c)?"good":"warn"),
      ...(c.submission_warnings || []).map(r=>el("small",reasonText(r))),
      ...(c.submission_reasons || []).map(r=>el("small",reasonText(r)))),selectTargetButton(w,c)
  ]));
  return panel("연결된 GPU · NPU 작업", "장비별 런타임과 작업을 연결한 기본 목록입니다. GPU 연산 점검은 학습·추론 성능 검증과 구분합니다.",
    rows.length?table(["장비","실행 환경 · 단위","작업 범위","상태","선택"],rows):empty("기본 실행 작업이 없습니다."));
}
function queueCards() {
  const cards=el("div",null,"queue-cards");
  for(const s of data.inventory) {
    if(s.backend==="slurm") {
      const q=observed(s.slurm_queue,s), v=q.value;
      cards.append(add(el("article",null,"queue-card"),el("small","SLURM · "+s.cluster_ref),
        el("h3",q.status==="ok"?`${v.account} / ${v.partition}`:"큐 상태 확인 필요"),
        q.status==="ok"?add(el("div",null,"queue-numbers"),add(el("div"),el("strong",v.pending_records),el("small","대기 레코드")),add(el("div"),el("strong",v.running_records),el("small","실행 레코드"))):badge(labels[q.status]||"확인 불가","warn"),
        el("small","설정된 계정·파티션 범위"),actionLink("큐 자세히 →","#history")));
    } else for(const entry of s.cluster_queues || []) {
      const q=observed(entry.observation,s),v=q.value;
      cards.append(add(el("article",null,"queue-card"),el("small","KUEUE · CLUSTER QUEUE"),el("h3",entry.ref),
        q.status==="ok"?add(el("div",null,"queue-numbers"),add(el("div"),el("strong",v.pending_workloads??"—"),el("small","대기")),add(el("div"),el("strong",v.admitted_workloads??"—"),el("small","승인"))):badge(labels[q.status]||"확인 불가","warn"),
        el("small","ClusterQueue 전체 네임스페이스 합계"),actionLink("쿼터·승인 조건 →","#history")));
    }
  }
  return cards.childElementCount?cards:empty("큐 관측 정보가 없습니다. 빈 큐로 판단하지 않습니다.");
}
function jobAction(job) {
  const open=el("button","상세 · 조치");open.onclick=()=>showJob(job);
  return add(el("div",null,"job-actions"),open,cancelButton(job));
}
function operationsView() {
  const op=data.operations, root=el("div",null,"operations-page");
  if(!op)return empty("운영 집계를 불러오지 못했습니다.");
  root.append(add(el("section",null,"operations-intro"),
    add(el("div"),el("p","SHARED COMPUTE · "+data.project_ref,"eyebrow"),el("h2","공동 자원 운영"),el("p","지금 기다리는 작업과 확인할 문제부터 살펴보세요.")),
    add(el("div",null,"hero-actions"),actionLink("작업 제출 →","#submit"),actionLink("장비 사용량 보기","#execution"))));
  const kpis=dashboardStats([["실행 중",op.running_total,"현재 프로젝트"],["접수 · 대기",op.waiting_total,"백엔드 대기 사유 확인"],["취소 확인 중",op.cancel_pending_total,"자원 반환 확인 전"],["최근 실패",op.failed_24h_total,"최근 24시간"],["장비 확인 필요",op.node_issues.length,"오래된 관측 포함"]]);
  kpis.classList.add("ops-kpis");root.append(kpis,queueCards());
  const activeRows=op.active_jobs.map(j=>[
    add(el("div",null,"job-name"),el("strong",j.template?.name||j.workload_ref),code(j.job_id)),
    add(el("div"),stateBadge(j.state),el("small",j.backend+" · "+(j.priority==="high"?"높음":"보통"))),
    add(el("div"),el("strong",j.attention.title),el("small",j.attention.action),j.attention.reason_code?code(j.attention.reason_code):null),
    duration(j.since_submission_seconds),jobAction(j)]);
  root.append(panel("진행 중인 작업",`현재 프로젝트 ${op.active_total}개 · 오래된 접수 순으로 최대 ${op.active_limit}개 표시 · 경과 시간은 접수 이후 시간입니다.`,
    activeRows.length?table(["작업","상태 · 우선순위","대기 사유 · 확인할 내용","접수 후 경과","조치"],activeRows):empty("현재 프로젝트에 진행 중인 작업이 없습니다.")));
  const incidents=el("div",null,"incident-list");
  for(const issue of op.node_issues) {
    const open=el("button","장비 확인");open.onclick=()=>{resourceSearch=issue.node_ref;resourceBackend=issue.backend;location.hash="execution";};
    incidents.append(add(el("article",null,"incident-row"),add(el("div"),badge(issue.title,"warn"),el("h3",issue.node_ref),el("p",issue.action),el("small",`${issue.backend} · ${stamp(issue.observed_at)}`)),open));
  }
  for(const j of op.recent_failures) incidents.append(add(el("article",null,"incident-row"),
    add(el("div"),stateBadge(j.state),el("h3",j.template?.name||j.workload_ref),el("p",j.attention.title+" · "+j.attention.action),el("small",`${j.backend} · ${stamp(j.finished_at)}`)),jobAction(j)));
  root.append(panel("확인할 문제", "현재 장비 관측과 최근 24시간 실패 작업입니다. 해결 완료를 자동 판정하지 않습니다.",incidents.childElementCount?incidents:empty("관측 범위에서 표시할 장비 문제나 최근 실패 작업이 없습니다.")));
  root.append(add(el("div",null,"operations-links"),actionLink("할당량·우선순위 확인 →","#history"),actionLink("프로젝트 사용량 →","#usage")));
  return root;
}
function usageView() {
  const u=data.operations?.usage, root=el("div");if(!u)return empty("사용량 집계를 불러오지 못했습니다.");
  root.append(panel("프로젝트 할당 사용량",`${data.project_ref} · 보관된 종료 작업 전체 · 다른 프로젝트의 기록은 포함하지 않습니다.`,
    dashboardStats([["종료 작업",u.attempts,"완료·실패·취소 포함"],["평균 큐 대기",u.queue_mean_seconds==null?"—":duration(u.queue_mean_seconds),`${u.queue_known_attempts}개 측정 기록 기준`],["대기 시간 미확인",u.queue_unknown_attempts,"0초로 계산하지 않음"],["자원 구분",u.groups.length,"백엔드·장치·할당 방식별"]])));
  const groups=u.groups;
  const rows=groups.map(g=>[data.project_ref,add(el("div"),badge(g.backend),el("small",g.accelerator_model)),
    add(el("div"),el("strong",g.device_class.toUpperCase()),el("small",modes[g.allocation_mode]||g.allocation_mode)),
    g.attempts,g.device_class==="cpu"?"가속기 시간 해당 없음":g.unknown_allocation_attempts===g.attempts?"미확인":fmt(g.known_allocated_device_seconds/3600,4)+(g.allocation_mode==="virtual_slot"?" 슬롯·h":" 장치·h"),
    g.unknown_allocation_attempts,details("종료 상태",g.outcomes)]);
  const exportButton=el("button","사용량 CSV 내려받기");exportButton.disabled=!groups.length;
  exportButton.onclick=()=>{
    const fields=["project","backend","device_class","allocation_mode","accelerator_model","attempts","known_allocated_device_hours","unknown_allocation_attempts"];
    const cell=v=>'"'+String(v??"").replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';
    const lines=[fields,...groups.map(g=>[data.project_ref,g.backend,g.device_class,g.allocation_mode,g.accelerator_model,g.attempts,g.known_allocated_device_seconds/3600,g.unknown_allocation_attempts])];
    const url=URL.createObjectURL(new Blob(["\ufeff"+lines.map(row=>row.map(cell).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
    const a=el("a");a.href=url;a.download="project-usage.csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  root.append(panel("백엔드·장치별 집계", "실제로 관측된 예약 시간입니다. GPU·NPU·공유 슬롯을 서로 합산하거나 실제 사용률로 해석하지 않습니다.",
    add(el("div"),add(el("div",null,"panel-head"),exportButton),groups.length?table(["프로젝트","백엔드 · 장비","할당 단위","작업 수","확인된 할당 시간","미확인 작업","종료 상태"],rows):empty("집계할 종료 기록이 없습니다."))));
  root.append(ledgerView());return root;
}
function execution() {
  const root = el("div");
  const allNodes = data.inventory.flatMap(s => s.nodes.map(n => ({n,s})));
  const ready = allNodes.filter(({n,s}) => nodeStatus(n,s).ready).length;
  const gpu = allNodes.filter(({n}) => Object.values(n.resources).some(r => r.device_class === "gpu")).length;
  const npu = allNodes.filter(({n}) => Object.values(n.resources).some(r => r.device_class === "npu")).length;
  const strip = el("div", null, "fleet-strip");
  for (const {n,s} of allNodes) {
    const status = nodeStatus(n,s);
    const item = add(el("div", null, "fleet-node " + (status.ready ? "ready" : "unknown")), el("small", s.backend === "slurm" ? "SLURM" : "K8S"), el("strong", n.node_ref));
    item.title = `${n.node_ref} · ${status.label}`; strip.append(item);
  }
  const counts = data.job_counts;
  const total = Object.values(counts).reduce((a,b) => a+b,0);
  const waiting = ["RECEIVED","VALIDATED","SUBMITTING","SUBMISSION_UNKNOWN","QUEUED"].reduce((a,k) => a+(counts[k]||0),0);
  const totals = el("div", null, "fleet-totals");
  const sumKnown = (samples, scale=1) => samples.length && samples.every(v => typeof v === "number") ? fmt(samples.reduce((a,b)=>a+b,0)/scale) : "—";
  const cpuTotal = sumKnown(allNodes.map(({n,s}) => value(n.resources.cpu?.capacity,s)));
  const memTotal = sumKnown(allNodes.map(({n,s}) => value(n.resources.memory?.capacity,s)),2**30);
  for (const [label,num] of [["CPU",cpuTotal+" cores"],["MEMORY",memTotal+" GiB"],["GPU / NPU",`${gpu} / ${npu} nodes`],["BACKENDS",`${data.inventory.length} clusters`]])
    totals.append(add(el("div"),el("small",label),el("strong",num)));
  root.append(add(el("section", null, "fleet-hero"),
    add(el("div",null,"operations-headline"), el("p", "● LIVE OBSERVATION · " + stamp(data.generated_at), "live-kicker"),
      el("p", "RESEARCH INFRASTRUCTURE CONTROL ROOM", "eyebrow"),
      add(el("h2"),el("strong",`${ready}/${allNodes.length}`),document.createTextNode(" nodes ready.")),
      el("p", "Kubernetes와 Slurm의 장비 상태, 실제 사용량과 작업 실행을 한곳에서 확인합니다."),
      add(el("div", null, "hero-actions"), actionLink("새 작업 제출 →", "#submit"), actionLink("작업 현황", "#jobs"))),
    add(el("div", null, "fleet-instrument"), add(el("div",null,"instrument-head"),el("small", "FLEET / OBSERVED HARDWARE"),el("small", `${ready} READY · ${allNodes.length-ready} 확인 필요`)), strip,totals)));
  const flow = el("div",null,"execution-flow");
  for (const [step,label,count,note] of [["01 · REQUEST","작업 접수",total,"프로젝트 전체 기록"],["02 · WAIT","대기 · 준비",waiting,"접수 / 제출 / 자원 대기"],["03 · EXECUTE","백엔드 실행",counts.RUNNING||0,"Kubernetes · Slurm"],["04 · COLLECT","결과 수집",counts.SUCCEEDED||0,"완료된 실행 기록"]])
    flow.append(add(el("a",null,"flow-step"),el("small",step),el("strong",label),el("b",String(count)),el("small",note)));
  for (const link of flow.children) link.href="#jobs";
  const flowPanel = panel("요청에서 실행 결과까지.","현재 프로젝트의 상태별 작업 수입니다. 개별 작업의 진행 단계는 작업 현황에서 확인하세요.",flow);
  flowPanel.classList.add("flow-panel"); root.append(flowPanel);
  const kpis = dashboardStats([["WORKLOADS",total,"프로젝트 전체 작업"],["RUNNING",counts.RUNNING||0,"현재 실행"],["QUEUE",waiting,"접수 · 대기"],["GPU NODES",gpu,"GPU 등록 노드"],["NPU NODES",npu,"NPU 등록 노드"]]);
  kpis.classList.add("ops-kpis"); root.append(kpis);
  const instruments = el("div",null,"command-grid");
  const signals = el("div",null,"accelerator-signals"), allocations = el("div",null,"allocation-list");
  for (const {n,s} of allNodes) {
    const devices=Object.entries(n.resources).filter(([,r])=>["gpu","npu"].includes(r.device_class));
    if (!devices.length) continue;
    const card=add(el("article",null,"accelerator-signal"),el("small",s.backend==="slurm"?"SLURM":"KUBERNETES"),el("h3",n.node_ref));
    const utils=Object.entries(n.telemetry).filter(([k])=>k.endsWith(":utilization"));
    if (!utils.length) card.append(el("p","가속기 사용률 미측정","muted"));
    for (const [key,sample] of utils) card.append(measure(key.split(":")[0]+" 사용률",sample,s,100,"%"));
    const facts=Object.entries(n.telemetry).filter(([k])=>/:(power|temperature)$/.test(k));
    const factsRow=el("div",null,"signal-facts");
    for (const [key,sample] of facts) factsRow.append(add(el("div"),el("small",key.endsWith(":power")?"POWER":"TEMP"),el("strong",value(sample,s)==null?"—":fmt(value(sample,s))+(key.endsWith(":power")?" W":" °C"))));
    card.append(factsRow); signals.append(card);
    for (const [key,r] of devices) allocations.append(add(el("div",null,"allocation-row"),
      add(el("div"),el("strong",n.node_ref),badge(r.device_class.toUpperCase())),el("small",key),
      measure("예약 / 할당 가능",r.requested,s,value(r.allocatable,s),modes[r.allocation_mode]||"단위 미확인")));
  }
  instruments.append(panel("GPU · NPU 관측소","PHYSICAL SIGNAL · 측정된 실제 사용량",signals.childElementCount?signals:empty("관측된 가속기가 없습니다.")),
    panel("가속기 공유 · 예약","LOGICAL ALLOCATION · 예약량은 실제 사용률과 다릅니다.",allocations.childElementCount?allocations:empty("등록된 가속기 자원이 없습니다.")));
  root.append(instruments);
  root.append(add(el("div",null,"section-heading"),el("p","RESOURCE FLEET","eyebrow"),el("h2","전체 장비"),el("p","노드별 CPU · 메모리 여유와 가속기 할당을 확인합니다.")));
  const toolbar = el("div", null, "view-toolbar");
  for (const [key,label] of [["all","전체 장비"],["kubernetes","Kubernetes"],["slurm","Slurm"]]) {
    const b = el("button",label,resourceBackend === key ? "selected" : ""); b.setAttribute("aria-pressed", String(resourceBackend === key));
    b.onclick = () => { resourceBackend = key; render(); }; toolbar.append(b);
  }
  const search = el("input"); search.id = "resource-search"; search.type = "search"; search.placeholder = "노드 · GPU · NPU 검색"; search.setAttribute("aria-label", "자원 검색"); search.value = resourceSearch;
  search.oninput = () => { resourceSearch = search.value; render(); }; toolbar.append(search); root.append(toolbar);
  const selected = allNodes.filter(({n,s}) => (resourceBackend === "all" || (s.backend || "kubernetes") === resourceBackend)
    && (n.node_ref + " " + Object.keys(n.resources).join(" ")).toLowerCase().includes(resourceSearch.toLowerCase()));
  const grid = add(el("div", null, "node-grid"), ...selected.map(({n,s}) => nodeCard(n,s)));
  root.append(selected.length ? grid : empty("조건에 맞는 자원이 없습니다."));
  root.append(el("p", "예약 여유와 실제 사용률은 다릅니다. 오래된 값·미측정 값은 막대를 표시하지 않습니다.", "muted"));
  const raw = add(el("details", null, "panel"), el("summary", "클러스터별 상세 자원 표", "panel-head"));
  data.inventory.forEach(s => raw.append(resourcePanel(s))); root.append(raw);
  return root;
}
function showJob(job) {
  const m = job.result?.measurements;
  const content = add(el("div", null, "job-detail-content"),
    el("p", job.template?.name || job.workload_ref, "detail-workload"),
    job.template ? el("small", "실행 환경: " + job.workload_ref) : null, code(job.job_id),
    add(el("div", null, "state-line"), stateBadge(job.state), badge(job.backend)),
    dashboardStats([["실행 노드", job.node_ref || "—", "대상 노드"], ["생성 시각", stamp(job.created_at), "서버 기록"]]),
    el("p", `CPU ${job.requested_resources?.host_cpu ?? "—"} · 메모리 ${job.requested_resources?.host_memory_mib ?? "—"} MiB · 가속기 ${job.requested_resources?.accelerator_count ?? "—"}`),
    el("p", "최근 백엔드 확인: " + stamp(job.backend_observed_at)),
    job.attention ? panel(job.attention.title, job.attention.action,
      add(el("div",null,"evidence"),job.scheduler_reason?code(job.scheduler_reason):null,
        el("p",`우선순위 ${job.priority==="high"?"높음":"보통"} · 실행 제한 ${job.execution_limits?.max_run_seconds??"—"}초`),
        table(["단계","관측 시각"],[["접수",stamp(job.created_at)],["백엔드 제출",stamp(job.queued_at)],["자원 할당 · 시작",stamp(job.started_at)],["종료 기록",stamp(job.finished_at)]]))) : null,
    job.scheduling_plan ? details("적용한 SchedulingProfile · 정책과 백엔드 설정", job.scheduling_plan) : null,
    job.error ? el("p",job.error,"error-message") : null,
    job.last_observation_error ? el("p", "최근 관측 오류: " + job.last_observation_error,"error-message") : null,
    m ? panel("실행 결과", "수집된 측정 구간의 값입니다.", dashboardStats([
      ["실행 시간", duration(m.elapsed_seconds), "모델 측정 구간"],
      ["p95 지연", m.latency_p95_ms == null ? "—" : fmt(m.latency_p95_ms) + " ms", "미측정은 — 표시"],
      ["처리량", m.throughput == null ? "—" : fmt(m.throughput), "측정값"],
    ])) : el("p", "아직 수집된 실행 결과가 없습니다."),
    details("전체 기록 · 결과 · MLflow", job));
  const cancel = cancelButton(job);
  if (cancel) { const handler = cancel.onclick; cancel.onclick = async () => { await handler(); $("job-dialog").close(); }; content.append(cancel); }
  $("job-dialog-body").replaceChildren(content); $("job-dialog").showModal();
}
function jobsView() {
  const root = el("div"), counts = data.job_counts;
  const total = Object.values(counts).reduce((a,b) => a+b,0);
  const waiting = ["RECEIVED","VALIDATED","SUBMITTING","SUBMISSION_UNKNOWN","QUEUED"].reduce((sum,k) => sum+(counts[k]||0),0);
  root.classList.add("jobs-page");
  root.append(add(el("div",null,"section-heading"),el("p","EXECUTIONS","eyebrow"),el("h2","작업 이력"),el("p",`전체 ${total}개 · 실행 ${counts.RUNNING||0}개 · 대기 ${waiting}개 · 완료 ${counts.SUCCEEDED||0}개`)));
  const toolbar = el("div",null,"jobs-toolbar"), search = el("input");
  search.id = "job-search"; search.type = "search"; search.maxLength = 128; search.placeholder = "작업 이름 · ID · 노드 검색"; search.setAttribute("aria-label","작업 검색"); search.value = jobFilters.search;
  const refreshFilters = () => { pages.jobs = 0; filterRevision++; load(); };
  search.oninput = () => { jobFilters.search = search.value; clearTimeout(searchTimer); searchTimer = setTimeout(refreshFilters,300); };
  const backend = el("select"); backend.setAttribute("aria-label","작업 백엔드");
  for (const [key,label] of [["all","모든 백엔드"],["kubernetes","Kubernetes"],["slurm","Slurm"]]) backend.append(new Option(label,key));
  backend.value = jobFilters.backend; backend.onchange = () => { jobFilters.backend = backend.value; refreshFilters(); };
  toolbar.append(search,backend);
  const filters = el("div",null,"status-filters");
  for (const [key,label] of [["all","전체"],["running","실행"],["pending","대기"],["succeeded","완료"],["failed","실패"],["canceled","취소"]]) {
    const b = el("button",label,jobFilters.status===key ? "selected" : ""); b.setAttribute("aria-pressed",String(jobFilters.status===key));
    b.onclick = () => { jobFilters.status=key; refreshFilters(); }; filters.append(b);
  }
  toolbar.append(filters);
  const rows = data.jobs.items.map(j => {
    const open = el("button", "상세 보기", "details-action"); open.setAttribute("aria-label", `상세 보기 · ${j.job_id}`); open.onclick = () => showJob(j);
    return [add(el("div",null,"job-name"),el("strong",j.template?.name || j.workload_ref),code(j.job_id)),add(el("div"),stateBadge(j.state),j.scheduler_reason?el("small",j.attention?.title||j.scheduler_reason):null),
      add(el("div"),badge(j.backend),el("small",j.node_ref)),
      el("span",`CPU ${j.requested_resources?.host_cpu ?? "—"} · GPU/NPU ${j.requested_resources?.accelerator_count ?? "—"}`),
      stamp(j.created_at),add(el("div",null,"job-actions"),open,cancelButton(j))];
  });
  root.append(add(el("section",null,"panel jobs-panel"), toolbar, rows.length ? table(["작업","상태","실행 대상","요청 자원","생성 시각","관리"],rows) : empty("조건에 맞는 작업이 없습니다."),pager("jobs",data.jobs)));
  return root;
}
function schedulingKey(workload) {
  return JSON.stringify([generation, schedulingProfile, selectionKey(workload), submissionDraft.candidate]);
}
function schedulingControls(workload) {
  const section = el("div", null, "submission-summary");
  const select = el("select"); select.id = "scheduling-profile";
  select.append(new Option("기존 작업 설정 사용", ""));
  for (const p of data.scheduling_profiles || []) select.append(new Option(`${p.name} · v${p.version}`, p.ref));
  select.value = schedulingProfile;
  select.onchange = () => { schedulingProfile = select.value; schedulingPreview = null; schedulingPreviewKey = ""; render(); };
  const label = el("label", "공통 스케줄링 프로필"); label.htmlFor = select.id;
  section.append(label, select);
  if (!schedulingProfile) return section;
  const key = schedulingKey(workload);
  const preview = el("button", "정책 적용 미리보기"); preview.type = "button";
  preview.onclick = async () => {
    preview.disabled = true; preview.textContent = "확인 중…";
    const session = generation;
    try {
      const response = await fetch(API + "/scheduling-plans", {
        method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ profile_ref: schedulingProfile, workload_ref: workload.workload_ref,
          ...(submissionDraft.candidate ? { candidate_ref: submissionDraft.candidate } : {}),
          ...(workload.template_ref ? { template_ref: workload.template_ref } : {}) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "정책을 확인하지 못했습니다.");
      if (session !== generation || key !== schedulingKey(workload)) return;
      schedulingPreviewKey = key; schedulingPreview = result; render();
    } catch (error) { if (session === generation) $("notice").textContent = error.message; }
    finally { preview.disabled = false; preview.textContent = "정책 적용 미리보기"; }
  };
  section.append(el("small", "장비를 선택하지 않으면 프로필의 백엔드 순서로 호환 후보를 선택합니다. 현재 빈 자원이나 성능에 따른 추천은 아닙니다."), preview);
  if (schedulingPreviewKey !== key || !schedulingPreview) return section;
  const result = schedulingPreview;
  if (!result.accepted) {
    section.append(badge("정책 적용 불가", "warn"), ...result.reasons.map(r => el("p", reasonText(r))));
    for (const [candidate, reasons] of Object.entries(result.excluded || {})) section.append(el("p", candidate + ": " + reasons.map(reasonText).join(" · ")));
  } else {
    const plan = result.plan;
    section.append(badge("정책 검증 통과", "good"), el("p", `${plan.backend} · ${plan.candidate_ref} · ${plan.node_ref}`),
      el("p", `우선순위 ${plan.execution.priority} · 실행 ${plan.execution.max_run_seconds}초 · 대기 ${plan.execution.max_queue_seconds}초`),
      el("p", plan.backend === "kubernetes" ? `LocalQueue: ${plan.adapter.local_queue} · 우선순위 클래스: ${plan.adapter.priority_class || "기본"}` : `Partition: ${plan.adapter.partition} · Account: ${plan.adapter.account} · QOS: ${plan.adapter.qos}`),
      el("small", "할당량과 선점은 각 백엔드의 기존 정책을 따릅니다. 이 미리보기는 자원 예약이 아닙니다."));
  }
  return section;
}
function submissionView() {
  const root = el("div");
  root.append(executionTargets());
  const catalogMode = !showAllTemplates && data.submission_catalog !== null && data.submission_catalog !== undefined;
  const sources = catalogMode ? data.submission_catalog : data.compatibility.items;
  const items = [...(data.templates?.items || []), ...sources];
  const create = el("button", templateEditorOpen ? "등록 폼 닫기" : "＋ 새 템플릿 등록", "primary");
  create.type = "button"; create.onclick = () => { templateEditorOpen = !templateEditorOpen; $("auto").checked = false; render(); if(templateEditorOpen) $("template-name")?.focus(); };
  root.append(add(el("div",null,"template-entry"),add(el("div"),el("h2","작업 템플릿"),el("p","실행 환경을 골라 템플릿을 저장하고, 필요할 때 다시 실행하세요.")),create));
  if (templateEditorOpen) root.append(templateEditor(sources));
  const showAll = el("input"); showAll.type = "checkbox"; showAll.checked = showAllTemplates;
  showAll.onchange = () => { showAllTemplates = showAll.checked; render(); };
  root.append(add(el("label", null, "template-filter"), showAll, document.createTextNode(" 고급: 전체 실험·테스트 템플릿 보기")));
  const form = el("div", null, "job-form");
  const workloadSelect = el("select");
  workloadSelect.id = "submit-workload";
  workloadSelect.append(new Option("실행할 작업을 선택하세요", ""));
  const types = { inference: "추론", training: "학습", benchmark: "벤치마크", preprocessing: "전처리" };
  for (const w of items) workloadSelect.append(new Option(
    `${w.name || w.workload_ref} · ${types[w.task_type] || w.task_type}${w.template_ref ? " · 저장한 템플릿" : ""}`, selectionKey(w)));
  workloadSelect.value = submissionDraft.workload;
  workloadSelect.onchange = () => {
    submissionDraft.workload = workloadSelect.value;
    submissionDraft.candidate = items.find(w => selectionKey(w) === workloadSelect.value)?.template_ref
      ? items.find(w => selectionKey(w) === workloadSelect.value).candidates[0]?.candidate_ref || "" : "";
    render();
    $("submit-candidate")?.focus();
  };
  const label = el("label", "1. 작업 템플릿"); label.htmlFor = workloadSelect.id;
  form.append(label, workloadSelect,
    el("small", "템플릿에 등록된 모델·입력·실행 환경을 사용합니다. 제출할 때마다 새 실행 기록이 만들어집니다."));
  const workload = items.find(w => selectionKey(w) === submissionDraft.workload);
  if (workload) {
    form.append(el("p", `${types[workload.task_type] || workload.task_type} · ${workload.precision} · 배치 ${workload.batch_size ?? "—"} · 입력 ${(workload.input_shape || []).join(" × ")}`));
    const candidates = el("select"); candidates.id = "submit-candidate";
    candidates.append(new Option(schedulingProfile ? "프로필로 자동 선택" : "실행 장비를 선택하세요", ""));
    for (const c of workload.candidates) candidates.append(new Option(
      `${c.backend === "slurm" ? "Slurm" : "Kubernetes"} · ${c.model || c.candidate_ref} · ${c.candidate_ref}${canSubmit(c) ? "" : " · 설정 확인 필요"}`, c.candidate_ref));
    candidates.value = submissionDraft.candidate;
    candidates.onchange = () => {
      submissionDraft.candidate = candidates.value;
      render(); $("submit-candidate")?.focus();
    };
    const targetLabel = el("label", "2. 실행 장비 · 백엔드"); targetLabel.htmlFor = candidates.id;
    form.append(targetLabel, candidates);
    form.append(schedulingControls(workload));
    const scheduling = schedulingProfile && schedulingPreviewKey === schedulingKey(workload) && schedulingPreview?.accepted ? schedulingPreview : null;
    const candidate = workload.candidates.find(c => c.candidate_ref === (submissionDraft.candidate || scheduling?.plan.candidate_ref));
    const effectiveWorkload = scheduling ? { ...workload, ...scheduling.plan.execution } : workload;
    if (candidate) {
      const r = candidate.resources;
      const summary = add(el("div", null, "submission-summary"),
        el("h3", "3. 요청 자원 확인 후 제출"),
        el("p", `${candidate.model || candidate.candidate_ref} · ${candidate.node_ref || "노드 미확인"}`),
        el("p", `가속기 ${r.accelerator_count}개 (${modes[candidate.allocation_mode] || "단위 미확인"}) · CPU ${r.host_cpu}코어 · 메모리 ${r.host_memory_mib} MiB`),
        el("p", `실행 제한 ${effectiveWorkload.max_run_seconds}초 · 대기 제한 ${effectiveWorkload.max_queue_seconds ?? "—"}초 · 우선순위 ${effectiveWorkload.priority === "high" ? "높음" : "보통"}`),
        badge(canSubmit(candidate) ? "제출 가능" : "설정 확인 필요", canSubmit(candidate) ? "good" : "warn"),
        ...(candidate.submission_reasons || candidate.reasons).map(reason => el("p", reasonText(reason), "reason")),
        ...(candidate.submission_warnings || []).map(reason => el("small", reasonText(reason) + " · 일반 실행은 허용됩니다. 검증 기록은 갱신하지 않습니다.")),
        el("small", candidate.backend === "slurm"
          ? "Slurm이 자원과 우선순위에 따라 실행합니다. 여유 자원이 없으면 큐에서 기다립니다."
          : "Kueue 승인 후 Kubernetes에서 실행합니다. 여유 자원이나 할당량이 없으면 큐에서 기다립니다."),
        schedulingProfile && !scheduling ? el("p", "정책 적용 미리보기를 통과한 뒤 제출할 수 있습니다.") : submitButton(effectiveWorkload, candidate, null, scheduling));
      form.append(summary);
    } else form.append(empty("장비를 선택하면 요청 자원과 제출 버튼이 표시됩니다."));
    if (!workload.template_ref) form.append(add(el("details"), el("summary", "어떤 장비를 선택할지 모르겠다면"), recommendButton(workload)));
  }
  root.append(panel("새 작업 만들기", "제출 후 실행 현황에서 대기·실행·결과를 확인하고 작업을 취소할 수 있습니다.",
    items.length ? form : empty("등록된 작업 템플릿이 없습니다. 아래에서 작업 명세를 등록하세요.")));
  if (!catalogMode) root.append(pager("compatibility", data.compatibility));
  if (data.templates?.total > data.templates?.size) root.append(pager("templates", data.templates));
  root.append(workloadImport());
  return root;
}
function templateEditor(sources) {
  const form = el("form", null, "job-form template-form");
  function field(label, input) { const l=el("label",label); l.htmlFor=input.id; return add(el("div",null,"template-field"),l,input); }
  const name=el("input"); name.id="template-name"; name.required=true; name.maxLength=100; name.placeholder="예: Orin 추론 테스트"; name.value=templateDraft.name || "";
  name.oninput=()=>{templateDraft.name=name.value;};
  const source=el("select"); source.id="template-source"; source.required=true;
  source.append(new Option("모델 · 실행 환경 선택", ""));
  for(const w of sources) source.append(new Option(w.workload_ref+" · "+w.task_type,w.workload_ref));
  source.value=templateDraft.workload || "";
  source.onchange=()=>{templateDraft.workload=source.value; templateDraft.candidate=""; delete templateDraft.run; delete templateDraft.queue; render(); $("template-device")?.focus();};
  const w=sources.find(x=>x.workload_ref===templateDraft.workload);
  const device=el("select"); device.id="template-device"; device.required=true;
  device.append(new Option("장비 · 자원 구성 선택", ""));
  for(const c of w?.candidates || []) device.append(new Option(`${c.backend} · ${c.model || c.candidate_ref} · CPU ${c.resources.host_cpu} / ${c.resources.host_memory_mib} MiB`,c.candidate_ref));
  device.value=templateDraft.candidate || "";
  device.onchange=()=>{templateDraft.candidate=device.value; render(); $("template-device")?.focus();};
  const priority=el("select"); priority.id="template-priority";
  priority.append(new Option("보통","normal"),new Option("높음 · 백엔드 정책 적용","high")); priority.value=templateDraft.priority || "normal";
  priority.onchange=()=>{templateDraft.priority=priority.value;};
  const number=(id,val,max)=>{const n=el("input");n.id=id;n.type="number";n.min=1;n.max=max;n.step=1;n.required=true;n.value=val;return n;};
  const run=number("template-run",templateDraft.run || w?.max_run_seconds || 60,w?.max_run_seconds || 86400);
  const queue=number("template-queue",templateDraft.queue || w?.max_queue_seconds || 600,w?.max_queue_seconds || 86400);
  run.oninput=()=>{templateDraft.run=run.value;}; queue.oninput=()=>{templateDraft.queue=queue.value;};
  const grid=add(el("div",null,"template-fields"),field("템플릿 이름",name),field("모델 · 실행 환경",source),field("장비 · 요청 자원",device),field("우선순위",priority),field("실행 시간 제한 (초)",run),field("대기 시간 제한 (초)",queue));
  const selected=w?.candidates.find(c=>c.candidate_ref===templateDraft.candidate);
  const summary=selected ? add(el("div",null,"template-runtime"),
    el("strong",`${selected.model || selected.candidate_ref} · ${selected.backend}`),
    el("p",`CPU ${selected.resources.host_cpu}코어 · 메모리 ${selected.resources.host_memory_mib} MiB · 가속기 ${selected.resources.accelerator_count} (${modes[selected.allocation_mode] || "등록 단위"})`),
    el("small",`${w.precision} · 배치 ${w.batch_size} · 입력 ${(w.input_shape || []).join(" × ")}`),
    el("small",`원본 한도: 실행 ${w.max_run_seconds}초 / 대기 ${w.max_queue_seconds}초`)) : null;
  const save=el("button",savingTemplate?"저장 중…":"템플릿 저장 후 사용", "primary");save.type="submit";save.disabled=savingTemplate || !selected;
  form.append(grid);if(summary) form.append(summary);
  form.append(el("p","모델·명령·요청 자원은 선택한 실행 환경의 구성을 사용합니다. 저장만으로 작업이 실행되지는 않습니다.","muted"),save);
  form.onsubmit=async e=>{
    e.preventDefault();if(savingTemplate || !selected || !form.reportValidity()) return;
    const session=generation; savingTemplate=true;save.disabled=true;save.textContent="저장 중…";
    templateDraft.ref ||= "tpl-" + crypto.randomUUID();
    const payload={ref:templateDraft.ref,name:name.value.trim(),workload_ref:w.workload_ref,candidate_ref:selected.candidate_ref,priority:priority.value,max_run_seconds:Number(run.value),max_queue_seconds:Number(queue.value)};
    try {
      const response=await fetch(API+"/job-templates",{method:"POST",headers:{...authHeaders(),"Content-Type":"application/json"},body:JSON.stringify(payload)});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==="string"?error.detail:"이름과 실행 조건을 확인해 주세요.");}
      if(session!==generation)return;
      submissionDraft.workload="template:"+payload.ref;submissionDraft.candidate=payload.candidate_ref;pages.templates=0;
      templateDraft={};templateEditorOpen=false;await load();
      $("notice").textContent="템플릿을 저장했습니다. 아래 요청 자원을 확인하고 작업 제출을 누르세요.";
    } catch(error) {if(session===generation) $("notice").textContent="등록 실패: "+error.message;}
    finally {savingTemplate=false;if(session===generation)render();}
  };
  return panel("새 작업 템플릿 등록","이름과 실행 조건을 저장하면 프로젝트에서 반복 사용할 수 있습니다.",form);
}
let importedWorkload = null, importingWorkload = false;
function workloadImport() {
  const body = el("div", null, "job-form");
  const upload = el("input"); upload.type = "file"; upload.accept = ".json,application/json";
  upload.id = "workload-file"; upload.disabled = importingWorkload;
  const label = el("label", "새 작업 명세 파일 (WorkloadSpec JSON)"); label.htmlFor = upload.id;
  const status = el("p", importedWorkload ? `선택됨: ${importedWorkload.ref}` : "선택한 파일 없음");
  const register = el("button", importingWorkload ? "등록 중…" : "작업 템플릿 등록");
  register.disabled = !importedWorkload || importingWorkload;
  upload.onchange = async () => {
    importedWorkload = null; register.disabled = true;
    const file = upload.files[0];
    if (!file) { status.textContent = "선택한 파일 없음"; return; }
    try {
      if (file.size > 1024 * 1024) throw new Error("1 MiB 이하의 JSON 파일을 선택하세요.");
      const value = JSON.parse(await file.text());
      if (!value || typeof value !== "object" || !value.ref || value.project_ref !== data.project_ref)
        throw new Error("작업 ref와 현재 프로젝트의 project_ref가 필요합니다.");
      importedWorkload = value; status.textContent = `선택됨: ${value.ref}`; register.disabled = false;
    } catch (error) { status.textContent = error.message; }
  };
  register.onclick = async () => {
    if (!importedWorkload || importingWorkload) return;
    const session = generation, value = importedWorkload;
    importingWorkload = true; register.disabled = true;
    try {
      const response = await fetch(API + "/workloads", {
        method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" }, body: JSON.stringify(value),
      });
      if (!response.ok) throw new Error("등록하지 못했습니다. 작업 명세 형식, 프로젝트와 기존 ref 중복을 확인해 주세요.");
      if (session !== generation) return;
      importedWorkload = null; submissionDraft.workload = value.ref; submissionDraft.candidate = "";
      showAllTemplates = true;
      pages.compatibility = 0; await load();
      $("notice").textContent = "작업 템플릿을 등록했습니다. 실행 장비를 선택하고 작업을 제출하세요.";
    } catch (error) { if (session === generation) $("notice").textContent = error.message; }
    finally { importingWorkload = false; if (session === generation) render(); }
  };
  const specLink = el("a", "작업 명세 예제와 등록 방법");
  specLink.href = "https://github.com/dsa04156/resource-advisor/blob/main/docs/quickstart-ko.md#새-작업-템플릿-등록";
  body.append(label, upload, status, register,
    el("p", "기존에 등록된 실행 환경과 장비 후보를 참조하는 작업 명세를 등록합니다. 임의의 Python 파일이나 모델 파일을 업로드해 실행하는 기능은 아직 지원하지 않습니다."), specLink);
  return add(el("details", null, "panel"), el("summary", "고급: WorkloadSpec JSON 가져오기", "panel-head"), body);
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
      submitButton(w, c),
    ]);
    root.append(
      panel(
        w.workload_ref,
        w.task_type +
          " · " +
          w.precision +
          " · 최종 자원 승인은 Kueue / Slurm이 결정합니다.",
        add(el("div"), recommendButton(w),
          table(["실행 후보", "장비 / 백엔드", "검증 단계", "현재 검사", "작업 제출"], rows)),
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
  for (const s of data.inventory) {
    if (s.backend !== "slurm") continue;
    queues++;
    const q = observed(s.slurm_queue, s);
    root.append(panel(
      "Slurm / " + s.cluster_ref,
      "계정·파티션으로 제한한 작업 레코드입니다. 배열 작업의 개별 태스크 수와 다를 수 있습니다.",
      q.status !== "ok"
        ? empty("Slurm 큐 상태: " + (labels[q.status] || "확인 불가"))
        : add(el("div"), table(
          ["계정", "파티션", "대기 레코드", "실행 레코드", "전체 레코드"],
          [[q.value.account, q.value.partition, fmt(q.value.pending_records, 0), fmt(q.value.running_records, 0), fmt(q.value.record_count, 0)]],
        ), details("관측 시각 · 상태별 집계", {observed_at: q.observed_at, states: q.value.states})),
    ));
  }
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
  return root;
}
function ledgerView() {
  const root = el("div");
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
          `${c.accelerator_model} · ${c.device_class.toUpperCase()} ${c.resources.accelerator_count} · CPU ${c.resources.host_cpu} · 메모리 ${c.resources.host_memory_mib} MiB · 최대 ${c.max_run_seconds}초`,
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
            headers: authHeaders(),
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
    add(content, b, target, approvalControls(r));
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
  const focused = document.activeElement;
  const keepFocus = ["job-search", "resource-search"].includes(focused?.id)
    ? { id: focused.id, start: focused.selectionStart, end: focused.selectionEnd } : null;
  const [label, title, description] = views[active];
  $("section-label").textContent = label;
  $("view-title").textContent = title;
  $("view-description").textContent = description;
  $("identity").textContent = data.project_ref;
  $("nav-job-count").textContent = Object.values(data.job_counts).reduce((a,b)=>a+b,0);
  $("updated").textContent = "조회 " + stamp(data.generated_at);
  $("content").replaceChildren(
    {
      operations: operationsView,
      usage: usageView,
      execution,
      jobs: jobsView,
      submit: submissionView,
      compatibility: compatibilityView,
      history: historyView,
      recommendations: recommendationsView,
    }[active](),
  );
  if (keepFocus && $(keepFocus.id)) {
    $(keepFocus.id).focus();
    $(keepFocus.id).setSelectionRange(keepFocus.start, keepFocus.end);
  }
  $("workspace").hidden = false;
  $("connect").hidden = true;
  $("logout").hidden = anonymousConnected;
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
        if (data && ["operations", "execution", "history"].includes(active)) render();
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
  importedWorkload = null;
  templateDraft = {}; templateEditorOpen = false;
  schedulingProfile = ""; schedulingPreview = null; schedulingPreviewKey = "";
  submissionDraft.workload = "";
  submissionDraft.candidate = "";
  anonymousConnected = false;
  data = null;
  $("job-dialog").close();
  $("job-dialog-body").replaceChildren();
  clearTimeout(searchTimer);
  jobFilters.status = "all"; jobFilters.backend = "all"; jobFilters.search = "";
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
  if ((!token && !anonymousConnected) || pending) return;
  pending = true;
  const session = generation;
  const requestedFilter = filterRevision;
  $("refresh").disabled = true;
  const query = new URLSearchParams(
    Object.entries(pages).map(([key, v]) => [key + "_page", v]),
  );
  for (const [key, val] of Object.entries(jobFilters)) query.set("jobs_" + key, val);
  try {
    const response = await fetch(API + "/overview?" + query, {
      headers: authHeaders(),
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
      if (requestedFilter !== filterRevision) load();
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
$("content").addEventListener("focusin", (e) => {
  if (e.target.closest(".job-form, .jobs-toolbar, .view-toolbar")) $("auto").checked = false;
});
$("close-job-dialog").onclick = () => $("job-dialog").close();
$("logout").onclick = () => {
  reset("연결을 해제했습니다.");
  $("token").focus();
};
$("refresh").onclick = () => load();
function navigate(event) {
  const view = new URL(event?.newURL || location.href).hash.slice(1);
  if (view === "main") return; // The skip link must not change the selected view.
  active = views[view] ? view : "operations";
  document.querySelectorAll("nav a").forEach((a) => {
    if (a.dataset.view === active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  render();
}
window.addEventListener("hashchange", navigate);
navigate();
setInterval(() => {
  if ($("auto").checked && (token || anonymousConnected)) load();
}, 15000);
window.addEventListener("pagehide", () => reset());

async function connectDefaultProject() {
  const session = generation;
  try {
    const response = await fetch("/console/session", { cache: "no-store" });
    if (!response.ok) return;
    const config = await response.json();
    if (session !== generation || token || config.authentication_required || !config.project_ref) return;
    anonymousConnected = true;
    controller = new AbortController();
    $("connect").hidden = true;
    $("notice").textContent = "기본 프로젝트에 연결하는 중…";
    await load();
  } catch (_) { /* Existing token login remains available if discovery fails. */ }
}
connectDefaultProject();
