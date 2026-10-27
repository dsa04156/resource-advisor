"""Render interview assets from archived evidence; does not contact the cluster.
Run: rtk proxy uv run --isolated --with matplotlib==3.11.2 --with cairosvg --with pillow python docs/nais-interview/build_visuals.py
"""
from pathlib import Path
import csv, json, html, hashlib, zipfile
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
import numpy as np
import cairosvg
from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
OUT = BASE / 'visuals'
OUT.mkdir(exist_ok=True)
DATA = ROOT / 'docs/evidence'
FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
fm.fontManager.addfont(FONT)
FAMILY = fm.FontProperties(fname=FONT).get_name()
plt.rcParams.update({'font.family': FAMILY, 'font.size': 15, 'axes.unicode_minus': False,
                     'svg.fonttype': 'none', 'axes.spines.top': False, 'axes.spines.right': False})
INK='#2d3142'; MUTED='#4f5d75'; ACCENT='#eb6c36'; PAPER='#f5f5f5'; BLUE='#2e5aa8'
assets=[]; source_rows=[]

def source(name, path):
    p=ROOT/path
    source_rows.append([name,path,hashlib.sha256(p.read_bytes()).hexdigest()])
    return p

def svg_text(x,y,text,size=24,color=INK,weight='normal',anchor='start'):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{html.escape(text)}</text>'

def box(x,y,w,h,title,lines=(),focal=False,existing=False):
    fill='#fcece4' if focal else ('#e9ebee' if existing else '#ffffff')
    stroke=ACCENT if focal else MUTED
    out=f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    out+=svg_text(x+20,y+36,title,24,INK,'bold')
    for i,line in enumerate(lines):out+=svg_text(x+20,y+72+i*32,line,20,MUTED)
    return out

def arrow(points,dashed=False):
    pts=' '.join(f'{x},{y}' for x,y in points)
    return f'<polyline points="{pts}" fill="none" stroke="{MUTED}" stroke-width="2" marker-end="url(#arrow)"'+(' stroke-dasharray="8 8"' if dashed else '')+'/>'

def diagram(name,title,subtitle,body,foot,caption):
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900">
    <defs><marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="{MUTED}"/></marker>
    <marker id="arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="{ACCENT}"/></marker>
    <marker id="arrow-link" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="{BLUE}"/></marker></defs>
    <rect width="1600" height="900" fill="{PAPER}"/>
    <g font-family="Noto Sans CJK JP,Noto Sans CJK KR,sans-serif">'''
    svg+=svg_text(64,64,'NAIS  /  연구기술직-3  /  면접 설명 자료',16,MUTED)
    svg+=svg_text(64,124,title,40,INK,'bold')+svg_text(64,172,subtitle,24,MUTED)
    svg+=body+f'<line x1="64" y1="804" x2="1536" y2="804" stroke="#bfc0c0"/>'
    svg+=svg_text(64,840,foot,20,MUTED)+svg_text(64,872,'구현·측정 범위와 출처: 함께 제공한 README.md 및 SOURCE-MATERIAL.md',16,MUTED)+'</g></svg>'
    (OUT/f'{name}.svg').write_text(svg)
    cairosvg.svg2png(bytestring=svg.encode(),write_to=str(OUT/f'{name}.png'),output_width=3200,output_height=1800)
    assets.append((name,title,caption))

def chart(name,title,subtitle,foot):
    fig=plt.figure(figsize=(16,9),facecolor=PAPER)
    fig.text(.045,.93,title,fontsize=27,weight='bold',color=INK)
    fig.text(.045,.875,subtitle,fontsize=15,color=MUTED)
    fig.text(.045,.10,foot,fontsize=13,color=MUTED,linespacing=1.6)
    return fig

def save(fig,name,title,caption):
    fig.savefig(OUT/f'{name}.png',dpi=200,facecolor=PAPER)
    fig.savefig(OUT/f'{name}.svg',facecolor=PAPER)
    plt.close(fig);assets.append((name,title,caption))

def bars(ax,labels,vals,title,unit,fmt='.2f',colors=None):
    ax.set_facecolor(PAPER)
    bs=ax.bar(labels,vals,width=.55,color=colors or [MUTED,ACCENT],zorder=3)
    ax.set_title(title,loc='left',fontsize=19,pad=22,weight='bold',color=INK)
    ax.set_ylabel(unit,color=MUTED)
    ax.set_ylim(0,max(vals)*1.28)
    ax.grid(axis='y',alpha=.18,zorder=0)
    ax.spines[['top','right','left']].set_visible(False)
    ax.tick_params(axis='x',length=0,labelsize=13)
    for b,v in zip(bs,vals):ax.text(b.get_x()+b.get_width()/2,v+max(vals)*.045,format(v,fmt),ha='center',fontsize=17,weight='bold',color=INK)

# 01: Nine nodes, separate backend pools, publication is not one global transaction.
b=''
for pts in [[(324,332),(420,332)],[(552,396),(552,484)],[(684,332),(736,332),(736,268),(800,268)],[(736,332),(736,484),(800,484)],[(1080,268),(1160,268)],[(1080,484),(1160,484)],[(1300,332),(1512,332),(1512,656),(1080,656)],[(1300,548),(1464,548),(1464,656),(1080,656)],[(800,656),(684,656)],[(552,612),(552,596)]]:b+=arrow(pts)
b+=box(64,268,260,128,'공통 API',['인증 · 불변 작업 계약','멱등 키로 요청 식별'],True)
b+=box(420,268,264,128,'Worker / route',['project × cluster','제출 · 관측 · 조정'])
b+=box(420,484,264,112,'PostgreSQL',['작업 상태 · 제출 intent'],existing=True)
b+=box(800,204,280,128,'Kubernetes adapter',['suspended Job 생성','queue / priority 라벨'])
b+=box(1160,204,280,128,'Kueue + K8s',['admission · 노드 배치','기존 스케줄러'],existing=True)
b+=box(800,420,280,128,'Slurm adapter',['제한 SSH → sbatch','squeue / sacct 관측'])
b+=box(1160,420,280,128,'Slurm',['account / QOS · 배치','검증된 native runtime'],existing=True)
b+=box(800,612,280,144,'결과 검증',['COLLECTING 후 검사','identity · hash · quality'],True)
b+=box(420,612,264,144,'기록·게시',['DB 상태 + 사용량','S3 / MLflow 재시도'])
diagram('01-architecture','서로 다른 스케줄러를 공통 실행 계약으로 연결','요청과 결과 형식을 통일하고, 배치 결정은 각 클러스터의 기존 스케줄러에 맡긴다.',b,
        '흰색: 프로젝트 구현  ·  회색: 기존 도구  ·  두 backend는 별도 장비 풀  ·  화살표: 주요 논리 흐름',
        '개념 구조도. backends.py, configuration.py, worker.py, service.py 근거. DB·S3·MLflow 전체가 단일 트랜잭션은 아니다.')

# 02: safety sequence, four lifelines.
b='';xs=[240,600,960,1360]
for x in xs:b+=f'<line x1="{x}" y1="284" x2="{x}" y2="752" stroke="#bfc0c0" stroke-dasharray="8 8"/>'
for x,title in zip(xs,['API','PostgreSQL','Worker','Native scheduler']):b+=box(x-132,212,264,72,title)
messages=[(0,1,336,'① 요청·멱등 키 저장'),(2,1,408,'② 제출 intent 커밋'),(2,3,480,'③ 외부 작업 제출'),(3,2,552,'④ 응답 유실 / 결과 불명'),(2,1,624,'⑤ UNKNOWN 상태 기록'),(2,3,696,'⑥ 같은 attempt를 탐색')]
for a,c,y,label in messages:
    b+=arrow([(xs[a],y),(xs[c],y)],a==3)
    mid=(xs[a]+xs[c])/2
    b+=f'<rect x="{mid-152}" y="{y-32}" width="304" height="28" fill="{PAPER}"/>'+svg_text(mid,y-12,label,20,anchor='middle')
diagram('02-submission-safety','응답 유실을 “새 작업 제출”로 처리하지 않는다','실행 전 intent를 저장하고, 불확실한 제출은 같은 attempt의 외부 작업을 찾아 조정한다.',b,
        '확정할 수 없으면 불확실 상태를 유지  ·  자동 재제출 없음  ·  모든 장애 경로의 live 검증을 뜻하지 않음',
        'worker.py submit_one/reconcile 및 SubmissionUnknown 설계. 특정 실험의 실제 통신 추적 캡처가 아닌 코드 기반 설명도.')

# 03: official goals / interpretation / actual scope.
b=''
for x,title in [(64,'공식 방향'),(568,'설계 해석'),(1072,'실습 구현')]:b+=svg_text(x,244,title,28,INK,'bold')
rows=[(['분산된 연구자원 공동 활용','데이터·모델·연산을 연결'],['연구자에게 공통 진입점','요청 형식과 결과 계약 통일'],['공통 API + 두 adapter','Kubernetes / 별도 Slurm']),
      (['기관별 우선순위 자율성','공동 기반과 기관 역할 분담'],['정책 권한을 현장에 유지','통합 계층은 정책을 매핑'],['normal / high 매핑','Kueue priority · Slurm QOS']),
      (['연구 생산성과 자산 재사용','연구 지원 기반의 운영'],['결과·환경·사용량 추적','성공과 품질 통과를 구분'],['결과 검증 + usage ledger','PostgreSQL · S3 · MLflow'])]
for row,(a,c,d) in enumerate(rows):
    y=280+row*164
    b+=arrow([(496,y+60),(552,y+60)])+arrow([(1000,y+60),(1056,y+60)])
    for x,ls in [(64,a),(568,c),(1072,d)]:b+=box(x,y,432,132,ls[0],[ls[1]],focal=(row==0 and x==1072))
diagram('03-nais-fit','NAIS의 목표 중, 공동 연산 운영 계층을 구현','왼쪽은 공식 자료의 요약, 가운데는 설계 해석, 오른쪽은 레포에서 확인한 구현이다.',b,
        '기관 연합 완성 아님  ·  SSO·기관 간 망·AMD·클라우드 운영은 별도 과제',
        'NST NAIS 소개 https://www.nst.re.kr/www/contents.do?key=358 및 SOURCE-MATERIAL.md. NAIS 실제 배포 구조도가 아니다.')

# 04: directly read each Slurm attempt.
p=source('04-slurm-loop','docs/evidence/right-sizing-slurm-attempts-v1.csv')
with p.open() as f:r=list(csv.DictReader(f))
reserve=[float(x['gpu_reservation_seconds']) for x in r]; compute=[float(x['forward_compute_seconds'])*1000 for x in r]
assert len(r)==5 and sum(reserve)==421
fig=chart('04-slurm-loop','Slurm 5회 실행: 예약량과 연산 구간을 따로 기록','2026-10-08 · 단일 native Orin 구성 · GPU 1 / CPU 1 / 1 GiB · 고정 workload',
          '모두 성공 · 합계 421 GPU 예약초. 준비·검증은 예약 구간에 포함된다.\n단일 구성의 폐루프 검증이며, 지연 감소나 자원 선택 개선을 입증하는 비교 실험은 아니다.')
labs=['자격 검증','관측 1','관측 2','관측 3','승인 실행']
bars(fig.add_axes([.07,.28,.40,.48]),labs,reserve,'자원 예약 시간','GPU 예약초','.0f',[MUTED]*4+[ACCENT])
bars(fig.add_axes([.57,.28,.38,.48]),labs,compute,'측정 순전파 구간 합','ms','.2f',[MUTED]*4+[ACCENT])
save(fig,'04-slurm-loop','Slurm 예약량과 연산 시간','원시 attempts CSV의 5개 행. compute는 측정 구간 합이며 개별 호출 지연이 아니다.')

# 05: no cherry picking compute improvement.
p=source('05-k8s-tradeoff','docs/evidence/profile-pool-20261008-v2-results/summary.json')
s=json.loads(p.read_text())['arms'];a=s['baseline'];c=s['reuse']
fig=chart('05-k8s-tradeoff','연산 구간은 짧아졌지만, 평균 완료 시간은 늘었다','2026-10-08 · K8s GPU 5노드 · 조건당 1회차 × 5작업 · 별도 실험 dispatcher',
          '대기 평균 1.0 → 4.2초 · 처리량 1,636 → 1,385 jobs/h. 본 실행 10/10 성공.\n공유 슬롯이 포함돼 슬롯 시간 절감을 물리 GPU 비용 절감으로 환산할 수 없다. 이 실험의 Slurm 작업은 0개다.')
for i,(key,title,unit,fmt) in enumerate([('synchronized_inference_seconds','측정 연산 구간 합','초','.2f'),('accelerator_slot_seconds','슬롯 할당 합','slot·초','.0f'),('mean_jct_seconds','평균 작업 완료 시간','초','.1f')]):
    bars(fig.add_axes([.065+i*.325,.29,.265,.46]),['Baseline','Profile reuse'],[a[key],c[key]],title,unit,fmt)
save(fig,'05-k8s-tradeoff','K8s 성능 지표의 상충','summary.json 직접 사용. 준비·profiling 비용은 이 본 실행 차트에서 제외. 통계적 유의성 주장 없음.')

# 06: recorded but audit stopped.
p=source('06-jct-recorded','docs/evidence/jct-profile-queue-20261008-short-v1-recorded/recorded-results.json')
d=json.loads(p.read_text());policies=['round_robin','profile_only','profile_queue'];labs=['Round Robin','Profile Only','Profile + Queue']
fig=chart('06-jct-recorded','162작업 기록: Profile + Queue의 전체 JCT 개선은 없었다','최종 독립 감사 중단 · 운영 배치 정책 미반영 · 3부하 × 3정책 × 3회차 × 6작업',
          '정책별 54작업, 독립 회차는 부하별 3회. 기록상 162 SUCCEEDED (K8s 143 / Slurm 19).\nJCT는 K8s 생성→컨테이너 종료, Slurm Submit→allocation End 기준. 초 단위 시각이며 통계적 유의성 주장은 없다.')
for i,(key,title,unit,fmt) in enumerate([('mean_jct_seconds','평균 JCT','초','.3f'),('p95_jct_seconds','p95 JCT','초','.0f'),('native_throughput_jobs_per_second','회차 기준 처리량','jobs/s','.3f')]):
    bars(fig.add_axes([.065+i*.325,.29,.265,.46]),['Round\nRobin','Profile\nOnly','Profile\n+ Queue'],[d['overall'][k][key] for k in policies],title,unit,fmt,[MUTED,'#9da8b8',ACCENT])
save(fig,'06-jct-recorded','162작업 JCT 기록','recorded-results.json 값. 최종 감사 미완료를 그림 안에 명시. 서로 다른 부하를 합친 전체 요약.')

# 07: operational evidence as quantified table, not misleading performance graph.
source('07-operations','docs/evidence/persistent-postgres.json')
source('07-operations','docs/evidence/service-deployment.json')
source('07-operations','docs/evidence/slurm-policy.json')
b=svg_text(64,248,'검증 항목',24,INK,'bold')+svg_text(480,248,'실제 기록',24,INK,'bold')+svg_text(1040,248,'주장의 경계',24,INK,'bold')
rs=[('Slurm 정책 집행','CPU2 · 2 GiB · 3분 요청 거절','다중 사용자 공정성·선점 아님'),('작업 우선순위','high가 normal보다 3초 먼저 시작','한 번의 대기 작업 순서 검증'),('DB 복사·Pod 교체','5개 테이블 내용 지문 일치','노드·디스크 소실 복구 아님'),('외부 결과 참조','46개 object · 52개 MLflow 링크','object store 복원 아님'),('서비스 재시작','관측된 combined recovery 6.78초','1회 관측 · RTO/SLO 아님')]
for i,row in enumerate(rs):
    y=312+i*88
    b+=f'<line x1="64" y1="{y+32}" x2="1536" y2="{y+32}" stroke="#d9dce1"/>'
    for x,tx in zip([64,480,1040],row):b+=svg_text(x,y,tx,24,INK if x<1040 else MUTED)
diagram('07-operations','운영 역량은 정책 집행과 복구 기록으로 설명','Slurm 정책: 2026-10-02 · DB·서비스 JSON은 정확한 실행 날짜 필드 없음',b,
        '서비스 사례: API Pod 교체 + collector의 처리된 SIGTERM  ·  전체 장애 시나리오 검증 아님',
        'slurm-policy.json, persistent-postgres.json, service-deployment.json. 사전 DB 행 수 655/52/201/8/52, 상세는 원자료.')

# 08: NPU evidence, values retained in published report, source table copy below.
p=source('08-hailo','docs/reference/hailo-resnet50.md')
vals=[.339502249,.339822885,.339404005,.343546645]
for v in vals:assert f'{v:.9f}' in p.read_text()
fig=chart('08-hailo','Hailo NPU도 같은 결과·사용량 계약으로 검증','고정 ResNet-50 / Hailo-8 · 같은 Imagenette 100장 반복 · 도우미 실행 기록',
          '4개 API 실행 모두 정확도 80/100 · 기준 예측과 일치 97/100. 독립 정확도 표본 400장이 아니다.\nAPI 4건 12 NPU 예약초 + qualification 8초 = 총 20초. NPU 메모리·전력·이용률은 미측정.')
bars(fig.add_axes([.075,.29,.53,.46]),['관측 1','관측 2','관측 3','승인 실행'],[v*1000 for v in vals],'100회 동기 추론 호출 합','ms','.2f',[MUTED]*3+[ACCENT])
fig.text(.68,.69,'80 / 100',fontsize=36,weight='bold',color=INK)
fig.text(.68,.63,'고정 입력의 top-1 정확도',fontsize=17,color=MUTED)
fig.text(.68,.49,'97 / 100',fontsize=36,weight='bold',color=INK)
fig.text(.68,.43,'TFLite 기준과 예측 일치',fontsize=17,color=MUTED)
fig.text(.68,.30,'특정 모델·입력·런타임만 검증',fontsize=15,color=MUTED)
save(fig,'08-hailo','NPU 품질 계약과 반복 실행','hailo-resnet50.md 표를 그대로 사용. 속도 개선·임의 NPU 호환성 주장 없음.')

# 09: comparison is functional positioning, not a deployment.
b=''
b+=box(64,240,1472,112,'연구자 공통 진입점 · 기관별 정책 연결 · 검증 가능한 결과',['NAIS 목표에 대한 설계 해석: 공동 활용과 기관의 운영 권한을 함께 고려'],True)
b+=box(64,404,696,232,'이 프로젝트',['공통 workload / runtime 계약','별도 K8s · Slurm adapter와 제출 복구','결과 품질·identity 검사, 사용량·결과 추적'])
b+=box(808,404,728,232,'Run:ai 공식 기능',['Kubernetes 기반 자원 배분·운영','quota · fair share · priority · preemption','GPU fractions / sharing, Slurm on K8s 접점'],existing=True)
b+=svg_text(64,716,'직접 비교 실험 없음',28,INK,'bold')+svg_text(568,716,'Run:ai adapter 미구현',28,INK,'bold')+svg_text(1072,716,'우월성 결론 없음',28,INK,'bold')
diagram('09-runai-scope','비교의 중심은 “어느 운영 계층을 맡는가”','두 열은 기능 범위 비교이며, 연동이 완료된 배포 구조를 뜻하지 않는다.',b,
        'Run:ai의 NodeSet 지원은 Slurm job/account/partition 전체 관리와 다름  ·  세부 공식 링크는 README',
        'Run:ai 공식 제품·scheduler·supported workload·Slurm tutorial 기준. 라이선스·비용·성능의 우열 비교 아님.')

# Self-contained SVG gallery, image index, source manifest, chart table and contact sheet.
with (OUT/'SOURCES.csv').open('w',newline='') as f:
    w=csv.writer(f);w.writerow(['figure','repository_path','sha256']);w.writerows(source_rows)
with (OUT/'CHART-DATA.csv').open('w',newline='') as f:
    w=csv.writer(f);w.writerow(['figure','condition','metric','value','unit'])
    for label,v,c in zip(['qualification','observe1','observe2','observe3','approved'],reserve,compute):
        w.writerow(['04',label,'reservation',v,'GPU_seconds']);w.writerow(['04',label,'compute_sum',c,'ms'])
    for k in ['baseline','reuse']:
        for metric in ['synchronized_inference_seconds','accelerator_slot_seconds','mean_jct_seconds']:w.writerow(['05',k,metric,s[k][metric],'slot_seconds' if 'slot' in metric else 'seconds'])
    for k in policies:
        for metric in ['mean_jct_seconds','p95_jct_seconds','native_throughput_jobs_per_second']:w.writerow(['06',k,metric,d['overall'][k][metric],'jobs_per_second' if 'throughput' in metric else 'seconds'])
    for i,v in enumerate(vals):w.writerow(['08',f'api_run_{i+1}','100_call_sum',v,'seconds'])
gallery=['<!doctype html><meta charset="utf-8"><title>NAIS 발표 이미지 자료</title><style>body{margin:32px;background:#e9ebee;font-family:sans-serif}section{max-width:1400px;margin:32px auto}svg{width:100%;height:auto}p{line-height:1.6}a{color:#2e5aa8}</style><h1>NAIS 발표 이미지 자료</h1><p>코드 기반 구조도와 기록 기반 데이터 차트. 실제 서비스 화면 캡처가 아닙니다.</p>']
readme=['# 발표용 이미지 자료','', 'PNG는 3200×1800(16:9)이며 바로 PPT에 삽입할 수 있다. SVG는 벡터 도형·텍스트를 편집할 수 있다. SVG의 한글 글꼴은 Noto Sans CJK 계열이 필요하며 폰트가 없으면 PNG를 사용한다.', '', '구조도는 코드 기반 재구성, 차트는 과거 기록의 시각화다. 실제 화면 캡처나 현재 가동 상태가 아니다. 중요한 표본·조건·한계는 그림 자체에도 표시했다.', '', '| 번호 | 내용 | PNG | SVG |', '|---|---|---|---|']
for name,title,caption in assets:
    svg=(OUT/f'{name}.svg').read_text();svg=svg[svg.index('<svg'):]
    gallery.append(f'<section><h2>{html.escape(title)}</h2>{svg}<p>{html.escape(caption)}</p></section>')
    readme.append(f'| {name[:2]} | {title} | [PNG]({name}.png) | [SVG]({name}.svg) |')
readme += ['', '## 그림별 설명', '']+[f'- **{n}**: {c}' for n,t,c in assets]
readme += ['', '## 데이터와 출처','', '- [CHART-DATA.csv](CHART-DATA.csv): 그림에 표시한 주요 수치와 단위.', '- [SOURCES.csv](SOURCES.csv): 데이터 원본 경로와 SHA-256. 경로는 레포 루트 기준.', '- [전체 원자료](../SOURCE-MATERIAL.md), [증거 색인](../EVIDENCE-INDEX.md).', '- 01·02 코드: src/resource_advisor/{configuration,backends,worker,service,contracts}.py', '- 03 공식: https://www.nst.re.kr/www/contents.do?key=358', '- 09 공식: https://www.nvidia.com/en-us/software/run-ai/', '- 09 scheduler: https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/platform-management/runai-scheduler/scheduling/how-the-scheduler-works', '- 09 NodeSet 범위: https://docs.nvidia.com/run-ai/self-hosted/documentation/workloads-in-nvidia-run-ai/workload-types/supported-workload-types', '- 09 Slurm: https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/tutorials/training-tutorials/deploying-slurm', '', '## 제작·재생성', '', 'build_visuals.py를 이용한 로컬 문서 산출 작업이며 시스템 구현을 변경하지 않는다. 그래프: Matplotlib, 도식: SVG/CairoSVG. 원본에서 수치 재독해, 표본·단위 확인, 이미지 육안 검수. 별도 실험 또는 독립 감사는 수행하지 않음.', '', '한글 적용: Noto Sans CJK, 제목·본문 모두 한글 가독성 우선. paper #f5f5f5 / ink #2d3142 / accent #eb6c36 / muted #4f5d75. 데이터 축은 0에서 시작. 허용되지 않은 오차막대나 유의성 표시를 추가하지 않음.', '', '시각화 절차 참고: Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents. https://doi.org/10.48550/arXiv.2609.00065 (확인된 최신 레코드 v2, 2026-09-02). 프로젝트의 실험 근거와는 별개인 제작 도구 참고문헌.']
(OUT/'gallery.html').write_text('\n'.join(gallery))
(OUT/'README.md').write_text('\n'.join(readme)+'\n')
thumb=Image.new('RGB',(1600,972),'#e9ebee')
for i,(n,_,_) in enumerate(assets):
    im=Image.open(OUT/f'{n}.png');im.thumbnail((512,288));x=16+(i%3)*528;y=16+(i//3)*320;thumb.paste(im,(x,y));ImageDraw.Draw(thumb).text((x,y+292),n,fill=INK)
thumb.save(OUT/'CONTACT-SHEET.png')
for p in OUT.iterdir():
    if p.suffix in {'.md', '.csv', '.svg', '.html'}:
        p.write_text('\n'.join(line.rstrip() for line in p.read_text().splitlines()) + '\n')
with zipfile.ZipFile(BASE/'NAIS-visual-assets.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(OUT.iterdir()):
        if p.is_file():z.write(p,'visuals/'+p.name)
    z.write(__file__,'build_visuals.py')
print(f'Rendered {len(assets)} figures to {OUT}')
