// Read-only management views over data.json (built by build_data.py from recorded evidence).
(() => {
'use strict';

const LABEL = {kueue: 'K8s · Kueue', kubernetes: 'Kubernetes', slurm: 'Slurm'};
const LANES = [
  {key: 'intake', title: '접수 · 검증', states: ['REQUESTED', 'VALIDATED']},
  {key: 'native', title: '네이티브 제출 · 대기', states: ['SUBMITTING', 'QUEUED']},
  {key: 'run', title: '실행 · 결과 수집', states: ['RUNNING', 'COLLECTING']},
  {key: 'done', title: '완료', states: ['SUCCEEDED', 'FAILED', 'RESULT_INVALID', 'CANCELED']},
];
const STATE_ORDER = ['REQUESTED', 'VALIDATED', 'SUBMITTING', 'QUEUED', 'RUNNING', 'COLLECTING', 'SUCCEEDED'];
const TITLES = {
  overview: ['개요', '두 날짜의 완료 기록 11건 · Kubernetes와 Slurm 실행 경로'],
  resources: ['자원', '기록에서 실행된 장치 8개 · 할당 단위와 완료 워크로드 수'],
  workloads: ['워크로드', '요청 11건의 최종 기록 · 장치·백엔드 필터와 기록된 단계'],
  queue: ['대기열 · 실행', '2026-10-07 저장 스냅샷의 요청 상태 · 보간 없음'],
  results: ['결과', '결과 수신과 Mobilint 하드웨어 측정 기록'],
};
const CAPTIONS = {
  overview: '2026-10-07 fleet_batch 10건과 2026-10-08 Mobilint 1건의 완료 기록 합계입니다.',
  resources: '기록에서 워크로드가 실행된 장치입니다. 공유 GPU 슬롯과 Slurm GRES는 물리 GPU 수로 합산하지 않습니다.',
  workloads: '요청별 최종 기록입니다. R01–R10은 2026-10-07 fleet_batch, R11은 2026-10-08 Mobilint 기록입니다.',
  queue: '선택한 저장 스냅샷의 요청 상태입니다. 스냅샷 사이는 보간하지 않습니다.',
  results: '결과 수신은 요청별 기록이고, 측정값은 Mobilint 1건의 하드웨어 기록입니다.',
};

const $ = sel => document.querySelector(sel);
const h = (tag, attrs, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid instanceof Node ? kid : String(kid));
  return n;
};
const utc = iso => iso.replace('T', ' ').replace(/(\.\d{3})\d*/, '$1').replace(/(\+00:00|Z)$/, '') + ' UTC';
const clock = iso => utc(iso).slice(11, -4);
const fmt = (v, d) => Number(v).toLocaleString('ko-KR', {minimumFractionDigits: d, maximumFractionDigits: d});

let DATA;
const view = {route: 'overview', selected: 'R10', backend: 'all', device: 'all', snapshot: 12};
const devices = () => Object.fromEntries(DATA.devices.map(d => [d.alias, d]));

const backendTag = path => h('span', {class: 'tag ' + (path === 'slurm' ? 'slurm' : 'k8s')}, LABEL[path]);
const classTag = c => h('span', {class: 'tag ' + c}, c.toUpperCase());
const stateEl = s => h('span', {class: 'state ' + s}, s);

function parseHash() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  const [route, arg] = path.split('/');
  view.route = TITLES[route] ? route : 'overview';
  const params = new URLSearchParams(query || '');
  if (view.route === 'workloads') {
    if (arg && DATA.requests.some(r => r.id === arg)) view.selected = arg;
    view.backend = ['kubernetes', 'slurm'].includes(params.get('backend')) ? params.get('backend') : 'all';
    view.device = ['gpu', 'npu'].includes(params.get('device')) ? params.get('device') : 'all';
  }
  if (view.route === 'queue') {
    const i = Number(arg);
    view.snapshot = Number.isInteger(i) && i >= 0 && i < DATA.snapshots.length ? i : DATA.default_snapshot;
  }
}

function workloadsHash() {
  const q = new URLSearchParams();
  if (view.backend !== 'all') q.set('backend', view.backend);
  if (view.device !== 'all') q.set('device', view.device);
  return `#/workloads/${view.selected}` + (q.toString() ? '?' + q : '');
}

function go(hash) { if (location.hash !== hash) history.replaceState(null, '', hash); render(); }

/* ---------- Overview ---------- */
function overview() {
  const s = DATA.summary, k = s.backend.kubernetes, sl = s.backend.slurm;
  const pct = (a, b) => (a / (a + b) * 100) + '%';
  const kpis = h('section', {class: 'kpis', 'aria-label': '완료 기록 요약'},
    h('div', {class: 'kpi'}, h('span', {class: 'kpi-label'}, '완료 기록'),
      h('span', {class: 'kpi-value', 'data-check': 'completed'}, s.completed, h('small', null, '건')),
      h('span', {class: 'kpi-meta'}, `2026-10-07 ${s.dates['2026-10-07']}건 · 2026-10-08 ${s.dates['2026-10-08']}건 · 모두 SUCCEEDED`)),
    h('div', {class: 'kpi'}, h('span', {class: 'kpi-label'}, '백엔드별 완료 요청'),
      h('span', {class: 'kpi-value'}, h('small', {class: 'pre'}, 'K8s'), k, h('small', null, '건'), h('small', {class: 'pre gap'}, 'Slurm'), sl, h('small', null, '건')),
      h('div', {class: 'split', 'aria-hidden': 'true'}, h('i', {class: 'k8s', style: `width:${pct(k, sl)}`}), h('i', {class: 'slurm', style: `width:${pct(sl, k)}`})),
      h('span', {class: 'kpi-meta'}, `Kubernetes ${k}건 (Kueue 경로 ${DATA.requests.filter(r => r.path === 'kueue').length}건) · 별도 Slurm ${sl}건`)),
    h('div', {class: 'kpi'}, h('span', {class: 'kpi-label'}, '실행 노드(누적)'),
      h('span', {class: 'kpi-value'}, s.nodes_cumulative, h('small', null, '개')),
      h('div', {class: 'split', 'aria-hidden': 'true'}, h('i', {class: 'gpu', style: `width:${pct(s.node_classes.gpu, s.node_classes.npu)}`}), h('i', {class: 'npu', style: `width:${pct(s.node_classes.npu, s.node_classes.gpu)}`})),
      h('span', {class: 'kpi-meta'}, `GPU ${s.node_classes.gpu} · NPU ${s.node_classes.npu} · 두 날짜 누적`)),
    h('div', {class: 'kpi'}, h('span', {class: 'kpi-label'}, '결과 · MLflow 수신'),
      h('span', {class: 'kpi-value'}, s.result_receipts, h('small', null, `/ ${s.completed}`)),
      h('span', {class: 'kpi-meta'}, `결과 envelope COMPLETED ${s.result_receipts}건 · MLflow 기록 ${s.mlflow_receipts}건`)));

  const max = Math.max(...DATA.devices.map(d => d.workloads));
  const bars = h('div', {class: 'bars'}, DATA.devices.map(d => h('div', {class: 'bar-row'},
    h('div', {class: 'who'}, h('b', null, d.model), h('small', null, `${d.alias} · ${LABEL[d.path]} · ${d.device_class.toUpperCase()}`)),
    h('div', {class: 'bar-track', role: 'img', 'aria-label': `${d.model} 완료 ${d.workloads}건`}, h('i', {class: d.backend === 'slurm' ? 'slurm' : '', style: `width:${d.workloads / max * 100}%`})),
    h('span', {class: 'n'}, d.workloads))));

  const byDate = d => DATA.requests.filter(r => r.date === d);
  const day1 = byDate('2026-10-07'), day2 = byDate('2026-10-08');
  const cnt = (rows, f) => rows.filter(f).length;
  const timeline = h('div', {class: 'timeline'},
    h('div', {class: 'day'}, h('time', null, '2026-10-07'),
      h('div', {class: 'what'}, h('b', null, 'fleet_batch · 독립 요청 10건'), h('br'),
        `K8s ${cnt(day1, r => r.backend === 'kubernetes')} · Slurm ${cnt(day1, r => r.backend === 'slurm')} · GPU ${cnt(day1, r => r.device_class === 'gpu')} / NPU ${cnt(day1, r => r.device_class === 'npu')} · 저장 스냅샷 ${DATA.snapshots.length}개`),
      h('span', {class: 'count'}, day1.length, h('small', null, '건'))),
    h('div', {class: 'day'}, h('time', null, '2026-10-08'),
      h('div', {class: 'what'}, h('b', null, 'Mobilint ARIES2 · Candy 실행'), h('br'), 'Kubernetes 1 · NPU 1 · 하드웨어 측정 결과 기록'),
      h('span', {class: 'count'}, day2.length, h('small', null, '건'))));

  return [kpis, h('div', {class: 'grid-2'},
    h('section', {class: 'panel'}, h('div', {class: 'panel-head'}, h('h2', null, '장치별 완료 기록'), h('span', {class: 'note'}, '단위: 완료 요청 수')), bars),
    h('section', {class: 'panel'}, h('div', {class: 'panel-head'}, h('h2', null, '기록 날짜'), h('span', {class: 'note'}, 'UTC 기준')), timeline)), flowPanel()];
}

/* ---------- Resources ---------- */
function resources() {
  const rows = DATA.devices.map(d => h('tr', null,
    h('td', null, h('b', null, d.model), h('span', {class: 'sub mono'}, d.alias)),
    h('td', null, classTag(d.device_class)),
    h('td', null, backendTag(d.path)),
    h('td', null, d.architecture || h('span', {class: 'muted'}, '미기록')),
    h('td', null, d.resource ? h('span', {class: 'mono'}, d.resource) : h('span', {class: 'muted'}, '리소스 이름 미기록'), h('span', {class: 'sub'}, d.boundary)),
    h('td', {class: 'num'}, h('b', null, d.workloads)),
    h('td', null, d.request_ids.join(' · ')),
    h('td', null, d.dates.map(x => h('span', {class: 'tag date'}, x.slice(5))))));
  const table = h('section', {class: 'panel'},
    h('div', {class: 'panel-head'}, h('h2', null, '실행된 자원 이력'), h('span', {class: 'note'}, '기록된 실행 기준')),
    h('div', {class: 'table-wrap'}, h('table', null,
      h('thead', null, h('tr', null, ['장치 모델 · 별칭', '유형', '백엔드', '아키텍처', '할당 단위', '완료', '요청', '기록일'].map((t, i) => h('th', {class: i === 5 ? 'num' : null}, t)))),
      h('tbody', null, rows))));

  const kinds = [
    ['nvidia.com/gpu', '전용 GPU', 'Pod가 물리 GPU 1장을 요청합니다. RTX 5060 Ti · RTX 5080 · GB10.'],
    ['nvidia.com/gpu.shared', '공유 GPU 슬롯', 'Jetson의 논리 슬롯입니다. 슬롯 수는 물리 GPU 수가 아닙니다.'],
    ['hailo.ai/h8 · Mobilint', 'NPU 장치', 'Hailo-8과 Mobilint ARIES2는 장치 단위로 요청합니다.'],
    ['gres/gpu', 'Slurm GRES', '별도 Slurm 클러스터의 GRES 단위입니다. K8s GPU와 같은 풀이 아닙니다.'],
  ];
  const legend = h('section', {class: 'panel'},
    h('div', {class: 'panel-head'}, h('h2', null, '할당 단위 경계'), h('span', {class: 'note'}, '서로 다른 단위는 합산하지 않습니다')),
    h('div', {class: 'facts'}, kinds.map(([res, name, text]) => h('div', null, h('dt', {class: 'mono'}, res), h('dd', null, name), h('p', {class: 'muted', style: 'font-size:13px;margin-top:2px'}, text)))));
  return [table, legend];
}

/* ---------- Workloads ---------- */
function filtered() {
  return DATA.requests.filter(r => (view.backend === 'all' || r.backend === view.backend) && (view.device === 'all' || r.device_class === view.device));
}

function seg(label, key, options) {
  return h('div', {class: 'seg', role: 'group', 'aria-label': label}, h('span', {class: 'seg-label'}, label),
    options.map(([v, t]) => h('button', {type: 'button', 'aria-pressed': String(view[key] === v), 'data-filter': `${key}:${v}`,
      onclick: () => { view[key] = v; const rows = filtered(); if (rows.length && !rows.some(r => r.id === view.selected)) view.selected = rows[0].id; go(workloadsHash()); }}, t)));
}

function workloads() {
  const dev = devices(), rows = filtered();
  const body = rows.map(r => {
    const d = dev[r.node];
    return h('tr', {class: 'selectable', 'aria-selected': String(r.id === view.selected), 'data-row': r.id, onclick: () => { view.selected = r.id; go(workloadsHash()); }},
      h('td', null, h('button', {type: 'button', class: 'row-btn', onclick: e => { e.stopPropagation(); view.selected = r.id; go(workloadsHash()); }}, r.id)),
      h('td', null, r.workload),
      h('td', null, d.model, h('span', {class: 'sub mono'}, `${r.node} · ${r.device_class.toUpperCase()}`)),
      h('td', null, backendTag(r.path)),
      h('td', null, r.date),
      h('td', null, stateEl(r.state)),
      h('td', null, r.result ? h('span', {class: 'check'}, r.result.outcome) : h('span', {class: 'muted'}, '없음')),
      h('td', null, r.mlflow_receipt ? h('span', {class: 'check'}, '수신') : h('span', {class: 'muted'}, '미수신')));
  });
  const table = h('section', {class: 'panel'},
    h('div', {class: 'panel-head'}, h('div', {class: 'toolbar'},
      seg('백엔드', 'backend', [['all', '전체'], ['kubernetes', 'K8s'], ['slurm', 'Slurm']]),
      seg('장치', 'device', [['all', '전체'], ['gpu', 'GPU'], ['npu', 'NPU']])),
      h('span', {class: 'count-line', 'data-count': rows.length}, `${rows.length} / ${DATA.requests.length}건 표시`)),
    h('div', {class: 'table-wrap'}, h('table', null,
      h('thead', null, h('tr', null, ['요청', '워크로드', '장치', '백엔드', '기록일', '상태', '결과', 'MLflow'].map(t => h('th', null, t)))),
      h('tbody', null, rows.length ? body : h('tr', null, h('td', {colspan: 8, class: 'muted'}, '조건에 맞는 기록이 없습니다.'))))));
  const sel = DATA.requests.find(r => r.id === view.selected);
  return [h('div', {class: 'grid-detail'}, table, sel && rows.includes(sel) ? detail(sel, dev[sel.node]) : h('aside', {class: 'panel detail'}, h('p', {class: 'muted'}, '행을 선택하면 기록이 표시됩니다.')))];
}

function detail(r, d) {
  const facts = [
    ['장치', [d.model, h('span', {class: 'sub mono'}, r.node)]], ['백엔드', LABEL[r.path]],
    ['가속기 요청', `${r.device_class.toUpperCase()} ${r.accelerator_count}`], ['기록일', r.date],
    ['결과', r.result ? `${r.result.outcome}${r.result.measured ? ' · 측정 기록' : ''}` : '없음'],
    ['MLflow', r.mlflow_receipt ? '수신 기록 있음' : '미수신'],
  ];
  if (r.requested) facts.splice(2, 1, ['요청 자원', `NPU ${r.requested.npu} · CPU ${r.requested.cpu} · ${r.requested.memory_mib} MiB`]);
  const life = r.lifecycle.length
    ? h('div', null, h('p', {class: 'section-title'}, '기록된 단계 (공통 요청 상태)'),
        h('ol', {class: 'life'}, r.lifecycle.map(l => h('li', {class: l.state}, h('span', null, h('b', null, l.state), l.reason ? [' ', h('span', {class: 'reason'}, l.reason)] : null), h('time', null, clock(l.observed_at))))))
    : h('div', null, h('p', {class: 'section-title'}, '기록된 시각 필드 (상태 이력 아님)'),
        h('ol', {class: 'life'}, r.times.map(t => h('li', {class: 'field'}, h('span', null, t.label), h('time', null, clock(t.at))))));
  const native = h('dl', {class: 'kv'},
    h('dt', null, '네이티브 시작'), h('dd', {class: 'mono'}, r.started_at ? utc(r.started_at) : '미기록'),
    h('dt', null, '종료 기록'), h('dd', {class: 'mono'}, r.finished_at ? utc(r.finished_at) : '미기록'));
  return h('aside', {class: 'panel detail', 'aria-label': `${r.id} 상세`, id: 'detail'},
    h('div', null, h('h2', null, `${r.id} · ${r.workload}`),
      h('div', {class: 'idline'}, backendTag(r.path), classTag(r.device_class), stateEl(r.state), h('span', {class: 'tag date'}, r.id === 'R11' ? 'Mobilint · 2026-10-08' : 'fleet_batch · 2026-10-07'))),
    h('dl', {class: 'facts'}, facts.map(([k, v]) => h('div', null, h('dt', null, k), h('dd', null, ...[v].flat())))),
    life, native,
    h('p', {class: 'muted', style: 'font-size:12.5px'}, r.id === 'R11'
      ? 'R11은 합본 표의 표시 번호입니다. 원본 jobs[0]의 식별자는 공개하지 않습니다.'
      : '단계 시각은 저장된 관측 기록이며, 기록 사이 상태는 알 수 없습니다.'));
}

/* ---------- Shared: management flow (architecture, not timed events) ---------- */
function flowPanel() {
  return h('section', {class: 'panel'},
    h('div', {class: 'panel-head'}, h('h2', null, '실행 관리 흐름'), h('span', {class: 'note'}, '구조 설명 · SOURCE-MATERIAL 3.4')),
    h('div', {class: 'flow'},
      h('div', {class: 'node'}, h('span', {class: 'owned'}, '플랫폼'), h('b', null, '요청 · 검증'), h('span', null, '불변 workload·variant, 멱등 키')),
      h('span', {class: 'arrow'}, '→'),
      h('div', {class: 'node'}, h('span', {class: 'owned'}, '플랫폼'), h('b', null, '실행 대상 선택 · adapter'), h('span', null, '호환 후보 → 실행 계획 저장 → route')),
      h('span', {class: 'arrow'}, '→'),
      h('div', {class: 'fork'},
        h('div', {class: 'node k'}, h('span', {class: 'owned'}, '기존 scheduler'), h('b', null, 'K8s Job → Kueue admission → kube-scheduler')),
        h('div', {class: 'node s'}, h('span', {class: 'owned'}, '기존 scheduler'), h('b', null, 'sbatch → Slurm 큐 · Account/QOS'))),
      h('span', {class: 'arrow'}, '→'),
      h('div', {class: 'node'}, h('span', {class: 'owned'}, '플랫폼'), h('b', null, '결과 수집 · 검증'), h('span', null, 'COLLECTING → 결과·원장·MLflow'))));
}

/* ---------- Queue / execution ---------- */
function queue() {
  const snaps = DATA.snapshots, snap = snaps[view.snapshot], dev = devices();
  const t0 = Date.parse(snaps[0].observed_at), span = Date.parse(snaps.at(-1).observed_at) - t0;
  const setSnap = i => go(`#/queue/${i}`);
  const bar = h('section', {class: 'panel snapbar', 'aria-label': '저장 스냅샷 선택'},
    h('div', {class: 'snap-time'}, h('span', null, `관측 시각 · 스냅샷 ${snap.index + 1} / ${snaps.length}`), h('time', {datetime: snap.observed_at, 'data-observed': snap.observed_at}, utc(snap.observed_at))),
    h('div', {class: 'slider'},
      h('input', {type: 'range', min: 0, max: snaps.length - 1, step: 1, value: snap.index, 'aria-label': '저장 스냅샷', 'aria-valuetext': utc(snap.observed_at),
        oninput: e => setSnap(Number(e.target.value))}),
      h('div', {class: 'ticks', 'aria-hidden': 'true'}, snaps.map(s => h('i', {class: s.index === snap.index ? 'on' : null, style: `left:${(Date.parse(s.observed_at) - t0) / span * 100}%`}))),
      h('div', {class: 'slider-meta'}, h('span', null, `${clock(snaps[0].observed_at)} 첫 관측`), h('span', null, snap.title), h('span', null, `${clock(snaps.at(-1).observed_at)} 마지막 관측`))),
    h('div', {class: 'step'},
      h('button', {type: 'button', disabled: snap.index === 0, onclick: () => setSnap(snap.index - 1), 'aria-label': '이전 스냅샷'}, '◀ 이전'),
      h('button', {type: 'button', disabled: snap.index === snaps.length - 1, onclick: () => setSnap(snap.index + 1), 'aria-label': '다음 스냅샷'}, '다음 ▶')));

  const present = STATE_ORDER.filter(s => snap.counts[s] || ['QUEUED', 'RUNNING', 'SUCCEEDED'].includes(s));
  const counts = h('div', {class: 'state-counts', 'aria-label': '공통 요청 상태 집계'},
    present.map(s => h('div', {class: snap.counts[s] ? null : 'zero'}, h('b', null, snap.counts[s] || 0), h('span', null, s))));
  const qo = snap.queue_observation;
  const native = h('div', {class: 'native', 'aria-label': 'Kueue 네이티브 카운터'},
    h('span', null, h('b', null, 'Kueue 네이티브 카운터'), ' · 별도 계층'),
    qo ? h('span', {class: 'vals'}, 'admitted ', h('b', null, qo.admitted), ' · pending ', h('b', null, qo.pending))
       : h('span', {class: 'vals muted'}, '이 스냅샷에 기록 없음'),
    h('span', {class: 'muted'}, '공통 요청 상태와 별도로 집계됩니다.'));

  const ids = Object.keys(snap.requests);
  const board = h('div', {class: 'board'}, LANES.map(lane => {
    const items = ids.filter(id => lane.states.includes(snap.requests[id].state));
    return h('section', {class: 'lane', 'aria-label': lane.title, 'data-lane': lane.key},
      h('div', {class: 'lane-head'}, h('h3', null, lane.title), h('b', null, items.length)),
      h('div', {class: 'lane-list'}, items.length ? items.map(id => {
        const r = snap.requests[id], node = r.native_job_recorded && r.node ? r.node : null;
        return h('article', {class: 'card', 'data-req': id},
          h('span', {class: 'rid'}, id), h('span', {class: 'wl'}, r.workload), stateEl(r.state),
          h('span', {class: 'where'}, backendTag(r.backend === 'kubernetes' ? 'kueue' : r.backend),
            node ? h('span', null, `${['SUBMITTING', 'QUEUED'].includes(r.state) ? '대상 ' : ''}${node} · ${dev[node].model}`) : h('span', null, '노드 배치 미관측'),
            r.reason ? h('span', {class: 'reason'}, r.reason) : null));
      }) : h('p', {class: 'empty'}, '이 스냅샷에 해당 요청 없음')));
  }));

  const flow = flowPanel();

  const note = h('p', {class: 'callout'}, h('b', null, 'R11 Mobilint '), '(2026-10-08 기록)은 이 2026-10-07 스냅샷에 포함되지 않습니다.');
  return [bar, h('div', {class: 'counters'}, counts, native), board, flow, note];
}

/* ---------- Results ---------- */
function results() {
  const m = DATA.requests.find(r => r.id === 'R11'), x = m.measurements, q = m.quality_contract, dev = devices();
  const metric = (label, value, unit, note) => h('div', {class: 'metric'}, h('dt', null, label), h('dd', null, value, unit ? h('small', null, unit) : null), note ? h('p', null, note) : null);
  const hw = h('section', {class: 'panel'},
    h('div', {class: 'panel-head'}, h('h2', null, 'Mobilint ARIES2 · Candy 하드웨어 기록'), h('span', {class: 'note'}, '2026-10-08 · R11 · evidence_kind hardware')),
    h('dl', {class: 'metrics'},
      metric('측정 연산 구간', fmt(x.measured_compute_seconds, 3), '초', `샘플 ${x.sample_count}개 실행의 측정 구간`),
      metric('지연 p50', fmt(x.latency_p50_ms, 2), 'ms', '샘플당 지연의 중앙값'),
      metric('지연 p95', fmt(x.latency_p95_ms, 2), 'ms', `샘플 ${x.sample_count}개 · p99 동일`),
      metric('처리량', fmt(x.throughput, 2), 'work units/s', `${x.work_units} work units ÷ 연산 구간`),
      metric('최대 메모리', fmt(x.peak_memory_mib, 2), 'MiB', `계약 상한 ${fmt(q.maximum_peak_memory_mib, 0)} MiB`),
      metric('반복 출력 원소 일치', fmt(x.quality_value, 1), null, `기준 ≥ ${fmt(q.minimum, 1)} · 최소 ${q.minimum_repeats}회 · 정확도 아님`)),
    h('dl', {class: 'kv', style: 'margin-top:16px'},
      h('dt', null, '실행 조건'), h('dd', null, `Candy 컴파일 모델 · 입력 ${m.runtime.input_shape.join('×')} · ${m.runtime.precision} · ${m.runtime.arch}`),
      h('dt', null, '요청 자원'), h('dd', null, `NPU ${m.requested.npu} · CPU ${m.requested.cpu} · 메모리 ${m.requested.memory_mib} MiB`),
      h('dt', null, '할당 기록'), h('dd', null, `${m.allocation.mode} · 할당 구간 ${m.allocation.interval_seconds}초 (초 단위 해상도) · 사용률이 아닌 예약 기록`),
      h('dt', null, '품질 지표'), h('dd', {class: 'mono'}, q.metric),
      h('dt', null, '미기록'), h('dd', null, '가속기 사용률 · 전력 · 온도'),
      h('dt', null, '재사용 범위'), h('dd', null, `호스트 firmware 미관측으로 성능 프로파일 재사용 보류 (${m.runtime.profile_reuse})`)),
    h('p', {class: 'callout', style: 'margin-top:16px'}, h('b', null, '측정 경계 '), '고정 입력을 반복 실행한 1건의 기록이며, 워크로드가 다른 R01–R10과 속도를 비교하지 않습니다.'));

  const rows = DATA.requests.map(r => h('tr', null,
    h('td', null, h('b', null, r.id)), h('td', null, dev[r.node].model, h('span', {class: 'sub'}, r.workload)),
    h('td', null, backendTag(r.path)), h('td', null, r.result.outcome), h('td', null, r.result.measured ? h('span', {class: 'check'}, '예') : '아니오'),
    h('td', null, r.mlflow_receipt ? h('span', {class: 'check'}, '수신') : h('span', {class: 'muted'}, '미수신'))));
  const receipts = h('section', {class: 'panel'},
    h('div', {class: 'panel-head'}, h('h2', null, '결과 수신'), h('span', {class: 'note'}, `COMPLETED ${DATA.summary.result_receipts} · MLflow ${DATA.summary.mlflow_receipts} / ${DATA.summary.completed}`)),
    h('div', {class: 'table-wrap'}, h('table', null,
      h('thead', null, h('tr', null, ['요청', '장치 · 워크로드', '백엔드', '결과', '측정', 'MLflow'].map(t => h('th', null, t)))),
      h('tbody', null, rows))));
  return [h('div', {class: 'grid-2'}, hw, receipts)];
}

/* ---------- Shell ---------- */
const VIEWS = {overview, resources, workloads, queue, results};

function sources() {
  const p = DATA.provenance.sources;
  return ['출처: ', h('a', {href: 'data.json'}, 'data.json'), ' · ', h('a', {href: p[0].path}, 'replay/observations.json'), ' · ',
    h('a', {href: p[0].manifest}, 'PROVENANCE.json'), ' · ', h('a', {href: p[1].url}, `Mobilint jobs[0] @ ${p[1].commit.slice(0, 7)}`)];
}

function render() {
  parseHash();
  const [title, sub] = TITLES[view.route];
  $('#page-title').textContent = title;
  $('#page-sub').textContent = sub;
  document.title = `HAIRP ${title} · 실증 기록`;
  document.querySelectorAll('.nav a').forEach(a => a.dataset.route === view.route ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'));
  $('#view').replaceChildren(...VIEWS[view.route]());
  $('#view').dataset.route = view.route;
  $('#caption-text').textContent = CAPTIONS[view.route];
  $('#caption-sources').replaceChildren(...sources());
}

fetch('data.json').then(r => { if (!r.ok) throw new Error('data.json ' + r.status); return r.json(); }).then(d => {
  DATA = d;
  window.__management = {data: DATA, view};
  addEventListener('hashchange', render);
  render();
  document.documentElement.dataset.ready = 'true';
}).catch(err => {
  $('#view').replaceChildren(h('section', {class: 'panel'}, h('h2', null, '기록 데이터를 불러오지 못했습니다'), h('p', {class: 'muted'}, String(err))));
  throw err;
});
})();
