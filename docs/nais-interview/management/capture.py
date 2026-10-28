"""Check and capture the recorded management views served by the existing static server.

Run from the repository root (server :18421 is rooted at docs/nais-interview):
  rtk proxy uv run --isolated --with playwright python docs/nais-interview/management/capture.py docs/nais-interview/management/captures
Screenshots are taken from the rendered page as served; the DOM and CSS are not modified.
"""
from pathlib import Path
import hashlib
import json
import re
import sys
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:18421/management/'
HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]).resolve()
SCALE = float(sys.argv[2]) if len(sys.argv) > 2 else 2
DATA = json.loads((HERE / 'data.json').read_text())
ROUTES = ['overview', 'resources', 'workloads', 'queue', 'results']
SHOTS = [
    ('01-overview.png', '#/overview', None),
    ('02-resources.png', '#/resources', None),
    ('03-workloads.png', '#/workloads/R10', None),
    ('03a-workload-detail-R10.png', '#/workloads/R10', '#detail'),
    ('03b-workload-detail-R11.png', '#/workloads/R11?device=npu', '#detail'),
    ('04-queue-snapshot-12.png', '#/queue/12', None),
    ('04a-queue-board-12.png', '#/queue/12', '.board'),
    ('05-results.png', '#/results', None),
]
checks, errors = [], []


def check(name, ok, detail=None):
    checks.append({'name': name, 'ok': bool(ok), **({'detail': detail} if detail is not None else {})})


def open_page(ctx, hash_):
    page = ctx.new_page()
    page.on('pageerror', lambda e: errors.append(f'pageerror {hash_}: {e}'))
    page.on('console', lambda m: m.type == 'error' and errors.append(f'console {hash_}: {m.text}'))
    page.on('response', lambda r: r.status >= 400 and errors.append(f'http {r.status} {r.url}'))
    page.goto(BASE + hash_)
    page.wait_for_selector('html[data-ready="true"]')
    return page


def layout(page):
    return page.evaluate("""() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
        sh: document.documentElement.scrollHeight, ih: innerHeight, text: document.body.innerText,
        minFont: Math.min(...[...document.querySelectorAll('#view *, .topbar *, .caption *')].filter(n => n.childNodes.length && [...n.childNodes].some(c => c.nodeType === 3 && c.textContent.trim()))
            .map(n => parseFloat(getComputedStyle(n).fontSize)))})""")


def rows(page):
    return page.eval_on_selector_all('tbody tr[data-row]', 'els => els.map(e => e.dataset.row)')


with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome')
    desktop = browser.new_context(viewport={'width': 1920, 'height': 1080}, device_scale_factor=SCALE)
    mobile = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=1)

    for route in ROUTES:
        for name, ctx in (('1920', desktop), ('390', mobile)):
            page = open_page(ctx, f'#/{route}')
            lay = layout(page)
            check(f'{route} {name}: no horizontal overflow', lay['sw'] <= lay['cw'], [lay['sw'], lay['cw']])
            if name == '1920':
                check(f'{route} 1920: fits 1080 viewport', lay['sh'] <= lay['ih'], [lay['sh'], lay['ih']])
                check(f'{route} 1920: smallest text >= 11.5px', lay['minFont'] >= 11.5, lay['minFont'])
            check(f'{route} {name}: record stamp visible', '실증 기록 · 2026-10-07~08' in lay['text'] and '관측 기록 기준' in lay['text'])
            check(f'{route} {name}: no repeated live/submit/simultaneous/online caveats or project-A',
                  not re.search(r'live 아님|제출·취소|동시|온라인|online|project-A', lay['text']), re.findall(r'.{0,20}(?:live 아님|제출·취소|동시|온라인|online|project-A).{0,20}', lay['text']))
            check(f'{route} {name}: no TEST/fixture/synthetic wording', not re.search(r'\b(TEST|fixture|synthetic|테스트)\b', lay['text'], re.I))
            page.close()

    s = DATA['summary']
    page = open_page(desktop, '#/overview')
    kpi = re.sub(r'\s+', '', page.inner_text('.kpis'))
    check('overview: completed 11, K8s 9건 / Slurm 2건, nodes 8 (GPU 6 / NPU 2), receipts 11/11',
          all(t in kpi for t in ['완료기록11건', '백엔드별완료요청K8s9건Slurm2건', 'Kueue경로8건', '실행노드(누적)8개', 'GPU6·NPU2', '11/11']), kpi)
    check('overview: Mobilint device labelled Kubernetes, not Kueue', 'NPU-B · Kubernetes · NPU' in page.inner_text('.bars')
          and page.inner_text('.bars').count('K8s · Kueue') == 6)
    check('sidebar: historical scope with both collection dates', all(t in page.inner_text('.scope') for t in ['이기종 실증 기록', 'fleet_batch · 2026-10-07', 'Mobilint · 2026-10-08']))
    check('overview: summary matches data', (s['completed'], s['backend'], s['nodes_cumulative'], s['mlflow_receipts']) == (11, {'kubernetes': 9, 'slurm': 2}, 8, 11))
    check('overview: Mobilint present', 'Mobilint ARIES2' in page.inner_text('#view'))
    page.close()

    page = open_page(desktop, '#/resources')
    check('resources: 8 device rows', page.locator('tbody tr').count() == 8)
    paths = page.eval_on_selector_all('tbody tr', 'els => els.map(e => e.cells[2].innerText)')
    check('resources: path labels (6 Kueue, Mobilint Kubernetes, 1 Slurm)', paths == ['K8s · Kueue'] * 6 + ['Kubernetes', 'Slurm'], paths)
    check('resources: workload counts sum to 11', sum(int(x) for x in page.eval_on_selector_all('tbody td.num', 'els => els.map(e => e.innerText)')) == 11)
    page.close()

    page = open_page(desktop, '#/workloads')
    check('workloads: 11 rows', len(rows(page)) == 11)
    expect = {('kubernetes', 'all'): 9, ('slurm', 'all'): 2, ('all', 'npu'): 3, ('all', 'gpu'): 8, ('slurm', 'npu'): 0, ('kubernetes', 'npu'): 3}
    for (b, d), n in expect.items():
        page.click(f'[data-filter="backend:{b}"]')
        page.click(f'[data-filter="device:{d}"]')
        got = rows(page)
        want = [r['id'] for r in DATA['requests'] if b in ('all', r['backend']) and d in ('all', r['device_class'])]
        check(f'workloads filter backend={b} device={d}: {n} rows', len(got) == n and got == want, got)
    page.click('[data-filter="backend:all"]')
    page.click('[data-filter="device:all"]')
    page.click('tr[data-row="R06"]')
    check('workloads: row click selects detail', 'R06' in page.inner_text('#detail h2') and '#/workloads/R06' in page.url)
    page.close()
    page = open_page(desktop, '#/workloads/R10')
    det = page.inner_text('#detail')
    check('workloads detail R10: lifecycle reason AssocGrpCpuLimit', 'AssocGrpCpuLimit' in det and '04:29:49.510' in det)
    page.close()
    page = open_page(desktop, '#/workloads/R11')
    det = page.inner_text('#detail')
    check('workloads detail R11: date 2026-10-08, MLflow, recorded time fields, Kubernetes path', all(t in det for t in ['2026-10-08', '수신 기록 있음', '07:22:15.316', '상태 이력 아님', 'Kubernetes'])
          and 'Kueue' not in det, det)
    page.close()

    for i, snap in enumerate(DATA['snapshots']):
        page = open_page(desktop, f'#/queue/{i}')
        lanes = {k: page.locator(f'[data-lane="{k}"] .card').count() for k in ('intake', 'native', 'run', 'done')}
        c = snap['counts']
        want = {'intake': c.get('REQUESTED', 0) + c.get('VALIDATED', 0), 'native': c.get('SUBMITTING', 0) + c.get('QUEUED', 0),
                'run': c.get('RUNNING', 0) + c.get('COLLECTING', 0), 'done': c.get('SUCCEEDED', 0)}
        text = page.inner_text('#view')
        reasons = sorted(r['reason'] for r in snap['requests'].values() if r['reason'])
        shown = sorted(page.eval_on_selector_all('.card .reason', 'els => els.map(e => e.innerText)'))
        qo = snap['queue_observation']
        native = page.inner_text('.native')
        check(f'queue snapshot {i}: lanes match recorded states, reasons only from snapshot, timestamp shown',
              lanes == want and shown == reasons and snap['observed_at'] == page.get_attribute('[data-observed]', 'data-observed')
              and (f"admitted {qo['admitted']} · pending {qo['pending']}" in native if qo else '기록 없음' in native)
              and 'R11' not in page.inner_text('.board'),
              {'lanes': lanes, 'want': want, 'reasons': shown} if lanes != want or shown != reasons else None)
        page.close()
    page = open_page(desktop, '#/queue/12')
    page.click('button[aria-label="다음 스냅샷"]')
    check('queue: next button moves to snapshot 13', page.url.endswith('#/queue/13') and DATA['snapshots'][13]['observed_at'] == page.get_attribute('[data-observed]', 'data-observed'))
    page.close()

    page = open_page(desktop, '#/results')
    txt = page.inner_text('#view')
    check('results: Mobilint measurements and units', all(t in txt for t in ['0.261', '26.05', '26.31', '38.27', '66.33', 'work units/s', '정확도 아님']), None)
    check('results: no accuracy percentage', not re.search(r'100\s*%|정확도\s*1', txt))
    page.close()

    OUT.mkdir(parents=True, exist_ok=True)
    shots = []
    for name, hash_, selector in SHOTS:
        page = open_page(desktop, hash_)
        target = page.locator(selector) if selector else page
        target.screenshot(path=str(OUT / name), animations='disabled')
        b = (OUT / name).read_bytes()
        shots.append({'file': name, 'url': 'management/index.html' + hash_, 'element': selector, 'sha256': hashlib.sha256(b).hexdigest()})
        page.close()
    version = browser.version
    browser.close()

check('no page/console/http errors', not errors, errors)
meta = {
    'scope': 'Newly rendered read-only view of recorded 2026-10-07~08 hardware evidence; not live state, not a past production UI screenshot',
    'server': BASE, 'browser': f'Chrome {version}', 'viewport': {'width': 1920, 'height': 1080, 'device_scale_factor': SCALE},
    'mobile_check_viewport': {'width': 390, 'height': 844},
    'data_json_sha256': hashlib.sha256((HERE / 'data.json').read_bytes()).hexdigest(),
    'sources': DATA['provenance']['sources'], 'screenshots': shots, 'checks': checks, 'errors': errors,
}
(OUT / 'capture-metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1) + '\n')
failed = [c for c in checks if not c['ok']]
print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
for c in failed:
    print('FAIL', c['name'], json.dumps(c.get('detail'), ensure_ascii=False)[:400])
sys.exit(1 if failed else 0)
