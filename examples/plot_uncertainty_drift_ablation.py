"""Plot recorded work times and pre-target reuse masks; plotting extra only."""

import argparse
import json
from pathlib import Path


def plot(report, destination):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if report["schema_version"] != "offline-uncertainty-drift-ablation-v1":
        raise ValueError("unexpected analysis report")
    paths = [destination.with_suffix(suffix) for suffix in (".svg", ".png")]
    if any(path.exists() for path in paths):
        raise FileExistsError("refusing to overwrite a figure")
    destination.parent.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 10, "svg.fonttype": "none"})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 5), sharex=True, height_ratios=[3, 1])
    phases = report["phase_descriptives"]
    for indices, phase, label, color, marker in [
        ([1, 2, 3], "support", "Support (not held out)", "#52677c", "s"),
        ([4, 5, 6], "competitor", "CPU competitor", "#b34c18", "o"),
        ([7, 8, 9], "recovery", "Normal recovery", "#16756d", "^"),
    ]:
        top.scatter(
            indices, phases[phase]["raw_seconds"], label=label, color=color, marker=marker, s=65
        )
    row = report["decisions"][0]
    top.fill_between(
        [3.5, 9.5],
        *row["interval_seconds"],
        color="#2b5fa3",
        alpha=0.12,
        label="Original heuristic interval",
    )
    top.plot(
        [3.5, 9.5],
        [row["predicted_seconds"]] * 2,
        color="#2b5fa3",
        linestyle="--",
        label="Frozen baseline mean",
    )
    top.axvline(6.5, color="#777777", linestyle=":")
    top.text(
        6.55, 0.88, "Drift known only AFTER Job 6", transform=top.get_xaxis_transform(), fontsize=9
    )
    top.set_ylabel("Measured work time (s)")
    top.set_ylim(0, max(phases["competitor"]["raw_seconds"]) * 1.22)
    top.legend(loc="upper left", ncols=2, frameon=False, fontsize=9)
    top.grid(axis="y", alpha=0.15)
    for y, field in [(1, "strict_reuse"), (0, "without_drift_reuse")]:
        for item in report["decisions"]:
            allowed = item[field]
            bottom.scatter(
                item["target_index"] + 1,
                y,
                color="#16756d" if allowed else "#b34c18",
                marker="o" if allowed else "x",
                s=75,
            )
    bottom.set_yticks([0, 1], ["Only drift gate removed", "Strict reuse gate"])
    bottom.set_ylim(-0.6, 1.6)
    bottom.set_xticks(range(1, 10))
    bottom.set_xlim(0.5, 9.5)
    bottom.set_xlabel("Original sequential Job (1–3 support; 4–9 held-out follow-ups)")
    bottom.text(
        0.99,
        0.96,
        "circle: reuse · cross: abstain",
        transform=bottom.transAxes,
        ha="right",
        va="top",
        fontsize=9,
    )
    fig.suptitle("Chronological offline replay: a three-result drift trigger is delayed")
    fig.text(
        0.5,
        0.01,
        "One fixed-order recorded sequence · no new GPU work · no causal improvement or calibrated interval claim",
        ha="center",
        fontsize=9,
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.94])
    for path in paths:
        fig.savefig(path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plot(json.loads(args.report.read_text()), args.output)
