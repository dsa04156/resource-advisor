"""Export sanitized replay observations from two private scheduler-lab run records.

The raw records stay private. This script copies only allowlisted structured fields,
maps node/request/attempt identifiers to stable public aliases, replaces raw titles
with fixed templates and drops free text, logs, endpoints, images and tracking ids.
Each output frame is one recorded snapshot; nothing is interpolated or backfilled.

Run (raw paths are arguments, never hardcoded):
  rtk proxy python3 docs/nais-interview/replay/export_replay.py \
      --fleet <RAW_FLEET_BATCH_JSON> --adaptive <RAW_ADAPTIVE_BATCH_JSON>
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse, hashlib, json, re

HERE = Path(__file__).resolve().parent
SCHEMA = 'nais-offline-replay/v1'
EXPORTER_VERSION = '1.0.0'

STATES = {'REQUESTED', 'VALIDATED', 'SUBMITTING', 'QUEUED', 'RUNNING', 'COLLECTING', 'SUCCEEDED', 'FAILED',
          'NOT_SUBMITTED', 'PENDING', 'ADMITTED', 'FINALIZING', 'REQUEUED'}
POD_STATES = {'Pending', 'Running', 'Succeeded', 'Failed'}
POD_PHASES = {'GPU_READY', 'BARRIER_RELEASED', 'COMPUTE_STARTED', 'COMPUTE_FINISHED', 'EXPECTED_FAILURE'}
FLEET_REASONS = {'SchedulingGated', 'AssocGrpCpuLimit', 'ContainerCreating'}
BACKENDS = {'kubernetes', 'slurm'}
DEVICES = {'gpu', 'npu'}
ACCELERATORS = {'nvidia.com/gpu': ('GPU', '전용 GPU'), 'nvidia.com/gpu.shared': ('JET', '공유 GPU 슬롯'),
                'hailo.ai/h8': ('NPU', 'Hailo-8 NPU'), 'gres/gpu': ('SLURM', 'Slurm GRES GPU')}
ARCHS = {'amd64', 'arm64', 'aarch64'}
PHASES = {'SUBMIT', 'EXECUTION', 'ARRIVALS', 'FINISHED'}
PRIORITY_REASONS = {'대기 20초 이상 · 시나리오 aging 정책'}

# Raw titles are replaced by these fixed templates; an unknown title fails the export.
TITLE_TEMPLATES = [
    (r'네이티브 스케줄러 실험 시작', '실험 시작 · 요청 없음'),
    (r'독립 요청 10개 · 플랫폼 접수 시작', '독립 요청 10개 · 플랫폼 접수 시작'),
    (r'등록된 (GPU|NPU) 작업 접수', '등록된 {0} 작업 접수'),
    (r'GPU·NPU 네이티브 작업 상태와 결과 관측', 'GPU·NPU 네이티브 작업 상태와 결과 관측'),
    (r'노드 자원 관측 갱신', '노드 자원 관측 갱신'),
    (r'새 요청 도착 · request-(\d{2})', '새 요청 도착 · 요청 {0}'),
    (r'대기 우선순위 승격 · request-(\d{2})', '대기 우선순위 승격 · 요청 {0}'),
    (r'실패 기록 보존 · 다른 GPU 후보로 재접수 · request-(\d{2})', '실패 기록 보존 · 다른 GPU 후보로 재접수 · 요청 {0}'),
    (r'도착·대기 순서·GPU 그룹·재할당 관측', '도착·대기 순서·GPU 그룹·재할당 관측'),
]
WORKLOADS = [('cuda-smoke-auto', 'CUDA 검증 커널 · 자동 배치'), ('hailo-resnet50', 'Hailo-8 ResNet-50 추론'),
             ('slurm-orin-cnn', 'Orin CNN 추론'), ('kernel-cache', '등록 CNN 추론')]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def iso(value):
    """Normalise a recorded timestamp to ISO-8601 UTC without changing its instant."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return datetime.fromisoformat(value.replace('Z', '+00:00').replace('+0000', '+00:00')).astimezone(timezone.utc).isoformat()


def title(raw, final):
    if final:
        return '최종 관측 · 실험 종료 기록'
    for pattern, template in TITLE_TEMPLATES:
        m = re.fullmatch(pattern, raw)
        if m:
            return template.format(*m.groups())
    raise ValueError(f'title is not in the public template allowlist (length {len(raw)})')


def enum(value, allowed):
    if value is None:
        return None
    if value not in allowed:
        raise ValueError(f'value outside allowlist: {value!r}')
    return value


def pending_reason(raw):
    """Map Kueue admission messages (which name private flavors) to public categories."""
    if raw is None:
        return None
    if "doesn't allow to fit" in raw:
        return '토폴로지 조건상 배치 노드 없음'
    m = re.search(r'insufficient unused quota for nvidia\.com/gpu in flavor \S+, (\d+) more needed', raw)
    if m:
        return f'공통 GPU 풀 쿼터 부족 · {m.group(1)}개 더 필요'
    if 'insufficient quota' in raw or "doesn't match node affinity" in raw:
        return '해당 flavor 용량·affinity 불일치'
    raise ValueError('unclassified admission reason')


def workload(ref):
    for key, text in WORKLOADS:
        if key in ref:
            return text
    raise ValueError('workload outside allowlist')


class Aliases:
    """Stable public node aliases shared by both scenarios, from recorded inventory."""

    def __init__(self, raws):
        self.meta = {}
        for raw in raws:
            for event in raw['body']['events']:
                for inv in event['snapshot'].get('resource_observation', {}).get('inventory', []):
                    for node in inv.get('nodes', []):
                        acc = [k for k in node.get('resources') or {} if k in ACCELERATORS]
                        arch = (node.get('hardware') or {}).get('architecture')
                        if acc and (arch or node['node_ref'] not in self.meta):
                            self.meta[node['node_ref']] = (acc[0], enum(arch, ARCHS), 'slurm' if 'slurm' in inv['cluster_ref'] else 'kubernetes')
        self.same = {}
        self.names = {}

    def declare_same(self, native, inventory_name):
        self.same[native] = inventory_name

    def build(self, used):
        canonical = sorted({self.same.get(n, n) for n in used})
        counters = {}
        for name in canonical:
            prefix = ACCELERATORS[self.meta[name][0]][0]
            counters[prefix] = counters.get(prefix, 0) + 1
            self.names[name] = f'{prefix}-{chr(64 + counters[prefix])}'

    def __call__(self, name):
        return None if name is None else self.names[self.same.get(name, name)]

    def public(self):
        out = {}
        for name, alias in sorted(self.names.items(), key=lambda x: x[1]):
            acc, arch, backend = self.meta[name]
            out[alias] = {'backend': backend, 'accelerator_resource': acc, 'accelerator': ACCELERATORS[acc][1], 'architecture': arch}
        return out


def row_of(text):
    m = re.search(r'(?:요청 |request-)(\d{2})', text)
    return f'R{m.group(1)}'


def attempt_alias(label):
    m = re.fullmatch(r'request-(\d{2})-a(\d+)', label)
    return f'R{m.group(1)}·a{m.group(2)}'


def used_nodes(raw):
    names = set()
    for event in raw['body']['events']:
        s = event['snapshot']
        for job in s.get('jobs', []):
            names.add(job.get('node'))
            for a in [job] + (job.get('attempts') or []):
                names.update(p['node'] for p in a.get('pods') or [])
            names.update(job.get('excluded_nodes') or [])
        for nodes in (s.get('execution_coverage') or {}).values():
            names.update(nodes)
    names.discard(None)
    return names


def declare_slurm_alias(raw, aliases):
    """Slurm jobs report the native node name; execution_coverage reports the inventory name.
    Pair them only when exactly one unmatched Slurm name exists on each side."""
    final = raw['body']['events'][-1]['snapshot']
    native = {j['node'] for j in final.get('jobs', []) if j.get('backend') == 'slurm' and j.get('node') and j['node'] not in aliases.meta}
    covered = {n for nodes in (final.get('execution_coverage') or {}).values() for n in nodes if aliases.meta.get(n, ('', '', ''))[2] == 'slurm'}
    if native:
        if len(native) != 1 or len(covered) != 1:
            raise ValueError('ambiguous Slurm node pairing')
        aliases.declare_same(native.pop(), covered.pop())


def fleet_request(job, alias):
    result = job.get('result') or {}
    measured = result.get('measurements') or {}
    return {
        'workload': workload(job['workload_ref']),
        'device_class': enum(job.get('device_class'), DEVICES),
        'backend': enum(job.get('backend'), BACKENDS),
        'state': enum(job.get('state'), STATES),
        'reason': enum(job.get('reason'), FLEET_REASONS),
        'node': alias(job.get('node')),
        'accelerator_count': (job.get('requested') or {}).get('accelerator_count'),
        'native_job_recorded': job.get('native_id') is not None,
        'started_at': iso(job.get('started_at')),
        'finished_at': iso(job.get('finished_at')),
        'lifecycle': [{'state': enum(e['state'], STATES), 'reason': enum(e.get('reason'), FLEET_REASONS), 'observed_at': iso(e['observed_at'])}
                      for e in job.get('lifecycle_events') or []],
        'result': None if not result else {'outcome': enum(result.get('outcome'), {'COMPLETED', 'FAILED'}),
                                           'measured': result.get('measured') is True,
                                           'quality_value': measured.get('quality_value')},
        'mlflow_receipt': bool((job.get('tracking') or {}).get('run_id')),
    }


def pods(items, alias):
    return [{'rank': int(p['rank']), 'node': alias(p.get('node')), 'state': enum(p.get('state'), POD_STATES),
             'exit_codes': [int(c) for c in p.get('exit_codes') or []],
             'phases': [enum(e['phase'], POD_PHASES) for e in p.get('events') or []]} for p in items or []]


def adaptive_request(job, alias):
    history = [{'attempt': attempt_alias(a['label']), 'state': enum(a.get('state'), STATES), 'priority': a.get('priority'),
                'gpu': a.get('gpu'), 'submitted_at': iso(a.get('submitted_at')), 'pods': pods(a.get('pods'), alias)}
               for a in job.get('attempts') or []]
    current = job.get('attempt') or 0
    return {
        'workload': 'CUDA 검증 커널 · 지속 실행',
        'device_class': enum(job.get('device_class', 'gpu'), DEVICES),
        'backend': enum(job.get('backend', 'kubernetes'), BACKENDS),
        'state': enum(job.get('state'), STATES),
        'reason': pending_reason(job.get('reason')),
        'gpu': job.get('gpu'),
        'priority': job.get('priority'),
        'planned_arrival_seconds': job.get('arrival_seconds'),
        'attempt': None if current == 0 else f'{row_of(job["label"])}·a{current}',
        'submitted_at': iso(job.get('submitted_at')),
        'pods': pods(job.get('pods'), alias),
        'previous_attempts': history,
        'excluded_nodes': [alias(n) for n in job.get('excluded_nodes') or []],
    }


def frames(raw, kind, alias):
    out = []
    events = raw['body']['events']
    for i, event in enumerate(events):
        s = event['snapshot']
        final = i == len(events) - 1 and s.get('phase') == 'FINISHED'
        frame = {'index': i, 'observed_at': iso(event['observed_at']), 'title': title(event['title'], final),
                 'phase': enum(s.get('phase'), PHASES), 'requests': {}}
        for job in s.get('jobs', []):
            row = row_of(job['label'])
            frame['requests'][row] = fleet_request(job, alias) if kind == 'fleet' else adaptive_request(job, alias)
        pool = s.get('pool_observation')
        if pool:
            frame['queue_observation'] = {'admitted': pool.get('admitted'), 'pending': pool.get('pending')}
        if 'execution_coverage' in s:
            frame['execution_coverage'] = {k: sorted(alias(n) for n in v) for k, v in s['execution_coverage'].items()}
        if 'elapsed_seconds' in s:
            frame['elapsed_seconds'] = s['elapsed_seconds']
        if 'native_attempt_evidence' in s:
            frame['native_attempts'] = [{'attempt': attempt_alias(a['label']), 'state': enum(a.get('state'), STATES),
                                         'nodes': [alias(p.get('node')) for p in a.get('pods') or []],
                                         'exit_codes': sorted({int(c) for p in a.get('pods') or [] for c in p.get('exit_codes') or []})}
                                        for a in s['native_attempt_evidence']]
        if 'priority_changes' in s:
            frame['priority_changes'] = [{'row': row_of(c['request_id']), 'attempt': attempt_alias(re.search(r'request-\d{2}-a\d+', c['request_id']).group()),
                                          'before': c['before'], 'after': c['after'],
                                          'reason': enum(c.get('reason'), PRIORITY_REASONS), 'observed_at': iso(c['observed_at'])}
                                         for c in s['priority_changes']]
        if s.get('cleanup'):
            frame['cleanup'] = enum(s['cleanup'], {'completed'})
        out.append(frame)
    return out


SCENARIOS = {
    'fleet': {'name': '전체 연결 자원 (fleet_batch)', 'raw_scenario': 'fleet_batch', 'summary': '../evidence/full-fleet-ten-request.json',
              'scope': 'K8s GPU·NPU와 별도 Slurm에 동시 제출한 독립 요청 10개. 실행 노드 수는 누적 관측이며 동시 할당이 아니다.'},
    'adaptive': {'name': '변동 도착·우선순위·재시도 (adaptive_batch)', 'raw_scenario': 'adaptive_batch', 'summary': '../evidence/adaptive-gpu-arrivals.json',
                 'scope': '공통 GPU 풀에 시차 도착한 요청 10개. 2-GPU 요청은 barrier가 있는 독립 worker 2개이며 DDP 학습이 아니다. exit 42는 주입된 실패이며 하드웨어 장애가 아니다.'},
}


def export(raw_paths):
    raws = {k: json.loads(Path(p).read_text()) for k, p in raw_paths.items()}
    for kind, raw in raws.items():
        if raw.get('scenario') != SCENARIOS[kind]['raw_scenario']:
            raise ValueError(f'{kind}: unexpected scenario')
    aliases = Aliases(raws.values())
    declare_slurm_alias(raws['fleet'], aliases)
    aliases.build(set().union(*(used_nodes(r) for r in raws.values())))
    data = {'schema': SCHEMA, 'exporter_version': EXPORTER_VERSION,
            'notice': '저장된 관측 기록의 재생용 파생 데이터. live 상태가 아니며, 관측 사이의 상태는 기록되지 않았다.',
            'time_zone': 'UTC', 'nodes': {}, 'scenarios': {}}
    for kind, raw in raws.items():
        cfg = SCENARIOS[kind]
        f = frames(raw, kind, aliases)
        data['scenarios'][kind] = {'name': cfg['name'], 'run_alias': f'run-{kind}', 'project_alias': 'project-A',
                                   'recorded_state': enum(raw.get('state'), {'SUCCEEDED', 'FAILED'}), 'scope': cfg['scope'],
                                   'public_summary': cfg['summary'], 'rows': [f'R{i:02d}' for i in range(1, 11)], 'frames': f}
    data['nodes'] = aliases.public()
    return raws, data


def counts(data):
    out = {}
    for kind, sc in data['scenarios'].items():
        final = sc['frames'][-1]
        c = {'frames': len(sc['frames']), 'rows': len(sc['rows']),
             'final_succeeded': sum(r['state'] == 'SUCCEEDED' for r in final['requests'].values())}
        if kind == 'fleet':
            c['mlflow_receipts'] = sum(r['mlflow_receipt'] for r in final['requests'].values())
            c['execution_nodes'] = {k: len(v) for k, v in final['execution_coverage'].items()}
        else:
            c['native_attempts'] = len(final['native_attempts'])
            c['priority_changes'] = len(final['priority_changes'])
            c['succeeded_worker_pods'] = sum(p['state'] == 'Succeeded' for r in final['requests'].values() for p in r['pods'])
        out[kind] = c
    return out


def manifest(raw_paths, raws, data, out_path):
    root = HERE.parents[2]
    def public(rel):
        p = (HERE.parent / rel).resolve()
        return {'path_from_materials': rel, 'sha256': sha256(p)}
    return {
        'schema': 'nais-offline-replay-provenance/v1', 'exporter': 'replay/export_replay.py', 'exporter_version': EXPORTER_VERSION,
        'output': {'path_from_materials': 'replay/observations.json', 'schema': SCHEMA, 'sha256': sha256(out_path)},
        'raw_inputs': [{'scenario': raw['scenario'], 'role': kind, 'location': 'private run-state store (not published)',
                        'sha256': sha256(raw_paths[kind]), 'bytes': Path(raw_paths[kind]).stat().st_size,
                        'record_version': raw.get('version'), 'recorded_state': raw.get('state'),
                        'events': len(raw['body']['events']),
                        'event_schema': ['title', 'observed_at', 'snapshot']}
                       for kind, raw in raws.items()],
        'extraction_scope': {
            'per_frame': 'one output frame per recorded body.events item, same order and observed_at; no interpolation',
            'request_fields': 'state, allowlisted reason or reason category, device_class, backend, node alias, requested accelerator count, '
                              'recorded lifecycle/attempt/pod state, exit codes, priority, result outcome/quality, MLflow receipt presence',
            'aliases': 'nodes → GPU/JET/NPU/SLURM-A.. ordered within accelerator type; requests → R01..R10; attempts → Rnn·aN; run/project → run-<scenario>/project-A',
            'replaced': 'event titles mapped to fixed templates; final verdict replaced by a structured summary; Kueue admission messages mapped to categories',
            'excluded': ['credentials', 'endpoints and URLs', 'image references and registries', 'shell commands and logs',
                         'local absolute paths', 'raw job/attempt/native/workload/MLflow identifiers', 'cluster queue and flavor names',
                         'node inventory metrics', 'raw free-text explanation/verdict/policy', 'pod names and UIDs'],
        },
        'public_summary_references': [public(SCENARIOS[k]['summary']) for k in raws] + [public('../evidence/common-gpu-pool-ten-request.json'),
                                                                                       {'path_from_materials': '../reference/native-scheduler-lab.md', 'sha256': sha256(root / 'docs/reference/native-scheduler-lab.md')}],
        'extracted_counts': counts(data),
        'command': 'rtk proxy python3 docs/nais-interview/replay/export_replay.py --fleet <RAW_FLEET_BATCH_JSON> --adaptive <RAW_ADAPTIVE_BATCH_JSON>',
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fleet', required=True, help='private fleet_batch run record JSON')
    ap.add_argument('--adaptive', required=True, help='private adaptive_batch run record JSON')
    ap.add_argument('--out-dir', default=str(HERE))
    args = ap.parse_args()
    raw_paths = {'fleet': args.fleet, 'adaptive': args.adaptive}
    raws, data = export(raw_paths)
    out = Path(args.out_dir) / 'observations.json'
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n')
    (Path(args.out_dir) / 'PROVENANCE.json').write_text(json.dumps(manifest(raw_paths, raws, data, out), ensure_ascii=False, indent=1) + '\n')
    print(json.dumps(counts(data), ensure_ascii=False))


if __name__ == '__main__':
    main()
