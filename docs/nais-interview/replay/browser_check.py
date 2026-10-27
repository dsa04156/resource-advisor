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
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))


def board(page):
    return page.evaluate("""() => { const s = window.__replay.state; const f = window.__replay.data.scenarios[s.scenario].frames[s.index];
        return {index: s.index, playing: s.playing, dom: [...document.querySelectorAll('#rows .row')].map(r => r.getAttribute('aria-label')),
                expected: Object.fromEntries(Object.entries(f.requests).map(([k, v]) => [k, v.state])),
                summary: !document.getElementById('summary').hidden, text: document.getElementById('rows').innerText}; }""")


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
        for scenario in ['fleet', 'adaptive']:
            page.check(f'input[value={scenario}]')
            n = len(DATA['scenarios'][scenario]['frames'])
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
        else:
            page.check('input[value=adaptive]'); page.evaluate('window.__replay.go(20)')
            check('mobile: no horizontal overflow mid-replay', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.screenshot(path=str(OUT / 'mobile-adaptive-frame21.png'), full_page=True)
        check(f'{name}: only the file itself was requested', requests == [PAGE])
        check(f'{name}: no page errors', not errors)
        ctx.close()
    browser.close()

for name, ok in checks:
    print('PASS' if ok else 'FAIL', name)
print(f'{sum(ok for _, ok in checks)}/{len(checks)} passed')
sys.exit(0 if all(ok for _, ok in checks) else 1)
