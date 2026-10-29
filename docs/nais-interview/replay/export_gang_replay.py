"""Export a sanitized gang-admission replay from one private gang_batch run record.

Standalone companion to export_replay.py: observations.json is not touched. Only allowlisted
structured fields are copied through the same helpers (enums, Kueue reason categories, node
aliases). Node aliases must match the recorded CUDA model of the published GPU-A/B/C aliases
(DEVICE-MODELS.md), otherwise the export fails. One frame per recorded event; no interpolation.

Run (raw path is an argument, never hardcoded):
  rtk proxy python3 docs/nais-interview/replay/export_gang_replay.py --gang <RAW_GANG_BATCH_JSON>
"""
from pathlib import Path
from datetime import datetime
import argparse, json, re

import export_replay as er

HERE = Path(__file__).resolve().parent
SCHEMA = 'nais-offline-replay-gang/v1'
EXPORTER_VERSION = '1.0.0'
PHASES = {'SUBMIT', 'QUEUE', 'DRAIN', 'FINISHED'}
SUMMARY = '../evidence/common-gpu-pool-ten-request.json'
# Published alias → CUDA model string the worker recorded on that node (DEVICE-MODELS.md).
EXPECTED_MODELS = {'GPU-A': 'NVIDIA GeForce RTX 5060 Ti', 'GPU-B': 'NVIDIA GeForce RTX 5080', 'GPU-C': 'NVIDIA GB10'}
TITLES = [
    (r'네이티브 스케줄러 실험 시작', '실험 시작 · 요청 없음'),
    (r'동시 요청 10개 접수 · 네이티브 Job 생성 시작', '동시 요청 10개 접수 · 네이티브 Job 생성 시작'),
    (r'10개 네이티브 요청 · 실행과 quota 대기 동시 관측', '10개 네이티브 요청 · 실행과 quota 대기 동시 관측'),
    (r'완료된 작업의 quota 반환 → 대기 요청 입장·실행', '완료된 작업의 quota 반환 → 대기 요청 입장·실행'),
]
SCENARIO = {
    'name': '공통 GPU 풀 · 2-GPU gang 입장 (gang_batch)', 'run_alias': 'run-gang', 'project_alias': 'project-A',
    'scope': '전용 GPU 쿼터 3개인 공통 GPU 풀에 같은 시각 일괄 제출한 요청 10개(R01–R03은 2-GPU, 나머지 1-GPU). '
             'Kueue가 2-GPU 요청을 Pod 2개 한 단위로 쿼터 입장시키는 과정을 관측했다. 두 worker는 barrier 뒤 각자 독립 CUDA 검증을 '
             '수행하며 DDP 학습이 아니다. Pod 생성·시작 시각은 동시가 아니다. 모든 요청의 우선순위는 0으로 기록되어 우선순위 변화는 없다. '
             '범용 전역 스케줄러가 아니라 공통 풀 쿼터 입장 관측이다.',
}


def title(raw, final):
    if final:
        return '최종 관측 · 실험 종료 기록'
    for pattern, template in TITLES:
        if re.fullmatch(pattern, raw):
            return template
    raise ValueError(f'title is not in the public template allowlist (length {len(raw)})')


def condition_time(job, kind):
    times = [c['lastTransitionTime'] for c in job.get('conditions') or [] if c.get('type') == kind and c.get('status') == 'True']
    return er.iso(times[-1]) if times else None


def pods(items, alias):
    out = []
    for p in items or []:
        events = p.get('events') or []
        if any(int(e['rank']) != int(p['rank']) for e in events):
            raise ValueError('pod event rank mismatch')
        checks = [e['correctness'] for e in events if e['phase'] == 'COMPUTE_FINISHED' and 'correctness' in e]
        barrier = [int(e['workers']) for e in events if e['phase'] == 'BARRIER_RELEASED']
        out.append({'rank': int(p['rank']), 'node': alias(p.get('node')), 'state': er.enum(p.get('state'), er.POD_STATES),
                    'exit_codes': [int(c) for c in p.get('exit_codes') or []],
                    'phases': [er.enum(e['phase'], er.POD_PHASES) for e in events],
                    'phase_times': [{'phase': er.enum(e['phase'], er.POD_PHASES), 'at': er.iso(e['time'])} for e in events],
                    'barrier_workers': barrier[-1] if barrier else None,
                    'compute_correct': all(checks) if checks else None})
    return out


def request(job, alias):
    admission = job.get('admission')
    return {
        'workload': 'CUDA 검증 커널 · gang 입장',
        'device_class': 'gpu',
        'backend': 'kubernetes',
        'state': er.enum(job.get('state'), er.STATES),
        'reason': er.pending_reason(job.get('reason')),
        'gpu': job.get('gpu'),
        'priority': job.get('priority'),
        'planned_arrival_seconds': None,
        'attempt': f'{er.row_of(job["label"])}·a1' if job.get('native_uid') else None,
        'submitted_at': er.iso(job.get('submitted_at')),
        'pods': pods(job.get('pods'), alias),
        'previous_attempts': [],
        'excluded_nodes': [],
        'gang': {'pod_count': job.get('expected_completions'),
                 'admitted_pod_count': sum(int(s['count']) for s in admission['podSetAssignments']) if admission else None,
                 'quota_reserved_at': condition_time(job, 'QuotaReserved'),
                 'admitted_at': condition_time(job, 'Admitted'),
                 'finished_at': condition_time(job, 'Finished')},
    }


def frames(raw, alias):
    events = raw['body']['events']
    start = datetime.fromisoformat(er.iso(events[0]['observed_at']))
    out = []
    for i, event in enumerate(events):
        s = event['snapshot']
        observed = er.iso(event['observed_at'])
        final = i == len(events) - 1 and s.get('phase') == 'FINISHED'
        jobs = s.get('jobs', [])
        frame = {'index': i, 'observed_at': observed,
                 'elapsed_seconds': round((datetime.fromisoformat(observed) - start).total_seconds(), 3),
                 'title': title(event['title'], final), 'phase': er.enum(s.get('phase'), PHASES),
                 'requests': {er.row_of(j['label']): request(j, alias) for j in jobs}}
        pool = s.get('pool_observation')
        if pool:
            frame['queue_observation'] = {'admitted': pool.get('admitted'), 'pending': pool.get('pending')}
        frame['native_attempts'] = [{'attempt': f'{er.row_of(j["label"])}·a1', 'state': er.enum(j.get('state'), er.STATES),
                                     'nodes': [alias(p.get('node')) for p in j['pods']],
                                     'exit_codes': sorted({int(c) for p in j['pods'] for c in p.get('exit_codes') or []})}
                                    for j in jobs if j.get('pods')]
        frame['priority_changes'] = []
        if s.get('cleanup'):
            frame['cleanup'] = er.enum(s['cleanup'], {'completed'})
        out.append(frame)
    return out


def verify_aliases(raw, aliases):
    """Every recorded CUDA model on a node must equal the published model of that node's alias."""
    seen = {}
    for event in raw['body']['events']:
        for job in event['snapshot'].get('jobs', []):
            for p in job.get('pods') or []:
                for e in p.get('events') or []:
                    if e.get('model'):
                        seen.setdefault(aliases(p['node']), set()).add(e['model'])
    if seen != {a: {m} for a, m in EXPECTED_MODELS.items()}:
        raise ValueError('node aliases do not match the published device models')
    return {a: sorted(m)[0] for a, m in sorted(seen.items())}


def export(raw_path):
    raw = json.loads(Path(raw_path).read_text())
    if raw.get('scenario') != 'gang_batch':
        raise ValueError('unexpected scenario')
    aliases = er.Aliases([raw])
    aliases.build(er.used_nodes(raw))
    models = verify_aliases(raw, aliases)
    f = frames(raw, aliases)
    data = {'schema': SCHEMA, 'exporter_version': EXPORTER_VERSION,
            'notice': '저장된 관측 기록의 재생용 파생 데이터. live 상태가 아니며, 관측 사이의 상태는 기록되지 않았다.',
            'time_zone': 'UTC', 'scenario_key': 'gang', 'nodes': aliases.public(),
            'provenance': {'raw_scenario': 'gang_batch', 'raw_sha256': er.sha256(raw_path), 'record_version': raw.get('version'),
                           'events': len(raw['body']['events']), 'exporter': 'replay/export_gang_replay.py',
                           'manifest': 'replay/gang-PROVENANCE.json', 'recorded_device_models': models,
                           'elapsed_basis': 'observed_at minus the first recorded event observed_at (derived, not recorded)'},
            'scenario': {**SCENARIO, 'recorded_state': er.enum(raw.get('state'), {'SUCCEEDED', 'FAILED'}),
                         'public_summary': SUMMARY, 'rows': [f'R{i:02d}' for i in range(1, 11)], 'frames': f}}
    return raw, data


def counts(data):
    frames_ = data['scenario']['frames']
    final = frames_[-1]['requests'].values()
    pods_ = [p for r in final for p in r['pods']]
    queue = next(f['queue_observation'] for f in frames_ if f.get('queue_observation', {}).get('pending'))
    return {'frames': len(frames_), 'rows': len(data['scenario']['rows']),
            'final_succeeded': sum(r['state'] == 'SUCCEEDED' for r in final),
            'two_gpu_groups': sum(r['gpu'] == 2 for r in final),
            'succeeded_worker_pods': sum(p['state'] == 'Succeeded' for p in pods_),
            'correct_worker_pods': sum(p['compute_correct'] is True for p in pods_),
            'barrier_worker_pods': sum('BARRIER_RELEASED' in p['phases'] for p in pods_),
            'first_nonzero_queue_observation': queue,
            'priority_changes': sum(len(f['priority_changes']) for f in frames_)}


def manifest(raw_path, raw, data, out_path):
    return {
        'schema': 'nais-offline-replay-provenance/v1', 'exporter': 'replay/export_gang_replay.py', 'exporter_version': EXPORTER_VERSION,
        'output': {'path_from_materials': 'replay/gang-observations.json', 'schema': SCHEMA, 'sha256': er.sha256(out_path)},
        'raw_inputs': [{'scenario': raw['scenario'], 'role': 'gang', 'location': 'private run-state store (not published)',
                        'sha256': er.sha256(raw_path), 'bytes': Path(raw_path).stat().st_size, 'record_version': raw.get('version'),
                        'recorded_state': raw.get('state'), 'events': len(raw['body']['events']),
                        'event_schema': ['title', 'observed_at', 'snapshot']}],
        'extraction_scope': {
            'per_frame': 'one output frame per recorded body.events item, same order and observed_at; no interpolation',
            'request_fields': 'state, Kueue reason category, gpu count, priority, submitted_at, pod rank/node alias/state/exit codes/'
                              'phase and phase time/barrier worker count/compute correctness, gang pod count, admitted pod count, QuotaReserved/Admitted/Finished times',
            'aliases': 'nodes → GPU-A..C, checked against the recorded CUDA model of each published alias; requests → R01..R10; '
                       'each request is one native Job → Rnn·a1 (no retries recorded)',
            'derived': 'elapsed_seconds from observed_at; priority_changes is empty because every recorded priority is 0',
            'replaced': 'event titles mapped to fixed templates; Kueue admission messages mapped to categories',
            'excluded': ['credentials', 'endpoints and URLs', 'image references and registries', 'shell commands and logs',
                         'local absolute paths', 'raw job/native/workload identifiers and UIDs', 'cluster/local queue and flavor names',
                         'topology assignment values', 'node inventory metrics', 'raw free-text explanation/verdict/condition messages',
                         'pod names'],
        },
        'public_summary_references': [{'path_from_materials': SUMMARY, 'sha256': er.sha256((HERE.parent / SUMMARY).resolve()),
                                       'section': 'gang_batch'},
                                      {'path_from_materials': 'replay/DEVICE-MODELS.md', 'sha256': er.sha256(HERE / 'DEVICE-MODELS.md')}],
        'extracted_counts': counts(data),
        'command': 'rtk proxy python3 docs/nais-interview/replay/export_gang_replay.py --gang <RAW_GANG_BATCH_JSON>',
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--gang', required=True, help='private gang_batch run record JSON')
    ap.add_argument('--out-dir', default=str(HERE))
    args = ap.parse_args()
    raw, data = export(args.gang)
    out = Path(args.out_dir) / 'gang-observations.json'
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n')
    (Path(args.out_dir) / 'gang-PROVENANCE.json').write_text(json.dumps(manifest(args.gang, raw, data, out), ensure_ascii=False, indent=1) + '\n')
    print(json.dumps(counts(data), ensure_ascii=False))


if __name__ == '__main__':
    main()
