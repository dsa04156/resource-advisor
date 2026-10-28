"""Build data.json for the recorded management views from published evidence only.

Inputs (no live service, no private raw store):
  - ../replay/observations.json  (fleet_batch frames, 2026-10-07, public aliases)
  - git object a5d1dde:docs/evidence/all-accelerators-20261008-v1.json  (Mobilint jobs[0], 2026-10-08)

Run from the repository root:
  rtk proxy python3 -I docs/nais-interview/management/build_data.py
Every displayed summary is derived here and asserted against the source before writing.
"""
from collections import Counter
from pathlib import Path
import hashlib
import json
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OBS = HERE.parent / 'replay' / 'observations.json'
EVIDENCE_COMMIT = 'a5d1dde87b56ba2794dc6a93559342eb85f1940f'
EVIDENCE_PATH = 'docs/evidence/all-accelerators-20261008-v1.json'
EVIDENCE_URL = f'https://github.com/dsa04156/resource-advisor/blob/{EVIDENCE_COMMIT}/{EVIDENCE_PATH}'
EVIDENCE_SHA256 = '64ebe1c9d4e585f59e0e7d7b55665d3758d031a0924c2426fb4de6c6ff44bd09'
QUEUE_FRAME = 12

# replay/DEVICE-MODELS.md
DEVICE_MODELS = {'GPU-A': 'NVIDIA RTX 5060 Ti', 'GPU-B': 'NVIDIA RTX 5080', 'GPU-C': 'NVIDIA GB10',
                 'JET-A': 'Jetson Orin Nano', 'JET-B': 'Jetson AGX Orin', 'NPU-A': 'Hailo-8',
                 'NPU-B': 'Mobilint ARIES2', 'SLURM-A': 'Jetson Orin (Slurm)'}
BOUNDARY = {'nvidia.com/gpu': '물리 GPU 1장 단위', 'nvidia.com/gpu.shared': '공유 GPU 슬롯 단위 (물리 GPU 아님)',
            'hailo.ai/h8': '물리 NPU 장치 단위', 'gres/gpu': 'Slurm GRES GPU 단위'}
REQUEST_FIELDS = ('workload', 'device_class', 'backend', 'state', 'reason', 'node', 'accelerator_count',
                  'native_job_recorded', 'started_at', 'finished_at')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_mobilint():
    raw = subprocess.run(['git', 'show', f'{EVIDENCE_COMMIT}:{EVIDENCE_PATH}'], cwd=REPO,
                         check=True, capture_output=True).stdout
    assert sha256(raw) == EVIDENCE_SHA256, 'Mobilint evidence bytes changed'
    doc = json.loads(raw)
    assert doc['synthetic'] is False
    job = doc['jobs'][0]
    assert (job['accelerator_model'], job['backend'], job['state'], job['node_alias']) == ('ARIES2', 'kubernetes', 'SUCCEEDED', 'npu-1')
    # The record names the Kubernetes backend only; nothing in jobs[0] or its qualification shows a Kueue admission.
    assert 'kueue' not in json.dumps([job, doc['qualifications'][0]]).lower()
    result, m, usage = job['result'], job['result']['measurements'], job['usage']
    assert (result['outcome'], result['measured'], result['evidence_kind']) == ('COMPLETED', True, 'hardware')
    qual = doc['qualifications'][0]['report']
    assert qual['runtime'] == 'mobilint-candy' and qual['model_digest'] == job['model_digest']
    # Allowlisted fields only: no job/attempt/native/MLflow identifiers, digests or endpoints.
    return {
        'id': 'R11', 'workload': 'Mobilint Candy 추론', 'workload_ref': job['workload_ref'],
        'device_class': job['device_class'], 'backend': job['backend'], 'path': 'kubernetes', 'state': job['state'], 'reason': None,
        'node': 'NPU-B', 'accelerator_count': job['requested_resources']['accelerator_count'],
        'native_job_recorded': True, 'date': job['created_at'][:10],
        'started_at': usage['started_at'], 'finished_at': job['finished_at'],
        'times': [  # recorded timestamp fields, not a state lifecycle
            {'label': '접수 (created_at)', 'at': job['created_at']},
            {'label': '네이티브 제출 (usage.submitted_at)', 'at': usage['submitted_at']},
            {'label': '실행 시작 (usage.started_at)', 'at': usage['started_at']},
            {'label': '백엔드 종료 (usage.backend_finished_at)', 'at': usage['backend_finished_at']},
            {'label': '결과 확정 (finished_at)', 'at': job['finished_at']},
        ],
        'lifecycle': [],
        'result': {'outcome': result['outcome'], 'measured': result['measured'], 'evidence_kind': result['evidence_kind'],
                   'quality_passed': job['quality_passed']},
        'mlflow_receipt': bool(job['mlflow_run_id']) and job['artifact_digest'] == job['artifact_tracking_digest'],
        'profile_recorded': job['profile_recorded'],
        'requested': {'npu': job['requested_resources']['accelerator_count'], 'cpu': job['requested_resources']['host_cpu'],
                      'memory_mib': job['requested_resources']['host_memory_mib']},
        'allocation': {'mode': usage['allocation_mode'], 'source': usage['observed_allocation']['source'],
                       'interval_seconds': usage['allocation_interval_seconds'], 'uncertainty': usage['uncertainty'],
                       'semantics': usage['semantics']},
        'measurements': {
            'measured_compute_seconds': usage['measured_compute_seconds'], 'elapsed_seconds': m['elapsed_seconds'],
            'latency_p50_ms': m['latency_p50_ms'], 'latency_p95_ms': m['latency_p95_ms'], 'latency_p99_ms': m['latency_p99_ms'],
            'throughput': m['throughput'], 'throughput_unit': 'work units/s', 'sample_count': m['sample_count'],
            'work_units': m['work_units'], 'peak_memory_mib': m['peak_memory_mib'], 'quality_value': m['quality_value'],
            'gpu_utilization': m['gpu_utilization'], 'power_watts': m['power_watts'], 'temperature_celsius': m['temperature_celsius'],
        },
        'quality_contract': {k: job['quality_contract'][k] for k in ('metric', 'minimum', 'minimum_repeats', 'maximum_peak_memory_mib')},
        'runtime': {'arch': qual['arch'], 'precision': qual['precision'], 'input_shape': qual['input_shape'],
                    'profile_reuse': qual['runtime_versions']['profile_reuse']},
        'source': {'url': EVIDENCE_URL, 'pointer': 'jobs[0]', 'qualification_pointer': 'qualifications[0].report'},
    }


def request_row(rid, req):
    # fleet_batch K8s requests ran in the pool whose Kueue admitted/pending counters were recorded for the run.
    path = 'kueue' if req['backend'] == 'kubernetes' else req['backend']
    row = {'id': rid, **{k: req[k] for k in REQUEST_FIELDS}, 'path': path, 'date': (req['started_at'] or req['finished_at'])[:10],
           'lifecycle': req['lifecycle'], 'result': req['result'], 'mlflow_receipt': req['mlflow_receipt']}
    return row


def main():
    obs_bytes = OBS.read_bytes()
    data = json.loads(obs_bytes)
    fleet = data['scenarios']['fleet']
    frames = fleet['frames']
    assert len(frames) == 24 and fleet['recorded_state'] == 'SUCCEEDED'
    final = frames[-1]['requests']
    assert sorted(final) == ['R%02d' % i for i in range(1, 11)]
    assert all(r['state'] == 'SUCCEEDED' and r['mlflow_receipt'] and r['result']['outcome'] == 'COMPLETED' and r['result']['measured']
               for r in final.values())

    assert any(f.get('queue_observation') for f in frames), 'fleet run has no recorded Kueue pool observation'
    mobilint = load_mobilint()
    requests = [request_row(rid, final[rid]) for rid in sorted(final)] + [mobilint]

    nodes = {**data['nodes'], 'NPU-B': {'backend': 'kubernetes', 'accelerator_resource': None,
                                         'accelerator': 'Mobilint ARIES2 NPU', 'architecture': mobilint['runtime']['arch']}}
    per_node = Counter(r['node'] for r in requests)
    devices = []
    for alias in ['GPU-A', 'GPU-B', 'GPU-C', 'JET-A', 'JET-B', 'NPU-A', 'NPU-B', 'SLURM-A']:
        meta = nodes[alias]
        res = meta['accelerator_resource']
        devices.append({
            'alias': alias, 'model': DEVICE_MODELS[alias], 'backend': meta['backend'],
            'path': next(r['path'] for r in requests if r['node'] == alias),
            'device_class': 'npu' if alias.startswith('NPU') else 'gpu', 'architecture': meta['architecture'],
            'resource': res, 'boundary': BOUNDARY[res] if res else '물리 NPU 장치 단위 (allocation_mode physical_device)',
            'workloads': per_node[alias], 'request_ids': [r['id'] for r in requests if r['node'] == alias],
            'dates': sorted({r['date'] for r in requests if r['node'] == alias}),
            'source': 'replay/observations.json nodes' if alias != 'NPU-B' else 'Mobilint jobs[0] · qualifications[0]',
        })

    backends = Counter(r['backend'] for r in requests)
    classes = Counter(d['device_class'] for d in devices)
    summary = {
        'completed': sum(r['state'] == 'SUCCEEDED' for r in requests),
        'backend': {'kubernetes': backends['kubernetes'], 'slurm': backends['slurm']},
        'nodes_cumulative': len(per_node), 'node_classes': {'gpu': classes['gpu'], 'npu': classes['npu']},
        'result_receipts': sum(bool(r['result']) and r['result']['outcome'] == 'COMPLETED' for r in requests),
        'mlflow_receipts': sum(r['mlflow_receipt'] for r in requests),
        'dates': {d: sum(r['date'] == d for r in requests) for d in sorted({r['date'] for r in requests})},
    }
    expected = {'completed': 11, 'backend': {'kubernetes': 9, 'slurm': 2}, 'nodes_cumulative': 8,
                'node_classes': {'gpu': 6, 'npu': 2}, 'result_receipts': 11, 'mlflow_receipts': 11,
                'dates': {'2026-10-07': 10, '2026-10-08': 1}}
    assert summary == expected, summary
    assert sum(d['workloads'] for d in devices) == 11

    snapshots = []
    for f in frames:
        snap = {'index': f['index'], 'observed_at': f['observed_at'], 'title': f['title'], 'phase': f['phase'],
                'queue_observation': f.get('queue_observation'),
                'requests': {rid: {k: r[k] for k in ('workload', 'device_class', 'backend', 'state', 'reason', 'node', 'native_job_recorded')}
                             for rid, r in sorted(f['requests'].items())}}
        snap['counts'] = dict(Counter(r['state'] for r in snap['requests'].values()))
        snapshots.append(snap)
    q = snapshots[QUEUE_FRAME]
    assert q['observed_at'] == '2026-10-07T04:29:54.303176+00:00'
    assert q['counts'] == {'SUCCEEDED': 4, 'VALIDATED': 2, 'RUNNING': 2, 'QUEUED': 2}
    assert q['queue_observation'] == {'admitted': 0, 'pending': 0}
    assert q['requests']['R10']['reason'] == 'AssocGrpCpuLimit'

    out = {
        'schema': 'nais-recorded-management/v1',
        'notice': '실증 기록 · 2026-10-07~08. 저장된 실행 기록을 새로 렌더링한 읽기 전용 관리 화면이며 현재 live 상태나 과거 운영 UI 캡처가 아니다.',
        'period': '2026-10-07~08', 'time_zone': 'UTC', 'default_snapshot': QUEUE_FRAME,
        'summary': summary, 'devices': devices, 'requests': requests, 'snapshots': snapshots,
        'fleet': {'run_alias': fleet['run_alias'], 'project_alias': fleet['project_alias'], 'scope': fleet['scope']},
        'provenance': {
            'builder': 'management/build_data.py',
            'sources': [
                {'role': '2026-10-07 fleet_batch 관측 24개 · 요청 R01–R10', 'path': '../replay/observations.json',
                 'sha256': sha256(obs_bytes), 'manifest': '../replay/PROVENANCE.json'},
                {'role': '2026-10-08 Mobilint ARIES2 실행 기록 jobs[0] (표시 번호 R11)', 'path': EVIDENCE_PATH,
                 'commit': EVIDENCE_COMMIT, 'url': EVIDENCE_URL, 'sha256': EVIDENCE_SHA256},
                {'role': '장치 모델 매핑', 'path': '../replay/DEVICE-MODELS.md'},
                {'role': '아키텍처 책임 경계', 'path': '../SOURCE-MATERIAL.md#34-상위-스케줄링-계층-무엇을-보고-어디까지-결정하는가'},
            ],
            'excluded': ['job/attempt/native/MLflow identifiers', 'digests and signatures', 'endpoints, URLs of services, credentials',
                         'host names', 'node inventory metrics (capacity, utilization)'],
            'paths': 'K8s · Kueue = 2026-10-07 fleet_batch Kubernetes 요청(실행 단위로 Kueue admitted/pending 관측 기록 있음). Kubernetes = Mobilint jobs[0](backend kubernetes만 기록, Kueue 기록 없음).',
            'aliases': 'NPU-B = Mobilint 기록의 node_alias npu-1. R11 = 합본 표 표시 번호이며 2026-10-07 원본의 요청 번호가 아니다.',
        },
    }
    (HERE / 'data.json').write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n')
    print('data.json written', summary)


if __name__ == '__main__':
    main()
