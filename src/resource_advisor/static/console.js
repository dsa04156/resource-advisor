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
let jobsDisplay="graph", graphJobId=null, graphSelection="compute";
let resourceBackend = "all", resourceSearch = "", searchTimer = null, filterRevision = 0;
const submissionDraft = { workload: "", candidate: "" };
let schedulingProfile = "", schedulingPreview = null, schedulingPreviewKey = "";
let schedulingPendingKey = "", trackedJobId = null;
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
  const automatic = candidate.candidate_ref === "auto";
  const label = approval ? "승인한 구성 실행" : automatic ? "스케줄러에 제출" : "작업 제출";
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
        body: JSON.stringify({ workload_ref: workload.workload_ref, ...(automatic ? {} : { candidate_ref: candidate.candidate_ref }),
          ...(workload.template_ref ? { template_ref: workload.template_ref } : {}),
          ...(scheduling ? { scheduling_profile_ref: scheduling.plan.profile_ref, ...(automatic ? {} : { scheduling_plan_digest: scheduling.digest }) } : {}),
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
      trackedJobId = job.job_id;
      $("auto").checked = true;
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
  return add(el("div"), button, automatic ? el("small", "접수 시점에 호환 자원을 다시 확인해 선택하고 해당 큐에 제출합니다.") : resources ? el("small",
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
  scenario: ["REQUEST → QUEUE → RUN", "자원 요청 시나리오", "두 연구 요청이 같은 실행 자원을 사용합니다. 실제 큐의 대기와 실행을 따라가고 기록을 다시 재생하세요."],
  experiments: ["EXPERIMENT WORKSPACE", "실험 · 비교", "MLflow 실행 기록, 파라미터, 지표와 아티팩트를 함께 확인합니다."],
  pipelines: ["WORKFLOW OPERATIONS", "파이프라인", "Kubeflow 단계 상태와 연결된 작업을 추적하고 실행·중지를 요청합니다."],
  notebooks: ["RESEARCH WORKSPACES", "노트북", "개발 환경의 상태와 자원을 확인하고 시작·중지·접속합니다."],
  operations: ["COMPUTE OPERATIONS", "운영 현황", "자원 배분, 대기 사유, 장애와 사용량을 확인합니다."],
  usage: ["PROJECT ACCOUNTING", "프로젝트 사용량", "물리 장치와 공유 슬롯을 구분한 할당 원장입니다."],
  submit: [
    "SUBMIT JOB",
    "작업 제출",
    "작업 요구사항을 보고 플랫폼이 호환 자원을 선택해 Kubernetes 또는 Slurm 큐에 제출합니다.",
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
  root.append(researchConnections());
  if(!op)return empty("운영 집계를 불러오지 못했습니다.");
  root.append(add(el("section",null,"operations-intro"),
    add(el("div"),el("p","SHARED COMPUTE · "+data.project_ref,"eyebrow"),el("h2","공동 자원 운영"),el("p","지금 기다리는 작업과 확인할 문제부터 살펴보세요.")),
    add(el("div",null,"hero-actions"),actionLink("자원 요청 시나리오 →","#scenario"),actionLink("작업 제출 →","#submit"),actionLink("장비 사용량 보기","#execution"))));
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
  $("job-dialog-title").textContent="작업 상세";
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
    ["FAILED","CANCELED","RESULT_INVALID"].includes(job.state) ? retryJobButton(job) : null,
    job.tracking?.run_id ? researchButton("MLflow 실험 기록 보기",()=>openExperiment(job.tracking.run_id)) : null,
    job.scheduling_plan ? schedulingProgress(job) : null,
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
  const tracked=data.jobs.items.find(j=>j.job_id===trackedJobId) || data.jobs.items.find(j=>j.scheduling_plan && !["SUCCEEDED","FAILED","CANCELED","RESULT_INVALID"].includes(j.state));
  if(tracked && jobsDisplay==="table")root.append(schedulingProgress(tracked));
  root.append(add(el("div",null,"section-heading"),el("p","ML EXECUTION MAP","eyebrow"),el("h2","ML 작업 흐름"),el("p",`전체 ${total}개 · 실행 ${counts.RUNNING||0}개 · 대기 ${waiting}개 · 완료 ${counts.SUCCEEDED||0}개`)));
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
  const display=el("div",null,"status-filters");
  for(const [key,label] of [["graph","그래프 보기"],["table","표 보기"]]) {
    const b=el("button",label,jobsDisplay===key?"selected":"");b.setAttribute("aria-pressed",String(jobsDisplay===key));
    b.onclick=()=>{jobsDisplay=key;render();};display.append(b);
  }
  toolbar.append(display);
  const rows = data.jobs.items.map(j => {
    const open = el("button", "상세 보기", "details-action"); open.setAttribute("aria-label", `상세 보기 · ${j.job_id}`); open.onclick = () => showJob(j);
    return [add(el("div",null,"job-name"),el("strong",j.template?.name || j.workload_ref),code(j.job_id)),add(el("div"),stateBadge(j.state),j.scheduler_reason?el("small",j.attention?.title||j.scheduler_reason):null),
      add(el("div"),badge(j.backend),el("small",j.node_ref)),
      el("span",`CPU ${j.requested_resources?.host_cpu ?? "—"} · GPU/NPU ${j.requested_resources?.accelerator_count ?? "—"}`),
      stamp(j.created_at),add(el("div",null,"job-actions"),open,cancelButton(j))];
  });
  root.append(add(el("section",null,"panel jobs-panel"), toolbar, rows.length ? (jobsDisplay==="graph"?mlJobsGraph():table(["작업","상태","실행 대상","요청 자원","생성 시각","관리"],rows)) : empty("조건에 맞는 작업이 없습니다."),pager("jobs",data.jobs)));
  return root;
}
function schedulingKey(workload) {
  return JSON.stringify([generation, schedulingProfile, selectionKey(workload), submissionDraft.candidate]);
}
function candidateEvaluation(plan) {
  const entries = plan?.evaluation || [];
  if (!entries.length) return empty("후보별 관측 기록이 없습니다.");
  const labels = {available:"예약 여유 관측",busy:"자원 대기 예상",unknown:"현재 여유 미확인",unavailable:"노드 사용 불가"};
  return table(["후보 자원", "요구 자원", "관측 · 판단", "선택 결과"], entries.map(c => [
    add(el("div"),el("strong",c.model || c.candidate_ref),el("small",`${c.backend} · ${c.node_ref || c.candidate_ref}`)),
    el("span",`CPU ${c.resources?.host_cpu ?? "—"} · ${c.resources?.host_memory_mib ?? "—"} MiB · 가속기 ${c.resources?.accelerator_count ?? "—"}`),
    add(el("div"),el("span",labels[c.availability?.status] || "미확인"),el("small",c.availability?.collected_at ? stamp(c.availability.collected_at) : "관측 없음")),
    add(el("div"),badge(c.selected ? "선택" : c.eligible ? "호환 후보" : "제외",c.selected ? "good" : c.eligible ? "" : "warn"),...c.reasons.map(r=>el("small",reasonText(r))))
  ]));
}
function schedulingFlow(job = null, preview = null) {
  const plan = job?.scheduling_plan || preview?.plan;
  const state = job?.state;
  const failed = ["FAILED","RESULT_INVALID","CANCELED"].includes(state);
  const complete = ["SUCCEEDED", "FAILED", "RESULT_INVALID", "CANCELED"].includes(state);
  const queueObserved = Boolean(job?.queued_at || job?.external_id);
  const started = Boolean(job?.started_at);
  const items = [
    ["작업 요구사항",job ? "작업 접수" : "모델 · 입력 · 실행 조건",job ? true : Boolean(preview)],
    ["후보 검토",plan ? `${plan.evaluation?.filter(x=>x.eligible).length ?? 1}개 호환 후보` : "런타임 · 자원 조건 검사",Boolean(plan)],
    ["자원 선택",plan ? `${plan.backend} · ${plan.candidate_ref}` : "플랫폼이 실행 후보 선택",Boolean(plan)],
    ["백엔드 큐",queueObserved ? (job.scheduler_reason || "제출 확인") : job ? "큐 제출 준비" : "Kueue 또는 Slurm",queueObserved],
    ["실행",started ? stamp(job.started_at) : "자원 할당 후 시작",started],
    ["결과",complete ? (state === "SUCCEEDED" ? "완료" : stateBadgeText(state)) : state === "COLLECTING" ? "결과 수집 중" : "결과 · 사용량 기록",complete],
  ];
  const row = el("ol",null,"scheduling-flow"); row.setAttribute("aria-label","작업 스케줄링 진행 단계");
  items.forEach(([title,description,done],i)=>{
    const item=add(el("li",null,done ? "done" : "waiting"),el("span",String(i+1),"flow-number"),el("strong",title),el("small",description));
    if(failed && i===5)item.classList.add("failed");
    if(!done && (i===0 || items[i-1][2]))item.setAttribute("aria-current","step");
    row.append(item);
  });
  return row;
}
function stateBadgeText(state) { return ({FAILED:"실패",RESULT_INVALID:"결과 검증 실패",CANCELED:"취소"})[state] || state; }
function schedulingProgress(job) {
  const plan=job.scheduling_plan;
  const body=add(el("div"),schedulingFlow(job),
    el("p",job.scheduler_reason ? "대기 이유: "+(job.attention?.title || job.scheduler_reason) : job.state === "QUEUED" ? "큐에 제출되었습니다. 백엔드의 자원 할당을 기다립니다." : job.state === "VALIDATED" ? "정책 검증 완료. 작업자가 백엔드 큐에 제출할 차례입니다." : "백엔드에서 관측한 상태를 표시합니다."),
    plan ? el("p",plan.backend === "kubernetes" ? `선택 큐: ${plan.adapter.local_queue}` : `선택 큐: ${plan.adapter.partition} · QOS ${plan.adapter.qos}`) : null,
    plan ? add(el("details"),el("summary","선택·제외 이유와 후보 자원"),candidateEvaluation(plan)) : null);
  return panel("스케줄링 진행",`${job.workload_ref} · ${job.job_id} · 자동 갱신을 켜면 15초마다 업데이트`,body);
}
async function refreshSchedulingPreview(workload) {
  if (!schedulingProfile) return;
  const key=schedulingKey(workload), session=generation;
  if(schedulingPendingKey===key) return;
  schedulingPendingKey=key;
  try {
    const response=await fetch(API+"/scheduling-plans",{method:"POST",headers:{...authHeaders(),"Content-Type":"application/json"},body:JSON.stringify({
      profile_ref:schedulingProfile,workload_ref:workload.workload_ref,
      ...(submissionDraft.candidate?{candidate_ref:submissionDraft.candidate}:{}),
      ...(workload.template_ref?{template_ref:workload.template_ref}:{})})});
    const result=await response.json();
    if(!response.ok)throw new Error(typeof result.detail==="string"?result.detail:"후보를 확인하지 못했습니다.");
    if(session!==generation || key!==schedulingKey(workload))return;
    schedulingPreview=result;schedulingPreviewKey=key;
  } catch(error) {if(session===generation) $("notice").textContent=error.message;}
  finally {if(schedulingPendingKey===key)schedulingPendingKey="";if(session===generation)render();}
}
function submissionView() {
  const root=el("div");
  const catalogMode=!showAllTemplates && data.submission_catalog != null;
  const sources=catalogMode?data.submission_catalog:data.compatibility.items;
  const items=[...sources,...(data.templates?.items || [])];
  const profiles=data.scheduling_profiles || [];
  if(!schedulingProfile && profiles.length) schedulingProfile=(profiles.find(p=>p.policy.priority==="normal" && p.policy.backend_order[0]==="kubernetes") || profiles[0]).ref;
  root.append(add(el("div",null,"section-heading"),el("p","WORKLOAD → SCHEDULER → QUEUE → EXECUTION","eyebrow"),el("h2","작업을 정의하면 실행 자원은 플랫폼이 선택합니다"),el("p","등록된 실행 환경과 요구 자원을 확인한 뒤, 선택한 백엔드 큐에서 자원 할당을 기다립니다.")));
  const form=el("div",null,"job-form");
  const workloadSelect=el("select");workloadSelect.id="submit-workload";
  workloadSelect.append(new Option("실행할 작업을 선택하세요",""));
  const types={inference:"추론",training:"학습",benchmark:"벤치마크",preprocessing:"전처리"};
  for(const w of items)workloadSelect.append(new Option(`${w.name || (w.candidates.length>1 && w.measurement_boundary?.startsWith("cuda-squares")?"GPU 연산 점검 · 자동 자원 선택":w.workload_ref)} · ${types[w.task_type] || w.task_type}${w.template_ref?" · 저장한 템플릿":""}`,selectionKey(w)));
  workloadSelect.value=submissionDraft.workload;
  workloadSelect.onchange=()=>{submissionDraft.workload=workloadSelect.value;submissionDraft.candidate="";schedulingPreview=null;render();const w=items.find(x=>selectionKey(x)===submissionDraft.workload);if(w)refreshSchedulingPreview(w);};
  const label=el("label","1. 실행할 작업");label.htmlFor=workloadSelect.id;form.append(label,workloadSelect);
  const workload=items.find(w=>selectionKey(w)===submissionDraft.workload);
  if(workload){
    form.append(el("p",`${types[workload.task_type] || workload.task_type} · ${workload.precision} · 배치 ${workload.batch_size ?? "—"} · 입력 ${(workload.input_shape || []).join(" × ")} · 등록된 실행 후보 ${workload.candidates.length}개`));
    const policy=el("select");policy.id="scheduling-profile";
    for(const p of profiles)policy.append(new Option(p.name,p.ref));policy.value=schedulingProfile;
    const policyLabel=el("label","2. 실행 정책");policyLabel.htmlFor=policy.id;
    policy.onchange=()=>{schedulingProfile=policy.value;schedulingPreview=null;render();refreshSchedulingPreview(workload);};
    form.append(policyLabel,policy,el("small","GPU를 지정할 필요가 없습니다. 같은 작업을 실행할 수 있는 후보 중 관측된 예약 여유와 정책을 보고 선택합니다."));
    const advanced=el("details");const candidates=el("select");candidates.id="submit-candidate";candidates.append(new Option("자동 선택 (기본)",""));
    for(const c of workload.candidates)candidates.append(new Option(`${c.backend} · ${c.model || c.candidate_ref} · ${c.candidate_ref}`,c.candidate_ref));
    candidates.value=submissionDraft.candidate;candidates.onchange=()=>{submissionDraft.candidate=candidates.value;schedulingPreview=null;render();refreshSchedulingPreview(workload);};
    const targetLabel=el("label","실행 후보 고정");targetLabel.htmlFor=candidates.id;
    advanced.append(el("summary","고급: 특정 실행 자원으로 고정"),targetLabel,candidates);advanced.open=Boolean(submissionDraft.candidate);form.append(advanced);
    const preview=schedulingPreviewKey===schedulingKey(workload)?schedulingPreview:null;
    form.append(schedulingFlow(null,preview));
    const inspect=el("button",schedulingPendingKey===schedulingKey(workload)?"후보 확인 중…":"후보·선택 이유 새로 확인");inspect.type="button";inspect.disabled=Boolean(schedulingPendingKey);inspect.onclick=()=>refreshSchedulingPreview(workload);form.append(inspect);
    if(preview){
      form.append(candidateEvaluation(preview.plan || {evaluation:preview.evaluation}));
      if(!preview.accepted)form.append(el("p","조건에 맞는 실행 후보가 없습니다: "+preview.reasons.map(reasonText).join(" · "),"error-message"));
      else form.append(el("p",`현재 예상 선택: ${preview.plan.node_ref} · ${preview.plan.backend} · 실행 제한 ${preview.plan.execution.max_run_seconds}초`),el("small","여유 관측은 예약 보장이 아닙니다. 자동 제출 시 다시 판단하고, 자원이 사용 중이면 큐에서 기다립니다."));
    }
    if(!profiles.length)form.append(empty("운영자가 공통 스케줄링 프로필을 등록해야 자동 제출할 수 있습니다."));
    else if(!submissionDraft.candidate){
      form.append(submitButton(workload,{candidate_ref:"auto",submittable_now:workload.candidates.some(canSubmit)},null,{plan:{profile_ref:schedulingProfile},digest:schedulingProfile}));
    }else if(preview?.accepted){
      const candidate=workload.candidates.find(c=>c.candidate_ref===submissionDraft.candidate);
      form.append(submitButton({...workload,...preview.plan.execution},candidate,null,preview));
    }
  }else form.append(schedulingFlow());
  root.append(panel("새 작업 제출","작업 → 요구사항 확인 → 자동 배치 → 큐 → 실행",form));
  const create=el("button",templateEditorOpen?"등록 폼 닫기":"＋ 새 템플릿 등록");create.onclick=()=>{templateEditorOpen=!templateEditorOpen;render();};root.append(create);
  if(templateEditorOpen)root.append(templateEditor(sources));
  const showAll=el("input");showAll.type="checkbox";showAll.checked=showAllTemplates;showAll.onchange=()=>{showAllTemplates=showAll.checked;render();};
  root.append(add(el("label",null,"template-filter"),showAll,document.createTextNode(" 고급: 전체 실험·테스트 템플릿 보기")));
  root.append(add(el("details",null,"panel"),el("summary","연결된 장비별 실행 작업 보기"),executionTargets()));
  if(!catalogMode)root.append(pager("compatibility",data.compatibility));
  root.append(workloadImport());return root;
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
  const device=el("select"); device.id="template-device"; device.required=false;
  device.append(new Option("자동 선택 · 제출 시 스케줄러가 결정", ""));
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
  const grid=add(el("div",null,"template-fields"),field("템플릿 이름",name),field("모델 · 실행 환경",source),field("실행 자원 · 기본 자동 선택",device),field("우선순위",priority),field("실행 시간 제한 (초)",run),field("대기 시간 제한 (초)",queue));
  const selected=w?.candidates.find(c=>c.candidate_ref===templateDraft.candidate);
  const summary=selected ? add(el("div",null,"template-runtime"),
    el("strong",`${selected.model || selected.candidate_ref} · ${selected.backend}`),
    el("p",`CPU ${selected.resources.host_cpu}코어 · 메모리 ${selected.resources.host_memory_mib} MiB · 가속기 ${selected.resources.accelerator_count} (${modes[selected.allocation_mode] || "등록 단위"})`),
    el("small",`${w.precision} · 배치 ${w.batch_size} · 입력 ${(w.input_shape || []).join(" × ")}`),
    el("small",`원본 한도: 실행 ${w.max_run_seconds}초 / 대기 ${w.max_queue_seconds}초`)) : null;
  const save=el("button",savingTemplate?"저장 중…":"템플릿 저장 후 사용", "primary");save.type="submit";save.disabled=savingTemplate || !w;
  form.append(grid);if(summary) form.append(summary);
  form.append(el("p","모델·명령·요청 자원은 선택한 실행 환경의 구성을 사용합니다. 저장만으로 작업이 실행되지는 않습니다.","muted"),save);
  form.onsubmit=async e=>{
    e.preventDefault();if(savingTemplate || !w || !form.reportValidity()) return;
    const session=generation; savingTemplate=true;save.disabled=true;save.textContent="저장 중…";
    templateDraft.ref ||= "tpl-" + crypto.randomUUID();
    const payload={ref:templateDraft.ref,name:name.value.trim(),workload_ref:w.workload_ref,candidate_ref:selected?.candidate_ref || null,priority:priority.value,max_run_seconds:Number(run.value),max_queue_seconds:Number(queue.value)};
    try {
      const response=await fetch(API+"/job-templates",{method:"POST",headers:{...authHeaders(),"Content-Type":"application/json"},body:JSON.stringify(payload)});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==="string"?error.detail:"이름과 실행 조건을 확인해 주세요.");}
      if(session!==generation)return;
      submissionDraft.workload="template:"+payload.ref;submissionDraft.candidate=payload.candidate_ref || "";pages.templates=0;
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
  if (active === "scenario" && !scenarioPending && Date.now()-scenarioUpdated > 3000) refreshScenario();
  if (["operations","experiments","pipelines","notebooks","jobs"].includes(active) && !researchPending && Date.now()-researchUpdated>30000) refreshResearch();
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
      scenario: scenarioView,
      experiments: experimentsView,
      pipelines: pipelinesView,
      notebooks: notebooksView,
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
  graphJobId=null;graphSelection="compute";
  scenarioData=null; scenarioRecent=[]; scenarioRef=""; scenarioUpdated=0; scenarioPending=false; scenarioError=""; scenarioCursor=null; scenarioSubmission=null; stopScenarioReplay();
  researchData=null; researchRuns=null; researchPending=false; experimentSelection.clear(); researchUpdated=0;
  controller?.abort();
  controller = null;
  pending = false;
  token = "";
  importedWorkload = null;
  templateDraft = {}; templateEditorOpen = false;
  schedulingProfile = ""; schedulingPreview = null; schedulingPreviewKey = ""; schedulingPendingKey = ""; trackedJobId = null;
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


// Research services remain independent from core scheduler polling.
let researchData=null,researchRuns=null,researchPending=false,researchUpdated=0;
let selectedExperiment="",pipelineDraft={workload:"",profile:"",template:""},pipelineRequestKey=null;
const experimentSelection=new Map();
async function researchRequest(path,body=null,key=null) {
  const response=await fetch(API+"/research"+path,{method:body===null?"GET":"POST",headers:{...authHeaders(),...(body===null?{}:{"Content-Type":"application/json"}),...(key?{"Idempotency-Key":key}:{})},...(body===null?{}:{body:JSON.stringify(body)})});
  const value=await response.json();if(!response.ok)throw new Error(typeof value.detail==="string"?value.detail:"연결과 프로젝트 권한을 확인해 주세요.");return value;
}
async function refreshResearch() {
  if(researchPending)return;researchPending=true;const session=generation;
  try {
    const result=await researchRequest("/overview");
    if(session!==generation)return;researchData=result;
    if(result.mlflow.status==="connected"){const runs=await researchRequest("/runs"+(selectedExperiment?"?experiment_id="+encodeURIComponent(selectedExperiment):""));if(session!==generation)return;researchRuns=runs;}
  }catch(error){if(session===generation)$("notice").textContent=error.message;}
  finally{if(session===generation){researchPending=false;researchUpdated=Date.now();render();}}
}
function researchButton(text,handler,primary=false) {
  const b=el("button",text,primary?"primary":"");b.type="button";b.onclick=async()=>{b.disabled=true;const session=generation;try{await handler();}catch(error){if(session===generation)$("notice").textContent=error.message;}finally{b.disabled=false;}};return b;
}
function researchLink(text,url) {
  if(!url || !/^https?:\/\//.test(url))return null;
  const a=el("a",text,"action-link");a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a;
}
function researchConnections() {
  const row=el("div",null,"research-connections");
  for(const [key,name,route] of [["mlflow","MLflow · 실험","experiments"],["kubeflow","Kubeflow · 워크플로","pipelines"],["notebooks","Jupyter · 노트북","notebooks"]]) {
    const state=researchData?.[key]?.status;
    const a=el("a",null,"service-connection");a.href="#"+route;
    a.append(el("strong",name),badge(state==="connected"?"연결됨":state==="unconfigured"?"미설정":state==="unavailable"?"연결 확인 필요":"연결 확인 중",state==="connected"?"good":"warn"));row.append(a);
  }
  return row;
}
function researchEmpty(key) {
  const state=researchData?.[key]?.status;
  return empty(!state?"서비스 상태를 불러오는 중입니다…":state==="unconfigured"?"이 프로젝트에 서비스 연결이 설정되지 않았습니다.":"서비스 API 연결 또는 권한을 확인해야 합니다. 기존 작업 운영은 계속 사용할 수 있습니다.");
}
function experimentsView() {
  const root=el("div");root.append(researchConnections());
  if(researchData?.mlflow.status!=="connected"){root.append(researchEmpty("mlflow"));return root;}
  const controls=el("div",null,"view-toolbar");const selector=el("select");selector.setAttribute("aria-label","MLflow 실험 선택");selector.append(new Option("프로젝트 전체 실험",""));
  for(const e of researchData.mlflow.items)selector.append(new Option(e.name,e.experiment_id));selector.value=selectedExperiment;
  selector.onchange=async()=>{selectedExperiment=selector.value;experimentSelection.clear();researchUpdated=0;await refreshResearch();};
  controls.append(selector,researchButton("새로고침",async()=>{researchUpdated=0;await refreshResearch();}),researchLink("MLflow 열기 ↗",researchData.mlflow.ui_url));
  const runs=researchRuns?.items || [];
  root.append(panel("실험 워크스페이스","실행을 최대 4개 선택하면 파라미터와 지표를 나란히 비교합니다. 서로 다른 작업·측정 구간의 수치를 같은 성능으로 비교하지 마세요.",controls));
  if(experimentSelection.size>0) {
    const selected=[...experimentSelection.values()];const metrics=[...new Set(selected.flatMap(r=>Object.keys(r.metrics)))];const params=[...new Set(selected.flatMap(r=>Object.keys(r.params)))];
    root.append(panel("선택한 실행 비교",`${selected.length}개 · 미측정 값은 —`,table(["지표 / 파라미터",...selected.map(r=>r.name)],
      [...metrics.map(k=>[el("strong",k),...selected.map(r=>el("span",r.metrics[k]==null?"—":fmt(r.metrics[k])))]),...params.map(k=>[el("span",k),...selected.map(r=>el("span",r.params[k]??"—"))])])));
  }
  const rows=runs.map(r=>{
    const check=el("input");check.type="checkbox";check.checked=experimentSelection.has(r.run_id);check.disabled=!check.checked && experimentSelection.size>=4;check.setAttribute("aria-label","비교 선택 · "+r.name);check.onchange=()=>{check.checked?experimentSelection.set(r.run_id,r):experimentSelection.delete(r.run_id);render();};
    const metricEntries=Object.entries(r.metrics).filter(([k])=>/latency_p95|quality|throughput/.test(k)).slice(0,3);
    return [check,add(el("div",null,"job-name"),el("strong",r.name),code(r.run_id)),badge(r.status,r.status==="FINISHED"?"good":r.status==="FAILED"?"warn":""),el("span",r.params.accelerator_model||r.params.backend||"—"),add(el("div"),...metricEntries.map(([k,v])=>el("small",`${k}: ${fmt(v)}`))),el("span",r.start_time?stamp(new Date(r.start_time).toISOString()):"—"),researchButton("상세 보기",()=>openExperiment(r.run_id))];
  });
  root.append(panel("실행 기록",`${runs.length}개 표시 · 최신 실행 순`,rows.length?table(["비교","실행","상태","실행 환경","주요 지표","시작","관리"],rows):empty("해당 실험에 실행 기록이 없습니다.")));
  if(researchRuns?.next_page_token)root.append(researchButton("다음 실행 보기",async()=>{researchRuns=await researchRequest("/runs?page_token="+encodeURIComponent(researchRuns.next_page_token)+(selectedExperiment?"&experiment_id="+encodeURIComponent(selectedExperiment):""));render();}));
  return root;
}
async function openExperiment(ref) {
  const session=generation;const r=await researchRequest("/runs/"+encodeURIComponent(ref));if(session!==generation)return;
  const body=el("div",null,"job-detail-content");
  body.append(el("h3",r.name),badge(r.status),code(r.run_id),table(["지표","값"],Object.entries(r.metrics).map(([k,v])=>[el("span",k),el("strong",fmt(v))])),details("파라미터와 출처",{params:r.params,tags:r.tags}));
  const artifacts=el("div");body.append(artifacts);
  const browse=async(path="")=>{const v=await researchRequest("/runs/"+encodeURIComponent(ref)+"/artifacts?path="+encodeURIComponent(path));if(session!==generation)return;artifacts.replaceChildren(el("h3","아티팩트"),path?researchButton("상위 폴더",()=>browse(path.split("/").slice(0,-1).join("/"))):el("small","기록된 결과 파일"));for(const f of v.items)artifacts.append(f.is_dir?researchButton("▸ "+f.path,()=>browse(f.path)):el("p",`${f.path} · ${f.file_size ?? "—"} bytes`));if(!v.items.length)artifacts.append(empty("아티팩트가 없습니다."));};
  const note=el("textarea");note.rows=3;note.maxLength=2000;note.setAttribute("aria-label","실험 운영 메모");note.value=r.tags["hairp.operator_note"]||"";note.placeholder="실험 결과와 운영 조치 메모";
  body.append(el("h3","운영 메모"),note,researchButton("메모 저장",async()=>{await researchRequest("/runs/"+encodeURIComponent(ref)+"/note",{text:note.value});$("notice").textContent="MLflow에 메모를 저장했습니다.";}));
  const base=researchData?.mlflow.ui_url;body.append(researchLink("MLflow에서 전체 기록 열기 ↗",base?base.replace(/\/$/,"")+"/#/experiments/"+encodeURIComponent(r.experiment_id)+"/runs/"+encodeURIComponent(ref):null));
  $("job-dialog-title").textContent="실험 상세";$("job-dialog-body").replaceChildren(body);$("job-dialog").showModal();await browse();
}
function pipelinesView() {
  const root=el("div");root.append(researchConnections());const info=researchData?.kubeflow;
  if(info?.status!=="connected"){root.append(researchEmpty("kubeflow"));return root;}
  const form=el("div",null,"job-form");const makeSelect=(name,items,current)=>{const s=el("select");s.setAttribute("aria-label",name);for(const [value,label] of items)s.append(new Option(label,value));s.value=current || items[0]?.[0] || "";return s;};
  const template=makeSelect("파이프라인 템플릿",(info.templates||[]).map(t=>[t.ref,t.name]),pipelineDraft.template);
  const workload=makeSelect("파이프라인 작업",(data.submission_catalog||[]).map(w=>[w.workload_ref,w.workload_ref]),pipelineDraft.workload);
  const profile=makeSelect("파이프라인 실행 정책",(data.scheduling_profiles||[]).map(p=>[p.ref,p.name]),pipelineDraft.profile || data.scheduling_profiles?.find(p=>p.policy.priority==="normal" && p.policy.backend_order[0]==="kubernetes")?.ref);
  for(const [key,input] of [["template",template],["workload",workload],["profile",profile]]){pipelineDraft[key]=input.value;input.onchange=()=>{pipelineDraft[key]=input.value;pipelineRequestKey=null;};}
  form.append(el("label","파이프라인"),template,el("label","실행할 작업"),workload,el("label","자동 배치 정책"),profile);
  const submit=researchButton("Kubeflow 파이프라인 실행",async()=>{
    pipelineRequestKey ||= "pipeline-console-"+crypto.randomUUID();
    const run=await researchRequest("/pipelines",{template_ref:template.value,workload_ref:workload.value,profile_ref:profile.value},pipelineRequestKey);
    pipelineRequestKey=null;researchUpdated=0;await refreshResearch();$("notice").textContent="Kubeflow에 실행을 요청했습니다: "+run.run_id;
  },true);submit.disabled=!(info.templates?.length && workload.value && profile.value);
  form.append(el("small","Kubeflow가 워크플로를 시작하고 플랫폼의 자동 배치 API가 실제 GPU/NPU 작업을 큐에 넣습니다."),submit,researchLink("Kubeflow 원본 열기 ↗",info.ui_url));
  root.append(panel("워크플로 실행","등록된 실행 환경으로 파이프라인을 시작합니다.",form));
  root.append(panel("파이프라인 실행 이력",`${info.items.length}개 표시 · 실제 KFP 상태`,info.items.length?table(["실행","상태","시작","연결 작업","관리"],info.items.map(r=>[
    add(el("div",null,"job-name"),el("strong",r.display_name),code(r.run_id)),badge(r.state,r.state==="SUCCEEDED"?"good":r.state==="FAILED"?"warn":""),el("span",stamp(r.created_at)),el("span",r.job_id||"아직 연결된 작업 없음"),researchButton("단계 · 관리",()=>openPipeline(r.run_id))])):empty("파이프라인 실행이 없습니다.")));
  if(info.next_page_token)root.append(researchButton("다음 실행 보기",async()=>{researchData.kubeflow={status:"connected",...await researchRequest("/pipelines?page_token="+encodeURIComponent(info.next_page_token))};render();}));return root;
}
async function openPipeline(ref) {
  const session=generation;const run=await researchRequest("/pipelines/"+encodeURIComponent(ref));if(session!==generation)return;
  const body=el("div",null,"job-detail-content");body.append(el("h3",run.display_name),badge(run.state));
  body.append(pipelineDag(run));
  body.append(table(["단계","상태","시작","종료"],run.tasks.map(t=>[el("span",t.display_name),badge(t.state),el("span",stamp(t.start_time)),el("span",stamp(t.end_time))])));
  if(run.job_id)body.append(researchButton("연결된 컴퓨트 작업 보기",async()=>{
    const response=await fetch(API+"/jobs/"+encodeURIComponent(run.job_id)+"/view",{headers:authHeaders()});if(!response.ok)throw new Error("작업 기록을 조회하지 못했습니다.");const job=await response.json();
    showJob(job);
  }));
  if(!["SUCCEEDED","FAILED","CANCELED","CANCELLED","SKIPPED"].includes(run.state)){
    const stop=researchButton("파이프라인 중지",async()=>{if(stop.dataset.confirm!=="yes"){stop.dataset.confirm="yes";stop.textContent="중지 확인 · 연결 작업도 정리됩니다";return;}await researchRequest("/pipelines/"+encodeURIComponent(ref)+"/terminate",{});$("notice").textContent="중지를 요청했습니다. 실제 종료 상태는 갱신 후 확인하세요.";$("job-dialog").close();researchUpdated=0;await refreshResearch();});body.append(stop);
  }
  body.append(details("전체 단계 기록",run));$("job-dialog-title").textContent="파이프라인 상세";$("job-dialog-body").replaceChildren(body);$("job-dialog").showModal();
}
function notebooksView() {
  const root=el("div");root.append(researchConnections());const info=researchData?.notebooks;
  if(info?.status!=="connected"){root.append(researchEmpty("notebooks"));return root;}
  const grid=el("div",null,"notebook-grid");
  for(const n of info.items){const card=el("section",null,"panel notebook-card");const title=el("h2",n.name);const state=n.stopped?(n.ready?"중지 처리 중":"중지됨"):n.ready?"사용 가능":"시작 대기";
    card.append(el("p","JUPYTER WORKSPACE","eyebrow"),title,badge(state,n.ready&&!n.stopped?"good":""),el("small",n.namespace));
    for(const r of n.resources)card.append(el("p",`CPU ${r.requests?.cpu||"—"} · 메모리 ${r.requests?.memory||"—"}`));
    if(n.ready&&!n.stopped)card.append(researchLink("JupyterLab 열기 ↗",n.url));
    const action=n.stopped?"start":"stop";const b=researchButton(action==="start"?"노트북 시작":"노트북 중지",async()=>{
      if(action==="stop" && b.dataset.confirm!=="yes"){b.dataset.confirm="yes";b.textContent="중지 확인 · 실행 중인 커널 종료";return;}
      await researchRequest("/notebooks/"+encodeURIComponent(n.name)+"/"+action,{});researchUpdated=0;await refreshResearch();$("notice").textContent="노트북 "+(action==="start"?"시작":"중지")+"을 요청했습니다. 컨트롤러가 상태를 반영합니다.";
    });card.append(b,details("자원 요청 · 컨디션",{resources:n.resources,conditions:n.conditions}));grid.append(card);
  }
  root.append(panel("연구 노트북","노트북에서 코드를 정의하고, 파이프라인이나 작업 API로 실행합니다. 접속 시 Kubeflow 로그인이 필요할 수 있습니다.",info.items.length?grid:empty("현재 프로필에 노트북이 없습니다.")));return root;
}

function retryJobButton(job) {
  let key=null;
  return researchButton("같은 작업 다시 제출",async()=>{key ||= "retry-console-"+crypto.randomUUID();
    const response=await fetch(API+"/jobs/"+encodeURIComponent(job.job_id)+"/retry",{method:"POST",headers:{...authHeaders(),"Idempotency-Key":key}});
    const value=await response.json();if(!response.ok)throw new Error(value.detail||"재제출하지 못했습니다.");trackedJobId=value.job_id;$("job-dialog").close();$("auto").checked=true;await load();$("notice").textContent="새 실행으로 재제출했습니다: "+value.job_id;
  });
}


// Real queue walkthrough. Replay advances recorded observations only.
let scenarioData=null, scenarioRecent=[], scenarioRef="", scenarioUpdated=0;
let scenarioPending=false, scenarioStarting=false, scenarioError="", scenarioCursor=null;
let scenarioSelected=1, scenarioReplay=null, scenarioWorkload="", scenarioProfile="", scenarioSubmission=null;
const scenarioTerminal = s => ["SUCCEEDED","FAILED","CANCELED","RESULT_INVALID"].includes(s);
function stopScenarioReplay() { clearInterval(scenarioReplay); scenarioReplay=null; }
function scenarioEvents() {
  return (scenarioData?.jobs || []).flatMap((j,index)=>j ? [
    {index,state:"VALIDATED",reason:null,observed_at:j.created_at},
    ...(j.lifecycle_events || []).map(e=>({...e,index}))
  ] : []).sort((a,b)=>Date.parse(a.observed_at)-Date.parse(b.observed_at) || a.index-b.index);
}
async function scenarioFetch(path) {
  const response=await fetch(API+path,{headers:authHeaders(),cache:"no-store"});
  const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==="string"?result.detail:"상태를 불러오지 못했습니다.");return result;
}
async function refreshScenario() {
  if(scenarioPending || !data)return;
  const session=generation;scenarioPending=true;
  try {
    const list=await scenarioFetch("/queue-scenarios");
    if(session!==generation)return;
    scenarioRecent=list.items;
    if(!scenarioRef)scenarioRef=scenarioRecent[0]?.ref || "";
    const result=scenarioRef ? await scenarioFetch("/queue-scenarios/"+encodeURIComponent(scenarioRef)) : null;
    if(session!==generation)return;
    scenarioData=result;scenarioError="";
  } catch(e) {if(session===generation)scenarioError=e.message;}
  finally {if(session===generation){scenarioPending=false;scenarioUpdated=Date.now();if(active==="scenario" && !document.activeElement?.closest(".scenario-request, .scenario-toolbar select"))render();}}
}
async function startScenario(workload,profile) {
  if(scenarioStarting)return;
  scenarioStarting=true;scenarioError="";stopScenarioReplay();scenarioCursor=null;
  const session=generation, storageKey="ra-queue-scenario:"+data.project_ref;
  let saved=scenarioSubmission;
  try{saved ||= JSON.parse(sessionStorage.getItem(storageKey));}catch(_){/* Memory-free recovery uses server list. */}
  const request={workload_ref:workload,profile_ref:profile};
  if(saved && JSON.stringify(saved.request)!==JSON.stringify(request)) {
    scenarioError="미확인 제출이 있습니다. 이전 작업·정책으로 같은 요청을 다시 확인해 주세요.";scenarioStarting=false;render();return;
  }
  saved ||= {request,key:"scenario-"+crypto.randomUUID()};scenarioSubmission=saved;
  try{sessionStorage.setItem(storageKey,JSON.stringify(saved));}catch(_){}
  render();
  try {
    const response=await fetch(API+"/queue-scenarios",{method:"POST",headers:{...authHeaders(),"Content-Type":"application/json","Idempotency-Key":saved.key},body:JSON.stringify(request)});
    const result=await response.json();
    if(!response.ok)throw new Error(typeof result.detail==="string"?result.detail:"시나리오를 접수하지 못했습니다.");
    try{sessionStorage.removeItem(storageKey);}catch(_){}
    scenarioSubmission=null;
    if(session!==generation)return;
    scenarioRef=result.ref;scenarioData=result;scenarioSelected=1;scenarioUpdated=Date.now();
    $("auto").checked=true;await refreshScenario();
  } catch(e) {if(session===generation)scenarioError=e.message+" · 같은 조건으로 다시 누르면 중복 없이 제출을 확인합니다.";}
  finally {if(session===generation){scenarioStarting=false;render();}}
}
function scenarioLane(state) {
  if(!state || ["VALIDATED","SUBMITTING","SUBMISSION_UNKNOWN"].includes(state))return 0;
  if(["QUEUED","CANCEL_REQUESTED"].includes(state))return 1;
  if(["RUNNING","COLLECTING"].includes(state))return 2;
  return 3;
}
function scenarioReason(state,reason) {
  if(["None","(null)","N/A"].includes(reason))reason=null;
  if(reason)return ({AssocGrpCpuLimit:"계정의 CPU 할당 한도에 도달해 대기합니다.",AssocGrpGRES:"계정의 GPU/GRES 할당 한도에 도달해 대기합니다.",AssocGrpMemLimit:"계정의 메모리 할당 한도에 도달해 대기합니다.",QOSGrpCpuLimit:"QOS의 CPU 할당 한도에 도달해 대기합니다.",QOSGrpGRES:"QOS의 GPU/GRES 할당 한도에 도달해 대기합니다.",AdmissionPending:"큐 승인 대기 · 아직 실행 자원을 배정받지 못했습니다.",Resources:"요청 자원 확보 대기",Priority:"먼저 처리할 작업이 있습니다.",ContainerCreating:"자원 배정 후 컨테이너 준비 중",Unschedulable:"배치 조건을 만족하는 노드 대기"})[reason] || reason;
  return ({VALIDATED:"자원 요청을 접수했습니다.",SUBMITTING:"스케줄러에 요청을 전달하고 있습니다.",QUEUED:"스케줄러가 실행 시작을 보고할 때까지 대기합니다.",RUNNING:"백엔드에서 실행 중임을 확인했습니다.",COLLECTING:"실행 결과를 수집하고 있습니다.",SUCCEEDED:"실행과 결과 수집이 완료됐습니다.",FAILED:"실행 실패 · 상세 오류를 확인하세요.",CANCELED:"취소 완료",RESULT_INVALID:"결과 검증 실패",SUBMISSION_UNKNOWN:"제출 여부 재확인 중 · 재제출하지 않습니다.",CANCEL_REQUESTED:"취소 확인 중 · 자원 반환은 아직 미확인"})[state] || "아직 제출되지 않았습니다.";
}
function scenarioView() {
  const root=el("div",null,"scenario-page");
  const catalog=(data.submission_catalog || []).filter(w=>w.candidates.length===1 && w.candidates[0].device_class==="gpu" && canSubmit(w.candidates[0]));
  scenarioWorkload ||= catalog.find(w=>w.candidates[0].backend==="slurm")?.workload_ref || catalog[0]?.workload_ref || "";
  const profiles=(data.scheduling_profiles || []).filter(p=>p.policy.priority==="normal");
  scenarioProfile ||= profiles.find(p=>p.policy.backend_order[0]==="kubernetes")?.ref || profiles[0]?.ref || "";
  const work=catalog.find(w=>w.workload_ref===scenarioWorkload), resources=work?.candidates[0]?.resources;
  const activeRun=scenarioData?.jobs.some(j=>j && !scenarioTerminal(j.state));
  const hero=add(el("section",null,"scenario-hero"),el("p","LIVE QUEUE WALKTHROUGH","eyebrow"),el("h2","내 자원 요청은 지금 어디에 있을까?"),
    el("p","같은 GPU 실행 환경을 요청하는 작업 A와 B를 제출합니다. 큐가 자원을 배분하고, 요청 카드는 관측된 상태에 따라 이동합니다."),
    add(el("div",null,"scenario-story"),el("span","01  연구자가 요청"),el("span","02  큐에서 대기"),el("span","03  자원 확보 · 실행"),el("span","04  결과 확인")));
  root.append(hero);
  const controls=el("div",null,"scenario-request");
  const workload=el("select");workload.setAttribute("aria-label","시나리오 작업");
  for(const w of catalog)workload.append(new Option(workloadPurpose(w)+" · "+w.workload_ref,w.workload_ref));workload.value=scenarioWorkload;
  workload.onchange=()=>{scenarioWorkload=workload.value;render();};
  const profile=el("select");profile.setAttribute("aria-label","시나리오 정책");
  for(const p of profiles)profile.append(new Option(p.name,p.ref));profile.value=scenarioProfile;
  profile.onchange=()=>{scenarioProfile=profile.value;};
  const launch=el("button",scenarioStarting?"요청 접수 중…":"두 작업으로 시나리오 실행","primary");
  launch.disabled=scenarioStarting || activeRun || !work || !scenarioProfile;
  launch.onclick=()=>startScenario(scenarioWorkload,scenarioProfile);
  controls.append(add(el("label"),el("span","연구 작업"),workload),add(el("label"),el("span","실행 정책"),profile),launch);
  const requestPanel=panel("연구자의 자원 요청서","등록된 실행 환경을 재사용합니다. GPU 모델·노드를 직접 고르지 않습니다.",add(el("div",null,"scenario-request-body"),controls,
    resources ? add(el("div",null,"scenario-request-summary"),badge("요청당 GPU "+resources.accelerator_count),badge("CPU "+resources.host_cpu+" core"),badge("메모리 "+resources.host_memory_mib+" MiB"),badge("최대 실행 "+work.max_run_seconds+"초"),badge("요청 2건")) : empty("현재 실행 가능한 GPU 작업이 없습니다."),
    el("small","실제 컴퓨트 작업 2개가 생성됩니다. 할당량·우선순위는 기존 정책을 따릅니다. 여유가 충분하거나 작업이 짧으면 대기를 관측하지 못할 수도 있습니다.")));
  if(scenarioData)root.append(add(el("details",null,"scenario-new-request"),el("summary","새 시나리오 실행 · 자원 요청서 열기"),requestPanel));else root.append(requestPanel);
  if(scenarioError)root.append(add(el("div",null,"scenario-error"),el("strong","최신 상태 확인 필요"),el("p",scenarioError),el("small","아래 기록은 마지막으로 성공한 조회 시점입니다.")));
  const toolbar=el("div",null,"scenario-toolbar");
  const recent=el("select");recent.setAttribute("aria-label","시나리오 실행 이력");
  for(const r of scenarioRecent)recent.append(new Option(stamp(r.created_at)+" · "+r.workload_ref,r.ref));recent.value=scenarioRef;
  recent.onchange=()=>{scenarioRef=recent.value;scenarioData=null;scenarioCursor=null;stopScenarioReplay();refreshScenario();};
  const refresh=el("button","상태 갱신");refresh.disabled=scenarioPending;refresh.onclick=()=>refreshScenario();
  toolbar.append(recent,refresh);root.append(toolbar);
  if(!scenarioData){root.append(empty(scenarioPending?"시나리오 기록을 불러오는 중…":"위에서 실행하면 두 요청의 실제 진행 과정이 여기에 나타납니다."));return root;}
  const events=scenarioEvents(), cursor=scenarioCursor===null?events.length-1:Math.min(scenarioCursor,events.length-1);
  const visible=events.slice(0,cursor+1), snapshots=[0,1].map(i=>visible.filter(e=>e.index===i).at(-1));
  const replay=el("button",scenarioReplay?"재생 일시정지":"기록 재생");replay.disabled=events.length<2;
  replay.onclick=()=>{if(scenarioReplay){stopScenarioReplay();render();return;}scenarioCursor=0;scenarioReplay=setInterval(()=>{if(active!=="scenario"){stopScenarioReplay();return;}scenarioCursor++;if(scenarioCursor>=scenarioEvents().length-1)stopScenarioReplay();render();},1100);render();};
  const live=el("button","실시간으로 돌아가기");live.onclick=()=>{stopScenarioReplay();scenarioCursor=null;refreshScenario();render();};
  const slider=el("input");slider.type="range";slider.min=0;slider.max=Math.max(0,events.length-1);slider.value=Math.max(0,cursor);slider.setAttribute("aria-label","관측 기록 탐색");
  slider.oninput=()=>{stopScenarioReplay();scenarioCursor=Number(slider.value);render();$("scenario-seek")?.focus();};slider.id="scenario-seek";
  root.append(add(el("section",null,"scenario-playback"),add(el("div",null,"scenario-toolbar"),badge(scenarioError?"마지막 관측":scenarioCursor===null?"실시간 관측 · 3초 갱신":"기록 탐색",scenarioError?"warn":""),replay,live),slider,
    el("small",`관측 ${Math.max(0,cursor+1)} / ${events.length} · ${stamp(visible.at(-1)?.observed_at)} · 재생은 저장된 관측 순서이며 실제 소요 시간과 다릅니다.`)));
  const board=el("div",null,"scenario-board");
  ["요청 접수","큐 · 실행 준비","실행 · 결과 수집","종료"].forEach((name,lane)=>{
    const column=add(el("section",null,"scenario-lane"),add(el("div",null,"scenario-lane-title"),el("small",String(lane+1).padStart(2,"0")),el("h3",name)));
    let count=0;
    snapshots.forEach((snapshot,index)=>{
      if(!snapshot || scenarioLane(snapshot.state)!==lane)return;count++;
      const job=scenarioData.jobs[index], card=el("button",null,"scenario-job"+(scenarioSelected===index?" selected":""));
      card.setAttribute("aria-label","요청 "+(index===0?"A":"B")+" 상세");card.setAttribute("aria-pressed",String(scenarioSelected===index));
      card.onclick=()=>{scenarioSelected=index;render();};
      add(card,el("small",index===0?"REQUEST A":"REQUEST B"),el("strong",index===0?"먼저 보낸 연구 요청":"뒤이어 보낸 연구 요청"),stateBadge(snapshot.state),
        el("p",scenarioReason(snapshot.state,snapshot.reason)),el("small",`${job.backend} · GPU ${job.requested_resources?.accelerator_count ?? "—"}`));
      column.append(card);
    });
    if(!count)column.append(el("p","이 단계의 요청 없음","scenario-lane-empty"));board.append(column);
  });root.append(board);
  const selected=scenarioData.jobs[scenarioSelected], snapshot=snapshots[scenarioSelected];
  if(selected){
    const detail=el("div",null,"scenario-inspector");
    const plan=selected.scheduling_plan, queue=plan?.adapter?.local_queue || plan?.adapter?.partition || "—";
    const observedWait=(selected.lifecycle_events || []).some(e=>e.state==="QUEUED" && ( ["AdmissionPending","Resources","Priority"].includes(e.reason) || /^(Assoc|QOS).*Limit$|^(Assoc|QOS).*GRES$/.test(e.reason || "")));
    const facts=add(el("div"),el("p","선택한 요청 · "+(scenarioSelected===0?"A":"B"),"eyebrow"),el("h2",scenarioReason(snapshot?.state,snapshot?.reason)),
      snapshot?.reason && snapshot.reason!=="None" ? code(snapshot.reason) : null,
      el("p",`배정 경로: ${selected.backend} → ${queue}`),el("p",`선택 노드: ${selected.node_ref || "확인 중"}`),
      el("p",`요청량: GPU ${selected.requested_resources?.accelerator_count ?? "—"} · CPU ${selected.requested_resources?.host_cpu ?? "—"} · 메모리 ${selected.requested_resources?.host_memory_mib ?? "—"} MiB`),
      el("small",observedWait?"이 실행에서 스케줄러 대기 사유가 기록됐습니다.":"이 실행에는 자원·승인 대기 사유가 아직 기록되지 않았습니다. 컨테이너 준비와 자원 부족은 구분합니다."));
    const actions=el("div",null,"scenario-toolbar");const open=el("button","현재 작업 상세");open.onclick=()=>showJob(selected);actions.append(open);
    if(selected.tracking?.run_id){const ml=el("button","MLflow 결과");ml.onclick=()=>openExperiment(selected.tracking.run_id);actions.append(ml);}
    const cancel=cancelButton(selected);if(cancel)actions.append(cancel);facts.append(actions);
    const log=el("ol",null,"scenario-event-log");
    events.forEach((e,i)=>{if(e.index!==scenarioSelected)return;const b=el("button",null,i===cursor?"selected":"");
      b.onclick=()=>{stopScenarioReplay();scenarioCursor=i;render();};add(b,el("small",stamp(e.observed_at)),el("strong",states[e.state]||e.state),el("span",scenarioReason(e.state,e.reason)));log.append(add(el("li"),b));});
    detail.append(facts,add(el("div"),el("h3","저장된 상태 변화"),log));root.append(detail);
    if(scenarioCursor===null){
      const matches=data.inventory.flatMap(s=>(s.cluster_queues || []).filter(q=>q.ref===queue).map(q=>({s,q})));const entry=matches[0];
      if(entry){const observation=observed(entry.q.observation,entry.s);if(observation.status==="ok")root.append(panel("이 큐의 할당량","수집 시점: "+stamp(entry.q.observation.observed_at)+" · 예약량은 실제 GPU 사용률과 다릅니다.",table(["자원","명목 할당량","예약량","승인량"],observation.value.resources.map(r=>[r.resource,String(r.nominal_quota),String(r.reserved),String(r.admitted)]))));}
    }
  }
  return root;
}
setInterval(()=>{if(active==="scenario" && data && !scenarioPending && !scenarioStarting)refreshScenario();},3000);

// Graph nodes are real buttons. Edges distinguish execution lineage from KFP DAGs.
let graphSerial=0;
function svgElement(name,attrs={}) {
  const node=document.createElementNS("http://www.w3.org/2000/svg",name);
  for(const [k,v] of Object.entries(attrs))node.setAttribute(k,String(v));
  return node;
}
function workflowCanvas(nodes,edges,onSelect,{width=1230,height=370,label="작업 실행 연결 그래프"}={}) {
  const wrap=el("div",null,"ml-graph-wrap"), bar=el("div",null,"ml-graph-controls");
  const viewport=svgElement("svg",{viewBox:`0 0 ${width} ${height}`,class:"ml-graph-canvas",role:"group","aria-label":label});
  const arrow="flow-arrow-"+(++graphSerial), defs=svgElement("defs");
  const marker=svgElement("marker",{id:arrow,viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:6,markerHeight:6,orient:"auto-start-reverse"});
  marker.append(svgElement("path",{d:"M 0 0 L 10 5 L 0 10 z",class:"ml-arrow"}));defs.append(marker);viewport.append(defs);
  for(const [from,to] of edges){
    const a=nodes.find(n=>n.id===from),b=nodes.find(n=>n.id===to);if(!a || !b)continue;
    const vertical=a.x===b.x, x1=vertical?a.x+100:a.x+200, y1=vertical?a.y+96:a.y+48, x2=vertical?b.x+100:b.x, y2=vertical?b.y:b.y+48;
    const d=vertical?`M ${x1} ${y1} L ${x2} ${y2}`:`M ${x1} ${y1} C ${x1+28} ${y1}, ${x2-28} ${y2}, ${x2} ${y2}`;
    viewport.append(svgElement("path",{d,class:"ml-edge "+(b.tone||""),"marker-end":`url(#${arrow})`}));
  }
  for(const n of nodes){
    const foreign=svgElement("foreignObject",{x:n.x,y:n.y,width:202,height:100});
    const b=document.createElementNS("http://www.w3.org/1999/xhtml","button");b.setAttribute("type","button");
    b.className="ml-graph-node "+(n.tone||"")+(n.selected?" selected":"");b.setAttribute("aria-label",n.label+" 상세");b.setAttribute("aria-pressed",String(Boolean(n.selected)));
    b.append(el("small",n.kicker),el("strong",n.label),el("span",n.caption));b.title=n.label+" · "+n.caption;b.onclick=()=>onSelect(n.id);foreign.append(b);viewport.append(foreign);
  }
  const focus=nodes.find(n=>n.selected)||nodes[0];
  let camera=window.innerWidth<650 && width>520 ? {x:Math.max(0,focus.x-100),y:Math.max(0,focus.y-100),w:420,h:312} : {x:0,y:0,w:width,h:height}, drag=null;
  const paint=()=>viewport.setAttribute("viewBox",`${camera.x} ${camera.y} ${camera.w} ${camera.h}`);
  paint();
  const scale=factor=>{const next=Math.max(320,Math.min(width*1.5,camera.w*factor)),ratio=next/camera.w;camera={x:camera.x+(camera.w-next)/2,y:camera.y+(camera.h-camera.h*ratio)/2,w:next,h:camera.h*ratio};paint();};
  for(const [text,label,fn] of [["−","그래프 축소",()=>scale(1.2)],["＋","그래프 확대",()=>scale(1/1.2)],["전체 보기","그래프 전체 보기",()=>{camera={x:0,y:0,w:width,h:height};paint();}]]){
    const b=el("button",text);b.setAttribute("aria-label",label);b.onclick=fn;bar.append(b);
  }
  viewport.onpointerdown=e=>{if(e.target.closest("button") || e.button!==0)return;const matrix=viewport.getScreenCTM();drag={x:e.clientX,y:e.clientY,camera:{...camera},scale:matrix.a};viewport.setPointerCapture(e.pointerId);};
  viewport.onpointermove=e=>{if(!drag)return;camera.x=drag.camera.x-(e.clientX-drag.x)/drag.scale;camera.y=drag.camera.y-(e.clientY-drag.y)/drag.scale;paint();};
  viewport.onpointerup=viewport.onpointercancel=()=>{drag=null;};
  bar.append(el("small","노드 클릭 · 빈 배경을 드래그해 이동"));wrap.append(bar,viewport);return wrap;
}
function mlJobsGraph() {
  const items=data.jobs.items, root=el("div",null,"ml-workbench");
  let job=items.find(j=>j.job_id===graphJobId) || items.find(j=>j.job_id===trackedJobId) || items.find(j=>!scenarioTerminal(j.state)) || items[0];
  graphJobId=job.job_id;
  const picker=el("div",null,"ml-job-picker");picker.setAttribute("aria-label","그래프로 볼 작업 선택");
  for(const j of items){const b=el("button",null,j.job_id===job.job_id?"selected":"");b.setAttribute("aria-label",`작업 그래프 · ${j.job_id}`);b.setAttribute("aria-pressed",String(j.job_id===job.job_id));
    b.append(el("small",j.backend+" · "+stamp(j.created_at)),el("strong",j.template?.name||j.workload_ref),stateBadge(j.state),el("small",j.job_id.slice(0,12)));
    b.onclick=()=>{graphJobId=j.job_id;graphSelection="compute";render();};picker.append(b);
  }
  root.append(picker,el("p",`현재 검색 결과 ${data.jobs.total}개 중 이 페이지 ${items.length}개 · 작업 카드를 선택하면 아래 그래프가 바뀝니다.`,"ml-graph-hint"));
  const info=job.workload_summary || {}, terminal=scenarioTerminal(job.state), successful=job.state==="SUCCEEDED";
  const running=job.state==="RUNNING" || job.state==="COLLECTING";
  const started=Boolean(job.started_at || running || job.result);
  const submitted=Boolean(job.external_id || job.queued_at);
  const tone=successful?"complete":["FAILED","RESULT_INVALID","CANCELED"].includes(job.state)?"problem":running?"active":"waiting";
  const pipeline=researchData?.kubeflow?.items?.find(r=>r.job_id===job.job_id);
  const q=job.scheduling_plan?.adapter;
  const short=v=>v?String(v).replace(/^sha256:/,"").slice(0,16):"등록 정보 없음";
  const nodes=[
    {id:"model",x:20,y:108,kicker:"MODEL",label:"모델 · 실행 대상",caption:info.model_digest?short(info.model_digest):"모델 정보 미기록",tone:"configured"},
    {id:"input",x:20,y:242,kicker:"DATA / INPUT",label:"데이터 · 입력",caption:info.input_shape?.length?`입력 ${info.input_shape.join(" × ")}`:"입력 정보 미기록",tone:"configured"},
    {id:"workload",x:270,y:175,kicker:(info.task_type||"ML WORKLOAD").toUpperCase(),label:({training:"학습 작업",inference:"추론 작업",benchmark:"벤치마크 작업",preprocessing:"전처리 작업"})[info.task_type]||"ML 작업",caption:`배치 ${info.batch_size ?? "—"} · ${info.precision || "정밀도 미기록"}`,tone:"complete"},
    {id:"policy",x:520,y:30,kicker:"SCHEDULING POLICY",label:"자원 요청 · 정책",caption:job.scheduling_plan?.profile_ref||"기본 실행 설정",tone:"configured"},
    {id:"queue",x:520,y:175,kicker:job.backend==="slurm"?"SLURM QUEUE":"KUEUE / KUBERNETES",label:q?.local_queue || q?.partition || "백엔드 큐",caption:submitted?(job.state==="QUEUED"?"대기 중 · 눌러서 이유 확인":"백엔드 제출 확인"):"제출 준비",tone:started?"complete":submitted?"waiting":""},
    {id:"compute",x:770,y:175,kicker:job.allocation_mode==="cpu_only"?"CPU EXECUTION":"GPU / NPU EXECUTION",label:job.node_ref || "실행 대상 확인 중",caption:states[job.state]||job.state,tone},
    {id:"result",x:1020,y:175,kicker:"RESULT / EXPERIMENT",label:successful?"실험 결과":terminal?"종료 기록":"결과 수집",caption:job.tracking?.run_id?"MLflow 기록 연결됨":job.result?"결과 수집됨":terminal?"종료 상세 확인":"실행 후 기록",tone:terminal?tone:""},
  ];
  const edges=[["model","workload"],["input","workload"],["workload","queue"],["policy","queue"],["queue","compute"],["compute","result"]];
  if(pipeline){nodes.push({id:"pipeline",x:270,y:30,kicker:"KUBEFLOW",label:"연결된 파이프라인",caption:pipeline.state,tone:"configured"});edges.push(["pipeline","workload"]);}
  for(const n of nodes)n.selected=n.id===graphSelection;
  const heading=add(el("div",null,"ml-graph-heading"),add(el("div"),el("p","EXECUTION LINEAGE","eyebrow"),el("h3",job.template?.name||job.workload_ref),el("small",job.job_id)),stateBadge(job.state));
  root.append(heading,workflowCanvas(nodes,edges,id=>{graphSelection=id;render();}),
    add(el("div",null,"ml-graph-legend"),el("span","● 파랑: 실행 중 / 선택"),el("span","● 초록: 확인된 완료"),el("span","● 주황: 대기"),el("span","○ 회색: 설정 / 미관측")),
    el("p","선은 이 작업의 입력·실행·결과 연결을 뜻합니다. 각 노드가 별도의 학습·전처리 작업이라는 의미는 아닙니다.","ml-graph-hint"));
  const selected=nodes.find(n=>n.id===graphSelection)||nodes.find(n=>n.id==="compute");
  const inspector=el("div",null,"ml-node-inspector");
  const title=add(el("div"),el("p",selected.kicker,"eyebrow"),el("h3",selected.label));
  const content=el("div",null,"ml-node-content");
  if(selected.id==="model")content.append(el("p","모델 식별자: "+(info.model_digest||"미기록")),el("p","정밀도: "+(info.precision||"미기록")),el("small","등록된 작업의 모델 식별자를 표시합니다. 모델 파일이나 학습 단계를 새로 만들지 않습니다."));
  if(selected.id==="input")content.append(el("p","데이터 버전: "+(info.dataset_version||"미기록")),el("p","입력 크기: "+(info.input_shape?.join(" × ")||"미기록")),el("p","배치 크기: "+(info.batch_size??"미기록")));
  if(selected.id==="workload")content.append(el("p",job.workload_ref),el("p","측정 범위: "+(info.measurement_boundary||"미기록")),el("small","Notebook·웹·파이프라인이 제출한 실행 단위입니다. 이 그래프는 등록된 실행 정보를 보여줍니다."));
  if(selected.id==="policy")content.append(el("p",`요청 가속기 ${job.requested_resources?.accelerator_count ?? "—"} · CPU ${job.requested_resources?.host_cpu ?? "—"} · 메모리 ${job.requested_resources?.host_memory_mib ?? "—"} MiB`),el("p",`우선순위: ${job.priority==="high"?"높음":"보통"} · 실행 제한 ${job.execution_limits?.max_run_seconds ?? "—"}초`),job.scheduling_plan?details("후보 비교 · 선택 근거",job.scheduling_plan):el("small","공통 정책 적용 기록이 없는 실행입니다."));
  if(selected.id==="queue"){
    content.append(el("p",scenarioReason(job.state,job.scheduler_reason)),el("p","백엔드 제출: "+stamp(job.queued_at)));
    const waited=(job.lifecycle_events||[]).filter(e=>e.state==="QUEUED" && e.reason && e.reason!=="None");
    for(const e of waited)content.append(el("small",stamp(e.observed_at)+" · "+scenarioReason(e.state,e.reason)));
    content.append(actionLink("전체 큐 · 쿼터 보기 →","#history"));
  }
  if(selected.id==="compute")content.append(el("p",`${job.backend} · ${modes[job.allocation_mode]||"할당 방식 미기록"}`),el("p",scenarioReason(job.state,job.scheduler_reason)),el("small","최근 실행 관측: "+stamp(job.backend_observed_at)),job.error?el("p",job.error,"error-message"):null);
  if(selected.id==="pipeline")content.append(el("p",pipeline.display_name),researchButton("실제 파이프라인 DAG 열기",()=>openPipeline(pipeline.run_id)));
  if(selected.id==="result"){
    const m=job.result?.measurements;
    content.append(m?dashboardStats([["p95 지연",m.latency_p95_ms==null?"—":fmt(m.latency_p95_ms)+" ms","모델 측정 구간"],["처리량",m.throughput==null?"—":fmt(m.throughput),"측정값"],["실행 측정",m.elapsed_seconds==null?"—":duration(m.elapsed_seconds),"큐 대기 시간 제외"]]):el("p",terminal?"수집된 모델 측정 결과가 없습니다. 종료 기록을 확인하세요.":"실행 결과를 기다립니다."));
    if(job.tracking?.run_id)content.append(researchButton("MLflow 지표 · 아티팩트",()=>openExperiment(job.tracking.run_id)));
  }
  const actions=add(el("div",null,"ml-node-actions"),researchButton("작업 전체 상세",()=>showJob(job)),cancelButton(job));
  if(pipeline && selected.id!=="pipeline")actions.append(researchButton("Kubeflow DAG",()=>openPipeline(pipeline.run_id)));
  inspector.append(title,content,actions);root.append(inspector);return root;
}
function pipelineDag(run) {
  if(!run.graph?.length)return empty("등록된 파이프라인 의존관계가 없습니다. 아래 실제 실행 기록을 확인하세요.");
  const tasks=run.graph, names=new Set(tasks.map(t=>t.name)), levels=new Map();
  for(let i=0;i<tasks.length;i++)for(const t of tasks)if(!levels.has(t.name) && t.dependencies.every(d=>!names.has(d)||levels.has(d)))levels.set(t.name,Math.max(-1,...t.dependencies.filter(d=>names.has(d)).map(d=>levels.get(d)))+1);
  if(levels.size!==tasks.length)return empty("의존관계에 순환 또는 미해결 연결이 있습니다. 원본 단계 기록을 확인하세요.");
  const rows=new Map(),positions=tasks.map(t=>{const level=levels.get(t.name),row=rows.get(level)||0;rows.set(level,row+1);const state=run.tasks.find(r=>r.display_name===t.name)?.state||"미관측";
    return {id:t.name,x:20+level*260,y:24+row*125,kicker:"KUBEFLOW TASK",label:t.name,caption:state,tone:state==="SUCCEEDED"?"complete":state==="RUNNING"?"active":state==="FAILED"?"problem":""};});
  const selected=el("div",null,"ml-task-inspector");selected.append(el("small","단계 노드를 누르면 실제 실행 상태와 선행 작업을 확인할 수 있습니다."));
  const canvas=workflowCanvas(positions,tasks.flatMap(t=>t.dependencies.filter(d=>names.has(d)).map(d=>[d,t.name])),id=>{const task=tasks.find(t=>t.name===id),observations=run.tasks.filter(t=>t.display_name===id);
    selected.replaceChildren(el("h3",id),el("p","선행 단계: "+(task.dependencies.join(", ")||"없음")),observations.length?table(["상태","시작","종료"],observations.map(t=>[badge(t.state),stamp(t.start_time),stamp(t.end_time)])):el("p","이 단계의 실행 상태는 아직 관측되지 않았습니다."));
  },{width:Math.max(520,Math.max(...levels.values())*260+245),height:Math.max(230,Math.max(...rows.values())*125+40),label:"Kubeflow 실제 단계 의존관계 그래프"});
  return add(el("div"),canvas,selected);
}
