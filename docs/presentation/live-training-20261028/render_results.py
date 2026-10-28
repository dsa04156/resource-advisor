"""Render presentation figures from the published acceptance records; no live access."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Patch

ROOT = Path(__file__).resolve().parent
FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
if FONT.exists():
    import matplotlib.font_manager as fm

    fm.fontManager.addfont(str(FONT))
    plt.rcParams["font.family"] = FontProperties(fname=str(FONT)).get_name()
plt.rcParams.update({"font.size": 14, "axes.spines.top": False, "axes.spines.right": False})
INK, BLUE, TEAL = "#17243b", "#2563eb", "#0f766e"
records = json.loads((ROOT / "../../evidence/live-training-20261028.json").read_text())
samples = json.loads((ROOT / "progress-samples.json").read_text())
jobs = {row["job_id"]: row for row in records["jobs"]}
names = list(samples)
kst = timezone(timedelta(hours=9))

fig, ax = plt.subplots(figsize=(16, 9), facecolor="white")
fig.subplots_adjust(left=0.16, right=0.95, top=0.76, bottom=0.24)
colors = {"QUEUED": "#cbd5e1", "RUNNING": BLUE, "COLLECTING": "#99c4f5"}
for row, job_id in enumerate(names):
    events = jobs[job_id]["lifecycle"]
    for index, event in enumerate(events[:-1]):
        state = event["state"]
        if state not in colors:
            continue
        start = mdates.date2num(datetime.fromisoformat(event["at"]))
        end = mdates.date2num(datetime.fromisoformat(events[index + 1]["at"]))
        color = TEAL if state == "RUNNING" and jobs[job_id]["backend"] == "slurm" else colors[state]
        ax.barh(row, end - start, left=start, height=0.52, color=color)
    end = mdates.date2num(datetime.fromisoformat(events[-1]["at"]))
    ax.plot(end, row, "o", color=INK, markersize=7)
    ax.annotate("완료", (end, row), xytext=(10, 0), textcoords="offset points", va="center", fontsize=12)
ax.set_yticks(range(4), [samples[j]["label"] for j in names])
ax.invert_yaxis()
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=kst))
ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=1))
ax.set_xlabel("2026-10-28 관측 시각 (KST)", labelpad=14)
ax.grid(axis="x", alpha=0.18)
ax.set_axisbelow(True)
ax.margins(x=0.12)
ax.legend(handles=[Patch(color="#cbd5e1", label="네이티브 큐 대기"), Patch(color=BLUE, label="Kubernetes 실행"), Patch(color=TEAL, label="Slurm VM 실행"), Patch(color="#99c4f5", label="결과 수집")], loc="upper left", bbox_to_anchor=(0, 1.11), ncol=4, frameon=False, fontsize=12)
fig.text(0.08, 0.90, "두 실행 환경에서 확인한 대기 → 실행 → 완료", fontsize=26, weight="bold", color=INK)
fig.text(0.08, 0.84, "Resource Advisor가 기록한 실제 상태 전이 · 각 백엔드의 2 CPU 큐", fontsize=16, color="#52647a")
fig.text(0.08, 0.10, "워커의 관측 시각 기준입니다. 폴링 지연으로 실행 구간이 겹쳐 보일 수 있습니다.\n서로 다른 설정의 CPU 학습입니다. QEMU TCG VM과 서버의 성능 비교가 아닙니다.", fontsize=12, color="#52647a")
fig.savefig(ROOT / "08-native-queue-timeline.png", dpi=160)
fig.savefig(ROOT / "08-native-queue-timeline.pdf")
plt.close(fig)

fig, axes = plt.subplots(1, 2, figsize=(16, 9), facecolor="white")
fig.subplots_adjust(left=0.09, right=0.95, top=0.74, bottom=0.24, wspace=0.24)
for ax, group, title in zip(axes, [names[:2], names[2:]], ["Kubernetes / Kueue", "Slurm VM"]):
    for job_id, color in zip(group, [BLUE, TEAL]):
        rows = samples[job_id]["samples"]
        ax.plot([r["epoch"] for r in rows], [r["loss"] for r in rows], "o-", color=color, markersize=5, linewidth=1.5, label=f'{samples[job_id]["label"]} · {len(rows)} 관측점')
    ax.set_title(title, fontsize=20, color=INK, pad=20)
    ax.set_xlabel("epoch", labelpad=12)
    ax.set_ylabel("학습 loss (로그 축)")
    ax.set_yscale("log")
    ax.grid(alpha=0.16)
    ax.legend(frameon=False, fontsize=12)
fig.text(0.08, 0.90, "학습 로그에서 수집한 epoch와 loss", fontsize=26, weight="bold", color=INK)
fig.text(0.08, 0.84, "실제 SGD 가중치 갱신 · 완료 작업의 초기/최종 checkpoint digest 변경 확인", fontsize=16, color="#52647a")
fig.text(0.08, 0.10, "점은 보관된 폴링 관측값이며 선은 관측점을 연결합니다.\n합성 회귀 데이터의 학습 loss입니다. 일반화 성능이나 백엔드 우열을 나타내지 않습니다.", fontsize=12, color="#52647a")
fig.savefig(ROOT / "09-observed-training-loss.png", dpi=160)
fig.savefig(ROOT / "09-observed-training-loss.pdf")
plt.close(fig)
