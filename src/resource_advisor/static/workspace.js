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
      ["jobs", "작업 목록"],
      ["submit", "새 작업"],
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
      ["scheduler-lab", "스케줄링 실험실"],
      ["scenario", "자원 요청 시나리오"],
    ],
  };
  let catalogFilter = "all",
    catalogSearch = "";
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
    const panel = document.querySelector(".submission-split > .panel");
    if (panel) {
      panel.tabIndex = -1;
      panel.focus({ preventScroll: true });
      panel.scrollIntoView({ block: "start" });
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
  function submission(legacyView) {
    const root = el("div", null, "submission-workspace"),
      all = catalog();
    const tools = el("div", null, "catalog-toolbar"),
      filters = el("div", null, "segmented");
    for (const [value, label] of [
      ["all", "전체"],
      ["inference", "추론"],
      ["training", "학습"],
      ["benchmark", "성능 측정"],
    ]) {
      const b = el("button", label, catalogFilter === value ? "selected" : "");
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
    search.setAttribute("aria-label", "예제 작업 검색");
    search.value = catalogSearch;
    search.oninput = () => {
      catalogSearch = search.value;
      c.render();
    };
    tools.append(filters, search);
    const cards = el("div", null, "workload-catalog");
    for (const w of all.filter(
      (w) =>
        (catalogFilter === "all" || w.task_type === catalogFilter) &&
        (title(w) + w.workload_ref)
          .toLowerCase()
          .includes(catalogSearch.toLowerCase()),
    )) {
      const chosen = c.selectionKey(w) === c.draft.workload,
        b = el("button", null, "workload-card" + (chosen ? " chosen" : ""));
      b.setAttribute("aria-pressed", String(chosen));
      b.onclick = () => {
        c.selectWorkload(c.selectionKey(w));
        c.render();
        c.preview(w);
        focusSetup();
      };
      const candidates = w.candidates || [],
        available = candidates.filter(c.canSubmit).length;
      const types = [
        ...new Set(
          candidates.map((x) => x.device_class?.toUpperCase()).filter(Boolean),
        ),
      ];
      b.append(
        add(
          el("div", null, "workload-card-top"),
          badge(names[w.task_type] || w.task_type),
          chosen ? icon("circle-check") : icon("layers"),
        ),
        el("h3", title(w)),
        el("p", c.workloadPurpose(w)),
        add(
          el("div", null, "workload-tags"),
          ...types.map((x) => badge(x)),
          el(
            "small",
            available ? "호환 경로 " + available + "개" : "실행 조건 확인 필요",
          ),
        ),
      );
      cards.append(b);
    }
    root.append(
      section(
        "1. 무엇을 실행할까요?",
        "처음이라면 등록된 예제를 선택하세요. 장치는 다음 단계에서 자동으로 선택할 수 있어요.",
        add(
          el("div"),
          tools,
          cards.childElementCount
            ? cards
            : c.empty("검색 조건에 맞는 예제가 없어요."),
        ),
      ),
    );
    const legacy = legacyView(),
      oldTitle = legacy.querySelector(".section-heading");
    oldTitle?.remove();
    const panel = legacy.querySelector(".panel"),
      form = panel?.querySelector(".job-form");
    if (form) {
      const select = form.querySelector("#submit-workload"),
        label = form.querySelector('label[for="submit-workload"]');
      // Keep the original selector available for advanced catalog routes and drafts.
      if (select) {
        select.hidden = true;
        if (label) label.hidden = true;
      }
      const heading = panel.querySelector("h2");
      if (heading)
        heading.textContent = c.draft.workload
          ? "2. 실행 설정 확인"
          : "2. 실행 설정";
      const p = panel.querySelector(".panel-head p");
      if (p)
        p.textContent =
          "기본 설정으로 실행하고, 필요한 경우에만 고급 설정을 바꾸세요.";
      const policy = form.querySelector("#scheduling-profile"),
        policyLabel = form.querySelector('label[for="scheduling-profile"]');
      if (policy && policyLabel) {
        const d = el("details", null, "execution-settings");
        d.append(el("summary", "고급 설정 · 실행 정책"), policyLabel, policy);
        form.insertBefore(d, form.querySelector("details") || form.firstChild);
      }
      const evidence = form.querySelector(".table-wrap");
      if (evidence) {
        const d = el("details");
        d.append(el("summary", "선택 근거 자세히 보기"), evidence);
        form.append(d);
      }
      const summary = add(
        el("aside", null, "execution-summary"),
        icon("cpu"),
        el("h3", "장치는 자동으로 선택해요"),
        el(
          "p",
          "같은 작업을 실행할 수 있는 장치만 후보로 확인해요. 현재 예약 여유와 실행 정책을 보고 선택합니다.",
        ),
        add(
          el("ol"),
          el("li", "실행할 작업 확인"),
          el("li", "호환 장치와 조건 확인"),
          el("li", "큐 접수 후 진행 상태 확인"),
        ),
        el("small", "장치가 사용 중이면 큐에서 기다릴 수 있어요."),
      );
      const split = add(el("div", null, "submission-split"), panel, summary);
      root.append(split);
      const advanced = add(
        el("details", null, "workspace-panel advanced-tools"),
        el("summary", "고급 · 템플릿 등록과 실행 환경"),
      );
      for (const node of [...legacy.children])
        if (node !== panel) advanced.append(node);
      const hiddenSelect = select;
      if (hiddenSelect) {
        hiddenSelect.hidden = false;
        if (label) label.hidden = false;
        advanced.append(
          el("p", "전체 목록에서 직접 선택"),
          label,
          hiddenSelect,
        );
      }
      root.append(advanced);
    } else root.append(legacy);
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
  function jobDetail(job, legacy) {
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
    body.id = "job-detail-panel";
    body.setAttribute("role", "tabpanel");
    const parts = {};
    parts.summary = add(
      el("div"),
      el(
        "p",
        job.attention?.action || "아래 실행 기록과 결과를 확인할 수 있어요.",
      ),
      c.schedulingProgress(job),
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
    parts.result = result
      ? add(
          el("div"),
          c.dashboardStats([
            ["실행 시간", duration(result.elapsed_seconds), "측정된 실행 구간"],
            [
              "p95 지연",
              result.latency_p95_ms == null
                ? "미측정"
                : result.latency_p95_ms + " ms",
              "측정 기록",
            ],
            ["처리량", result.throughput ?? "미측정", "측정 기록"],
          ]),
          c.details("결과 전체", job.result),
        )
      : c.empty(
          "아직 수집된 결과가 없어요. 완료 상태와 결과 수집 상태는 별도로 확인해 주세요.",
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
      b.id = "job-tab-" + key;
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
    select("summary", tabs.firstChild);
    root.append(tabs, body);
    return root;
  }
  function reset() {
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
    inference,
    navigation,
    jobDetail,
    reset,
    refreshPools,
  };
}
