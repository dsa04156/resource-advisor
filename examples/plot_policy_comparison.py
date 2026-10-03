"""Render an audited completed comparison; requires matplotlib==3.10.7 separately."""

import argparse
import csv
import hashlib
import json
import statistics
from pathlib import Path

from evaluate_policy_comparison import summarize


def render(capture, plan, predecessor, output):
    report = json.loads(capture.read_text())
    if report["plan"] != json.loads(plan.read_text()):
        raise ValueError("capture differs from frozen plan")
    previous = json.loads(predecessor.read_text()) if "predecessor" in report["plan"] else None
    summary = summarize(report, predecessor=previous)
    # No partial run, different plan or unaccounted trial reaches plotting.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    output.mkdir(parents=True, exist_ok=False)
    policies = ["lookup", "random", "qlognei"]
    labels = ["S0: lookup", "S1: random", "S2: qLogNEI"]
    colors, markers = ["#0072B2", "#996000", "#704090"], ["o", "s", "^"]
    selected_points, oracle_points = [], []
    style = {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "svg.fonttype": "none",
        "pdf.fonttype": 42,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    }
    with plt.rc_context(style):
        fig, axes = plt.subplots(2, 2, figsize=(12, 8.8), layout="constrained")
        fig.get_layout_engine().set(rect=(0, 0.12, 1, 0.70), hspace=0.13, wspace=0.06)
        fig.suptitle(
            "Three temporal blocks on one GPU\n"
            "Generated CNN numerical fixture; descriptive comparison",
            fontsize=17,
            y=0.99,
        )
        a, b, c, d = axes.flat
        for item in summary["studies"]:
            policy_index, block = policies.index(item["strategy"]), item["block"]
            x = policy_index + (block - 1) * 0.23
            study = report["studies"][item["label"]]
            observations = [
                o
                for o in study["observations"]
                if o["mode"] == "confirmation"
                and o["candidate_ref"] == item["selected_candidate"]
                and o["outcome"] == "COMPLETED"
            ]
            if not observations:
                a.text(x, 0.03, "N/A", transform=a.get_xaxis_transform(), ha="center")
            else:
                values = [1000 * o["measurements"]["elapsed_seconds"] for o in observations]
                offsets = [(i - (len(values) - 1) / 2) * 0.035 for i in range(len(values))]
                a.scatter(
                    [x + offset for offset in offsets],
                    values,
                    marker=markers[block],
                    s=40,
                    facecolors="none",
                    edgecolors=colors[block],
                    linewidths=1.4,
                    zorder=3,
                )
                mean = statistics.mean(values)
                a.plot([x - 0.065, x + 0.065], [mean, mean], color="#222222", linewidth=2)
                selected_points.extend(
                    {
                        "label": item["label"],
                        "block": block,
                        "candidate": item["selected_candidate"],
                        "attempt_id": o["attempt_id"],
                        "measured_block_sum_ms": value,
                    }
                    for o, value in zip(observations, values, strict=True)
                )
            b.scatter(
                x,
                item["first_use_gpu_seconds_including_history"],
                marker=markers[block],
                color=colors[block],
                s=50,
                zorder=3,
            )
        a.set(
            title="A   Independent confirmation of selected option",
            ylabel="Measured block sum (ms)",
        )
        b.set(
            title="B   First-use cost in each block",
            ylabel="GPU reservation (s), including history",
        )
        for ax in (a, b, c):
            ax.set_xticks(range(3), labels)
            ax.set_xlim(-0.5, 2.5)
        bottom = [0.0] * 3
        components = [
            ("History, once", "#888888", "//", None),
            ("Pilots", "#0072B2", "", "pilot"),
            ("Confirmations", "#996000", "..", "confirmation"),
        ]
        for name, color, hatch, mode in components:
            values = []
            for policy in policies:
                if mode is None:
                    value = summary["history_cost"]["gpu_seconds"] if policy == "lookup" else 0
                else:
                    refs = {
                        report["studies"][s["label"]]["ref"]
                        for s in summary["studies"]
                        if s["strategy"] == policy
                    }
                    value = sum(
                        o["allocated_device_seconds"]
                        for o in report["observations"]
                        if o["study_ref"] in refs and o["mode"] == mode
                    )
                values.append(value)
            c.bar(
                range(3),
                values,
                bottom=bottom,
                color=color,
                hatch=hatch,
                edgecolor="#333333",
                linewidth=0.6,
                width=0.52,
                label=name,
            )
            bottom = [v + base for v, base in zip(values, bottom, strict=True)]
        for i, value in enumerate(bottom):
            if value != summary["three_block_gpu_cost_including_history_once"][policies[i]]:
                raise ValueError("figure component costs differ from audited totals")
            c.annotate(
                f"{value:g} s", (i, value), xytext=(0, 6), textcoords="offset points", ha="center"
            )
        c.set(
            title="C   Actual cost across all three uses",
            ylabel="GPU reservation (s); history charged once",
        )
        c.legend(fontsize=8, loc="upper center", ncol=3)
        c.set_ylim(0, max(bottom) * 1.4)
        oracle = report["studies"]["oracle"]
        candidates = sorted(
            oracle["spec"]["candidates"], key=lambda v: v["context"]["resources"]["host_cpu"]
        )
        for i, candidate in enumerate(candidates):
            observations = [
                o
                for o in oracle["observations"]
                if o["candidate_ref"] == candidate["ref"] and o["mode"] == "confirmation"
            ]
            values = [1000 * o["measurements"]["elapsed_seconds"] for o in observations]
            d.scatter([i - 0.07, i, i + 0.07], values, color="#222222", marker="D", s=30)
            mean = statistics.mean(values)
            d.plot([i - 0.15, i + 0.15], [mean, mean], color="#222222", linewidth=2)
            oracle_points.extend(
                {
                    "candidate": candidate["ref"],
                    "attempt_id": o["attempt_id"],
                    "measured_block_sum_ms": value,
                }
                for o, value in zip(observations, values, strict=True)
            )
        d.set_xticks(
            range(3), [f"{v['context']['resources']['host_cpu']:g} CPU" for v in candidates]
        )
        d.set(
            title="D   Later reference: three repeats per option",
            ylabel="Measured block sum (ms)",
        )
        for ax in (a, b, d):
            ax.set_ylim(bottom=0)
        for ax in axes.flat:
            ax.grid(axis="y", color="#dddddd", linewidth=0.6)
            ax.set_axisbelow(True)
        fig.legend(
            [
                Line2D([], [], marker=m, color=color, linestyle="none", markersize=7)
                for m, color in zip(markers, colors, strict=True)
            ],
            ["Block 1", "Block 2", "Block 3"],
            loc="upper center",
            bbox_to_anchor=(0.5, 0.9),
            ncol=3,
            frameon=False,
        )
        fig.text(
            0.035,
            0.014,
            "A/D: raw independent confirmation runs; short horizontal marks are means, not confidence intervals.\n"
            "Time sums 12 measured blocks (32 CUDA forwards each), including preprocessing and transfer; not request latency.\n"
            "B/C: reservation time, not active GPU compute or energy. Qualification, oracle and predecessor costs are outside policy bars.\n"
            f"Current protocol total: {summary['total_gpu_reservation_seconds']:g} GPU s; retained stopped predecessor: "
            f"{summary['retained_predecessor_gpu_reservation_seconds']:g} GPU s. No statistical superiority claim.",
            fontsize=9,
            color="#333333",
            linespacing=1.5,
        )
        fig.savefig(output / "comparison.png", dpi=180, facecolor="white")
        fig.savefig(output / "comparison.svg", facecolor="white")
        plt.close(fig)
    for filename, rows in [
        ("selected-confirmations.csv", selected_points),
        ("oracle-confirmations.csv", oracle_points),
        ("study-summary.csv", summary["studies"]),
    ]:
        if rows:
            with (output / filename).open("x") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    manifest = {
        "source_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "matplotlib_version": matplotlib.__version__,
        "size_inches": [12, 8.8],
        "raster_dpi": 180,
        "destination": "Public repository report; no target journal",
        "transforms": [
            "Seconds multiplied by 1000 for A/D",
            "Arithmetic means of independent confirmation runs",
            "Fixed horizontal offsets for legibility, no value jitter",
            "GPU reservation sums, history charged once in C and in each first-use scenario in B",
        ],
        "exclusions": "A displays the confirmed selected option only; all candidates and all outcomes remain in the source capture. D uses only the later independent reference confirmations.",
        "uncertainty": "Individual runs and means, no CI; three repeated temporal blocks on one device",
        "missingness": "Abstaining policy has N/A and no invented timing point; unknown queue time is not plotted as zero",
        "limitations": summary["limitations"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "description.txt").write_text(
        "Four-panel comparison of lookup, random and qLogNEI on one GPU. "
        "Panel A shows each selected option's three independent confirmation durations for every temporal block. "
        "Panel B includes the history acquisition cost for each first-use lookup scenario. "
        "Panel C charges the same history once across three actual reuses, and separates history, pilots and confirmations. "
        "Panel D displays three later reference runs for every CPU option. "
        "All durations are measured block sums, not individual request latency. "
        "Raw points, configuration selections and costs are available in the accompanying CSVs. "
        "No statistical superiority inference is drawn.\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--predecessor-stop", type=Path, default=Path("docs/evidence/policy-stop.json")
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory; existing data is never overwritten",
    )
    args = parser.parse_args()
    render(args.capture, args.plan, args.predecessor_stop, args.output)
