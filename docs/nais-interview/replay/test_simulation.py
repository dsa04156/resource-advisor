"""Checks for the synthetic 30-request simulation scenario (generate_simulation.py -> simulation-observations.json).

Run: rtk proxy uv run --isolated --with pytest pytest docs/nais-interview/replay/test_simulation.py -q
"""
from pathlib import Path
import json, re, sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate_simulation as gen

SIM = json.loads((HERE / 'simulation-observations.json').read_text())
SC = SIM['scenario']
FRAMES = SC['frames']
ORDER = ['NOT_SUBMITTED', 'PENDING', 'RUNNING', 'SUCCEEDED']
REAL_NODES = set(json.loads((HERE / 'observations.json').read_text())['nodes']) | {'NPU-B'}


def counts(f):
    return {k: sum(r['state'] == k for r in f['requests'].values()) for k in ORDER}


def test_file_is_reproducible_from_generator():
    assert (HERE / 'simulation-observations.json').read_text() == gen.dump(gen.build())


def test_marked_synthetic():
    assert SIM['synthetic'] is True and '합성' in SIM['notice'] and '실행 기록이 아니' in SIM['notice']
    assert '합성' in SC['name'] and SC['public_summary'] is None
    assert FRAMES[0]['observed_at'].startswith('2000-01-01')  # virtual clock, not a real date


def test_thirty_unique_logical_requests_in_every_frame():
    assert len(SC['rows']) == len(set(SC['rows'])) == 30
    assert all(sorted(f['requests']) == sorted(SC['rows']) for f in FRAMES)


def test_initial_frame_is_busy_mix():
    c = counts(FRAMES[SC['initial_index']])
    assert c['NOT_SUBMITTED'] == 0 and c['RUNNING'] >= 5 and c['SUCCEEDED'] >= 5 and c['PENDING'] >= 10 and sum(c.values()) == 30


def test_capacity_never_overbooked_and_allocation_matches():
    for f in FRAMES:
        used = {}
        for r in f['requests'].values():
            if r['state'] == 'RUNNING':
                used[r['node']] = used.get(r['node'], 0) + r['gpu']
        for n, meta in SIM['nodes'].items():
            assert used.get(n, 0) <= meta['capacity_gpu']
            assert f['allocated_gpu'][n] == used.get(n, 0)


def test_states_progress_waiting_running_completed():
    for row in SC['rows']:
        seq = [ORDER.index(f['requests'][row]['state']) for f in FRAMES]
        assert seq == sorted(seq) and seq[0] == 0 and seq[-1] == 3
        nodes = {f['requests'][row]['node'] for f in FRAMES} - {None}
        assert len(nodes) == 1  # one coherent placement per request, kept after completion
    assert sum(any(f['requests'][r]['state'] == 'PENDING' for f in FRAMES) for r in SC['rows']) >= 10


def test_frames_ordered_and_deterministic_groups_priorities():
    times = [f['observed_at'] for f in FRAMES]
    assert times == sorted(times) and len(set(times)) == len(times)
    last = FRAMES[-1]['requests'].values()
    assert {r['gpu'] for r in last} == {1, 2} and len({r['priority'] for r in last}) >= 2
    assert all(SIM['nodes'][r['node']]['capacity_gpu'] >= r['gpu'] for r in last)


def test_virtual_nodes_do_not_reuse_lab_aliases_or_claim_measurement():
    assert not set(SIM['nodes']) & REAL_NODES
    for meta in SIM['nodes'].values():
        assert meta['backend'] == 'sim' and '가상' in meta['label'] and '선언' in meta['accelerator'] and '아님' in meta['architecture']
    text = json.dumps(SIM, ensure_ascii=False)
    assert not re.search(r'NVIDIA|Jetson|Hailo|Mobilint|ARIES|RTX|GB10|Kueue|kubectl|sbatch', text, re.I)


def test_page_embeds_simulation_as_default():
    html = (HERE.parent / 'offline-demo.html').read_text()
    body = re.search(r'<script type="application/json" id="simulation">(.*?)</script>', html, re.S).group(1)
    assert json.loads(body) == SIM
