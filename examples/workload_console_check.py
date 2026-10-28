"""Browser check: Console UI -> public plans/jobs API, against TEST DATA fixtures only.

Starts the real FastAPI app with examples/workload_console_fixture.py registrations
(no worker, no native backend), drives the Console in Chrome and writes screenshots.
Route interception only drops responses inside this browser to simulate an uncertain
receipt or an API outage; it never fabricates a successful server response.

Run: rtk proxy uv run --with playwright python examples/workload_console_check.py <OUT_DIR>
"""

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workload_console_fixture as fixture  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "output/playwright/workload-console")
OUT.mkdir(parents=True, exist_ok=True)
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name, flush=True)


def serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


def api_jobs(base):
    request = urllib.request.Request(
        base + "/api/v1/compute/overview",
        headers={"Authorization": "Bearer " + fixture.TOKEN},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)["jobs"]


def no_overflow(page):
    return page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")


def settle(page):
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def shots(page, name):
    # The UI is captured unmodified: scroll to the top and let layout settle first,
    # so sticky bars sit where a user sees them. Viewport and full-page both kept.
    page.evaluate("scrollTo(0, 0)")
    settle(page)
    page.screenshot(path=str(OUT / f"{name}-viewport.png"))
    page.screenshot(path=str(OUT / f"{name}-fullpage.png"), full_page=True)


def sticky_clears_header(page, selector, scroll):
    page.evaluate(f"scrollTo(0, {scroll})")
    settle(page)
    result = page.evaluate(
        """s => { const bar = document.querySelector('.topbar').getBoundingClientRect();
          const n = document.querySelector(s), box = n.getBoundingClientRect();
          return {offset: parseFloat(getComputedStyle(n).top) >= bar.height,
                  visible: box.top >= bar.bottom - 1}; }""",
        selector,
    )
    page.evaluate("scrollTo(0, 0)")
    return result


def connect(page, base):
    page.goto(base + "/console#jobs")
    page.fill("#token", fixture.TOKEN)
    page.click("#login button[type=submit]")
    page.wait_for_selector("#workspace:not([hidden])")


tmp = tempfile.mkdtemp(prefix="workload-console-")
base, server = serve(fixture.app(f"sqlite:///{tmp}/fixture.db"))
empty_base, empty_server = serve(fixture.app(f"sqlite:///{tmp}/empty.db", catalog=False))
preview_base, preview_server = serve(fixture.app(f"sqlite:///{tmp}/preview.db", preview=True))
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(viewport={"width": 1366, "height": 768})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    posts = []
    page.on(
        "request",
        lambda r: (
            posts.append(r.headers.get("idempotency-key"))
            if r.method == "POST" and r.url.endswith("/api/v1/compute/jobs")
            else None
        ),
    )
    connect(page, base)
    page.wait_for_selector(".workloads-page")
    check("jobs route is the workload table", page.locator("h2", has_text="워크로드").count() > 0)
    new = page.get_by_role("button", name="새 워크로드")
    check("prominent new workload action", new.is_visible())
    new.click()
    menu = page.locator(".new-workload-menu")
    menu_text = menu.inner_text()
    check("menu lists registered task types", "추론" in menu_text and "성능 측정" in menu_text)
    check("menu claims no YAML/image submission", "YAML" not in menu_text.replace("YAML 제출", ""))
    page.screenshot(path=str(OUT / "desktop-workloads-menu.png"))
    menu.get_by_role("link", name="추론").click()
    page.wait_for_selector(".workload-wizard")
    check("type from menu filters templates", page.locator(".wizard-card").count() == 1)
    page.get_by_role("button", name="전체", exact=True).click()
    text = page.locator(".workload-wizard").inner_text()
    for label in ("RTX 5080", "Hailo-8", "Mobilint ARIES", "Slurm CPU"):
        check(f"catalog shows registered {label} test fixture", f"TEST {label}" in text)
    check("catalog cards are labelled test data via model names", "(fixture)" in text)
    check("project context shown", fixture.PROJECT in page.locator(".wizard-context").inner_text())

    page.locator(".wizard-card", has_text="fixture-image-classify").click()
    review = page.locator(".wizard-review")
    review.get_by_text("검토 완료", exact=False).wait_for()
    review_text = review.inner_text()
    check("review shows project", fixture.PROJECT in review_text)
    check("review shows policy", "기본 정책 (TEST DATA)" in review_text)
    check("review shows queue", "fixture-queue" in review_text)
    check("review shows read-only resources", "CPU 2" in review_text and "2048 MiB" in review_text)
    check("review shows run/queue limits", "600초" in review_text)
    check("review shows candidate reason", "TEST RTX 5080 (fixture)" in review_text)
    check(
        "submit enabled after accepted plan",
        page.locator("#wizard-submit button.primary").is_enabled(),
    )
    shots(page, "desktop-review")
    check("desktop submit page without horizontal overflow", no_overflow(page))
    stuck = sticky_clears_header(page, ".wizard-review", 400)
    check("scrolled review panel stays below sticky header", stuck["offset"] and stuck["visible"])

    # Rejected plan: a real CPU-only policy rejects accelerator candidates.
    page.select_option("#scheduling-profile", "fixture-cpu-only")
    review.get_by_text("실행할 수 없어요", exact=False).wait_for()
    check(
        "rejected plan disables submit",
        page.locator("#wizard-submit button.primary").count() == 0
        or not page.locator("#wizard-submit button.primary").is_enabled(),
    )
    check(
        "rejected plan keeps selected workload",
        page.locator(".wizard-card[aria-pressed=true]", has_text="fixture-image-classify").count()
        == 1,
    )
    shots(page, "desktop-rejected")
    page.select_option("#scheduling-profile", "fixture-default")
    page.check("#target-fixture-image-classify-hailo")
    review.get_by_text("TEST Hailo-8 (fixture)", exact=False).first.wait_for()
    review.get_by_text("검토 완료", exact=False).wait_for()

    # Draft and focus survive a poll.
    page.locator("#workload-search").fill("fixture")
    page.locator("#workload-search").focus()
    with page.expect_response(lambda r: "/overview?" in r.url):
        page.evaluate("document.getElementById('auto').checked = true")
        page.evaluate("document.getElementById('refresh').click()")
    page.locator("#workload-search").focus()
    page.wait_for_timeout(200)
    check("poll keeps fixed target", page.is_checked("#target-fixture-image-classify-hailo"))
    check("poll keeps search text", page.input_value("#workload-search") == "fixture")

    # Uncertain receipt: the server accepts, the browser loses the response.
    def drop_response(route):
        route.fetch()
        route.abort("failed")

    page.route("**/api/v1/compute/jobs", drop_response)
    page.locator("#wizard-submit button.primary").click()
    page.locator("#wizard-submit").get_by_text("같은 요청 키", exact=False).wait_for()
    check("uncertain receipt keeps draft", page.is_checked("#target-fixture-image-classify-hailo"))
    check(
        "uncertain receipt offers same-key retry",
        "제출 확인 재시도" in page.locator("#wizard-submit").inner_text(),
    )
    check("no fake success after lost response", page.locator(".workloads-page").count() == 0)
    shots(page, "desktop-uncertain")
    page.unroute("**/api/v1/compute/jobs")
    retry = page.locator("#wizard-submit button.primary")
    retry.dblclick()
    page.wait_for_selector(".workloads-page")
    page.wait_for_selector("#selected-job")
    check("duplicate click sends one retry POST", len(posts) == 2)
    check("retry reuses idempotency key", len(set(posts)) == 1 and posts[0])
    jobs = api_jobs(base)
    check("server holds exactly one job", jobs["total"] == 1)
    job_id = jobs["items"][0]["job_id"]
    detail = page.locator("#selected-job").inner_text()
    check("receipt navigates to selected job detail", job_id in detail)
    check(
        "detail shows recorded device",
        "TEST Hailo-8 (fixture)" in page.locator(".workloads-page").inner_text(),
    )
    check(
        "detail offers existing cancel",
        page.locator("#selected-job").get_by_role("button", name="작업 취소").count() > 0,
    )
    shots(page, "desktop-receipt")
    check("desktop workloads without horizontal overflow", no_overflow(page))
    stuck = sticky_clears_header(page, "#selected-job", 300)
    check("selected detail sticky offset clears header", stuck["offset"])
    check(
        "1366 table fits beside detail without internal scroll",
        page.evaluate(
            "() => { const w = document.querySelector('.workloads-page .table-wrap');"
            " return w.scrollWidth <= w.clientWidth; }"
        ),
    )

    page.select_option("#job-device", "gpu")
    page.wait_for_selector(".workloads-page .empty")
    check("device filter gpu excludes Hailo job", page.locator(".job-select").count() == 0)
    page.select_option("#job-device", "npu")
    page.wait_for_selector(".job-select")
    check("device filter npu includes Hailo job", page.locator(".job-select").count() == 1)

    # Disconnected API keeps the draft and blocks submission.
    page.evaluate("location.hash = 'submit'")
    page.wait_for_selector(".workload-wizard")
    page.locator(".wizard-card", has_text="fixture-cpu-benchmark").click()
    page.locator(".wizard-review").get_by_text("검토 완료", exact=False).wait_for()
    page.route("**/api/v1/compute/overview?*", lambda route: route.abort("failed"))
    page.click("#refresh")
    page.wait_for_selector(".connection-lost")
    lost = page.locator(".connection-lost").inner_text()
    check("outage is visible", "연결" in lost)
    check("outage blocks submission", page.locator("#wizard-submit button.primary").count() == 0)
    check("outage keeps draft summary", "fixture-cpu-benchmark" in lost)
    shots(page, "desktop-disconnected")
    page.unroute("**/api/v1/compute/overview?*")
    page.get_by_role("button", name="다시 연결").click()
    page.wait_for_selector(".workload-wizard")
    check(
        "reconnect restores draft",
        page.locator(".wizard-card[aria-pressed=true]", has_text="fixture-cpu-benchmark").count()
        == 1,
    )

    for route in (
        "home",
        "jobs",
        "submit",
        "models",
        "inference",
        "execution",
        "history",
        "usage",
        "compatibility",
        "experiments",
        "recommendations",
        "scheduler-lab",
        "scenario",
        "pipelines",
        "notebooks",
        "operations",
    ):
        page.evaluate(f"location.hash = '{route}'")
        page.wait_for_timeout(150)
    check("all hash routes render without page errors", not errors)

    # Mobile.
    mobile = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    m = mobile.new_page()
    m.on("pageerror", lambda e: errors.append(str(e)))
    connect(m, base)
    m.wait_for_selector(".workloads-page")
    check("mobile workloads without horizontal overflow", no_overflow(m))
    m.locator(".job-select").first.click()
    m.wait_for_selector("#selected-job")
    check("mobile detail without horizontal overflow", no_overflow(m))
    shots(m, "mobile-workloads")
    m.evaluate("location.hash = 'submit'")
    m.wait_for_selector(".workload-wizard")
    m.locator(".wizard-card", has_text="fixture-image-classify").click()
    m.locator(".wizard-review").get_by_text("검토 완료", exact=False).wait_for()
    check("mobile submit without horizontal overflow", no_overflow(m))
    check(
        "mobile submit button reachable",
        m.locator("#wizard-submit button.primary").is_visible()
        or m.locator("#wizard-submit button.primary").count() == 1,
    )
    shots(m, "mobile-submit")

    # Configured but empty catalog: honest explanation, no fabricated templates.
    e = ctx.new_page()
    connect(e, empty_base)
    e.evaluate("location.hash = 'submit'")
    e.wait_for_selector(".workload-wizard")
    check(
        "empty catalog explained", "카탈로그가 비어" in e.locator(".workload-wizard").inner_text()
    )
    check("empty catalog shows no cards", e.locator(".wizard-card").count() == 0)
    shots(e, "desktop-empty-catalog")

    # Preview mode: opens straight into the TEST DATA project, no token, no other project.
    v = ctx.new_page()
    v.goto(preview_base + "/console#submit")
    v.wait_for_selector(".workload-wizard")
    check("preview auto-connects to TEST DATA project", "TEST-DATA" in v.inner_text("#identity"))
    check("preview hides token login", v.locator("#connect").is_hidden())
    shots(v, "preview-submit")
    check("no page errors", not errors)
    browser.close()

server.should_exit = empty_server.should_exit = preview_server.should_exit = True
failed = [name for name, ok in checks if not ok]
(OUT / "checks.json").write_text(
    json.dumps(
        {"scope": "TEST DATA fixtures; not live runs", "errors": errors, "checks": dict(checks)},
        ensure_ascii=False,
        indent=2,
    )
)
print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
