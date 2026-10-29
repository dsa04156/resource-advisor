"""Write simulation-observations.json: a deterministic, fully synthetic 30-request queue for the replay console.

Nothing is submitted and no cluster is called. Virtual nodes have a declared simulation capacity only; times are a
virtual clock (T0 = 2000-01-01T00:00:00Z, not a real date). The rule is simple and stated on screen: waiting requests
are ordered by priority, then arrival, and each starts on the best-fitting virtual node that has room (a 2-GPU group
needs both GPUs on one node).
Run: rtk proxy python3 docs/nais-interview/replay/generate_simulation.py
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

REPLAY = Path(__file__).resolve().parent
OUT = REPLAY / 'simulation-observations.json'
T0 = datetime(2000, 1, 1, tzinfo=timezone.utc)
NODES = {'SIM-N1': 4, 'SIM-N2': 4, 'SIM-N3': 2, 'SIM-N4': 2}
COUNT = 30
PRIORITY = [100, 50, 10]


def iso(sec):
    return (T0 + timedelta(seconds=sec)).isoformat()


def plan():
    """Deterministic request table: arrival, GPU group size, priority, duration (virtual seconds)."""
    reqs = {}
    for i in range(COUNT):
        row = f'R{i + 1:02d}'
        arrival = 2 + i * 3 if i < 8 else 26 + (i - 8) * 4
        gpu = 2 if i % 3 == 2 else 1
        priority = PRIORITY[(i * 7) % 3] if i >= 8 else 50
        duration = 18 + (i * 5) % 16 if i < 8 else 70 + (i * 37) % 90
        reqs[row] = {'arrival': arrival, 'gpu': gpu, 'priority': priority, 'duration': duration}
    return reqs


def simulate(reqs):
    free = dict(NODES)
    state = {r: {'state': 'NOT_SUBMITTED', 'node': None, 'started': None, 'finished': None, 'reason': None} for r in reqs}
    times = sorted({0} | {q['arrival'] for q in reqs.values()})
    frames, now, guard = [], 0, 0
    while True:
        guard += 1
        assert guard < 1000
        events = []
        for r, s in state.items():
            if s['state'] == 'RUNNING' and s['started'] + reqs[r]['duration'] <= now:
                s.update(state='SUCCEEDED', finished=now); free[s['node']] += reqs[r]['gpu']; events.append(('done', r))
        for r, s in state.items():
            if s['state'] == 'NOT_SUBMITTED' and reqs[r]['arrival'] <= now:
                s['state'] = 'PENDING'; events.append(('arrive', r))
        waiting = sorted((r for r, s in state.items() if s['state'] == 'PENDING'), key=lambda r: (-reqs[r]['priority'], reqs[r]['arrival'], r))
        for r in waiting:
            need = reqs[r]['gpu']
            fits = sorted((n for n in NODES if free[n] >= need), key=lambda n: (free[n], n))
            if fits:
                n = fits[0]; free[n] -= need
                state[r].update(state='RUNNING', node=n, started=now, reason=None); events.append(('start', r))
            else:
                state[r]['reason'] = f'가상 GPU 여유 없음 · {need}-GPU 필요' if sum(free.values()) < need else '한 가상 노드에 2-GPU 여유 없음'
        frames.append(frame(now, reqs, state, free, events))
        if all(s['state'] == 'SUCCEEDED' for s in state.values()):
            return frames
        ends = [s['started'] + reqs[r]['duration'] for r, s in state.items() if s['state'] == 'RUNNING']
        now = min([x for x in times if x > now] + ends)


def frame(now, reqs, state, free, events):
    requests = {}
    for r, s in state.items():
        q = reqs[r]
        requests[r] = {'workload': f'합성 작업 · {q["gpu"]}-GPU ' + ('그룹' if q['gpu'] > 1 else '단일'), 'device_class': 'gpu', 'backend': 'sim',
                       'state': s['state'], 'reason': s['reason'] if s['state'] == 'PENDING' else None, 'gpu': q['gpu'], 'priority': q['priority'],
                       'planned_arrival_seconds': q['arrival'], 'duration_seconds': q['duration'],
                       'submitted_at': iso(q['arrival']) if s['state'] != 'NOT_SUBMITTED' else None, 'node': s['node'],
                       'started_at': iso(s['started']) if s['started'] is not None else None, 'finished_at': iso(s['finished']) if s['finished'] is not None else None}
    label = {'arrive': '도착', 'start': '시작', 'done': '완료'}
    counts = [f'{label[k]} {sum(1 for e, _ in events if e == k)}' for k in label if any(e == k for e, _ in events)]
    return {'observed_at': iso(now), 'title': '가상 프레임 · ' + (' · '.join(counts) if counts else '모든 요청 도착 전' if now == 0 else '변화 없음'), 'requests': requests,
            'allocated_gpu': {n: NODES[n] - free[n] for n in NODES}}


def build():
    reqs = plan()
    frames = simulate(reqs)
    for i, f in enumerate(frames):
        f['index'] = i
    counts = lambda f: {k: sum(r['state'] == k for r in f['requests'].values()) for k in ('NOT_SUBMITTED', 'RUNNING', 'SUCCEEDED', 'PENDING')}
    # Default view: first frame where every request has arrived and >=10 each run, succeeded and wait at once.
    initial = next(i for i, f in enumerate(frames) if counts(f)['NOT_SUBMITTED'] == 0 and counts(f)['RUNNING'] >= 10 and counts(f)['SUCCEEDED'] >= 10 and counts(f)['PENDING'] >= 10)
    return {
        'schema': 'nais-offline-simulation/v1', 'synthetic': True, 'generator': 'replay/generate_simulation.py',
        'notice': '합성 시뮬레이션 데이터. 저장된 실행 기록이 아니며, 실제 작업 제출·클러스터 호출·측정은 없다. 시각은 가상 시계(T0 기준 초)이다.',
        'time_zone': '가상 시계',
        'nodes': {n: {'backend': 'sim', 'label': '가상 노드 (합성)', 'accelerator': f'가상 GPU 슬롯 {c}개 (선언 용량)', 'architecture': '실제 하드웨어 아님', 'capacity_gpu': c} for n, c in NODES.items()},
        'scenario': {'name': f'{COUNT}건 시뮬레이션 (합성)', 'run_alias': 'sim-30', 'project_alias': '합성 데이터', 'recorded_state': '해당 없음 (합성)',
                     'scope': f'가상 노드 {len(NODES)}대(선언 용량 GPU {sum(NODES.values())}개)에 합성 요청 {COUNT}건. 규칙: 우선순위 → 도착 순, 여유가 맞는 가상 노드에 배정, 2-GPU 그룹은 한 노드. 실제 실행 기록이 아니다.',
                     'public_summary': None, 'initial_index': initial, 'rows': list(reqs), 'frames': frames},
    }


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n'


if __name__ == '__main__':
    OUT.write_text(dump(build()))
