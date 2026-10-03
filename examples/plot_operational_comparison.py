"""Plot only audited operational evidence; raw points and actual repeat costs."""

import argparse
import csv
import json
import statistics
from pathlib import Path

from evaluate_operational_comparison import summarize


def main():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    summary = summarize(json.loads(a.capture.read_text()), json.loads(a.plan.read_text()))
    a.output.mkdir(exist_ok=False)
    plt.rcParams.update({"font.size": 10, "svg.hashsalt": "operational-v1"})
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.6), layout="constrained")
    arms = ["B0", "B1", "B2"]
    colors, markers = ["#0072B2", "#996000", "#704090"], ["o", "s", "^"]
    for i, (arm, color, marker) in enumerate(zip(arms, colors, markers, strict=True)):
        values = [r["raw_result_wall_seconds"] for r in summary["main_timings"] if r["arm"] == arm]
        axes[0].scatter(
            [i + (j - 2.5) * 0.06 for j in range(len(values))],
            values,
            color=color,
            marker=marker,
            s=40,
        )
        axes[0].hlines(statistics.mean(values), i - 0.23, i + 0.23, colors="black", linewidth=1.5)
        cumulative = [r for r in summary["actual_serial_cumulative"] if r["arm"] == arm]
        axes[1].plot(
            [r["uses"] for r in cumulative],
            [r["raw_wall_seconds_including_profile"] for r in cumulative],
            marker=marker,
            color=color,
            label=arm,
        )
    axes[0].set(
        xticks=range(3),
        xticklabels=["B0\nDirect", "B1\nValidated", "B2\nRecommended"],
        ylabel="Request to raw result (seconds)",
        title="A  Six observed Jobs per arm",
        ylim=(0, None),
    )
    axes[1].set(
        xlabel="Actual completed uses per arm",
        ylabel="Serial wall cost (seconds)",
        title="B  B2 profiling charged once",
        xticks=range(1, 7),
        ylim=(0, None),
    )
    axes[1].legend(frameon=False, loc="center right")
    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#DDDDDD", linewidth=0.5)
        ax.set_axisbelow(True)
    fig.suptitle(
        "One generated CNN / one RTX 5080 — descriptive operational comparison", fontsize=12
    )
    fig.savefig(a.output / "comparison.png", dpi=180)
    fig.savefig(a.output / "comparison.svg", metadata={"Date": None})
    plt.close(fig)
    svg = a.output / "comparison.svg"
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
    for name, rows in (
        ("raw-main", summary["main_timings"]),
        ("serial-cost", summary["actual_serial_cumulative"]),
    ):
        with (a.output / f"{name}.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    manifest = {
        "source_capture_digest": summary["source_capture_digest"],
        "matplotlib": matplotlib.__version__,
        "raw_points": len(summary["main_timings"]),
        "cumulative_points": len(summary["actual_serial_cumulative"]),
        "interpretation": "Panel A points are individual Jobs; black lines are means, not uncertainty intervals. Panel B connects actual repeat counts, not forecasts. Shared qualification is reported separately. Platform delivery time and the recorded collector interruption are not substituted for the common raw-result endpoint.",
    }
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (a.output / "description.txt").write_text(
        "B0 raw-result wall times cluster near seven seconds. B1 and B2 vary more; each shows six actual Jobs. B2's cumulative curve remains far above both controls through all six uses because approximately354seconds of profiling and approval preparation are charged once. No break-even is observed. One device and generated workload do not establish general effectiveness.\n"
    )


if __name__ == "__main__":
    main()
