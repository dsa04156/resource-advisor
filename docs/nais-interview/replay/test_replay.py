"""Checks for the sanitized replay data and the offline demo page.

Public-only checks always run. Raw-comparison checks run when the private records are given:
  NAIS_RAW_FLEET=<RAW_FLEET_BATCH_JSON> NAIS_RAW_ADAPTIVE=<RAW_ADAPTIVE_BATCH_JSON> \
  rtk proxy uv run --isolated --with pytest pytest docs/nais-interview/replay -q
"""
from pathlib import Path
from datetime import datetime
import hashlib, json, os, re
import pytest

HERE = Path(__file__).resolve().parent
MATERIALS = HERE.parent
DATA = json.loads((HERE / 'observations.json').read_text())
PROV = json.loads((HERE / 'PROVENANCE.json').read_text())
FLEET, ADAPTIVE = DATA['scenarios']['fleet'], DATA['scenarios']['adaptive']
RAW_PATHS = {'fleet': os.environ.get('NAIS_RAW_FLEET'), 'adaptive': os.environ.get('NAIS_RAW_ADAPTIVE')}
needs_raw = pytest.mark.skipif(not all(RAW_PATHS.values()), reason='private raw records not provided')
MOBILINT_SOURCE_URL = 'https://github.com/dsa04156/resource-advisor/blob/a5d1dde87b56ba2794dc6a93559342eb85f1940f/docs/evidence/all-accelerators-20261008-v1.json'
PUBLISHED = [HERE / 'observations.json', HERE / 'PROVENANCE.json', HERE / 'export_replay.py', MATERIALS / 'offline-demo.html']


def summary(rel):
    return json.loads((MATERIALS / rel).read_text())


def raw(kind):
    return json.loads(Path(RAW_PATHS[kind]).read_text())


def ts(value):
    return datetime.fromisoformat(value)


def test_frames_are_ordered_recorded_observations():
    for sc in (FLEET, ADAPTIVE):
        times = [ts(f['observed_at']) for f in sc['frames']]
        assert times == sorted(times)
        assert [f['index'] for f in sc['frames']] == list(range(len(sc['frames'])))
        assert sc['rows'] == [f'R{i:02d}' for i in range(1, 11)]


def test_first_frame_has_no_request_state():
    for sc in (FLEET, ADAPTIVE):
        assert sc['frames'][0]['requests'] == {}


def test_counts_agree_with_public_summaries():
    fleet, adaptive = summary(FLEET['public_summary']), summary(ADAPTIVE['public_summary'])
    final = FLEET['frames'][-1]
    assert len(FLEET['frames']) == fleet['node_observations'] == 24
    assert sum(r['state'] == 'SUCCEEDED' for r in final['requests'].values()) == fleet['results'] == 10
    assert sum(r['device_class'] == 'gpu' for r in final['requests'].values()) == fleet['gpu_requests'] == 8
    assert sum(r['device_class'] == 'npu' for r in final['requests'].values()) == fleet['npu_requests'] == 2
    assert sum(r['mlflow_receipt'] for r in final['requests'].values()) == fleet['mlflow_run_receipts'] == 10
    assert len(final['execution_coverage']['gpu']) == fleet['gpu_execution_nodes'] == 6
    assert len(final['execution_coverage']['npu']) == fleet['npu_execution_nodes'] == 1
    assert sorted({r['backend'] for r in final['requests'].values()}) == fleet['backends']

    last = ADAPTIVE['frames'][-1]
    assert len(ADAPTIVE['frames']) == adaptive['replay']['events'] == 52
    assert len(last['native_attempts']) == adaptive['native_attempts'] == 11
    assert len(last['priority_changes']) == adaptive['priority']['native_promotions'] == 6
    assert {(c['before'], c['after']) for c in last['priority_changes']} == {(adaptive['priority']['from'], adaptive['priority']['to'])}
    assert sum(p['state'] == 'Succeeded' for r in last['requests'].values() for p in r['pods']) == adaptive['correct_cuda_worker_results'] == 13
    assert sum(r['gpu'] == 2 for r in last['requests'].values()) == adaptive['successful_two_gpu_requests'] == 3
    assert last['elapsed_seconds'] == adaptive['observed_run_elapsed_seconds'] == 218.1
    failed = [a for a in last['native_attempts'] if a['state'] == 'FAILED']
    assert len(failed) == adaptive['native_failure_injection']['attempts'] == 1
    assert failed[0]['exit_codes'] == [adaptive['native_failure_injection']['exit_code']] == [42]


def test_first_native_creation_offsets_match_public_summary():
    last = ADAPTIVE['frames'][-1]['requests']
    first = {row: ts((r['previous_attempts'] or [r])[0]['submitted_at']) for row, r in last.items()}
    origin = first['R01']
    offsets = [int((first[row] - origin).total_seconds()) for row in ADAPTIVE['rows']]
    assert offsets == summary(ADAPTIVE['public_summary'])['arrival']['actual_first_native_creation_offset_seconds']


def test_retry_stays_on_one_logical_row_with_distinct_attempts():
    last = ADAPTIVE['frames'][-1]['requests']
    retried = {row: r for row, r in last.items() if r['previous_attempts']}
    assert list(retried) == ['R02']
    r = retried['R02']
    old = r['previous_attempts'][0]
    assert (old['attempt'], old['state'], r['attempt'], r['state']) == ('R02·a1', 'FAILED', 'R02·a2', 'SUCCEEDED')
    assert old['pods'][0]['exit_codes'] == [42] and old['pods'][0]['phases'] == ['EXPECTED_FAILURE']
    assert r['excluded_nodes'] == [old['pods'][0]['node']]
    assert {p['node'] for p in r['pods']}.isdisjoint(r['excluded_nodes'])
    # Every frame keeps exactly ten logical rows once requests appear; the retry never adds a row.
    for f in ADAPTIVE['frames'][1:]:
        assert sorted(f['requests']) == ADAPTIVE['rows']


def test_failure_is_absent_before_it_was_recorded():
    def failed(f):
        history = [a for r in f['requests'].values() for a in r.get('previous_attempts', [])]
        return any(a['state'] == 'FAILED' for a in history + f.get('native_attempts', []))
    flags = [failed(f) for f in ADAPTIVE['frames']]
    first = flags.index(True)
    assert ADAPTIVE['frames'][first]['title'].startswith('실패 기록 보존')
    assert all(flags[first:]), 'failure evidence disappeared after being recorded'
    assert not any(r['state'] == 'SUCCEEDED' for f in ADAPTIVE['frames'][:first] for r in f['requests'].values() if r['previous_attempts'])


def test_node_aliases_are_consistent_across_scenarios():
    pattern = re.compile(r'(GPU|JET|NPU|SLURM)-[A-Z]')
    assert all(pattern.fullmatch(a) for a in DATA['nodes'])
    used = set()
    for sc in (FLEET, ADAPTIVE):
        for f in sc['frames']:
            for r in f['requests'].values():
                used.update(n for n in [r.get('node')] + r.get('excluded_nodes', []) if n)
                for a in [r] + r.get('previous_attempts', []):
                    used.update(p['node'] for p in a.get('pods', []))
            for nodes in f.get('execution_coverage', {}).values():
                used.update(nodes)
    assert used == set(DATA['nodes'])
    fleet_nodes = {r['node'] for r in FLEET['frames'][-1]['requests'].values()}
    assert fleet_nodes == set(FLEET['frames'][-1]['execution_coverage']['gpu']) | set(FLEET['frames'][-1]['execution_coverage']['npu'])
    adaptive_nodes = {p['node'] for r in ADAPTIVE['frames'][-1]['requests'].values() for p in r['pods']}
    assert adaptive_nodes <= {'GPU-A', 'GPU-B', 'GPU-C'} and adaptive_nodes <= fleet_nodes


FORBIDDEN = [r'etri-', r'hairp', r'\blab-[0-9a-f]', r'ra-lab', r'https?://', r'/home/', r'\.state/', r'sha256:', r'\bj-[0-9a-f]{8}',
             r'\ba-[0-9a-f]{8}', r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', r'ghcr\.io|docker\.io|registry',
             r'kubectl|sbatch |ssh ', r'password|secret|token', r'slurm-w\d|slurm-gpu-lab', r'request-\d{2}', r'cuda-smoke|e5-kernel|disjoint100']


@pytest.mark.parametrize('path', PUBLISHED, ids=lambda p: p.name)
def test_published_files_have_no_private_markers(path):
    text = path.read_text()
    if path.name == 'offline-demo.html':
        # The Mobilint evidence link is a public URL pinned to a fixed commit; only this exact URL is exempt.
        text = text.replace(MOBILINT_SOURCE_URL, '')
    if path.name == 'export_replay.py':
        # The exporter legitimately names raw field patterns it maps away; only check for leaked values.
        patterns = [r'etri-', r'\blab-[0-9a-f]', r'/home/', r'\.state/', r'slurm-w\d|slurm-gpu-lab', r'[0-9a-f]{32}']
    else:
        patterns = FORBIDDEN
    hits = [p for p in patterns if re.search(p, text, re.I)]
    assert not hits, f'{path.name}: {hits}'


@needs_raw
def test_raw_hashes_match_provenance():
    for item in PROV['raw_inputs']:
        assert hashlib.sha256(Path(RAW_PATHS[item['role']]).read_bytes()).hexdigest() == item['sha256']
    assert hashlib.sha256((HERE / 'observations.json').read_bytes()).hexdigest() == PROV['output']['sha256']


@needs_raw
def test_frames_copy_raw_states_without_interpolation():
    for kind, sc in (('fleet', FLEET), ('adaptive', ADAPTIVE)):
        events = raw(kind)['body']['events']
        assert len(events) == len(sc['frames'])
        for event, frame in zip(events, sc['frames']):
            assert ts(frame['observed_at']) == ts(event['observed_at'])
            states = {f"R{re.search(r'(?:요청 |request-)(\d{2})', j['label']).group(1)}": j['state'] for j in event['snapshot'].get('jobs', [])}
            assert {row: r['state'] for row, r in frame['requests'].items()} == states
            for r in frame['requests'].values():
                for e in r.get('lifecycle', []):
                    assert ts(e['observed_at']) <= ts(frame['observed_at'])


@needs_raw
def test_no_raw_identifier_string_reaches_published_files():
    ident = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+-]{5,}$')
    iso = re.compile(r'^\d{4}-\d{2}-\d{2}T')
    allowed = {'nvidia.com/gpu', 'nvidia.com/gpu.shared', 'hailo.ai/h8', 'gres/gpu', 'aarch64', 'kubernetes', 'SchedulingGated',
               'AssocGrpCpuLimit', 'ContainerCreating', 'NOT_SUBMITTED', 'SUCCEEDED', 'COLLECTING', 'FINALIZING', 'VALIDATED', 'REQUESTED',
               'SUBMITTING', 'REQUEUED', 'ADMITTED', 'EXECUTION', 'ARRIVALS', 'FINISHED', 'Succeeded', 'Running', 'Pending', 'GPU_READY',
               'COMPUTE_STARTED', 'COMPUTE_FINISHED', 'BARRIER_RELEASED', 'EXPECTED_FAILURE', 'RUNNING', 'PENDING', 'QUEUED', 'SUBMIT',
               'completed', 'recorded', 'hardware', 'Hailo-8', 'ResNet-50', 'COMPLETED', 'FAILED', 'Failed'}

    def strings(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from strings(v)
        elif isinstance(o, str):
            yield o

    private = set()
    for kind in RAW_PATHS:
        for s in strings(raw(kind)):
            for token in re.split(r'[\s,;"\'()\[\]{}=]+', s):
                token = token.strip('.:')
                if ident.match(token) and not iso.match(token) and token not in allowed and re.search(r'[\d-]', token) \
                        and not re.fullmatch(r'[\d.e+-]+', token):
                    private.add(token)
    assert len(private) > 100
    for path in [HERE / 'observations.json', HERE / 'PROVENANCE.json', MATERIALS / 'offline-demo.html']:
        text = path.read_text()
        leaked = sorted(t for t in private if t in text and not re.fullmatch(r'v\d+|\d+[a-z]+|[a-z]+\d', t))
        assert not leaked, f'{path.name}: {leaked[:10]}'


def test_offline_demo_embeds_payloads_unchanged():
    html = (MATERIALS / 'offline-demo.html').read_text()
    template = (HERE / 'offline-demo.template.html').read_text()
    embed = lambda o: json.dumps(o, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    gang_path = HERE / 'gang-observations.json'
    gang = json.loads(gang_path.read_text()) if gang_path.exists() else None
    base = re.search(r'<script type="application/json" id="observations">(.*?)</script>', html, re.S).group(1)
    assert base == embed(DATA)
    assert html == template.replace('__OBSERVATIONS_JSON__', embed(DATA)).replace('__GANG_JSON__', embed(gang))
