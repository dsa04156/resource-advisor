"""Checks for the sanitized gang replay (gang-observations.json).

Public-only checks always run. Raw-comparison checks run when the private record is given:
  NAIS_RAW_GANG=<RAW_GANG_BATCH_JSON> \
  rtk proxy uv run --isolated --with pytest pytest docs/nais-interview/replay/test_gang_replay.py -q
"""
from pathlib import Path
from datetime import datetime
import hashlib, json, os, re, sys
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import export_gang_replay as eg  # noqa: E402

MATERIALS = HERE.parent
OUT = HERE / 'gang-observations.json'
DATA = json.loads(OUT.read_text())
PROV = json.loads((HERE / 'gang-PROVENANCE.json').read_text())
SC = DATA['scenario']
FRAMES = SC['frames']
ADAPTIVE = json.loads((HERE / 'observations.json').read_text())
RAW_PATH = os.environ.get('NAIS_RAW_GANG')
needs_raw = pytest.mark.skipif(not RAW_PATH, reason='private raw record not provided')
ACTIVE = {'ADMITTED', 'RUNNING', 'FINALIZING', 'SUCCEEDED'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ts(value):
    return datetime.fromisoformat(value)


def raw():
    return json.loads(Path(RAW_PATH).read_text())


def test_standalone_shape_and_ordered_frames():
    assert DATA['schema'] == eg.SCHEMA and DATA['scenario_key'] == 'gang'
    assert set(DATA) == {'schema', 'exporter_version', 'notice', 'time_zone', 'scenario_key', 'nodes', 'provenance', 'scenario'}
    assert [f['index'] for f in FRAMES] == list(range(len(FRAMES))) and len(FRAMES) == 19
    times = [ts(f['observed_at']) for f in FRAMES]
    assert times == sorted(times)
    assert [f['elapsed_seconds'] for f in FRAMES] == [round((t - times[0]).total_seconds(), 3) for t in times]
    assert SC['rows'] == [f'R{i:02d}' for i in range(1, 11)] and FRAMES[0]['requests'] == {}


def test_compatible_with_adaptive_scenario_shape():
    adaptive = ADAPTIVE['scenarios']['adaptive']
    assert set(adaptive) - {'frames'} <= set(SC)
    a_final = adaptive['frames'][-1]
    assert set(a_final) - {'cleanup'} <= set(FRAMES[-1])
    a_req, a_pod = a_final['requests']['R01'], a_final['requests']['R01']['pods'][0]
    for f in FRAMES:
        assert f['priority_changes'] == []
        for r in f['requests'].values():
            assert set(a_req) <= set(r) and r['previous_attempts'] == [] and r['excluded_nodes'] == []
            for p in r['pods']:
                assert set(a_pod) <= set(p)


def test_nodes_reuse_published_aliases_and_models():
    published = ADAPTIVE['nodes']
    assert set(DATA['nodes']) == {'GPU-A', 'GPU-B', 'GPU-C'}
    assert all(DATA['nodes'][a] == published[a] for a in DATA['nodes'])
    table = dict(re.findall(r'^\| (GPU-[A-C]) \| (.+?) \|$', (HERE / 'DEVICE-MODELS.md').read_text(), re.M))
    assert {a: m.replace('GeForce ', '') for a, m in DATA['provenance']['recorded_device_models'].items()} == table
    used = {p['node'] for f in FRAMES for r in f['requests'].values() for p in r['pods']} - {None}
    assert used == set(DATA['nodes'])


def test_counts_agree_with_public_summary():
    summary = json.loads((MATERIALS / SC['public_summary']).read_text())['gang_batch']
    c = PROV['extracted_counts']
    assert c == eg.counts(DATA)
    assert c['frames'] == summary['node_observations'] == 19
    assert c['final_succeeded'] == summary['requests'] == 10 and SC['recorded_state'] == summary['state']
    assert c['two_gpu_groups'] == summary['two_gpu_groups'] == 3
    assert c['succeeded_worker_pods'] == summary['worker_results'] == 13
    assert c['correct_worker_pods'] == summary['correctness_worker_results'] == 13
    assert c['barrier_worker_pods'] == summary['barrier_worker_results'] == 6
    q = c['first_nonzero_queue_observation']
    assert (q['admitted'], q['pending']) == (summary['running_at_queue_observation'], summary['pending_at_queue_observation'])
    assert FRAMES[-1]['cleanup'] == summary['cleanup']


def test_whole_gang_admission_and_worker_barrier():
    for f in FRAMES:
        for r in f['requests'].values():
            g = r['gang']
            assert g['pod_count'] == r['gpu']
            if r['state'] == 'PENDING':
                assert g['admitted_pod_count'] is None and r['pods'] == []  # reason may be unrecorded (null)
            if r['state'] in ACTIVE:
                assert g['admitted_pod_count'] == g['pod_count'] and g['admitted_at']
            if r['gpu'] == 2 and r['pods'] and all('BARRIER_RELEASED' in p['phases'] for p in r['pods']):
                times = {p['rank']: {t['phase']: ts(t['at']) for t in p['phase_times']} for p in r['pods']}
                ready = max(t['GPU_READY'] for t in times.values())
                assert all(t['BARRIER_RELEASED'] >= ready and t['COMPUTE_STARTED'] >= t['BARRIER_RELEASED'] for t in times.values())
                assert {p['barrier_workers'] for p in r['pods']} == {2}
    # 2-GPU gangs waited for two free GPUs while 1-GPU requests were admitted.
    pending = [r for f in FRAMES for r in f['requests'].values() if r['state'] == 'PENDING' and r['gpu'] == 2]
    assert any(r['reason'] == '공통 GPU 풀 쿼터 부족 · 2개 더 필요' for r in pending)
    assert {f['requests'][k]['priority'] for f in FRAMES for k in f['requests']} == {0}


def test_no_private_strings_in_public_files():
    for path in (OUT, HERE / 'gang-PROVENANCE.json'):
        text = path.read_text()
        assert not re.search(r'/home/|https?://|request-\d|native_uid|clusterQueue|local_queue|\b[0-9a-f]{8}-[0-9a-f]{4}-', text), path.name


def test_manifest_hashes_and_existing_payload_untouched():
    assert PROV['output']['sha256'] == sha(OUT)
    assert PROV['raw_inputs'][0]['sha256'] == DATA['provenance']['raw_sha256']
    for ref in PROV['public_summary_references']:
        assert ref['sha256'] == sha((HERE.parent / ref['path_from_materials']).resolve()
                                    if ref['path_from_materials'].startswith('..') else MATERIALS / ref['path_from_materials'])
    old = json.loads((HERE / 'PROVENANCE.json').read_text())
    assert sha(HERE / 'observations.json') == old['output']['sha256']


@needs_raw
def test_raw_matches_export_and_is_reproducible(tmp_path):
    r = raw()
    assert sha(RAW_PATH) == PROV['raw_inputs'][0]['sha256']
    events = r['body']['events']
    assert len(events) == len(FRAMES)
    _, again = eg.export(RAW_PATH)
    alias = {}
    for event, frame in zip(events, FRAMES):
        assert ts(frame['observed_at']) == ts(event['observed_at'])
        s = event['snapshot']
        assert (frame.get('queue_observation') or None) == ({k: s['pool_observation'][k] for k in ('admitted', 'pending')}
                                                           if s.get('pool_observation') else None)
        assert len(frame['requests']) == len(s.get('jobs', []))
        for job in s.get('jobs', []):
            out = frame['requests'][f'R{job["label"][-2:]}']
            assert (out['state'], out['gpu'], out['priority']) == (job['state'], job['gpu'], job['priority'])
            assert ts(out['submitted_at']) == ts(job['submitted_at'].replace('Z', '+00:00'))
            assert (out['reason'] is None) == (job['reason'] is None)
            for rp, op in zip(job['pods'], out['pods'], strict=True):
                assert alias.setdefault(rp['node'], op['node']) == op['node'] if rp.get('node') else op['node'] is None
                assert (op['rank'], op['state'], op['exit_codes']) == (int(rp['rank']), rp['state'], rp['exit_codes'])
                assert op['phases'] == [e['phase'] for e in rp.get('events') or []]
    assert len(set(alias.values())) == len(alias) == 3
    text = OUT.read_text().replace(SC['public_summary'], '')  # published evidence file name
    secrets = set(alias) | {r['ref'], r['project']}
    for event in events:
        s = event['snapshot']
        secrets |= {s.get('queue_scope', {}).get('local_queue'), (s.get('pool_observation') or {}).get('ref')}
        for job in s.get('jobs', []):
            secrets |= {job['id'], job['native_uid'], job['label'], (job.get('admission') or {}).get('clusterQueue')}
            secrets |= {p['name'] for p in job['pods']}
        for inv in s.get('resource_observation', {}).get('inventory', []):
            secrets |= {inv['ref'], inv['cluster_ref']} | {n['node_ref'] for n in inv.get('nodes', [])}
    secrets.discard(None)
    assert not [x for x in secrets if x in text]
    eg_out = tmp_path / 'gang-observations.json'
    eg_out.write_text(json.dumps(again, ensure_ascii=False, indent=1) + '\n')
    assert eg_out.read_bytes() == OUT.read_bytes()
