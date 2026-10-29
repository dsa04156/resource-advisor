"""Open offline-demo.html from file:// and check the default synthetic 30-request simulation; writes screenshots.

Run: rtk proxy uv run --isolated --with playwright python docs/nais-interview/replay/browser_check_simulation.py <SCREENSHOT_DIR>
"""
from pathlib import Path
import json, sys
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = (HERE.parent / 'offline-demo.html').as_uri()
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
SC = json.loads((HERE / 'simulation-observations.json').read_text())['scenario']
N, INIT = len(SC['rows']), SC['initial_index']
GROUP = {'PENDING': 'wait', 'RUNNING': 'run', 'SUCCEEDED': 'done', 'NOT_SUBMITTED': 'before'}
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))


def total(page, g):
    loc = page.locator(f'#totals [data-group={g}] b')
    return int(loc.inner_text()) if loc.count() else 0


def expected(i):
    out = {}
    for r in SC['frames'][i]['requests'].values():
        out[GROUP[r['state']]] = out.get(GROUP[r['state']], 0) + 1
    return out


with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome')
    for name, viewport in [('desktop', {'width': 1366, 'height': 768}), ('mobile', {'width': 390, 'height': 844})]:
        ctx = browser.new_context(viewport=viewport)
        ctx.set_offline(True)
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(PAGE)
        st = page.evaluate('[window.__replay.state.scenario, window.__replay.state.index]')
        check(f'{name}: default scenario is simulation at representative frame', st == ['sim', INIT] and page.is_checked('input[value=sim]'))
        check(f'{name}: scenario label count from data', page.inner_text('#sim-label') == f'{N}건 시뮬레이션 · 합성')
        exp = expected(INIT)
        check(f'{name}: default mixed states >=5 run, >=5 done, >=10 wait', exp.get('run', 0) >= 5 and exp.get('done', 0) >= 5 and exp.get('wait', 0) >= 10)
        check(f'{name}: counters match frame and total {N}', total(page, 'all') == N and all(total(page, g) == n for g, n in exp.items()))
        flow = sum(int(page.inner_text(f'#lanes [data-stage={k}] h3 b')) for k in ['entry', 'queue', 'run', 'out'])
        check(f'{name}: flow shows all {N}', flow == N and page.locator('#lanes [data-stage=run] [data-card]').count() == exp['run'] and page.locator('#lanes [data-stage=queue] [data-card]').count() == exp['wait'])
        check(f'{name}: table holds all {N} rows', page.locator('#rows .row').count() == N)
        check(f'{name}: synthetic badge top', '합성 시뮬레이션' in page.inner_text('#record') and '실행 기록 아님' in page.inner_text('#record'))
        check(f'{name}: synthetic policy events, timestamps, nodes', '시뮬레이션 변화 (합성)' in page.inner_text('#events') and '가상 T+' in page.inner_text('#time')
              and '선언 용량' in page.inner_text('#nodes') and '측정 사용률이 아닙니다' in page.inner_text('#roster-foot'))
        check(f'{name}: no Mobilint row/section in simulation', not page.is_visible('#mobilint-row') and page.locator('#mobilint-success').get_attribute('hidden') is not None and 'NPU-B' not in page.inner_text('#nodes'))
        check(f'{name}: no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.screenshot(path=str(OUT / f'{name}-sim-default.png'))
        if name == 'mobile':
            check('mobile: sticky timeline tag says synthetic', page.is_visible('#rec-tag') and '합성 시뮬레이션' in page.inner_text('#rec-tag'))
            page.screenshot(path=str(OUT / 'mobile-sim-default-full.png'), full_page=True)
        page.click('#detail-list > summary')
        run_row = next(r for r, q in SC['frames'][INIT]['requests'].items() if q['state'] == 'RUNNING')
        page.click(f'#lanes [data-card={run_row}]')
        node = SC['frames'][INIT]['requests'][run_row]['node']
        detail = page.inner_text('#detail')
        check(f'{name}: inspector marks synthetic assignment', '가상 배정' in detail and '합성 시뮬레이션' in detail and node in detail and page.locator(f'#nodes .nbtn.hit[data-node={node}]').count() == 1)
        page.click('#totals [data-group=wait]')
        check(f'{name}: state filter count', page.locator('#rows .row').count() == exp['wait'])
        page.click('#totals [data-group=all]')
        page.click(f'#nodes .nbtn[data-node={node}]')
        on_node = sum(q['node'] == node for q in SC['frames'][INIT]['requests'].values())
        check(f'{name}: node filter count', page.locator('#rows .row').count() == on_node)
        page.click('#node-filter-clear')
        if name == 'desktop':
            page.click('#reset')
            check('reset: all requests before arrival', total(page, 'before') == N)
            page.evaluate(f'window.__replay.go({INIT})'); page.click('#next')
            i1 = page.evaluate('window.__replay.state.index')
            check('next frame matches its counters', i1 == INIT + 1 and all(total(page, g) == n for g, n in expected(i1).items()))
            page.select_option('#speed', '30'); page.click('#play'); page.wait_for_timeout(1500); page.click('#play')
            i2 = page.evaluate('window.__replay.state.index')
            check('playback advances and counters follow', i2 > i1 and all(total(page, g) == n for g, n in expected(i2).items()))
            page.evaluate(f'window.__replay.go({len(SC["frames"]) - 1})')
            check('end frame: all completed, synthetic summary', total(page, 'done') == N and '시뮬레이션 종료 요약' in page.inner_text('#summary'))
            page.screenshot(path=str(OUT / 'desktop-sim-end.png'))
            page.click('summary:has-text("출처")')
            check('source panel says synthetic', 'simulation-observations.json' in page.inner_text('.prov-panel') and '합성' in page.inner_text('#prov-rules'))
            page.keyboard.press('Escape')
        for real in ['fleet', 'adaptive', 'gang']:
            if page.locator(f'input[value={real}]').count() == 0:
                continue
            page.check(f'input[value={real}]')
            check(f'{name}: switch to {real} restores recorded mode', page.evaluate('window.__replay.state.index') == 0 and '기록 재생' in page.inner_text('#record')
                  and '합성' not in page.inner_text('#record') and '합성' not in page.inner_text('#rec-tag') and page.locator('#rows .row').count() == 10)
        page.check('input[value=fleet]')
        check(f'{name}: fleet keeps Mobilint and source links', page.is_visible('#mobilint-success') or page.locator('#mobilint-success').get_attribute('hidden') is None)
        page.screenshot(path=str(OUT / f'{name}-fleet-after-switch.png'))
        page.goto(PAGE + '#success'); page.reload()
        check(f'{name}: #success deep link opens fleet end with recorded roster (no node marked outside scenario)', page.evaluate('[window.__replay.state.scenario, window.__replay.state.index]') == ['fleet', 23]
              and page.locator('#nodes .nbtn.outside').count() == 0 and '이 시나리오 기록에 없음' not in page.inner_text('#nodes'))
        check(f'{name}: no page errors', not errors)
        ctx.close()
    browser.close()

for name, ok in checks:
    print('PASS' if ok else 'FAIL', name)
print(f'{sum(ok for _, ok in checks)}/{len(checks)} passed')
sys.exit(0 if all(ok for _, ok in checks) else 1)
