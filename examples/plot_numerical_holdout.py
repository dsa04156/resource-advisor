"""Plot all raw Job timings and optional source-only numerical holdout intervals.

Requires matplotlib only for this report command; never contacts a backend.
"""

import argparse
import json
from pathlib import Path


def render(report, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    numerical = "whole_workload_folds" in report
    folds = report["whole_workload_folds" if numerical else "whole_workload_rank_holdout"]
    with plt.rc_context({"svg.hashsalt": "ra-numerical-holdout-v1", "font.size": 10}):
        fig, axes = plt.subplots(1, len(folds), figsize=(12, 4.8), sharey=True)
        limits = []
        for ax, fold in zip(axes, folds, strict=True):
            predictions = (
                {
                    r["descriptor"]["candidate_ref"]: r["prediction"]
                    for r in fold["forecast"]["targets"]
                }
                if numerical
                else {}
            )
            for i, (ref, sample) in enumerate(sorted(fold["held_out_samples"].items())):
                times = sample["raw_seconds"]
                limits.extend(times)
                ax.scatter(
                    [i + (j - 1) * 0.07 for j in range(len(times))],
                    times,
                    s=30,
                    color="#202b38",
                    zorder=4,
                )
                prediction = predictions.get(ref)
                if prediction:
                    for offset, kind, color in (
                        (-0.17, "job", "#0072b2"),
                        (0.17, "latent", "#d55e00"),
                    ):
                        lo, hi = prediction[f"{kind}_interval_seconds"]
                        limits.extend((lo, hi))
                        ax.vlines(i + offset, lo, hi, color=color, linewidth=3)
                        ax.plot(
                            [i + offset],
                            [prediction["median_seconds"]],
                            marker="_",
                            markersize=11,
                            color=color,
                        )
            suffix = (
                (
                    "shadow interpolation"
                    if any(predictions.values())
                    else "abstain: outside source range"
                )
                if numerical
                else "raw observations"
            )
            ax.set_title(f"Withheld {fold['input_shape'][2]} x {fold['input_shape'][3]}\n{suffix}")
            ax.set_xticks(range(3), ["0.5", "1", "2"])
            ax.set_xlabel("Requested CPU cores")
            ax.set_xlim(-0.45, 2.45)
            ax.set_yscale("log")
            ax.grid(axis="y", which="both", alpha=0.2)
        axes[0].set_ylim(min(limits) * 0.85, max(limits) * 1.2)
        axes[0].set_ylabel("Complete Job objective (seconds, log scale)")
        handles = [Line2D([], [], color="#202b38", marker="o", linestyle="", label="Actual Job")]
        if numerical:
            handles.extend(
                [
                    Line2D([], [], color=c, linewidth=3, label=label)
                    for c, label in (
                        ("#0072b2", "Approximate individual-Job interval"),
                        ("#d55e00", "Latent function interval"),
                    )
                ]
            )
        fig.legend(
            handles=handles, loc="lower center", ncol=len(handles), bbox_to_anchor=(0.5, 0.07)
        )
        fig.suptitle("Whole-shape holdout: 27 existing GPU Jobs, no new hardware runs", y=0.98)
        fig.text(
            0.5,
            0.02,
            "One CNN / GPU; correlated folds. Intervals are uncalibrated; no execution approval.",
            ha="center",
            fontsize=10,
        )
        fig.tight_layout(rect=(0, 0.17, 1, 0.94))
        fig.savefig(output, dpi=160, metadata={"Date": None} if output.suffix == ".svg" else None)
        plt.close(fig)
        if output.suffix == ".svg":
            output.write_text(
                "\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must not already exist")
    render(json.loads(args.input.read_text()), args.output)


if __name__ == "__main__":
    main()
