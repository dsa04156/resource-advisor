"""Browser check: Console #placement -> public topology/placement API, TEST DATA only.

Starts the real FastAPI app with examples/placement_console_fixture.py registrations
(no worker, no native backend, temporary SQLite) and drives the Console in Chrome.
Route interception only holds or drops real responses inside this browser to exercise
stale-response and transport-failure handling; it never fabricates a server response.
Link bandwidths in the fixture are synthetic test inputs, not hardware measurements.

Run: rtk proxy uv run --with playwright python examples/placement_console_check.py <OUT_DIR>
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
import placement_console_fixture as fixture  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "output/playwright/placement-console")
OUT.mkdir(parents=True, exist_ok=True)
checks = []
MIB = 1048576


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


def shots(page, name):
    page.evaluate("scrollTo(0, 0)")
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    page.screenshot(path=str(OUT / f"{name}-viewport.png"))
    page.screenshot(path=str(OUT / f"{name}-fullpage.png"), full_page=True)


def connect(page, base, view="placement"):
    page.goto(base + "/console#" + view)
    page.fill("#token", fixture.TOKEN)
    page.click("#login button[type=submit]")
    page.wait_for_selector("#workspace:not([hidden])")


def plan(page):
    """Click the review action and wait for the real API response to render."""
    with page.expect_response(lambda r: r.url.endswith("/placement-plans")):
        page.click("#placement-submit")
    page.wait_for_selector("#placement-results")
    return page.locator("#placement-results").inner_text()


tmp = tempfile.mkdtemp(prefix="placement-console-")
base, server = serve(fixture.app(f"sqlite:///{tmp}/fixture.db"))
bare_base, bare_server = serve(fixture.app(f"sqlite:///{tmp}/bare.db", snapshots=False))
old_base, old_server = serve(fixture.app(f"sqlite:///{tmp}/old.db", placement_api=False))
errors = []
report = {"scope": "TEST DATA fixtures; preview only, not live runs or hardware evidence"}

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    job_posts, plan_bodies = [], []
    page.on(
        "request",
        lambda r: (
            job_posts.append(r.url)
            if r.method == "POST" and r.url.endswith("/api/v1/compute/jobs")
            else plan_bodies.append(json.loads(r.post_data))
            if r.method == "POST" and r.url.endswith("/placement-plans")
            else None
        ),
    )

    # Entry from the existing new-workload flow, with back navigation.
    connect(page, base, "submit")
    page.wait_for_selector(".workload-wizard")
    page.get_by_role("link", name="이기종 자원 조합 검토").click()
    page.wait_for_selector(".placement-console")
    check("new-workload flow links to #placement", page.url.endswith("#placement"))
    check("section tab marks placement", page.locator("#section-tabs a[aria-current=page]").inner_text() == "자원 조합 검토")
    check("back link to new workload", page.get_by_role("link", name="← 새 워크로드로 돌아가기").count() == 1)
    page.wait_for_selector("#placement-snapshot")
    snapshot_options = page.eval_on_selector_all("#placement-snapshot option", "o => o.map(x => x.value)")
    check("project snapshots listed", fixture.FRESH in snapshot_options and fixture.EXPIRED in snapshot_options)
    check("other project's snapshot not listed", fixture.OTHER not in snapshot_options)
    check("fresh snapshot preselected", page.input_value("#placement-snapshot") == fixture.FRESH)
    check("snapshot provenance shown", "TEST DATA synthetic link table" in page.locator(".placement-context").inner_text())
    footer = "배치 검토이며 작업을 실행하거나 자원을 예약하지 않습니다."
    check("non-execution footer", page.locator(".placement-footer").inner_text() == footer)
    check("no execution/training action", page.get_by_role("button", name="실행").count() == 0
          and page.locator(".placement-console button", has_text="학습").count() == 0)

    # Stage 1: registered workload -> real registered GPU/NPU candidates with exact models.
    page.select_option("#placement-stage-1-workload", "fixture-image-classify")
    stage1 = page.locator(".placement-stage").first.inner_text()
    for label in ("TEST Mobilint ARIES (fixture)", "TEST RTX 5080 (fixture)", "TEST Hailo-8 (fixture)"):
        check(f"stage candidate shows {label}", label in stage1)
    check("stage candidate shows device class/node", "NPU · kubernetes" in stage1 and "fixture-mobilint-node" in stage1)
    check("stage candidate shows runtime", "mobilint fixture" in stage1)
    page.click("#placement-add-stage")
    page.select_option("#placement-stage-2-workload", "fixture-gpu-head")
    stage2 = page.locator(".placement-stage").nth(1).inner_text()
    check("unregistered device labelled", "이 작업의 실행 환경 미등록" in stage2 and "mobilint.example/npu" in stage2)
    check("plan blocked until transfer size entered", page.is_disabled("#placement-submit"))
    check("no default bandwidth assumed", page.input_value("#placement-stage-2-bandwidth") == "")

    # Many tiny transfers: low-overhead (slow) link wins.
    page.fill("#placement-stage-2-mib", "0.05")
    page.fill("#placement-stage-2-count", "1000")
    check("exact MiB->bytes conversion shown", "= 52,429 bytes" in page.locator(".placement-transfer").inner_text())
    text = plan(page)
    small = plan_bodies[-1]
    check("request bytes exact", small["edges"][0]["bytes_per_transfer"] == round(0.05 * MIB))
    check("request roles unique ids", [r["role_ref"] for r in small["roles"]] == ["stage-1", "stage-2"])
    path = page.locator(".placement-path").inner_text()
    check("selected path shows Mobilint then RTX 4090", "TEST Mobilint ARIES" in path and "TEST RTX 4090" in path)
    check("measured link labelled separately from estimate", "측정 대역폭" in path and "예상 통신" in path and "추정" in path)
    check("availability not shown as reservation", "현재 여유 미확인" in text and "예약 아님" in text)
    check("missing link stays 미측정", "미측정 · 추정 불가" in text)
    check("excluded combination reason shown", "서로 다른 물리 장치 부족" in text)
    check("observation timestamp and source shown", "operator_import" in text and "측정" in text)
    small_choice = page.locator(".placement-path-stage").nth(1).inner_text()
    shots(page, "desktop-plan-small-transfers")
    check("desktop placement without horizontal overflow", no_overflow(page))

    # Download the actual plan JSON for traceability.
    with page.expect_download() as info:
        page.click("#placement-download")
    saved = OUT / "placement-plan-small-transfers.json"
    info.value.save_as(saved)
    exported = json.loads(saved.read_text())
    check("exported JSON is the server plan", exported["digest"] in text and exported["schema"] == "placement-plan/v1")

    # Input change invalidates the accepted result; large transfers flip the choice.
    page.fill("#placement-stage-2-mib", "64")
    check("input change hides accepted result", page.locator("#placement-results").count() == 0)
    check("invalidation explained", "무효" in page.locator(".placement-action").inner_text())
    page.fill("#placement-stage-2-count", "10")
    plan(page)
    large_choice = page.locator(".placement-path-stage").nth(1).inner_text()
    check("network size changes selected stage-2 device",
          "TEST RTX 4090" in small_choice and "TEST RTX 5080" in large_choice)
    shots(page, "desktop-plan-large-transfers")

    # Hard minimum bandwidth excludes slow and unmeasured links.
    page.select_option("#placement-stage-2-requirement", "hard")
    page.fill("#placement-stage-2-bandwidth", "5")
    check("bandwidth unit conversion shown", "= 625,000,000 bytes/s" in page.locator(".placement-transfer").inner_text())
    text = plan(page)
    check("hard bandwidth in request", plan_bodies[-1]["edges"][0]["min_bandwidth_bytes_per_second"] == 625_000_000)
    check("hard requirement excludes slow link", "측정 대역폭이 최소 요구보다 낮음" in text)
    check("hard requirement excludes unmeasured link", "필수 요구 링크 미측정" in text)
    check("one feasible combination left", "가능 1" in text)
    shots(page, "desktop-plan-hard-bandwidth")

    # Expired topology: link costs are not presented as current estimates.
    page.select_option("#placement-snapshot", fixture.EXPIRED)
    check("snapshot change invalidates result", page.locator("#placement-results").count() == 0)
    page.select_option("#placement-stage-2-requirement", "soft")
    page.fill("#placement-stage-2-bandwidth", "")
    text = plan(page)
    check("expired snapshot labelled 만료", "만료" in page.locator(".placement-verdict").inner_text())
    check("expired evidence fails closed with reason", "물리 장치 식별 근거 만료" in text)
    check("expired snapshot shows no communication estimate", "예상 통신" not in text and " ms" not in text)
    shots(page, "desktop-plan-expired")
    page.select_option("#placement-snapshot", fixture.FRESH)

    # Training stage without isolation binding: rejected, candidates and reasons retained.
    page.select_option("#placement-stage-2-workload", "fixture-cnn-training")
    text = plan(page)
    check("rejected plan explained", "가능한 조합 없음" in text and "정책·호환성을 통과한 후보 없음" in text)
    check("rejected plan still lists candidates", "TEST RTX 5080" in text and "TEST Mobilint ARIES" in text and "제외" in text)
    check("training stage hints NPU training not inferred",
          "NPU 학습 가능을 추정하지 않습니다" in page.locator(".placement-stage").nth(1).inner_text())
    shots(page, "desktop-plan-rejected")
    page.select_option("#placement-stage-2-workload", "fixture-gpu-head")
    page.fill("#placement-stage-2-mib", "64")
    page.fill("#placement-stage-2-count", "10")

    # Stale response: hold the real response, change the draft, then release it.
    held = []
    page.route("**/placement-plans", lambda route: held.append(route))
    page.click("#placement-submit")
    page.wait_for_function("() => document.querySelector('#placement-submit').textContent.includes('확인 중')")
    page.fill("#placement-stage-2-count", "11")
    while not held:
        page.wait_for_timeout(50)
    response = held[0].fetch()
    held[0].fulfill(response=response)
    page.wait_for_function("() => !document.querySelector('#placement-submit').textContent.includes('확인 중')")
    check("stale response for old draft is ignored", page.locator("#placement-results").count() == 0)
    page.unroute("**/placement-plans")

    # Transport failure keeps the draft and offers retry.
    page.route("**/placement-plans", lambda route: route.abort("failed"))
    page.click("#placement-submit")
    page.wait_for_selector(".placement-failure")
    check("transport error message", "연결하지 못했습니다" in page.locator(".placement-failure").inner_text())
    check("draft preserved after transport error",
          page.input_value("#placement-stage-2-mib") == "64" and page.input_value("#placement-stage-2-count") == "11")
    page.unroute("**/placement-plans")
    with page.expect_response(lambda r: r.url.endswith("/placement-plans")):
        page.get_by_role("button", name="다시 시도").click()
    page.wait_for_selector("#placement-results")
    check("retry succeeds after transport error", page.locator(".placement-path").count() == 1)

    # Profile change invalidates and a CPU-only policy rejects accelerators.
    page.select_option("#placement-profile", "fixture-cpu-only")
    check("profile change invalidates result", page.locator("#placement-results").count() == 0)
    text = plan(page)
    check("cpu-only policy rejects accelerator stages", "가능한 조합 없음" in text)
    page.select_option("#placement-profile", "fixture-default")
    plan(page)

    # Stage add/remove bounds.
    for _ in range(2):
        page.click("#placement-add-stage")
    check("max four stages", page.is_disabled("#placement-add-stage") and page.locator(".placement-stage").count() == 4)
    page.get_by_role("button", name="단계 4 삭제").click()
    page.get_by_role("button", name="단계 3 삭제").click()
    check("removal returns to two stages", page.locator(".placement-stage").count() == 2)
    check("placement flow sent zero POST /jobs", not job_posts)

    # Mobile 390px: same real API, no page overflow.
    m = ctx.new_page()
    m.on("pageerror", lambda e: errors.append(str(e)))
    m.set_viewport_size({"width": 390, "height": 844})
    connect(m, base)
    m.wait_for_selector("#placement-snapshot")
    m.select_option("#placement-stage-1-workload", "fixture-image-classify")
    m.click("#placement-add-stage")
    m.select_option("#placement-stage-2-workload", "fixture-gpu-head")
    m.fill("#placement-stage-2-mib", "64")
    m.fill("#placement-stage-2-count", "10")
    plan(m)
    check("mobile placement without horizontal overflow", no_overflow(m))
    shots(m, "mobile-plan")

    # Empty snapshot list and an older server without the API.
    e = ctx.new_page()
    e.on("pageerror", lambda x: errors.append(str(x)))
    connect(e, bare_base)
    e.get_by_text("토폴로지 스냅샷이 없습니다", exact=False).wait_for()
    check("empty snapshots explained", e.is_disabled("#placement-submit"))
    shots(e, "desktop-no-snapshots")
    o = ctx.new_page()
    o.on("pageerror", lambda x: errors.append(str(x)))
    connect(o, old_base)
    o.get_by_text("사용할 수 없는 서버", exact=False).wait_for()
    check("unsupported API explained without crash", "단일 워크로드 제출은 그대로" in o.locator(".placement-console").inner_text())
    shots(o, "desktop-unsupported-api")

    # Existing single-workload submit still works on the same server.
    page.evaluate("location.hash = 'submit'")
    page.wait_for_selector(".workload-wizard")
    page.get_by_role("button", name="전체", exact=True).click()
    page.locator(".wizard-card", has_text="fixture-image-classify").click()
    page.locator(".wizard-review").get_by_text("검토 완료", exact=False).wait_for()
    page.locator("#wizard-submit button.primary").click()
    page.wait_for_selector(".workloads-page")
    check("existing submit still posts one job", len(job_posts) == 1 and api_jobs(base)["total"] == 1)
    check("no page errors", not errors)
    browser.close()

server.should_exit = bare_server.should_exit = old_server.should_exit = True
failed = [name for name, ok in checks if not ok]
report.update(errors=errors, checks=dict(checks), placement_requests=plan_bodies)
(OUT / "checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
