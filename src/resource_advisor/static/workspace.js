import { imageDimensions } from "./image-header.js";

/** Task-first workspace. Existing scheduler and release contracts remain authoritative. */
export function createWorkflowWorkspace(c) {
  const { el, add, badge, stateBadge, table, stamp, duration, actionLink } = c;
  const terminal = new Set([
    "SUCCEEDED",
    "FAILED",
    "CANCELED",
    "RESULT_INVALID",
  ]);
  const groups = {
    home: [
      ["home", "시작하기"],
      ["operations", "운영 현황"],
    ],
    jobs: [
      ["jobs", "워크로드"],
      ["submit", "새 워크로드"],
      ["placement", "자원 조합 검토"],
      ["pipelines", "파이프라인"],
      ["notebooks", "노트북"],
    ],
    models: [
      ["models", "서비스 상태"],
      ["inference", "이미지 체험"],
    ],
    execution: [
      ["execution", "장비 현황"],
      ["history", "큐·할당량"],
      ["usage", "사용량"],
      ["compatibility", "호환성"],
    ],
    experiments: [
      ["experiments", "실험 비교"],
      ["recommendations", "추천 근거"],
      ["jct", "JCT 비교"],
      ["scheduler-lab", "스케줄링 실험실"],
      ["scenario", "자원 요청 시나리오"],
    ],
  };
  let catalogFilter = "all",
    catalogSearch = "",
    menuOpen = false,
    inlineTab = "summary",
    inlineJobId = null;
  let pools = [],
    poolPage = 0,
    poolHasNext = false,
    poolRef = "",
    poolHealth = null;
  let poolsAt = 0,
    poolsPending = false,
    poolError = "",
    poolRequest = 0;
  let imageURL = "",
    imageName = "",
    imagePixels = null,
    imagePending = false;
  let predictPending = false,
    prediction = null,
    inferenceError = "",
    imageRequest = 0;
  let inferenceAbort = null,
    labels = null;
  const icon = (name) => {
    const n = el("img", null, "ui-icon");
    n.src = "/console/icons/" + name + ".svg";
    n.alt = "";
    n.setAttribute("aria-hidden", "true");
    return n;
  };
  const names = {
    inference: "추론",
    training: "학습",
    benchmark: "성능 측정",
    preprocessing: "전처리",
  };
  function title(w) {
    if (w.name) return w.name;
    if (w.measurement_boundary?.startsWith("cuda-squares"))
      return /sustained/i.test(w.workload_ref)
        ? "GPU 연산 성능 측정 · 90초"
        : "GPU 연결·연산 확인";
    if (/resnet50/i.test(w.workload_ref))
      return /disjoint100/i.test(w.workload_ref)
        ? "ResNet50 · 고정 입력 품질 확인"
        : "이미지 분류 · ResNet50";
    if (/resnet18/i.test(w.workload_ref)) return "이미지 분류 · ResNet18";
    if (/cnn/i.test(w.workload_ref))
      return w.task_type === "training"
        ? "이미지 모델 학습 · CNN"
        : "CNN 이미지 추론";
    if (/right-sizing-slurm/i.test(w.workload_ref))
      return "CNN 추론 성능 측정 · Slurm";
    if (/intel-conv/i.test(w.workload_ref)) return "NPU 연산 확인 · Intel";
    if (/mobilint-candy/i.test(w.workload_ref)) return "이미지 추론 · Mobilint";
    if (/kernel|e5/i.test(w.workload_ref)) return "CNN 추론 · GPU 캐시 측정";
    return w.workload_ref;
  }
  const catalog = () =>
    Array.isArray(c.data?.submission_catalog)
      ? c.data.submission_catalog
      : c.data?.templates?.items || [];
  function focusSetup() {
    const step = document.getElementById("wizard-step-2");
    if (step) {
      step.focus({ preventScroll: true });
      step.scrollIntoView({ block: "nearest" });
    }
  }
  function pick(w) {
    const session = c.generation();
    c.selectWorkload(c.selectionKey(w));
    c.navigate("submit");
    setTimeout(() => {
      if (session === c.generation()) {
        c.preview(w);
        focusSetup();
      }
    }, 0);
  }
  function navigation(active) {
    const group =
      Object.keys(groups).find((key) =>
        groups[key].some(([ref]) => ref === active),
      ) || "home";
    document.querySelectorAll('nav[aria-label="주 메뉴"] a').forEach((a) => {
      if (a.dataset.view === group) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    const tabs = document.getElementById("section-tabs");
    tabs.replaceChildren(
      ...groups[group].map(([ref, label]) => {
        const a = el("a", label);
        a.href = "#" + ref;
        if (ref === active) a.setAttribute("aria-current", "page");
        return a;
      }),
    );
    document.body.dataset.workspaceView = active;
  }
  function section(titleText, description, body, link = null) {
    return add(
      el("section", null, "workspace-panel"),
      add(
        el("div", null, "workspace-panel-heading"),
        add(
          el("div"),
          el("h2", titleText),
          description ? el("p", description) : null,
        ),
        link,
      ),
      body,
    );
  }
  function home() {
    const data = c.data,
      root = el("div", null, "home-workspace"),
      op = data.operations;
    const first = !Object.values(data.job_counts || {}).reduce(
      (sum, n) => sum + n,
      0,
    );
    root.append(
      add(
        el("section", null, "welcome-block"),
        el("p", "AI 작업, 여기서 시작하세요", "eyebrow"),
        el(
          "h2",
          first ? "첫 작업을 실행해볼까요?" : "오늘은 어떤 작업을 해볼까요?",
        ),
        el(
          "p",
          "실행할 작업을 고르면 호환되는 장치를 찾아요. 실행 과정과 결과도 여기서 확인할 수 있어요.",
        ),
        add(
          el("div", null, "welcome-actions"),
          actionLink("예제 작업 고르기", "#submit"),
          actionLink("GPU·NPU 이미지 체험", "#inference"),
        ),
      ),
    );
    const guide = el("div", null, "start-guide");
    for (const [name, heading, description, href] of [
      ["book-open", "1. 작업 고르기", "등록된 예제로 시작해요.", "#submit"],
      ["cpu", "2. 실행 확인하기", "장치 선택과 대기 이유를 확인해요.", "#jobs"],
      [
        "circle-check",
        "3. 결과 살펴보기",
        "결과와 실험 기록을 함께 봐요.",
        "#experiments",
      ],
    ]) {
      const a = el("a", null, "guide-step");
      a.href = href;
      a.append(
        icon(name),
        add(el("div"), el("strong", heading), el("p", description)),
      );
      guide.append(a);
    }
    root.append(guide);
    const counts = el("div", null, "home-status-strip");
    for (const [label, n, href, tone] of [
      [
        "실행 중",
        op?.running_total ?? data.job_counts.RUNNING ?? 0,
        "#jobs",
        "good",
      ],
      ["대기·준비", op?.waiting_total ?? 0, "#jobs", "warn"],
      ["완료한 작업", data.job_counts.SUCCEEDED ?? 0, "#jobs", ""],
      [
        "확인할 문제",
        (op?.node_issues.length ?? 0) + (op?.failed_24h_total ?? 0),
        "#operations",
        "bad",
      ],
    ]) {
      const a = el("a", null, "home-status " + tone);
      a.href = href;
      a.append(el("span", label), el("strong", String(n)));
      counts.append(a);
    }
    root.append(counts);
    const recent = data.jobs.items.slice(0, 5),
      rows = recent.map((j) => [
        add(
          el("div", null, "job-name"),
          el("strong", j.template?.name || title(j)),
          el("small", j.job_id),
        ),
        stateBadge(j.state),
        j.node_ref || "아직 선택 전",
        stamp(j.created_at),
        (() => {
          const b = el("button", "결과·상세");
          b.onclick = () => c.openJob(j);
          return b;
        })(),
      ]);
    root.append(
      section(
        "최근 작업",
        "진행 상태와 결과를 이어서 확인하세요.",
        rows.length
          ? table(["작업", "상태", "실행 장치", "접수 시각", "상세"], rows)
          : c.empty(
              "아직 실행한 작업이 없어요. 위의 예제 작업 고르기로 시작하세요.",
            ),
        actionLink("전체 작업 보기", "#jobs"),
      ),
    );
    const suggestions = el("div", null, "starter-grid");
    for (const w of catalog().slice(0, 3)) {
      const button = el("button", null, "starter-card");
      button.onclick = () => pick(w);
      button.append(
        badge(names[w.task_type] || w.task_type),
        el("h3", title(w)),
        el("p", c.workloadPurpose(w)),
        add(
          el("span", null, "text-action"),
          document.createTextNode("이 작업 선택"),
          icon("arrow-right"),
        ),
      );
      suggestions.append(button);
    }
    if (suggestions.childElementCount)
      root.append(
        section(
          "바로 시작할 수 있는 예제",
          "프로젝트에 등록된 실행 환경을 사용해요.",
          suggestions,
        ),
      );
    root.append(
      add(
        el("p", null, "workspace-note"),
        icon("layers"),
        document.createTextNode(
          "장치 현황과 큐·할당량은 자원에서, 연구용 비교는 실험에서 확인할 수 있어요.",
        ),
      ),
    );
    return root;
  }
  const typeFilters = [
    ["inference", "추론"],
    ["training", "학습"],
    ["benchmark", "성능 측정"],
    ["preprocessing", "전처리"],
  ];
  const backendNames = { kubernetes: "Kubernetes", slurm: "Slurm" };
  function matchesType(w, type) {
    return type === "all" || (type === "template" ? Boolean(w.template_ref) : w.task_type === type);
  }
  function resourceText(r, device) {
    if (!r) return "자원 미기록";
    return (
      (r.accelerator_count
        ? (device ? device.toUpperCase() : "가속기") + " " + r.accelerator_count + " · "
        : "") +
      "CPU " + r.host_cpu + " · " + r.host_memory_mib + " MiB"
    );
  }
  function workloadName(w) {
    const name = title(w);
    return name === w.workload_ref ? name : name + " (" + w.workload_ref + ")";
  }
  function candidateLabel(x) {
    return (x.model || x.candidate_ref) + " · " + (backendNames[x.backend] || x.backend);
  }
  /** Run:ai-style create action: only registered task types, never arbitrary images or YAML. */
  function newWorkloadMenu() {
    const wrap = el("div", null, "new-workload");
    const button = add(
      el("button", null, "primary"),
      icon("play"),
      document.createTextNode("새 워크로드"),
    );
    button.id = "new-workload";
    button.setAttribute("aria-expanded", String(menuOpen));
    button.setAttribute("aria-controls", "new-workload-menu");
    button.onclick = () => {
      menuOpen = !menuOpen;
      c.render();
      if (menuOpen) document.querySelector("#new-workload-menu a")?.focus();
    };
    wrap.append(button);
    if (!menuOpen) return wrap;
    const { items } = c.submissionModel();
    const menu = el("div", null, "new-workload-menu");
    menu.id = "new-workload-menu";
    menu.onkeydown = (e) => {
      if (e.key !== "Escape") return;
      menuOpen = false;
      c.render();
      document.getElementById("new-workload")?.focus();
    };
    const entries = [...typeFilters, ["template", "저장한 템플릿"]]
      .map(([type, label]) => [type, label, items.filter((w) => matchesType(w, type)).length])
      .filter(([, , n]) => n);
    for (const [type, label, n] of entries) {
      const a = add(el("a"), el("strong", label), el("small", n + "개 등록"));
      a.href = "#submit";
      a.onclick = () => {
        catalogFilter = type;
        catalogSearch = "";
        menuOpen = false;
      };
      menu.append(a);
    }
    if (!entries.length) menu.append(el("p", "제출할 수 있게 등록된 작업이 없어요."));
    menu.append(
      el("small", "등록된 실행 환경만 제출할 수 있어요. 임의 이미지·YAML 제출은 지원하지 않습니다."),
    );
    wrap.append(menu);
    return wrap;
  }
  function draftSummary() {
    if (!c.data) return "";
    const m = c.submissionModel();
    if (!m.workload) return "";
    const target = m.workload.candidates.find((x) => x.candidate_ref === m.candidate);
    return (
      workloadName(m.workload) + " · 정책 " +
      (m.profile?.name || "미선택") + " · 대상 " + (target ? candidateLabel(target) : "자동 선택")
    );
  }
  function steps(m) {
    const accepted = m.preview?.accepted === true;
    const list = el("ol", null, "wizard-steps");
    list.setAttribute("aria-label", "새 워크로드 단계");
    [
      ["작업 선택", Boolean(m.workload)],
      ["정책 · 대상", Boolean(m.workload && m.profile)],
      ["검토", accepted],
      ["제출", false],
    ].forEach(([label, done], i, all) => {
      const item = add(el("li", null, done ? "done" : ""), el("span", String(i + 1)), el("strong", label));
      if (!done && (i === 0 || all[i - 1][1])) item.setAttribute("aria-current", "step");
      list.append(item);
    });
    return list;
  }
  function wizardCards(m) {
    const root = el("section", null, "wizard-section");
    root.append(el("h2", "1. 작업 선택"));
    const tools = el("div", null, "catalog-toolbar"),
      filters = el("div", null, "segmented");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "작업 유형");
    const options = [["all", "전체"], ...typeFilters];
    if (m.items.some((w) => w.template_ref)) options.push(["template", "저장한 템플릿"]);
    for (const [value, label] of options) {
      if (value !== "all" && !m.items.some((w) => matchesType(w, value))) continue;
      const b = el("button", label, catalogFilter === value ? "selected" : "");
      b.type = "button";
      b.setAttribute("aria-pressed", String(catalogFilter === value));
      b.onclick = () => {
        catalogFilter = value;
        c.render();
      };
      filters.append(b);
    }
    const search = el("input");
    search.id = "workload-search";
    search.type = "search";
    search.placeholder = "작업 이름으로 검색";
    search.setAttribute("aria-label", "등록된 작업 검색");
    search.value = catalogSearch;
    search.oninput = () => {
      catalogSearch = search.value;
      c.render();
    };
    tools.append(filters, search);
    root.append(tools);
    if (!m.catalogConfigured && !m.showAll)
      root.append(
        el(
          "p",
          "운영자가 고른 제출 카탈로그가 설정되지 않아 프로젝트에 등록된 실행 환경을 그대로 보여줘요.",
          "wizard-note",
        ),
      );
    root.append(
      el(
        "p",
        "‘제출 조건 통과’는 등록·호환·검증 시효를 확인한 수예요. 장치가 지금 비어 있거나 정상 동작한다는 뜻은 아니에요.",
        "wizard-note",
      ),
    );
    const cards = el("div", null, "wizard-cards");
    for (const w of m.items.filter(
      (w) =>
        matchesType(w, catalogFilter) &&
        (title(w) + w.workload_ref).toLowerCase().includes(catalogSearch.toLowerCase()),
    )) {
      const chosen = c.selectionKey(w) === c.draft.workload,
        candidates = w.candidates || [],
        ready = candidates.filter(c.canSubmit).length;
      const b = el("button", null, "wizard-card" + (chosen ? " chosen" : ""));
      b.type = "button";
      b.setAttribute("aria-pressed", String(chosen));
      b.onclick = () => {
        c.updateDraft({ draft: { workload: c.selectionKey(w), candidate: "" } });
        focusSetup();
      };
      const chips = el("ul", null, "candidate-chips");
      for (const x of candidates)
        chips.append(el("li", candidateLabel(x), c.canSubmit(x) ? "" : "blocked"));
      b.append(
        add(
          el("div", null, "workload-card-top"),
          badge(w.template_ref ? "저장한 템플릿" : names[w.task_type] || w.task_type),
          el(
            "small",
            candidates.length
              ? "등록 후보 " + candidates.length + "개 · 제출 조건 통과 " + ready + "개"
              : "등록 후보 없음",
          ),
        ),
        el("h3", title(w)),
        el("code", w.workload_ref),
        chips,
      );
      cards.append(b);
    }
    root.append(
      cards.childElementCount
        ? cards
        : c.empty(
            m.items.length
              ? "검색 조건에 맞는 작업이 없어요."
              : m.catalogConfigured
                ? "제출 카탈로그가 비어 있어요. 운영자가 검증한 작업을 카탈로그에 등록하면 여기에 나타나요."
                : "프로젝트에 등록된 실행 환경이 없어요.",
          ),
    );
    return root;
  }
  function policyAndTarget(m) {
    const root = el("section", null, "wizard-section");
    root.id = "wizard-step-2";
    root.tabIndex = -1;
    root.dataset.keepFocus = "true";
    root.append(el("h2", "2. 실행 정책과 대상"));
    if (!m.workload) {
      root.append(el("p", "먼저 실행할 작업을 선택하세요.", "wizard-note"));
      return root;
    }
    if (!m.profiles.length) {
      root.append(c.empty("운영자가 스케줄링 정책을 등록해야 제출할 수 있어요."));
      return root;
    }
    const policy = el("select");
    policy.id = "scheduling-profile";
    for (const p of m.profiles) policy.append(new Option(p.name, p.ref));
    policy.value = m.profile?.ref || "";
    policy.onchange = () => c.updateDraft({ profile: policy.value });
    const label = el("label", "실행 정책");
    label.htmlFor = policy.id;
    root.append(label, policy);
    const p = m.profile?.policy;
    if (p)
      root.append(
        add(
          el("dl", null, "policy-facts"),
          el("dt", "백엔드 순서"),
          el("dd", p.backend_order.map((x) => backendNames[x] || x).join(" → ")),
          el("dt", "최대 자원"),
          el("dd", resourceText(p.max_resources)),
          el("dt", "시간 제한"),
          el("dd", "실행 " + p.max_run_seconds + "초 · 대기 " + p.max_queue_seconds + "초"),
        ),
      );
    const set = el("fieldset", null, "target-options");
    set.append(el("legend", "실행 대상"));
    const option = (ref, heading, lines, disabled) => {
      const input = el("input");
      input.type = "radio";
      input.name = "wizard-target";
      input.id = "target-" + (ref || "auto");
      input.value = ref;
      input.checked = m.candidate === ref;
      input.disabled = disabled;
      input.dataset.keepFocus = "true";
      input.onchange = () => c.updateDraft({ draft: { candidate: ref } });
      const l = add(el("label", null, "target-option" + (disabled ? " blocked" : "")), input,
        add(el("span"), el("strong", heading), ...lines.map((t) => el("small", t))));
      return l;
    };
    set.append(
      option("", "자동 선택 (권장)", ["접수할 때 호환 후보 중 관측된 여유와 정책 순서로 다시 선택해요."], false),
    );
    for (const x of m.workload.candidates)
      set.append(
        option(
          x.candidate_ref,
          candidateLabel(x),
          [
            (x.node_ref || "노드 미확인") + " · " + resourceText(x.resources, x.device_class),
            ...(c.canSubmit(x) ? x.submission_warnings || [] : x.submission_reasons || []).map(c.reasonText),
          ],
          !c.canSubmit(x),
        ),
      );
    root.append(set);
    const jct = el("a", "후보별 JCT 근거·정책 비교 보기", "action-link");
    jct.href = "#jct";
    jct.onclick = () => c.openJct(m.workload.workload_ref);
    root.append(jct);
    return root;
  }
  function fact(list, label, value) {
    list.append(el("dt", label), el("dd", value));
  }
  function review(m) {
    const root = el("aside", null, "wizard-review");
    root.id = "wizard-review";
    root.setAttribute("aria-live", "polite");
    root.append(el("h2", "3. 검토"));
    const submit = el("div", null, "wizard-submit");
    submit.id = "wizard-submit";
    const blocked = (text) => {
      const b = el("button", text);
      b.type = "button";
      b.disabled = true;
      return b;
    };
    if (!m.workload) {
      root.append(el("p", "작업을 선택하면 정책 검토 결과가 여기에 나타나요.", "wizard-note"));
    } else if (!m.profiles.length) {
      root.append(el("p", "등록된 스케줄링 정책이 없어 검토할 수 없어요.", "wizard-note"));
    } else {
      const plan = m.preview?.plan;
      const status = el("div", null, "review-status");
      if (m.pending) status.append(badge("검토 중…"), el("p", "실행 후보와 정책을 서버에서 확인하고 있어요. 자원은 예약하지 않아요."));
      else if (m.error) {
        const retry = el("button", "다시 검토");
        retry.type = "button";
        retry.onclick = () => c.preview(m.workload);
        status.append(badge("검토 실패", "bad"), el("p", m.error, "error-message"), retry);
      } else if (m.preview && !m.preview.accepted)
        status.append(
          badge("실행할 수 없어요", "bad"),
          el("p", (m.preview.reasons || []).map(c.reasonText).join(" · ")),
          el("small", "선택한 작업과 정책은 그대로 유지돼요. 정책이나 대상을 바꿔 다시 검토하세요."),
        );
      else if (plan)
        status.append(badge("검토 완료", "good"), el("small", "여유 관측은 예약 보장이 아니에요. 자원이 사용 중이면 큐에서 기다려요."));
      else status.append(badge("검토 대기"));
      root.append(status);
      const facts = el("dl", null, "review-facts");
      fact(facts, "프로젝트", c.data.project_ref);
      fact(facts, "작업", workloadName(m.workload));
      fact(facts, "정책", m.profile ? m.profile.name + " · v" + m.profile.version : "미선택");
      if (plan) {
        const chosen = (plan.evaluation || []).find((x) => x.selected);
        const a = plan.adapter || {};
        fact(facts, "백엔드 · 큐", plan.backend === "kubernetes"
          ? "Kubernetes · " + a.namespace + " / " + a.local_queue
          : "Slurm · " + a.partition + " · " + a.account + " · QOS " + a.qos);
        fact(facts, m.candidate ? "실행 대상" : "현재 예상 대상",
          plan.node_ref + " · " + (chosen?.model || plan.candidate_ref) + (m.candidate ? "" : " (접수 시 다시 선택)"));
        const target = m.workload.candidates.find((x) => x.candidate_ref === plan.candidate_ref);
        fact(facts, "요청 자원 (등록값)", resourceText(plan.resources, target?.device_class));
        fact(facts, "시간 제한", "실행 최대 " + plan.execution.max_run_seconds + "초 · 대기 최대 " + plan.execution.max_queue_seconds + "초");
        fact(facts, "우선순위", plan.execution.priority === "high" ? "높음" : "보통");
      }
      root.append(facts);
      root.append(el("small", "자원 요청·명령은 등록된 실행 환경에 고정되어 있어 이 화면에서 바꿀 수 없어요.", "wizard-note"));
      const evaluation = plan || (m.preview && { evaluation: m.preview.evaluation });
      if (evaluation?.evaluation?.length)
        root.append(add(el("details", null, "review-candidates"), el("summary", "후보별 판단 이유"), c.candidateEvaluation(evaluation)));
    }
    root.append(el("h2", "4. 제출"));
    if (m.workload && m.preview?.accepted && !m.pending) submit.append(c.draftSubmitButton(m), c.kubeflowSubmitOption(m));
    else submit.append(blocked(m.workload ? "검토가 끝나면 제출할 수 있어요" : "작업을 먼저 선택하세요"));
    root.append(submit);
    return root;
  }
  function submission() {
    const m = c.submissionModel();
    // A selected draft without a matching review asks the server once; errors wait for an explicit retry.
    if (m.workload && m.profiles.length && !m.preview && !m.pending && !m.error)
      setTimeout(() => {
        const now = c.submissionModel();
        if (now.workload && !now.preview && !now.pending && !now.error) c.preview(now.workload);
      }, 0);
    const root = el("div", null, "workload-wizard");
    const clusters = [
      ...new Set(m.items.flatMap((w) => (w.candidates || []).map((x) => x.cluster_ref)).filter(Boolean)),
    ];
    root.append(
      add(
        el("div", null, "wizard-context"),
        add(
          el("div"),
          el("small", "프로젝트"),
          el("strong", c.data.project_ref),
          badge("API 연결됨", "good"),
        ),
        clusters.length ? add(el("div"), el("small", "등록된 클러스터"), el("span", clusters.join(", "))) : null,
        actionLink("이기종 자원 조합 검토", "#placement"),
        actionLink("워크로드 목록", "#jobs"),
      ),
      steps(m),
      add(
        el("div", null, "wizard-grid"),
        add(el("div", null, "wizard-main"), wizardCards(m), policyAndTarget(m)),
        review(m),
      ),
      add(
        el("details", null, "workspace-panel advanced-tools"),
        el("summary", "고급 · 템플릿 등록과 실행 환경"),
        c.submissionTools(),
      ),
    );
    return root;
  }
  async function refreshPools(force = false) {
    if (poolsPending || (!force && Date.now() - poolsAt < 15000)) return;
    poolsPending = true;
    const session = c.generation(),
      serial = ++poolRequest;
    poolError = "";
    try {
      const response = await fetch(
        c.API + "/inference-pools?page=" + poolPage,
        { headers: c.authHeaders(), cache: "no-store" },
      );
      if (!response.ok)
        throw new Error(
          "추론 서비스를 확인할 수 없어요. 프로젝트 연결을 확인해 주세요.",
        );
      const value = await response.json();
      if (session !== c.generation() || serial !== poolRequest) return;
      pools = value.items;
      poolHasNext = value.has_next;
      poolsAt = Date.now();
      if (!pools.some((p) => p.ref === poolRef)) {
        poolRef = pools.find((p) => p.configured)?.ref || "";
        prediction = null;
      }
      await refreshPool(session, serial);
    } catch (error) {
      if (session === c.generation() && serial === poolRequest) {
        poolError = error.message;
        poolHealth = null;
        poolsAt = Date.now();
      }
    } finally {
      if (session === c.generation() && serial === poolRequest) {
        poolsPending = false;
        c.render();
      }
    }
  }
  async function refreshPool(session = c.generation(), serial = poolRequest) {
    if (!poolRef) {
      poolHealth = null;
      return;
    }
    const selected = poolRef;
    try {
      const response = await fetch(
        c.API + "/inference-pools/" + encodeURIComponent(selected),
        { headers: c.authHeaders(), cache: "no-store" },
      );
      if (!response.ok)
        throw new Error(
          "선택한 서비스가 준비되지 않았어요. 잠시 후 다시 확인해 주세요.",
        );
      const result = await response.json();
      if (
        session === c.generation() &&
        serial === poolRequest &&
        selected === poolRef
      ) {
        poolHealth = result;
        poolError = "";
      }
    } catch (error) {
      if (
        session === c.generation() &&
        serial === poolRequest &&
        selected === poolRef
      ) {
        poolHealth = null;
        poolError = error.message;
      }
    }
  }
  async function readImage(file) {
    const session = c.generation(),
      serial = ++imageRequest;
    inferenceAbort?.abort();
    prediction = null;
    inferenceError = "";
    imagePixels = null;
    if (imageURL) {
      URL.revokeObjectURL(imageURL);
      imageURL = "";
    }
    imageName = "";
    if (!file) {
      c.render();
      return;
    }
    if (
      !["image/jpeg", "image/png", "image/webp"].includes(file.type) ||
      file.size > 8 * 1024 * 1024
    ) {
      inferenceError = "8MB 이하의 JPG·PNG·WebP 이미지를 선택해 주세요.";
      c.render();
      return;
    }
    imagePending = true;
    c.render();
    let bitmap;
    try {
      const dimensions = imageDimensions(
        await file.slice(0, 512 * 1024).arrayBuffer(),
        file.type,
      );
      if (dimensions.width * dimensions.height > 25_000_000)
        throw new Error(
          "이미지 크기가 너무 커요. 2,500만 화소 이하로 줄여 주세요.",
        );
      if (session !== c.generation() || serial !== imageRequest) return;
      bitmap = await createImageBitmap(file);
      if (bitmap.width * bitmap.height > 25_000_000)
        throw new Error(
          "이미지 크기가 너무 커요. 2,500만 화소 이하로 줄여 주세요.",
        );
      if (session !== c.generation() || serial !== imageRequest) return;
      const canvas = document.createElement("canvas");
      canvas.width = 224;
      canvas.height = 224;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, 224, 224);
      ctx.drawImage(bitmap, 0, 0, 224, 224);
      const rgba = ctx.getImageData(0, 0, 224, 224).data,
        pixels = new Array(224 * 224 * 3);
      for (let i = 0, j = 0; i < rgba.length; i += 4) {
        pixels[j++] = rgba[i];
        pixels[j++] = rgba[i + 1];
        pixels[j++] = rgba[i + 2];
      }
      imagePixels = pixels;
      imageURL = URL.createObjectURL(file);
      imageName = file.name;
    } catch (error) {
      if (session === c.generation() && serial === imageRequest)
        inferenceError = error.message.startsWith("이미지 크기가 너무 커요")
          ? error.message
          : "이미지를 읽지 못했어요. 다른 이미지로 시도해 주세요.";
    } finally {
      bitmap?.close();
      if (session === c.generation() && serial === imageRequest) {
        imagePending = false;
        c.render();
      }
    }
  }
  async function predict() {
    if (predictPending || !imagePixels || !poolRef) return;
    const session = c.generation(),
      serial = imageRequest,
      selected = poolRef;
    predictPending = true;
    prediction = null;
    inferenceError = "";
    inferenceAbort = new AbortController();
    const abort = inferenceAbort;
    const timeout = setTimeout(() => abort.abort(), 15000);
    c.render();
    try {
      const response = await fetch(
        c.API + "/inference-pools/" + encodeURIComponent(selected) + ":predict",
        {
          method: "POST",
          headers: { ...c.authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ instances: [imagePixels], deadline_ms: 5000 }),
          signal: abort.signal,
        },
      );
      const value = await response.json();
      if (!response.ok)
        throw new Error(
          response.status === 429
            ? "지금 요청이 많아요. 잠시 후 다시 실행해 주세요."
            : response.status === 503
              ? "준비된 장치가 없거나 응답 시간이 초과됐어요. 잠시 후 다시 실행해 주세요."
              : "예측하지 못했어요. 입력과 서비스 상태를 확인해 주세요.",
        );
      if (
        !Array.isArray(value.predictions?.[0]) ||
        value.predictions[0].length !== 1000 ||
        !value.predictions[0].every(Number.isFinite)
      )
        throw new Error("결과 형식을 확인할 수 없어요.");
      if (
        session !== c.generation() ||
        serial !== imageRequest ||
        selected !== poolRef
      )
        return;
      prediction = value;
      if (!labels)
        try {
          const res = await fetch("/console/imagenet-labels.json", {
            signal: abort.signal,
          });
          if (res.ok) labels = await res.json();
        } catch (_) {
          /* Class indexes remain meaningful without display labels. */
        }
    } catch (error) {
      if (
        session === c.generation() &&
        serial === imageRequest &&
        selected === poolRef
      )
        inferenceError =
          error.name === "AbortError"
            ? "응답을 기다리는 시간이 초과됐어요. 다시 실행해 주세요."
            : error.message;
    } finally {
      clearTimeout(timeout);
      if (session === c.generation()) {
        predictPending = false;
        inferenceAbort = null;
        if (serial === imageRequest && selected === poolRef)
          void refreshPool().then(() => c.render());
        c.render();
      }
    }
  }
  function inference() {
    if (!poolsPending && Date.now() - poolsAt >= 15000) void refreshPools();
    const root = el("div", null, "inference-workspace"),
      select = el("select");
    select.id = "inference-pool";
    select.append(
      new Option(pools.length ? "서비스 선택" : "등록된 서비스 없음", ""),
    );
    for (const pool of pools) {
      const option = new Option(
        pool.model_ref +
          " · " +
          pool.runtimes
            .map((x) => (x === "cuda" ? "GPU" : "Hailo NPU"))
            .join(" / "),
        pool.ref,
      );
      option.disabled = !pool.configured;
      select.append(option);
    }
    select.value = poolRef;
    select.disabled = predictPending || imagePending;
    select.onchange = () => {
      poolRef = select.value;
      poolHealth = null;
      prediction = null;
      poolError = "";
      void refreshPool().then(() => c.render());
      c.render();
    };
    const ready = poolHealth?.endpoints?.some((x) => x.ready) === true,
      fresh = Date.now() - poolsAt < 30000;
    const routeRow = el("div", null, "route-readiness");
    for (const endpoint of poolHealth?.endpoints || [])
      routeRow.append(
        badge(
          (endpoint.runtime === "cuda" ? "GPU" : "Hailo NPU") +
            " · " +
            (fresh && endpoint.ready ? "요청 가능" : "확인 필요"),
          fresh && endpoint.ready ? "good" : "warn",
        ),
      );
    const config = add(
      el("div", null, "inference-config"),
      el("label", "이미지 분류 서비스"),
      select,
      routeRow,
    );
    config.querySelector("label").htmlFor = select.id;
    const refresh = el(
      "button",
      poolsPending ? "확인 중…" : "서비스 다시 확인",
    );
    refresh.disabled = poolsPending;
    refresh.onclick = () => void refreshPools(true);
    if (poolHasNext || poolPage) {
      const paging = el("div", null, "pager");
      for (const [label, offset, enabled] of [
        ["이전 서비스", -1, poolPage > 0],
        ["다음 서비스", 1, poolHasNext],
      ]) {
        const b = el("button", label);
        b.disabled = !enabled || poolsPending;
        b.onclick = () => {
          poolPage += offset;
          poolsAt = 0;
          void refreshPools(true);
        };
        paging.append(b);
      }
      config.append(paging);
    }
    root.append(
      section(
        "이미지 한 장으로 확인해보세요",
        "같은 ResNet50 모델을 GPU 또는 Hailo NPU에서 실행하고, 실제 선택된 장치를 확인해요.",
        config,
        refresh,
      ),
    );
    if (poolError) root.append(el("p", poolError, "error-message"));
    if (!pools.length && !poolsPending && !poolError)
      root.append(
        c.empty(
          "아직 사용할 수 있는 추론 서비스가 없어요. 운영자가 검증된 서비스를 등록하면 여기에서 선택할 수 있어요.",
        ),
      );
    const file = el("input");
    file.id = "inference-image";
    file.type = "file";
    file.accept = "image/jpeg,image/png,image/webp";
    file.disabled = predictPending || imagePending;
    file.onchange = () => void readImage(file.files[0]);
    const label = el("label", "이미지 선택");
    label.htmlFor = file.id;
    const input = el("div", null, "inference-input");
    input.append(el("h2", "입력"));
    if (imageURL) {
      const img = el("img", null, "image-preview");
      img.src = imageURL;
      img.alt = "선택한 이미지 미리보기";
      input.append(img, el("p", imageName, "image-filename"));
    } else
      input.append(
        add(
          el("div", null, "upload-guide"),
          icon("image"),
          el(
            "strong",
            imagePending ? "이미지를 읽는 중…" : "분류할 이미지를 골라주세요",
          ),
          el("p", "JPG·PNG·WebP, 최대 8MB"),
        ),
      );
    input.append(
      label,
      file,
      el(
        "small",
        "이미지를 224×224 RGB로 맞춘 뒤 예측해요. 입력 이미지와 결과는 저장하지 않아요.",
      ),
    );
    const run = add(
      el("button", null, "primary inference-run"),
      icon("play"),
      document.createTextNode(
        predictPending ? "이미지를 분석하는 중…" : "이미지 분석하기",
      ),
    );
    run.disabled =
      !imagePixels || !ready || !fresh || predictPending || imagePending;
    run.onclick = () => void predict();
    input.append(run);
    const output = el("div", null, "inference-output");
    output.setAttribute("aria-live", "polite");
    output.append(el("h2", "결과"));
    if (inferenceError) output.append(el("p", inferenceError, "error-message"));
    if (prediction) {
      const scores = prediction.predictions[0]
          .map((score, index) => ({ score, index }))
          .sort((a, b) => b.score - a.score)
          .slice(0, 5),
        route = prediction.routing;
      output.append(
        badge("분석 완료", "good"),
        el("p", "모델이 가장 가깝다고 예측한 분류", "muted"),
        el(
          "h3",
          labels?.[scores[0].index] || "분류 " + scores[0].index,
          "prediction-class",
        ),
      );
      output.append(
        add(
          el("div", null, "prediction-facts"),
          add(
            el("div"),
            el("small", "처리 장치"),
            el("strong", route.runtime === "cuda" ? "GPU" : "Hailo NPU"),
          ),
          add(
            el("div"),
            el("small", "라우터 처리 시간"),
            el("strong", route.duration_ms.toFixed(1) + " ms"),
          ),
          add(
            el("div"),
            el("small", "경로 전환"),
            el(
              "strong",
              route.fallback ? "다른 장치로 재시도" : "첫 선택 장치",
            ),
          ),
        ),
      );
      output.append(
        table(
          ["상위 분류", "모델 점수"],
          scores.map((x) => [
            labels?.[x.index] || "분류 " + x.index,
            x.score.toFixed(4),
          ]),
        ),
        el(
          "small",
          "점수는 정답일 확률을 보장하지 않아요. 분류 이름은 ImageNet의 영어 이름을 사용해요.",
        ),
        c.details("실행 경로 자세히 보기", {
          model: prediction.model,
          routing: route,
        }),
      );
    } else
      output.append(
        add(
          el("div", null, "result-empty"),
          icon(predictPending ? "clock" : "layers"),
          el(
            "strong",
            predictPending
              ? "준비된 장치에서 분석하고 있어요"
              : "결과가 여기에 나타나요",
          ),
          el(
            "p",
            predictPending
              ? "장치 선택·추론·응답을 기다리는 중입니다."
              : "왼쪽에서 이미지를 선택하고 분석하기를 눌러주세요.",
          ),
        ),
      );
    root.append(add(el("div", null, "inference-split"), input, output));
    return root;
  }
  function jobDetail(job, legacy, scope = "dialog") {
    const inline = scope === "inline",
      prefix = inline ? "inline-job-tab-" : "job-tab-";
    if (inline && inlineJobId !== job.job_id) {
      inlineJobId = job.job_id;
      inlineTab = "summary";
    }
    const root = el("div", null, "job-workspace-detail"),
      head = add(
        el("div", null, "job-detail-heading"),
        el("h3", job.template?.name || title(job)),
        add(
          el("div", null, "state-line"),
          stateBadge(job.state),
          badge(job.backend),
        ),
        el(
          "p",
          job.attention?.title ||
            (terminal.has(job.state)
              ? "작업이 종료됐어요. 결과와 실행 기록을 확인하세요."
              : "실제 백엔드에서 관측한 상태입니다."),
        ),
      );
    root.append(head);
    const tabs = el("div", null, "detail-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "작업 상세 항목");
    const body = el("div", null, "detail-tab-body");
    body.id = prefix + "panel";
    body.setAttribute("role", "tabpanel");
    const parts = {};
    parts.summary = add(
      el("div"),
      el(
        "p",
        job.attention?.action || "아래 실행 기록과 결과를 확인할 수 있어요.",
      ),
      c.schedulingProgress(job),
      c.liveExecution(job),
      add(
        el("div", null, "job-facts"),
        add(
          el("div"),
          el("small", "실행 장치"),
          el("strong", job.node_ref || "아직 선택 전"),
        ),
        add(
          el("div"),
          el("small", "접수 시각"),
          el("strong", stamp(job.created_at)),
        ),
        add(
          el("div"),
          el("small", "종료 시각"),
          el("strong", stamp(job.finished_at)),
        ),
      ),
    );
    const result = job.result?.measurements;
    const actions = el("div", null, "job-actions");
    const cancel = c.cancelButton(job);
    if (cancel) actions.append(cancel);
    if (["FAILED", "CANCELED", "RESULT_INVALID"].includes(job.state))
      actions.append(c.retryJobButton(job));
    if (job.tracking?.run_id)
      actions.append(
        c.researchButton("실험 기록 보기", () =>
          c.openExperiment(job.tracking.run_id),
        ),
      );
    parts.summary.append(actions);
    const outcome = result
      ? add(
          el("div"),
          c.dashboardStats([
            ["실행 시간", duration(result.elapsed_seconds), "측정된 실행 구간"],
            [
              "p95 지연",
              result.latency_p95_ms == null
                ? "미측정"
                : c.fmt(result.latency_p95_ms) + " ms",
              "측정 기록",
            ],
            [
              "처리량",
              typeof result.throughput === "number"
                ? c.fmt(result.throughput)
                : (result.throughput ?? "미측정"),
              "측정 기록",
            ],
          ]),
          c.details("결과 전체", job.result),
        )
      : c.empty(
          "아직 수집된 결과가 없어요. 완료 상태와 결과 수집 상태는 별도로 확인해 주세요.",
        );
    parts.result = add(
      el("div"),
      outcome,
      job.scheduling_plan ? c.recommendationPanel(job) : null,
    );
    const events = job.lifecycle_events || [];
    parts.events = events.length
      ? table(
          ["관측 시각", "상태", "이유"],
          events.map((e) => [
            stamp(e.observed_at),
            stateBadge(e.state),
            e.reason ? c.reasonText(e.reason) : "—",
          ]),
        )
      : table(
          ["서버 기록", "시각"],
          [
            ["작업 접수", stamp(job.created_at)],
            ["큐 제출", stamp(job.queued_at)],
            ["실행 시작", stamp(job.started_at)],
            ["작업 종료", stamp(job.finished_at)],
          ],
        );
    parts.technical = legacy;
    function select(key, button) {
      if (inline) inlineTab = key;
      body.replaceChildren(parts[key]);
      body.setAttribute("aria-labelledby", button.id);
      for (const child of tabs.children) {
        child.setAttribute("aria-selected", String(child === button));
        child.tabIndex = child === button ? 0 : -1;
      }
    }
    for (const [key, label] of [
      ["summary", "요약"],
      ["result", "결과"],
      ["events", "실행 기록"],
      ["technical", "자원·전체 기록"],
    ]) {
      const b = el("button", label);
      b.id = prefix + key;
      b.dataset.tab = key;
      if (inline) b.dataset.keepFocus = "true";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", body.id);
      b.onclick = () => select(key, b);
      b.onkeydown = (e) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        e.preventDefault();
        const all = [...tabs.children],
          i = all.indexOf(b);
        const next =
          e.key === "Home"
            ? all[0]
            : e.key === "End"
              ? all.at(-1)
              : all[
                  (i + (e.key === "ArrowRight" ? 1 : -1) + all.length) %
                    all.length
                ];
        next.click();
        next.focus();
      };
      tabs.append(b);
    }
    const initial = inline ? inlineTab : "summary";
    select(initial, [...tabs.children].find((b) => b.dataset.tab === initial) || tabs.firstChild);
    root.append(tabs, body);
    return root;
  }
  function reset() {
    menuOpen = false;
    inlineJobId = null;
    inlineTab = "summary";
    catalogFilter = "all";
    catalogSearch = "";
    poolRequest++;
    imageRequest++;
    inferenceAbort?.abort();
    inferenceAbort = null;
    if (imageURL) URL.revokeObjectURL(imageURL);
    pools = [];
    poolPage = 0;
    poolHasNext = false;
    poolRef = "";
    poolHealth = null;
    poolsAt = 0;
    poolsPending = false;
    poolError = "";
    imageURL = "";
    imageName = "";
    imagePixels = null;
    imagePending = false;
    predictPending = false;
    prediction = null;
    inferenceError = "";
  }
  return {
    home,
    submission,
    newWorkloadMenu,
    draftSummary,
    inference,
    navigation,
    jobDetail,
    reset,
    refreshPools,
  };
}

/** Heterogeneous placement preview (#placement): registered workloads per stage ->
 * registered GPU/NPU candidates -> measured topology -> POST /placement-plans.
 * Read-only: it never creates a Job, reservation or training run. */
export function createPlacementConsole(c) {
  const { el, add, badge, table, stamp, empty, details } = c;
  const MIB = 1048576;
  const units = { gbit: ["Gbit/s", 1e9 / 8], mib: ["MiB/s", MIB] };
  const classNames = { gpu: "GPU", npu: "NPU", cpu: "CPU" };
  const availability = {
    available: ["관측 여유 있음 (예약 아님)", ""],
    busy: ["자원 대기 예상", "warn"],
    unknown: ["현재 여유 미확인", "warn"],
    unavailable: ["사용 불가", "muted"],
  };
  const admission = {
    observed_headroom_recheck_at_submission: "관측 여유 기준 · 제출 시 다시 확인",
    wait_for_resources: "자원 대기 예상 · 제출 시 다시 확인",
    unknown: "자원 여유 미확인 · 제출 시 다시 확인",
  };
  const codes = {
    NO_ELIGIBLE_CANDIDATE: "정책·호환성을 통과한 후보 없음",
    NO_FEASIBLE_COMBINATION: "조건을 모두 만족하는 조합 없음",
    CANDIDATE_UNAVAILABLE: "후보 사용 불가 관측",
    CANDIDATE_CONTRACT_UNAVAILABLE: "등록된 실행 계약을 확인할 수 없음",
    SAME_NODE_UNMET: "같은 노드 요구 미충족",
    BANDWIDTH_BELOW_MINIMUM: "측정 대역폭이 최소 요구보다 낮음",
    OVERHEAD_ABOVE_MAXIMUM: "측정 요청 지연이 최대 허용보다 큼",
    HARD_LINK_MISSING: "필수 요구 링크 미측정",
    HARD_LINK_EXPIRED: "필수 요구 링크 측정 만료",
    HARD_LINK_ENDPOINT_UNKNOWN: "필수 요구 장치가 토폴로지에 없음",
    LINK_MISSING: "링크 미측정",
    LINK_EXPIRED: "링크 측정 만료",
    LINK_ENDPOINT_UNKNOWN: "장치가 토폴로지에 없음",
    AVAILABILITY_UNKNOWN: "현재 여유 미확인",
    SHARED_CAPACITY_EXCEEDED: "같은 호스트 합산 요구가 관측 여유 초과",
    SHARED_CAPACITY_UNKNOWN: "같은 호스트 합산 여유 미확인",
    VIRTUAL_SLOT_NOT_PHYSICAL_DEVICE: "공유 슬롯은 물리 장치로 계산하지 않음",
    PHYSICAL_DEVICE_IDENTITY_UNKNOWN: "물리 장치 식별 정보 없음",
    PHYSICAL_DEVICE_IDENTITY_EXPIRED: "스냅샷 만료로 물리 장치 식별 근거 만료",
    TOPOLOGY_SNAPSHOT_EXPIRED: "토폴로지 스냅샷 만료",
    DISTINCT_PHYSICAL_DEVICE_INSUFFICIENT: "서로 다른 물리 장치 부족",
  };
  const linkStates = {
    measured: ["측정됨", "verified"],
    expired: ["만료", "warn"],
    missing: ["미측정", "warn"],
    endpoint_unknown: ["토폴로지에 없음", "warn"],
  };
  let stageSerial = 0;
  const newStage = () => ({ id: "stage-" + ++stageSerial, workload: "", excluded: [], transfer: newTransfer() });
  const newTransfer = () => ({ mib: "", count: "1", requirement: "soft", sameNode: false, bandwidth: "", unit: "gbit", overhead: "" });
  const draft = { taskRef: "", profile: "", snapshot: "", stages: [] };
  let snapshots = { state: "idle", items: [], message: "", at: 0 };
  let plan = null, invalidated = null, failure = null, pendingKey = "", serial = 0, snapshotSerial = 0;

  function catalog() {
    const d = c.data;
    return Array.isArray(d?.submission_catalog) ? d.submission_catalog : d?.compatibility?.items || [];
  }
  function stageName(id) {
    const index = draft.stages.findIndex((s) => s.id === id);
    return index < 0 ? id : `단계 ${index + 1}`;
  }
  function reason(raw) {
    const parts = String(raw).split(":");
    const at = parts.findIndex((p) => /^[A-Z][A-Z0-9_]+$/.test(p));
    if (at < 0) return c.reasonText(raw);
    const scope = parts.slice(0, at).join(":"), extra = parts.slice(at + 1).join(":");
    const where = scope.includes("->")
      ? scope.split("->").map(stageName).join(" → ")
      : draft.stages.some((s) => s.id === scope) ? stageName(scope) : scope;
    const text = codes[parts[at]] || c.reasonText(parts[at]);
    return (where ? where + " · " : "") + text + (extra ? ` (${extra})` : "");
  }
  function reasonList(list) {
    const box = el("ul", null, "placement-reasons");
    for (const r of list || []) {
      const li = el("li", reason(r));
      li.title = r;
      box.append(li);
    }
    return box;
  }
  const number = (v, digits = 3) => c.fmt(v, digits);
  const bandwidthText = (bps) => `${number((bps * 8) / 1e9)} Gbit/s · ${number(bps / MIB, 1)} MiB/s`;
  function clock() {
    const t = c.now();
    return Number.isFinite(t) ? t : Date.now();
  }
  function snapshotState(s) {
    const t = clock();
    if (!s) return "missing";
    return Date.parse(s.observed_at) <= t && t < Date.parse(s.expires_at) ? "fresh" : "expired";
  }
  const snapshotBadge = (state) =>
    ({ fresh: badge("유효 기간 내", "verified"), expired: badge("만료", "warn"), missing: badge("스냅샷 없음", "warn") })[state];

  function invalidate(why) {
    if (plan) invalidated = { digest: plan.result.digest, why };
    plan = null;
    failure = null;
  }
  function change(mutate, why) {
    mutate();
    invalidate(why);
    c.render();
  }
  function ensureDraft() {
    if (!draft.taskRef) draft.taskRef = "placement-preview-" + c.randomId().slice(0, 12);
    if (!draft.stages.length) draft.stages.push(newStage());
    const profiles = c.data?.scheduling_profiles || [];
    // Same default as the single-workload wizard: normal priority, Kubernetes first.
    if (!profiles.some((p) => p.ref === draft.profile))
      draft.profile = (profiles.find((p) => p.policy.priority === "normal" && p.policy.backend_order[0] === "kubernetes") || profiles[0])?.ref || "";
  }

  async function loadSnapshots() {
    const session = c.generation(), mine = ++snapshotSerial;
    snapshots = { ...snapshots, state: "loading", message: "" };
    try {
      const response = await fetch(c.API + "/topology-snapshots", { headers: c.authHeaders(), cache: "no-store" });
      if (session !== c.generation() || mine !== snapshotSerial) return;
      if (!response.ok) {
        const message = {
          401: "프로젝트 인증이 필요합니다. 토큰을 확인하고 다시 연결하세요.",
          403: "이 프로젝트에서 토폴로지 스냅샷을 볼 권한이 없습니다.",
          404: "이 서버는 이기종 배치 검토 API(GET /topology-snapshots)를 제공하지 않습니다.",
        }[response.status] || `토폴로지 스냅샷을 불러오지 못했습니다 (HTTP ${response.status}).`;
        snapshots = { state: response.status === 404 ? "unsupported" : "error", items: [], message, at: Date.now() };
      } else {
        const body = await response.json();
        if (session !== c.generation() || mine !== snapshotSerial) return;
        const items = [...(body.items || [])].sort((a, b) => Date.parse(b.observed_at) - Date.parse(a.observed_at));
        snapshots = { state: "ready", items, message: "", at: Date.now() };
        if (draft.snapshot && !items.some((s) => s.ref === draft.snapshot)) {
          draft.snapshot = "";
          invalidate("선택한 토폴로지 스냅샷이 더 이상 목록에 없습니다.");
        }
        if (!draft.snapshot) draft.snapshot = (items.find((s) => snapshotState(s) === "fresh") || items[0])?.ref || "";
      }
    } catch (error) {
      if (session !== c.generation() || mine !== snapshotSerial) return;
      snapshots = { ...snapshots, state: "error", message: "토폴로지 API에 연결하지 못했습니다. 입력은 그대로 보존됩니다." };
    }
    c.render();
  }

  function requestBody() {
    const problems = [];
    const roles = draft.stages.map((s, i) => {
      if (!s.workload) problems.push(`단계 ${i + 1}: 등록된 작업을 선택하세요.`);
      const w = catalog().find((x) => x.workload_ref === s.workload);
      const refs = (w?.candidates || []).map((x) => x.candidate_ref).filter((r) => !s.excluded.includes(r));
      if (w && !refs.length) problems.push(`단계 ${i + 1}: 검토할 후보를 하나 이상 남겨 두세요.`);
      if (refs.length > 8) problems.push(`단계 ${i + 1}: 후보는 8개까지 검토할 수 있습니다.`);
      return {
        role_ref: s.id,
        workload_ref: s.workload,
        ...(w && refs.length && refs.length < w.candidates.length ? { candidate_refs: refs } : {}),
      };
    });
    const edges = draft.stages.slice(1).map((s, i) => {
      const t = s.transfer, name = `단계 ${i + 1} → 단계 ${i + 2}`;
      const mib = Number(t.mib), count = Number(t.count);
      if (t.mib === "" || !Number.isFinite(mib) || mib < 0) problems.push(`${name}: 전송량(MiB)을 0 이상으로 입력하세요.`);
      if (!Number.isInteger(count) || count < 1) problems.push(`${name}: 전송 횟수는 1 이상의 정수입니다.`);
      const edge = {
        producer_role: draft.stages[i].id,
        consumer_role: s.id,
        bytes_per_transfer: Math.round(mib * MIB),
        transfer_count: count,
        requirement: t.requirement,
        same_node: t.sameNode,
      };
      if (t.bandwidth !== "") {
        const v = Number(t.bandwidth);
        if (!Number.isFinite(v) || v <= 0) problems.push(`${name}: 최소 대역폭은 0보다 커야 합니다.`);
        else edge.min_bandwidth_bytes_per_second = v * units[t.unit][1];
      }
      if (t.overhead !== "") {
        const v = Number(t.overhead);
        if (!Number.isFinite(v) || v < 0) problems.push(`${name}: 최대 요청 지연은 0 이상입니다.`);
        else edge.max_request_overhead_ms = v;
      }
      return edge;
    });
    if (!draft.profile) problems.push("스케줄링 정책을 선택하세요.");
    if (!draft.snapshot) problems.push("토폴로지 스냅샷을 선택하세요.");
    const body = {
      task_ref: draft.taskRef,
      scheduling_profile_ref: draft.profile,
      topology_snapshot_ref: draft.snapshot,
      roles,
      edges,
      require_distinct_physical_devices: true,
    };
    return { body, key: JSON.stringify([c.data?.project_ref, body]), problems };
  }

  async function submitPlan() {
    const { body, key, problems } = requestBody();
    if (problems.length || pendingKey) return;
    const session = c.generation(), mine = ++serial;
    pendingKey = key;
    failure = null;
    invalidated = null;
    c.render();
    try {
      const response = await fetch(c.API + "/placement-plans", {
        method: "POST",
        headers: { ...c.authHeaders(), "Content-Type": "application/json", "X-Request-ID": c.randomId() },
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      // Only the newest request for the unchanged draft may land.
      if (session !== c.generation() || mine !== serial || key !== requestBody().key) return;
      if (!response.ok) {
        const detail = Array.isArray(result.detail)
          ? result.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join(" / ")
          : result.detail || "";
        const message = {
          401: "프로젝트 인증이 필요합니다. 토큰을 확인하고 다시 연결하세요. 입력은 보존됩니다.",
          403: "이 프로젝트에서 배치 검토 권한이 없습니다.",
          404: detail === "Not Found"
            ? "이 서버는 배치 검토 API(POST /placement-plans)를 제공하지 않습니다."
            : "선택한 작업·정책·스냅샷을 프로젝트에서 찾을 수 없습니다: " + detail,
        }[response.status] || `배치 검토 요청이 거절되었습니다 (HTTP ${response.status}) ${detail}`;
        failure = { message, status: response.status, retry: response.status >= 500 };
      } else {
        plan = { key, result, received: Date.now() };
      }
    } catch (error) {
      if (session !== c.generation() || mine !== serial) return;
      failure = { message: "배치 검토 API에 연결하지 못했습니다. 입력은 그대로 보존됩니다.", retry: true };
    } finally {
      if (session === c.generation() && mine === serial) {
        pendingKey = "";
        c.render();
      }
    }
  }

  function field(labelText, control, hint) {
    const label = el("label", null, "placement-field");
    label.htmlFor = control.id;
    add(label, el("span", labelText));
    const wrap = add(el("div", null, "placement-field-wrap"), label, control);
    if (hint) wrap.append(el("small", hint, "placement-hint"));
    return wrap;
  }
  function input(id, value, onInput, attrs = {}) {
    const n = el("input");
    n.id = id;
    n.value = value;
    n.dataset.keepFocus = "true";
    Object.assign(n, attrs);
    n.oninput = () => onInput(n.value);
    return n;
  }
  function select(id, options, value, onChange) {
    const n = el("select");
    n.id = id;
    n.dataset.keepFocus = "true";
    for (const [v, text, disabled] of options) {
      const o = new Option(text, v);
      o.disabled = Boolean(disabled);
      n.append(o);
    }
    n.value = value;
    n.onchange = () => onChange(n.value);
    return n;
  }

  function contextBar() {
    const profiles = c.data.scheduling_profiles || [];
    const box = el("section", null, "placement-context");
    box.append(
      profiles.length
        ? field(
            "스케줄링 정책",
            select("placement-profile", profiles.map((p) => [p.ref, p.name]), draft.profile, (v) =>
              change(() => (draft.profile = v), "스케줄링 정책이 바뀌었습니다."),
            ),
            "등록된 정책의 백엔드 순서·허용 자원으로 후보를 다시 평가합니다.",
          )
        : empty("운영자가 등록한 스케줄링 정책이 없어 검토할 수 없습니다."),
      snapshotField(),
    );
    return box;
  }
  function snapshotField() {
    const wrap = el("div", null, "placement-field-wrap");
    if (snapshots.state === "loading" || snapshots.state === "idle")
      return add(wrap, el("span", "토폴로지 스냅샷", "placement-field"), el("p", "프로젝트 토폴로지 스냅샷을 불러오는 중…", "placement-hint"));
    if (snapshots.state !== "ready") {
      const retry = el("button", "다시 불러오기");
      retry.type = "button";
      retry.onclick = loadSnapshots;
      return add(wrap, el("span", "토폴로지 스냅샷", "placement-field"), el("p", snapshots.message, "error-message"), retry);
    }
    if (!snapshots.items.length)
      return add(
        wrap,
        el("span", "토폴로지 스냅샷", "placement-field"),
        el("p", "이 프로젝트에 운영자가 등록한 토폴로지 스냅샷이 없습니다. 링크 측정 근거 없이 조합을 검토하지 않습니다.", "placement-warning"),
      );
    const options = snapshots.items.map((s) => [
      s.ref,
      `${s.ref} · ${stamp(s.observed_at)} 측정${snapshotState(s) === "expired" ? " · 만료" : ""}`,
    ]);
    const control = select("placement-snapshot", options, draft.snapshot, (v) =>
      change(() => (draft.snapshot = v), "토폴로지 스냅샷이 바뀌었습니다."),
    );
    const s = snapshots.items.find((x) => x.ref === draft.snapshot);
    const facts = s
      ? add(
          el("dl", null, "placement-facts"),
          el("dt", "상태"), add(el("dd"), snapshotBadge(snapshotState(s))),
          el("dt", "측정"), el("dd", `${stamp(s.observed_at)} · 만료 ${stamp(s.expires_at)}`),
          el("dt", "출처"), el("dd", `${s.source} · ${s.measurement_method}`),
          el("dt", "근거"), add(el("dd"), c.code(s.evidence_ref)),
          el("dt", "범위"), el("dd", `엔드포인트 ${s.endpoints.length}개 · 방향 링크 ${s.links.length}개 (역방향 추정 없음)`),
        )
      : null;
    return add(wrap, field("토폴로지 스냅샷", control), facts);
  }

  function candidateTable(stage, w) {
    if (!w) return el("p", "등록된 작업을 고르면 이 작업에 등록된 GPU·NPU 실행 후보가 보입니다.", "placement-hint");
    const rows = (w.candidates || []).map((x) => {
      const box = el("input");
      box.type = "checkbox";
      box.id = `placement-${stage.id}-${x.candidate_ref}`;
      box.dataset.keepFocus = "true";
      box.checked = !stage.excluded.includes(x.candidate_ref);
      box.onchange = () =>
        change(() => {
          stage.excluded = box.checked
            ? stage.excluded.filter((r) => r !== x.candidate_ref)
            : [...stage.excluded, x.candidate_ref];
        }, "검토 후보가 바뀌었습니다.");
      const label = el("label", null, "placement-device");
      label.htmlFor = box.id;
      add(label, el("strong", x.model || "장치 모델 미기록"), el("small", x.candidate_ref));
      const blocked = x.submission_reasons?.length ? x.submission_reasons : x.reasons || [];
      return [
        add(el("div", null, "placement-pick"), box, label),
        add(el("div"), el("span", `${classNames[x.device_class] || "장치 미분류"} · ${x.backend}`),
          el("small", `${x.cluster_ref || "클러스터 미기록"} / ${x.node_ref || "노드 미기록"}`)),
        add(el("div"), el("span", Object.entries(x.runtime_versions || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "런타임 미기록"),
          el("small", `${x.resource_key || "자원 키 없음"} · ${c.modes[x.allocation_mode] || x.allocation_mode || "단위 미확인"}`)),
        blocked.length
          ? add(el("div"), badge("제외 예상", "muted"), ...blocked.map((r) => el("small", c.reasonText(r), "placement-blocked")))
          : add(el("div"), badge(x.verification === "TRAINING_VERIFIED" ? "학습 검증 등록" : x.verification === "MODEL_VERIFIED" ? "모델 검증 등록" : "등록됨", "")),
      ];
    });
    const unregistered = unregisteredEndpoints(w);
    return add(
      el("div"),
      table(["실행 후보 (실제 장치 모델)", "종류 · 백엔드 · 노드", "런타임 · 자원 키", "등록 호환성"], rows),
      unregistered.length
        ? add(el("div", null, "placement-unregistered"),
            el("strong", "이 작업의 실행 환경 미등록"),
            ...unregistered.map((e) => el("small", `${e.node_ref} · ${e.devices.map((d) => d.resource_key).join(", ")} (토폴로지에는 있으나 이 작업에 등록된 실행 환경 없음)`)))
        : null,
      w.task_type === "training"
        ? el("small", "학습 가능 여부는 등록된 학습 실행 환경으로만 판단하며, 추론 검증으로 NPU 학습 가능을 추정하지 않습니다.", "placement-hint")
        : null,
    );
  }
  function unregisteredEndpoints(w) {
    const s = snapshots.items.find((x) => x.ref === draft.snapshot);
    if (!s) return [];
    const nodes = new Set((w.candidates || []).map((x) => `${x.backend}/${x.cluster_ref}/${x.node_ref}`));
    return s.endpoints.filter((e) => e.devices.length && !nodes.has(`${e.backend}/${e.cluster_ref}/${e.node_ref}`));
  }

  function stageCard(stage, index) {
    const items = catalog();
    const w = items.find((x) => x.workload_ref === stage.workload);
    const options = [["", "등록된 작업을 선택하세요"], ...items.map((x) => [x.workload_ref, `${x.name || x.workload_ref} · ${x.task_type}`])];
    const workload = select(`placement-${stage.id}-workload`, options, stage.workload, (v) =>
      change(() => { stage.workload = v; stage.excluded = []; }, `단계 ${index + 1} 작업이 바뀌었습니다.`),
    );
    const remove = el("button", "단계 삭제", "placement-remove");
    remove.type = "button";
    remove.disabled = draft.stages.length === 1;
    remove.setAttribute("aria-label", `단계 ${index + 1} 삭제`);
    remove.onclick = () => change(() => draft.stages.splice(index, 1), `단계 ${index + 1}이 삭제되었습니다.`);
    return add(
      el("section", null, "placement-stage"),
      add(el("div", null, "placement-stage-head"),
        add(el("h3"), document.createTextNode(`단계 ${index + 1} `), el("code", stage.id)),
        remove),
      field("등록된 작업", workload),
      candidateTable(stage, w),
    );
  }
  function transferCard(stage, index) {
    const t = stage.transfer, id = `placement-${stage.id}-`;
    const set = (key, why = "네트워크 요구가 바뀌었습니다.") => (v) => change(() => (t[key] = v), why);
    const bytes = Number(t.mib);
    const box = add(
      el("fieldset", null, "placement-transfer"),
      el("legend", `단계 ${index} → 단계 ${index + 1} 전송 (생산자 → 소비자 방향)`),
    );
    const grid = el("div", null, "placement-transfer-grid");
    grid.append(
      field("1회 전송량 (MiB)", input(id + "mib", t.mib, set("mib"), { type: "number", min: "0", step: "any", inputMode: "decimal", placeholder: "예: 64" }),
        t.mib !== "" && Number.isFinite(bytes) && bytes >= 0
          ? `= ${Math.round(bytes * MIB).toLocaleString("ko-KR")} bytes (MiB × 1,048,576, 정수 반올림)`
          : "요청에는 bytes 정수로 변환해 보냅니다."),
      field("전송 횟수", input(id + "count", t.count, set("count"), { type: "number", min: "1", step: "1", inputMode: "numeric" })),
      field("요구 강도", select(id + "requirement", [["soft", "선호 (soft) · 근거 있는 후보 우선"], ["hard", "필수 (hard) · 미측정/만료 링크 제외"]], t.requirement, set("requirement"))),
      add(el("div", null, "placement-field-wrap"),
        add(el("label", null, "placement-check"), (() => {
          const n = el("input");
          n.type = "checkbox"; n.id = id + "same-node"; n.dataset.keepFocus = "true"; n.checked = t.sameNode;
          n.onchange = () => set("sameNode")(n.checked);
          return n;
        })(), el("span", "같은 노드에 배치"))),
      add(el("div", null, "placement-field-wrap placement-unit-field"),
        field("최소 대역폭 (선택)", input(id + "bandwidth", t.bandwidth, set("bandwidth"), { type: "number", min: "0", step: "any", inputMode: "decimal", placeholder: "제한 없음" }),
          t.bandwidth !== "" && Number(t.bandwidth) > 0
            ? `= ${(Number(t.bandwidth) * units[t.unit][1]).toLocaleString("ko-KR")} bytes/s`
            : "비우면 대역폭 하한을 두지 않습니다. 기본 측정값을 가정하지 않습니다."),
        field("단위", select(id + "unit", Object.entries(units).map(([k, [name, f]]) => [k, `${name} (×${f.toLocaleString("ko-KR")} B/s)`]), t.unit, set("unit")))),
      field("최대 요청 지연 (ms, 선택)", input(id + "overhead", t.overhead, set("overhead"), { type: "number", min: "0", step: "any", inputMode: "decimal", placeholder: "제한 없음" }),
        "측정된 링크의 요청당 오버헤드와 비교합니다."),
    );
    box.append(grid);
    return box;
  }

  function actionBar() {
    const { problems, key } = requestBody();
    const box = el("section", null, "placement-action");
    const button = el("button", pendingKey ? "배치 계획 확인 중…" : "배치 계획 확인", "primary");
    button.type = "button";
    button.id = "placement-submit";
    button.disabled = Boolean(problems.length || pendingKey || snapshots.state !== "ready");
    button.onclick = submitPlan;
    const add1 = el("button", "단계 추가");
    add1.type = "button";
    add1.id = "placement-add-stage";
    add1.dataset.keepFocus = "true";
    add1.disabled = draft.stages.length >= 4;
    add1.onclick = () => change(() => draft.stages.push(newStage()), "단계가 추가되었습니다.");
    box.append(add(el("div", null, "placement-action-buttons"), add1, button));
    if (problems.length) box.append(add(el("ul", null, "placement-problems"), ...problems.map((p) => el("li", p))));
    if (pendingKey && pendingKey !== key) box.append(el("p", "입력이 바뀌어 진행 중인 응답은 반영하지 않습니다.", "placement-hint"));
    if (invalidated)
      box.append(el("p", `${invalidated.why} 이전 검토 결과(${invalidated.digest?.slice(7, 19) || "—"})는 무효이며 다시 확인해야 합니다.`, "placement-warning"));
    if (failure) {
      const p = add(el("div", null, "placement-failure"), el("p", failure.message, "error-message"));
      if (failure.retry) {
        const retry = el("button", "다시 시도");
        retry.type = "button";
        retry.onclick = submitPlan;
        p.append(retry);
      }
      box.append(p);
    }
    return box;
  }

  function costCell(item) {
    const states = item.transfers.map((t) => t.link.status);
    if (states.includes("expired")) return badge("만료 · 추정 불가", "warn");
    if (states.some((s) => s !== "measured")) return badge("미측정 · 추정 불가", "warn");
    if (item.estimated_communication_ms == null) return badge("추정 불가", "warn");
    return add(el("div"), el("strong", number(item.estimated_communication_ms) + " ms"), el("small", "예상 통신 시간 (추정)"));
  }
  function modelOf(result, role, ref) {
    return result.candidates.find((x) => x.role_ref === role && x.candidate_ref === ref);
  }
  function transferNode(t) {
    const [label, tone] = linkStates[t.link.status] || [t.link.status, "warn"];
    const link = t.link;
    const lines = link.status === "measured" || link.bandwidth_bytes_per_second != null
      ? [
          `측정 대역폭 ${bandwidthText(link.bandwidth_bytes_per_second)}`,
          `측정 요청 지연 ${number(link.request_overhead_ms)} ms`,
          `측정 ${stamp(link.measured_at)} · ${link.evidence_ref}`,
        ]
      : ["대역폭 미측정", "요청 지연 미측정"];
    const estimate = link.status === "measured" && t.estimated_total_ms != null
      ? `예상 통신 ${number(t.estimated_total_ms)} ms (추정 · ${t.transfer_count.toLocaleString("ko-KR")}회 × ${t.bytes_per_transfer.toLocaleString("ko-KR")} bytes)`
      : link.status === "expired" ? "만료된 측정으로 통신 시간을 추정하지 않음" : "측정 링크가 없어 통신 시간 미추정";
    return add(
      el("li", null, "placement-path-link " + t.link.status),
      add(el("div", null, "placement-link-head"), el("span", `${stageName(t.producer_role)} → ${stageName(t.consumer_role)}`), badge(label, tone),
        badge(t.requirement === "hard" ? "필수" : "선호", "")),
      ...lines.map((x) => el("small", x)),
      el("small", t.same_node ? "같은 노드" : t.same_physical_host ? "같은 물리 호스트 (무료 링크로 가정하지 않음)" : "다른 노드"),
      el("strong", estimate),
    );
  }
  function selectedPath(result) {
    const s = result.selected;
    const roles = draft.stages.map((x) => x.id).filter((r) => s.assignment[r]);
    const path = el("ol", null, "placement-path");
    path.setAttribute("aria-label", "단계별 선택 자원 경로");
    roles.forEach((role, i) => {
      const a = s.assignment[role];
      const [label, tone] = availability[a.availability] || [a.availability, "warn"];
      path.append(
        add(
          el("li", null, "placement-path-stage"),
          el("small", `${stageName(role)} · ${a.workload_ref}`),
          el("strong", a.accelerator_model || "장치 모델 미기록"),
          el("span", `${classNames[a.device_class] || "장치 미분류"} · ${a.backend} · ${a.cluster_ref || "—"}/${a.node_ref || "—"}`),
          el("small", `엔드포인트 ${a.endpoint_ref || "토폴로지에 없음"} · 고정 장치 ${(s.pinned_device_refs?.[role] || []).join(", ") || "없음 (CPU)"}`),
          badge(label, tone),
        ),
      );
      const t = s.transfers.find((x) => x.producer_role === role && x.consumer_role === roles[i + 1]);
      if (t) path.append(transferNode(t));
    });
    return path;
  }
  function results() {
    if (!plan) return null;
    const r = plan.result;
    const root = el("div", null, "placement-results");
    root.id = "placement-results";
    const head = add(
      el("section", null, "placement-verdict " + (r.accepted ? "accepted" : "rejected")),
      add(el("div"),
        el("small", "배치 검토 결과"),
        el("h2", r.accepted ? (r.selected.status === "complete" ? "조합 선택됨 · 근거 완전" : "조합 선택됨 · 일부 미확인") : "가능한 조합 없음"),
        el("p", r.accepted ? admission[r.selected.admission] || r.selected.admission : "후보와 제외 이유를 확인하세요. 아래 후보 목록은 그대로 보존됩니다.")),
      add(el("dl", null, "placement-facts"),
        el("dt", "평가 시각"), el("dd", stamp(r.evaluated_at)),
        el("dt", "스냅샷"), add(el("dd"), snapshotBadge(r.snapshot.status), document.createTextNode(` ${r.snapshot.ref || "—"} · 측정 ${stamp(r.snapshot.observed_at)} · ${r.snapshot.source || "출처 없음"}`)),
        el("dt", "조합"), el("dd", `평가 ${r.combinations_evaluated} · 가능 ${r.feasible_count} · 제외 ${r.excluded_combinations.count}`),
        el("dt", "계획 digest"), add(el("dd"), c.code(r.digest))),
    );
    root.append(head);
    if (!r.accepted && r.reasons.length) root.append(reasonList(r.reasons));
    if (r.accepted) {
      const sel = r.selected;
      root.append(
        add(el("section", null, "placement-block"),
          el("h3", "선택된 단계별 자원 경로"),
          selectedPath(r),
          add(el("p", null, "placement-hint"),
            document.createTextNode(sel.estimated_communication_ms != null && sel.transfers.every((t) => t.link.status === "measured")
              ? `예상 통신 시간 합계 ${number(sel.estimated_communication_ms)} ms — 측정 링크 기반 추정이며 학습·실행 시간이 아닙니다.`
              : "일부 링크가 미측정·만료라 통신 시간 합계를 추정하지 않습니다.")),
          sel.unknowns.length ? add(el("div"), el("strong", "미확인 근거"), reasonList(sel.unknowns)) : null,
          sel.soft_unmet.length ? add(el("div"), el("strong", "충족하지 못한 선호 조건"), reasonList(sel.soft_unmet)) : null),
      );
    }
    const chosen = r.selected?.assignment || {};
    root.append(
      add(el("section", null, "placement-block"),
        el("h3", "단계별 후보와 판단"),
        table(["단계", "실제 장치 모델", "종류 · 백엔드 · 노드", "토폴로지", "관측 여유 (예약 아님)", "판단"],
          r.candidates.map((x) => {
            const [label, tone] = availability[x.availability] || [x.availability, "warn"];
            const headroom = Object.entries(x.headroom || {}).map(([k, v]) => `${k} ${v == null ? "미확인" : number(v)}`).join(" · ");
            const picked = chosen[x.role_ref]?.candidate_ref === x.candidate_ref;
            return [
              add(el("div"), el("span", stageName(x.role_ref)), el("small", x.workload_ref)),
              add(el("div"), el("strong", x.accelerator_model || "장치 모델 미기록"), el("small", x.candidate_ref)),
              add(el("div"), el("span", `${classNames[x.device_class] || "장치 미분류"} · ${x.backend}`), el("small", `${x.cluster_ref || "—"} / ${x.node_ref || "—"} · ${x.resource_key || "자원 키 없음"}`)),
              add(el("div"), el("span", x.topology === "matched" ? x.endpoint_ref : x.topology === "not_in_snapshot" ? "스냅샷에 없음" : "스냅샷 없음"),
                el("small", x.device_refs ? `장치 ${x.device_refs.join(", ")}` : "물리 장치 식별 없음")),
              add(el("div"), badge(label, tone), headroom ? el("small", headroom) : null),
              add(el("div"), picked ? badge("선택", "selected") : x.eligible ? badge("조합 후보", "") : badge("제외", "muted"),
                x.reasons.length ? reasonList(x.reasons) : null),
            ];
          })),
      ),
    );
    if (r.combinations.length) {
      const roles = draft.stages.map((s) => s.id);
      root.append(
        add(el("section", null, "placement-block"),
          el("h3", `가능한 조합 순위 (${r.combinations.length}/${r.feasible_count})`),
          el("p", "정렬: 미확인 근거 없음 → 선호 조건 충족 → 대기 적음 → 예상 통신 시간 → 정책 백엔드 순서.", "placement-hint"),
          table(["순위", "단계별 자원", "근거 상태", "미확인 · 선호 미충족", "예상 통신 (추정)"],
            r.combinations.map((x) => [
              el("strong", String(x.rank)),
              add(el("div"), ...roles.filter((role) => x.assignment[role]).map((role) =>
                el("small", `${stageName(role)}: ${modelOf(r, role, x.assignment[role])?.accelerator_model || x.assignment[role]}`))),
              badge(x.status === "complete" ? "근거 완전" : "일부 미확인", x.status === "complete" ? "" : "warn"),
              x.unknowns.length || x.soft_unmet.length ? reasonList([...x.unknowns, ...x.soft_unmet]) : el("span", "없음"),
              costCell(x),
            ])),
        ),
      );
    }
    const ex = r.excluded_combinations;
    if (ex.count)
      root.append(
        add(el("section", null, "placement-block"),
          el("h3", `제외된 조합 ${ex.count}개`),
          table(["제외 이유", "조합 수"], Object.entries(ex.reason_counts).map(([k, n]) => [el("span", reason(k)), el("span", String(n))])),
          add(el("details"), el("summary", `제외 조합 상세 (${ex.items.length}개 표시)`),
            table(["단계별 자원", "이유"], ex.items.map((x) => [
              add(el("div"), ...Object.entries(x.assignment).map(([role, ref]) => el("small", `${stageName(role)}: ${modelOf(r, role, ref)?.accelerator_model || ref}`))),
              reasonList(x.reasons),
            ])))),
      );
    const download = el("button", "계획 JSON 내려받기");
    download.type = "button";
    download.id = "placement-download";
    download.onclick = () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }));
      const a = el("a");
      a.href = url;
      a.download = `placement-plan-${(r.digest || "plan").replace(/[^a-z0-9]/gi, "").slice(-16)}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    };
    root.append(
      add(el("section", null, "placement-block"),
        el("h3", "근거와 해석"),
        add(el("ul", null, "placement-semantics"), ...r.semantics.map((x) => el("li", x))),
        add(el("dl", null, "placement-facts"),
          el("dt", "요청 digest"), add(el("dd"), c.code(r.request_digest)),
          el("dt", "후보 digest"), add(el("dd"), c.code(r.candidates_digest)),
          el("dt", "스냅샷 digest"), add(el("dd"), c.code(r.snapshot.digest)),
          el("dt", "측정 방법"), el("dd", r.snapshot.measurement_method || "—"),
          el("dt", "스냅샷 근거"), add(el("dd"), c.code(r.snapshot.evidence_ref))),
        add(el("div", null, "placement-action-buttons"), download),
        details("원본 계획 JSON", r)),
    );
    return root;
  }

  function view() {
    ensureDraft();
    if (snapshots.state === "idle") setTimeout(loadSnapshots, 0);
    const root = el("div", null, "placement-console placement-form");
    root.append(
      add(el("div", null, "wizard-context"),
        add(el("div"), el("small", "프로젝트"), el("strong", c.data.project_ref)),
        add(el("div"), el("small", "검토 범위"), el("span", "단계 1–4 · 등록된 작업과 실행 후보만")),
        c.actionLink("← 새 워크로드로 돌아가기", "#submit")),
    );
    if (snapshots.state === "unsupported")
      root.append(
        add(el("section", null, "placement-block"),
          el("h2", "이기종 배치 검토를 사용할 수 없는 서버"),
          el("p", snapshots.message),
          el("p", "기존 단일 워크로드 제출은 그대로 사용할 수 있습니다."),
          c.actionLink("새 워크로드로 이동", "#submit")),
      );
    else {
      root.append(contextBar());
      const flow = el("div", null, "placement-stages");
      draft.stages.forEach((stage, i) => {
        if (i) flow.append(transferCard(stage, i));
        flow.append(stageCard(stage, i));
      });
      root.append(flow, actionBar(), results());
    }
    root.append(el("p", "배치 검토이며 작업을 실행하거나 자원을 예약하지 않습니다.", "placement-footer"));
    return root;
  }
  function reset() {
    serial++;
    snapshotSerial++;
    stageSerial = 0;
    Object.assign(draft, { taskRef: "", profile: "", snapshot: "", stages: [] });
    snapshots = { state: "idle", items: [], message: "", at: 0 };
    plan = null; invalidated = null; failure = null; pendingKey = "";
  }
  return { view, reset, refresh: loadSnapshots };
}
