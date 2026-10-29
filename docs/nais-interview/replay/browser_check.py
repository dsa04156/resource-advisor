"""Open offline-demo.html from file:// in Chrome and check replay behaviour; writes screenshots.

Run: rtk proxy uv run --isolated --with playwright python docs/nais-interview/replay/browser_check.py <SCREENSHOT_DIR>
"""
from pathlib import Path
import json, sys
from playwright.sync_api import sync_playwright

PAGE = (Path(__file__).resolve().parents[1] / 'offline-demo.html').as_uri()
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
DATA = json.loads((Path(__file__).resolve().parent / 'observations.json').read_text())
GANG_PATH = Path(__file__).resolve().parent / 'gang-observations.json'
SCENARIOS = {**DATA['scenarios'], **({'gang': json.loads(GANG_PATH.read_text())['scenario']} if GANG_PATH.exists() else {})}
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))


def board(page):
    return page.evaluate("""() => { const s = window.__replay.state; const f = window.__replay.scenarios[s.scenario].frames[s.index];
        return {index: s.index, playing: s.playing, dom: [...document.querySelectorAll('#rows .row')].map(r => r.getAttribute('aria-label')),
                expected: Object.fromEntries(Object.entries(f.requests).map(([k, v]) => [k, v.state])),
                summary: !document.getElementById('summary').hidden, text: document.getElementById('rows').innerText}; }""")


def board_sel(page):
    return page.evaluate('window.__replay.state.selected')


def matches(b):
    return all((f'{row} ' in label and (row not in b['expected'] or f"상태 {b['expected'][row]} " in label)) for row, label in zip(sorted(['R%02d' % i for i in range(1, 11)]), b['dom']))


with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome')
    for name, viewport in [('desktop', {'width': 1366, 'height': 768}), ('mobile', {'width': 390, 'height': 844})]:
        ctx = browser.new_context(viewport=viewport, reduced_motion='reduce' if name == 'mobile' else 'no-preference')
        requests = []
        ctx.on('request', lambda r: requests.append(r.url))
        ctx.set_offline(True)
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(PAGE)
        b = board(page)
        check(f'{name}: starts at frame 0, paused, no request state', b['index'] == 0 and not b['playing'] and b['expected'] == {} and 'SUCCEEDED' not in b['text'])
        check(f'{name}: summary hidden at start', not b['summary'])
        check(f'{name}: no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.screenshot(path=str(OUT / f'{name}-fleet-start.png'), full_page=False)
        check(f'{name}: request table collapsed by default behind 요청 상세 목록, flow visible',
              not page.evaluate("document.getElementById('detail-list').open") and '요청 상세 목록' in page.inner_text('#detail-list > summary') and page.is_visible('#lanes'))
        page.click('#detail-list > summary')
        check(f'{name}: 요청 상세 목록 opens the table', page.evaluate("document.getElementById('detail-list').open") and page.is_visible('#rows'))
        for scenario in SCENARIOS:
            page.check(f'input[value={scenario}]')
            n = len(SCENARIOS[scenario]['frames'])
            ok = True
            for i in range(n):
                page.evaluate(f'window.__replay.go({i})')
                b = board(page)
                ok &= matches(b) and b['summary'] == (i == n - 1)
            check(f'{name}/{scenario}: every seeked frame shows exactly its recorded states; summary only on last', ok)
            page.evaluate('window.__replay.go(5)')
            page.click('#next'); check(f'{name}/{scenario}: next', board(page)['index'] == 6)
            page.click('#prev'); check(f'{name}/{scenario}: prev', board(page)['index'] == 5)
            page.click('#reset'); check(f'{name}/{scenario}: reset to 0, prev disabled', board(page)['index'] == 0 and page.is_disabled('#prev'))
            page.fill('#seek', str(n - 1)); check(f'{name}/{scenario}: slider to end, next disabled', board(page)['index'] == n - 1 and page.is_disabled('#next'))
            page.evaluate('window.__replay.go(999)'); check(f'{name}/{scenario}: clamp high', board(page)['index'] == n - 1)
            page.evaluate('window.__replay.go(-5)'); check(f'{name}/{scenario}: clamp low', board(page)['index'] == 0)
        if name == 'desktop':
            page.check('input[value=adaptive]')
            page.select_option('#speed', '30')
            page.click('#play'); page.wait_for_timeout(1200); b = board(page)
            check('play advances frames in order', b['playing'] and b['index'] > 0)
            page.click('#play'); i = board(page)['index']; page.wait_for_timeout(800)
            check('pause stops advancing', not board(page)['playing'] and board(page)['index'] == i)
            page.focus('body'); page.keyboard.press('Home'); page.keyboard.press('ArrowRight'); page.keyboard.press('ArrowRight')
            check('keyboard Home/ArrowRight', board(page)['index'] == 2)
            page.keyboard.press('ArrowLeft'); check('keyboard ArrowLeft', board(page)['index'] == 1)
            page.keyboard.press('Tab'); check('Tab focus visible on a control', page.evaluate("document.activeElement.matches(':focus-visible')"))
            page.evaluate('window.__replay.go(8)'); page.click('#rows .row[data-row=R02]')
            detail = page.inner_text('#detail')
            check('R02 retry shown under same row with distinct attempts', 'R02·a1 FAILED' in detail and 'R02·a2' in detail and '(주입 실패)' in detail)
            check('ten logical rows during retry', page.locator('#rows .row').count() == 10)
            page.screenshot(path=str(OUT / 'desktop-adaptive-retry.png'))
            page.evaluate('window.__replay.go(51)'); page.screenshot(path=str(OUT / 'desktop-adaptive-end.png'), full_page=True)
            page.check('input[value=fleet]'); page.evaluate('window.__replay.go(12)'); page.screenshot(path=str(OUT / 'desktop-fleet-frame13.png'))
            check('1366x768: page fits viewport, panels scroll locally', page.evaluate('document.documentElement.scrollHeight <= innerHeight'))
            check('timeline stays inside viewport', page.evaluate("document.querySelector('.timeline').getBoundingClientRect().bottom <= innerHeight"))
            check('historical badge names date and not live', '2026-10-07' in page.inner_text('.record') and 'live 아님' in page.inner_text('.record'))
            # R02 at frame 13 is VALIDATED with a routing target only: never shown as an actual placement.
            r02 = page.locator('#rows .row[data-row=R02]')
            check('routing target shown as target, not placement', r02.locator('.node.target').inner_text() == 'GPU-C' and r02.locator('.node.actual').count() == 0 and '장치 배치 미관측' in r02.inner_text())
            r02.click()
            check('selected target-only request: roster marks target, no actual hit', page.locator('#nodes .nbtn.hit-target[data-node=GPU-C]').count() == 1 and page.locator('#nodes .nbtn.hit').count() == 0)
            check('inspector separates target from placement', '배치 대상' in page.inner_text('#detail') and '배치 미관측' in page.inner_text('#detail'))
            page.click('#rows .row[data-row=R01]')
            check('selected request highlights its observed node', page.locator('#nodes .nbtn.hit').evaluate_all('n => n.map(x => x.dataset.node)') == [DATA['scenarios']['fleet']['frames'][12]['requests']['R01']['node']])
            check('Mobilint separate-date row visible in fleet', page.is_visible('#mobilint-row') and '2026-10-08' in page.inner_text('#mobilint-row') and page.locator('#rows .row').count() == 10)
            check('Mobilint provenance retained', 'a5d1dde87b56ba2794dc6a93559342eb85f1940f' in page.content() and page.locator('#mobilint-success[data-source-sha256]').count() == 1)
            frame = DATA['scenarios']['fleet']['frames'][12]['requests']
            succ = sum(r['state'] == 'SUCCEEDED' for r in frame.values())
            check('state totals count this observation', page.inner_text('#totals [data-group=done] b') == str(succ))
            page.click('#totals [data-group=done]')
            check('state filter keeps only that group', page.locator('#rows .row').count() == succ and page.get_attribute('#totals [data-group=done]', 'aria-pressed') == 'true')
            page.click('#totals [data-group=all]')
            page.click('#nodes .nbtn[data-node=SLURM-A]')
            slurm = sorted(k for k, r in frame.items() if r.get('node') == 'SLURM-A')
            check('node filter shows requests placed on or targeted to node', sorted(page.locator('#rows .row').evaluate_all('n => n.map(x => x.dataset.row)')) == slurm and page.is_visible('#node-filter'))
            page.click('#node-filter-clear')
            page.fill('#q', 'Hailo')
            check('search filters rows', page.locator('#rows .row').count() == sum('Hailo' in r['workload'] for r in frame.values()))
            page.evaluate('window.__replay.go(13)')
            check('filters persist across seek', page.input_value('#q') == 'Hailo' and page.locator('#rows .row').count() >= 1)
            page.fill('#q', 'zzz-none'); check('empty filter state explained', '조건에 맞는 요청이 없습니다' in page.inner_text('#rows'))
            page.fill('#q', '')
            page.click('summary:has-text("출처")'); check('provenance panel opens', page.is_visible('.prov-panel') and 'PROVENANCE.json' in page.inner_text('.prov-panel'))
            page.screenshot(path=str(OUT / 'desktop-provenance.png'))
            page.keyboard.press('Escape'); check('Escape closes provenance', not page.is_visible('.prov-panel'))
            page.check('input[value=adaptive]'); page.evaluate('window.__replay.go(8)'); page.click('#rows .row[data-row=R02]')
            check('excluded node marked on roster for retried request', page.locator('#nodes .nbtn.hit-excluded[data-node=GPU-C]').count() == 1)
            # Execution flow: queue display order, recorded policy events, node chips, focus and evidence links.
            def ids(sel):
                return page.locator(sel).evaluate_all('n => n.map(x => x.dataset.card)')
            page.evaluate('window.__replay.go(20)')
            reqs = DATA['scenarios']['adaptive']['frames'][20]['requests']
            waiting = [r for r, q in reqs.items() if q['state'] in ('QUEUED', 'PENDING', 'REQUEUED')]
            expected = sorted(waiting, key=lambda r: (-reqs[r]['priority'], reqs[r]['submitted_at'] or '9', r))
            check('flow: pending queue ordered by observed priority then submission', ids('#lanes [data-stage=queue] [data-card]') == expected and len(expected) == 8)
            check('flow: queue order labelled as display order, not scheduler order', '실제 처리 순서는 보장하지 않음' in page.inner_text('#lanes'))
            check('flow: running stage shows actual nodes of each admitted request', ids('#lanes [data-stage=run] [data-card]') == ['R01', 'R04'] and page.locator('#lanes [data-card=R01] .node.actual').all_inner_texts() == ['GPU-A', 'GPU-B'])
            page.evaluate('window.__replay.go(14)')
            check('flow: aging promotion event shown at its recorded frame', '요청 02' in page.inner_text('#events') and '승격 이벤트 P10→P100' in page.inner_text('#events') and page.locator('#lanes [data-card=R02] .bdg.promote').count() == 1)
            page.evaluate('window.__replay.go(13)')
            check('flow: no promotion badge before the promotion record', page.locator('#lanes .bdg.promote').count() == 0)
            page.evaluate('window.__replay.go(7)')
            check('flow: injected failure and retry shown from recorded attempts', 'R02·a1 FAILED exit 42 (주입 실패)' in page.inner_text('#events'))
            page.evaluate('window.__replay.go(26)')
            r03 = page.locator('#lanes [data-stage=run] [data-card=R03]')
            check('flow: admission moves card from queue to running with badges', r03.count() == 1 and 'moved' in r03.get_attribute('class') and '승인·배치' in r03.inner_text() and '← 대기 큐' in r03.inner_text())
            check('flow: two-worker barrier readiness annotated', 'worker 2/2 GPU 준비 · barrier 해제' in page.inner_text('#events'))
            page.evaluate('window.__replay.go(3)')
            check('flow: first-frame-of-group barrier recorded for R01', 'barrier 해제' in page.inner_text('#lanes [data-card=R01]'))
            page.evaluate('window.__replay.go(20)')
            page.click('#nodes .chip[data-chip=R04]')
            check('node chip selects its job across queue, flow and inspector', board_sel(page) == 'R04' and page.get_attribute('#lanes [data-card=R04]', 'aria-pressed') == 'true' and page.get_attribute('#rows .row[data-row=R04]', 'aria-pressed') == 'true' and '요청 04' in page.inner_text('#detail'))
            page.click('#lanes [data-card=R05]')
            check('flow card selects job and highlights roster', board_sel(page) == 'R05' and page.get_attribute('#rows .row[data-row=R05]', 'aria-pressed') == 'true')
            # Focus regression: the activated control is rebuilt; focus must land on its replacement, not BODY.
            for sel, attr in [('#nodes .nbtn[data-node=GPU-C]', 'node'), ('#totals [data-group=all]', 'group'), ('#totals [data-group=wait]', 'group'),
                              ('#lanes [data-card=R06]', 'card'), ('#nodes .chip[data-chip=R04]', 'chip'), ('#rows .row[data-row=R07]', 'row')]:
                page.evaluate("Object.assign(window.__replay.state, {node: null, group: 'all'}); window.__replay.go(20)")
                want = page.get_attribute(sel, f'data-{attr}')
                page.focus(sel); page.keyboard.press('Enter')
                got = page.evaluate(f"[document.activeElement.tagName, document.activeElement.dataset['{attr}'] || null, document.activeElement.isConnected]")
                check(f'focus kept on replaced control after Enter: {sel}', got == ['BUTTON', want, True])
            page.evaluate("Object.assign(window.__replay.state, {node: null, group: 'all'}); window.__replay.go(20)")
            page.focus('#rows .row[data-row=R07]'); page.keyboard.press('ArrowRight')
            check('focus kept on row after replay refresh (ArrowRight)', board(page)['index'] == 21 and page.evaluate("document.activeElement.dataset.row") == 'R07')
            page.focus('#nodes .nbtn[data-node=GPU-C]'); page.keyboard.press('Tab')
            check('Tab after node toggle continues from that node', page.evaluate("document.activeElement.closest('#nodes') !== null && document.activeElement.dataset.node !== 'GPU-A'"))
            for scenario in SCENARIOS:
                page.check(f'input[value={scenario}]')
                href = page.locator('#caveat a').get_attribute('href') if page.locator('#caveat a').count() == 1 else None
                check(f'{scenario}: public summary is a clickable anchor', href == SCENARIOS[scenario]['public_summary'])
            if 'gang' in SCENARIOS:
                page.check('input[value=gang]'); page.evaluate('window.__replay.go(13)')
                check('gang: two-pod unit admission annotated from recorded admission', 'Pod 2개 한 단위 입장 기록' in page.inner_text('#lanes [data-card=R02]') and 'DDP' in page.inner_text('#flow-sub'))
                page.screenshot(path=str(OUT / 'desktop-gang-frame14.png'))
            page.check('input[value=adaptive]'); page.evaluate('window.__replay.go(26)'); page.screenshot(path=str(OUT / 'desktop-adaptive-admission-frame27.png'))
            page.evaluate('window.__replay.go(14)'); page.screenshot(path=str(OUT / 'desktop-adaptive-promotion-frame15.png'))
            page.set_viewport_size({'width': 1366, 'height': 768}); page.evaluate("document.getElementById('detail-list').open = false; document.querySelector('.work').scrollTop = 0")
            page.evaluate('window.__replay.go(20)'); page.click('#lanes [data-card=R03]')
            wide = page.evaluate("[document.getElementById('flow').getBoundingClientRect().width, document.querySelector('#lanes [data-stage=queue] [data-card]').getBoundingClientRect().width, document.querySelector('#nodes').getBoundingClientRect().top < innerHeight]")
            check('desktop: flow spans the workspace with roomy queue cards; node summary starts in first screen', wide[0] >= 1300 and wide[1] >= 190 and wide[2])
            page.screenshot(path=str(OUT / 'desktop-layout-adaptive-frame21.png'))
            page.evaluate("document.querySelector('.work').scrollTop = 1e6")
            check('desktop: workspace scrolls to full detail above the transport', page.evaluate("document.querySelector('.board').getBoundingClientRect().bottom <= document.querySelector('.timeline').getBoundingClientRect().top + 1"))
            page.screenshot(path=str(OUT / 'desktop-layout-scrolled.png'))
            check('flow view still fits 1366x768 shell', page.evaluate('document.documentElement.scrollHeight <= innerHeight && document.documentElement.scrollWidth <= innerWidth'))
            page.set_viewport_size({'width': 1180, 'height': 768}); page.evaluate('window.__replay.go(51)')
            check('1180 width: no overflow, fits viewport', page.evaluate('document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight'))
            page.screenshot(path=str(OUT / 'desktop1180-adaptive-end.png'))
        else:
            page.check('input[value=adaptive]'); page.evaluate('window.__replay.go(20)')
            check('mobile: no horizontal overflow mid-replay', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.screenshot(path=str(OUT / 'mobile-adaptive-frame21.png'), full_page=True)
            page.locator('#flow').scroll_into_view_if_needed(); page.screenshot(path=str(OUT / 'mobile-adaptive-flow-frame21.png'))
            page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
            check('mobile: sticky transport does not cover the end of the content', page.evaluate("document.querySelector('.board').getBoundingClientRect().bottom <= document.querySelector('.timeline').getBoundingClientRect().top + 1"))
            page.screenshot(path=str(OUT / 'mobile-layout-end.png'))
            check('mobile: flow lanes stack in one column without overflow', page.evaluate("getComputedStyle(document.getElementById('lanes')).gridTemplateColumns.split(' ').length === 1 && document.documentElement.scrollWidth <= innerWidth"))
            check('mobile: replay tag visible in sticky timeline', page.is_visible('#rec-tag'))
            check('mobile: reduced motion disables row flash', page.locator('#rows .row.changed').count() == 0)
            small = page.evaluate("[...document.querySelectorAll('.buttons button, .speed select, #q')].filter(n => n.getBoundingClientRect().height < 40).length")
            check('mobile: transport/search controls at least 40px', small == 0)
            page.evaluate('window.scrollTo(0, 0)'); page.screenshot(path=str(OUT / 'mobile-adaptive-frame21-top.png'))
        check(f'{name}: only the file itself was requested', requests == [PAGE])
        check(f'{name}: no page errors', not errors)
        ctx.close()
    browser.close()

for name, ok in checks:
    print('PASS' if ok else 'FAIL', name)
print(f'{sum(ok for _, ok in checks)}/{len(checks)} passed')
sys.exit(0 if all(ok for _, ok in checks) else 1)
