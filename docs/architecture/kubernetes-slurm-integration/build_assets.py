#!/usr/bin/env python3
"""Rebuild documentation only. No network or cluster operations.
uv run --isolated --with cairosvg==2.9.1 --with markdown==3.11.2 --with weasyprint==70.0 python build_assets.py
"""
from pathlib import Path
import argparse, base64, hashlib, html, json, re, math
import cairosvg
from urllib.parse import quote
BASE = Path(__file__).resolve().parent
INK, MUTED, BLUE, PAPER = '#2d3142', '#4f5d75', '#2e5aa8', '#f5f5f5'

class Diagram:
    def __init__(self, title, subtitle, index):
        self.parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080" role="img" aria-labelledby="title desc"><title id="title">{html.escape(title)}</title><desc id="desc">{html.escape(subtitle)}</desc><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="{BLUE}"/></marker></defs><rect width="1920" height="1080" fill="{PAPER}"/><g font-family="Noto Sans CJK KR, sans-serif">''']
        self.text(64,76,f'RESOURCE ADVISOR / {index}',20,MUTED)
        self.text(64,140,title,44,INK,True)
        self.text(64,188,subtitle,24,MUTED)
    def text(self,x,y,s,size=24,color=INK,bold=False):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{700 if bold else 400}">{html.escape(s)}</text>')
    def node(self,x,y,w,h,title,lines=(),kind='custom'):
        fill={'custom':'#ffffff','native':'#e9ebee','focus':'#fcece4'}[kind]
        stroke='#eb6c36' if kind=='focus' else MUTED
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        self.text(x+24,y+48,title,32,INK,True)
        for i,line in enumerate(lines): self.text(x+24,y+88+i*36,line,24,MUTED)
    def edge(self,points,label='',xy=None,dashed=False):
        p=' '.join(f'{x},{y}' for x,y in points)
        dash=' stroke-dasharray="10 8"' if dashed else ''
        self.parts.append(f'<polyline points="{p}" fill="none" stroke="{BLUE}" stroke-width="3"{dash}/>')
        x,y=points[-1];px,py=points[-2]
        length=math.hypot(x-px,y-py);dx,dy=(x-px)/length,(y-py)/length
        arrow=f'{x},{y} {x-20*dx-10*dy},{y-20*dy+10*dx} {x-20*dx+10*dy},{y-20*dy-10*dx}'
        self.parts.append(f'<polygon points="{arrow}" fill="{BLUE}"/>')
        if xy:
            x,y=xy
            self.parts.append(f'<rect x="{x-8}" y="{y-24}" width="{max(72,len(label)*16)}" height="32" fill="{PAPER}"/>')
            self.text(x,y,label,20,BLUE)
    def footer(self,s):
        self.parts.append('<path d="M64 988 H1856" stroke="#cbd0d8"/>')
        self.text(64,1024,s,20,MUTED)
        self.text(64,1056,'출처: 소스 코드 · 2026-10-29 관측 스냅샷 · Slurm 배포 증거 / 성능 측정 결과가 아님',20,MUTED)
    def save(self,name,mermaid):
        out=BASE/'figures';out.mkdir(exist_ok=True)
        svg=''.join(self.parts)+'</g></svg>'
        (out/f'{name}.svg').write_text(svg)
        (out/f'{name}.mmd').write_text(mermaid.strip()+'\n')
        cairosvg.svg2png(bytestring=svg.encode(),write_to=str(out/f'{name}.png'))

def figures():
    d=Diagram('Kubernetes·Slurm을 연결하는 상위 운영 계층','공통 요청·상태·사용량을 관리하고, 최종 자원 할당은 각 Native Scheduler가 수행한다.','01')
    d.edge([(384,292),(512,292)],'HTTPS',(408,272))
    d.edge([(912,292),(1088,292)],'읽기·기록',(940,272))
    d.edge([(912,560),(992,560),(992,392),(1200,392),(1200,360)])
    d.edge([(1256,488),(1256,360)])
    d.edge([(1688,488),(1688,360)])
    d.edge([(712,664),(712,784)],dashed=True)
    d.edge([(1256,664),(1256,728),(872,728),(872,784)])
    d.edge([(1688,664),(1688,784)],'SSH',(1712,728))
    d.node(64,224,320,136,'연구자·운영자',['Console / 공통 API'])
    d.node(512,224,400,136,'Resource Advisor',['요청 계약 · 권한 · 라우팅'],'focus')
    d.node(1088,224,768,136,'PostgreSQL',['인벤토리 · Job / Attempt · Outbox · Usage'],'native')
    d.node(512,488,400,176,'Lifecycle Worker',['Kubernetes worker 활성','가상 Slurm worker 미배포'])
    d.node(1088,488,336,176,'K8s 수집기',['API · Prometheus','30초 주기'])
    d.node(1520,488,336,176,'Slurm 수집기',['SSH 제한 명령','30초 주기'])
    d.node(64,784,848,160,'Kubernetes · Kueue',['실제 클러스터 12노드 · GPU / NPU / Jetson','상태 partial: 일부 관측 누락'],'native')
    d.node(1088,784,768,160,'Docker Slurm · SlurmDBD',['가상 6노드 · GPU 슬롯 8개 · 성능 의미 없음','관측 연결 / 새 작업 제출 0건'],'native')
    d.footer('흰색: 자체 구현 · 회색: 기존 오픈소스 · 실선: 연결된 관측/저장 경로 · 점선: 구현된 실행 경로')
    d.save('01-overview','''flowchart TD
 U[연구자 / Console] --> A[Resource Advisor API]
 A --> D[(PostgreSQL)]
 W[Lifecycle Worker] --> D
 W -. Kubernetes 실행 경로 .-> K[Kubernetes / Kueue]
 KI[K8s 수집기] --> D
 SI[Slurm 수집기] --> D
 KI -->|API / Prometheus| K
 SI -->|SSH 제한 명령| S[Docker Slurm / SlurmDBD]
 %% Virtual Slurm worker is NOT deployed.
''')
    d=Diagram('Kubernetes: 관측 경로와 실행 경로','API·메트릭을 공통 상태로 정규화한다. Kueue는 Admission, kube-scheduler는 Pod 배치를 담당한다.','02')
    d.text(64,252,'현재 연결된 관측 경로',24,BLUE,True)
    d.edge([(624,376),(744,376)]);d.edge([(1160,376),(1280,376)])
    d.node(64,288,560,176,'Kubernetes API · Prometheus',['Node / Pod / Queue 상태','GPU·NPU·Jetson exporter 메트릭'],'native')
    d.node(744,288,416,176,'Inventory Collector',['자원 단위 · 출처 · 관측 시각','미측정 unknown / 오래되면 stale'])
    d.node(1280,288,576,176,'공통 DB · 자원 화면',['물리 GPU / 논리 슬롯 구분','할당량과 실제 사용률을 분리'],'focus')
    d.text(64,592,'코드로 구현된 실행 경로 — 이번 문서 작업에서 새 Job 실행·복구 실증은 하지 않음',24,BLUE,True)
    for a,b in [(480,600),(936,1056),(1392,1512)]:d.edge([(a,736),(b,736)],dashed=True)
    d.node(64,648,416,176,'Worker · K8s Adapter',['Job 생성 · hostname 제약','후보 노드를 nodeSelector로 고정'])
    d.node(600,648,336,176,'Kueue',['Quota / Admission','승인 전 suspend'],'native')
    d.node(1056,648,336,176,'kube-scheduler',['nodeSelector 제약 확인','제약 안에서 최종 bind'],'native')
    d.node(1512,648,344,176,'kubelet · 실행',['GPU / NPU Job','상태 · 결과 수집'],'native')
    d.text(64,888,'현재 RA는 후보 노드를 hostname으로 고정한다. 자원풀만 지정하고 노드를 위임하는 방식은 향후 개선 항목이다.',24,MUTED)
    d.text(64,932,'Kueue Admission과 kube-scheduler 최종 bind는 유지한다. KAI 연계·전역 GPU 예약은 검증 범위 밖이다.',24,MUTED)
    d.footer('흰색: 자체 구현 · 회색: 기존 오픈소스 · 실선: 현재 관측 · 점선: 구현된 실행 흐름(이번 신규 실증 없음)')
    d.save('02-kubernetes','''flowchart LR
 subgraph OBS[현재 관측]
 K[Kubernetes API / Prometheus] --> I[Inventory Collector] --> D[공통 DB / 자원 화면]
 end
 subgraph EXEC[구현된 실행 경로 - 이번 신규 실증 없음]
 W[Worker / K8s Adapter - hostname nodeSelector 고정] -. API Job 생성 .-> Q[Kueue Admission]
 Q -. 승인 .-> S[kube-scheduler - 제약 내 최종 bind]
 S -.-> R[kubelet / Job 실행]
 end
''')
    d=Diagram('가상 Slurm: SSH 관측·회계 연결','SSH 22 → forced-command → docker exec. 공개 포트 16817 → 6817은 어댑터 연결에 사용하지 않는다.','03')
    d.edge([(480,336),(592,336)]);d.edge([(1056,336),(1200,336)])
    d.edge([(480,624),(536,624),(536,400),(592,400)])
    d.edge([(1528,424),(1528,536)],dashed=True)
    d.edge([(1200,392),(1120,392),(1120,624),(1056,624)],'6819',(1136,504))
    d.edge([(824,712),(824,804)],'3306',(848,768))
    d.node(64,248,416,176,'관측 전용 수집기',['scontrol / squeue JSON','ra-lab 계정 범위 조회'])
    d.node(592,248,464,176,'SSH Forced-command',['키 분리 · 명령 허용 목록','고정 host key · 출발지 제한'],'focus')
    d.node(1200,248,656,176,'slurmctld 24.11.5',['Docker 컨테이너의 sim 클러스터','호스트 native Slurm과 분리'],'native')
    d.node(64,536,416,176,'실행 어댑터·전용 키',['squeue / sacct 조회 검증','제출 route 미등록·worker 미배포'])
    d.node(592,536,464,176,'SlurmDBD 24.11.5',['회계 조회 · 계정 association','sacct 가능 / 신규 Job 행 0'],'native')
    d.node(1200,536,656,176,'slurmd 컨테이너 6개',['가상 GPU 슬롯 8개 (4 / 2 / 1 / 1)','실제 GPU 연산·성능 검증 없음'],'native')
    d.node(592,804,464,136,'MariaDB 11.8.9',['내부 Docker망 · 영속 볼륨'],'native')
    d.text(1200,852,'회계 DB는 호스트 포트를 공개하지 않음',24,MUTED)
    d.text(1200,892,'신뢰된 Docker 운영 계정의 명령 경계',24,MUTED)
    d.text(1200,932,'멀티테넌트·root 격리를 입증한 것은 아님',24,MUTED)
    d.footer('실선: 연결/조회 확인 · 점선: Slurm 고유 실행 관계(이번 작업 제출 0건) · 가상 슬롯 ≠ 물리 GPU')
    d.save('03-slurm','''flowchart LR
 O[관측 수집기] --> G[SSH forced-command]
 E[실행 어댑터 키 - 조회만 검증] --> G
 G -->|docker exec| C[slurmctld]
 C -. 실행 미검증 .-> N[slurmd 6개 / 가상 GPU 슬롯 8]
 C -->|6819| A[SlurmDBD]
 A -->|3306| M[(MariaDB / 영속 볼륨)]
 %% Public 16817 -> 6817 is not the RA adapter transport.
''')

def document():
    import markdown
    from weasyprint import HTML
    chunks=[]
    for i,name in enumerate(['ARCHITECTURE-KO.md','PRESENTATION-NOTES-KO.md','SOURCES.md']):
        content=markdown.markdown((BASE/name).read_text(),extensions=['tables','fenced_code','toc'])
        def figure(m):
            return '<figure>'+(BASE/m.group(1).replace('.png','.svg')).read_text()+'</figure>'
        content=re.sub(r'<p><img[^>]*src="(figures/[^\"]+\.png)"[^>]*/?></p>',figure,content)
        def portable_link(m):
            target=html.unescape(m.group(1))
            local_parts={'ARCHITECTURE-KO.md':'part-0','PRESENTATION-NOTES-KO.md':'part-1','SOURCES.md':'part-2'}
            name=target.split('#')[0]
            if name in local_parts:
                return f'href="#{local_parts[name]}"'
            if target.startswith(('https:','http:','#','mailto:')):
                return m.group(0)
            path=(BASE/name).resolve()
            relative=path.relative_to(BASE.parents[2])
            url='https://github.com/dsa04156/resource-advisor/blob/docs/integration-architecture-20261029/'+quote(str(relative),safe='/')
            return f'href="{url}"'
        content=re.sub(r'href="([^"]+)"',portable_link,content)
        chunks.append(f'<article id="part-{i}">{content}</article>')
    css='''@font-face{font-family:Noto;src:url(file:///usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc)}
body{font-family:Noto,sans-serif;color:#2d3142;background:#f5f5f5;margin:0;line-height:1.7}main{max-width:1240px;margin:auto;padding:48px;background:white}h1{font-size:30px;line-height:1.4}h2{margin-top:40px;color:#2e5aa8}h3{margin-top:28px}a{color:#2e5aa8;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #ccd2dc;padding:9px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#e9ebee}pre{padding:16px;background:#f5f5f5;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}code{font-size:.88em;overflow-wrap:anywhere}blockquote{border-left:4px solid #eb6c36;margin:24px 0;padding:8px 20px;background:#fff5ed}figure{margin:32px 0}figure svg{width:100%;height:auto}article+article{border-top:4px solid #2e5aa8;margin-top:64px;padding-top:32px}
@page{size:A4;margin:18mm 15mm;@bottom-right{content:counter(page);font-size:9pt;color:#4f5d75}}
@page diagram{size:A4 landscape;margin:12mm}
@media print{body{background:white;font-size:9pt;line-height:1.55}main{padding:0;max-width:none}h1{font-size:20pt}h2{font-size:15pt;margin-top:20pt}h3{font-size:11pt}table{font-size:7.5pt;table-layout:fixed}td,th{padding:5px}thead{display:table-header-group}tr{break-inside:avoid}h1,h2,h3{break-after:avoid}pre{font-size:7.5pt}figure{page:diagram;break-before:page;break-after:page;margin:0}figure svg{width:100%;height:auto}article+article{break-before:page;border:0;margin:0;padding:0}a{color:#2e5aa8;text-decoration:none}}
'''
    doc='<!doctype html><html lang="ko"><meta charset="utf-8"><title>Resource Advisor 통합 아키텍처</title><style>'+css+'</style><body><main>'+''.join(chunks)+'</main></body></html>'
    (BASE/'architecture.html').write_text(doc)
    HTML(string=doc,base_url=str(BASE)).write_pdf(BASE/'architecture.pdf')

def manifest():
    paths=[p for p in BASE.rglob('*') if p.is_file() and p.name!='artifact-manifest.json' and '__pycache__' not in p.parts]
    data={'scope':'Documentation and diagrams; no performance measurements generated','render_dependencies':{'cairosvg':'2.9.1','markdown':'3.11.2','weasyprint':'70.0'},'files':{str(p.relative_to(BASE)):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths)}}
    (BASE/'artifact-manifest.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

def screenshot_gallery():
    from weasyprint import HTML
    items=[
        ('01-resource-overview.png','통합 자원 현황','Kubernetes·가상 Slurm·과거 Slurm 이력이 함께 표시된다. 등록 노드 수는 물리 GPU 수가 아니다.'),
        ('02-kubernetes-accelerators.png','Kubernetes GPU·NPU 관측','Jetson·Hailo·Rockchip의 장치 상태와 예약량. 미측정 값과 스케줄 불가 상태도 그대로 표시한다.'),
        ('03-mobilint-slurm.png','Mobilint ARIES2 · 가상 Slurm','Mobilint 센서 관측과 가상 Slurm 슬롯 4/2/1/1. 가상 슬롯의 실제 GPU 연산을 검증한 것은 아니다.'),
        ('04-slurm-fleet.png','가상 Slurm GPU 노드','Slurm 필터와 virtual-slurm-140-gpu 검색을 적용한 실제 화면. 아래 행 일부는 뷰포트 밖이다.'),
        ('05-queues-quota.png','큐 · 할당량','새 가상 Slurm의 계정 범위 조회 0건과 Kueue 관측 상태. 기존 Slurm의 확인 불가도 함께 표시한다.'),
        ('06-usage-accounting.png','프로젝트 할당 사용량','과거 종료 작업의 집계 화면. 새 실험 결과가 아니며, 과거 Slurm Orin 이력을 가상 Slurm 성과로 해석하지 않는다.'),
    ]
    if not all((BASE/'screenshots'/f).exists() for f,_,_ in items):return
    pages=[]
    for f,title,caption in items:
        data=base64.b64encode((BASE/'screenshots'/f).read_bytes()).decode()
        pages.append(f'<section><h1>{html.escape(title)}</h1><img alt="{html.escape(title)}" src="data:image/png;base64,{data}"><p>{html.escape(caption)}</p><footer>실제 포털 캡처 · 2026-10-29 17:26–17:27 KST · 원본 1920×1080 · 화면 수치 변경 없음</footer></section>')
    css='''@font-face{font-family:Noto;src:url(file:///usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc)}
body{font-family:Noto,sans-serif;color:#2d3142;background:#eef0f4;margin:0}section{max-width:1440px;background:white;margin:24px auto;padding:24px}h1{font-size:24px;margin:0 0 12px}img{width:100%;height:auto}p{font-size:14px;margin:10px 0}footer{font-size:12px;color:#4f5d75}@page{size:A4 landscape;margin:8mm}@media print{body{background:white}section{padding:0;margin:0;break-after:page}section:last-child{break-after:auto}h1{font-size:13pt;margin:0 0 3mm}img{height:151mm;width:100%;object-fit:contain}p{font-size:8pt;margin:2mm 0}footer{font-size:7pt}}
'''
    doc='<!doctype html><html lang="ko"><meta charset="utf-8"><title>실제 포털 화면 캡처</title><style>'+css+'</style><body>'+''.join(pages)+'</body></html>'
    (BASE/'screenshots.html').write_text(doc)
    HTML(string=doc,base_url=str(BASE)).write_pdf(BASE/'screenshots.pdf')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--figures-only',action='store_true');args=p.parse_args()
    figures()
    if not args.figures_only:
        document()
        screenshot_gallery()
    manifest()
    print('Generated three 1920x1080 diagrams'+('' if args.figures_only else ', HTML and PDF'))
