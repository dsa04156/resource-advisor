"""Portfolio figures and CSVs from audited hardware captures; no extrapolation."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from audit_right_sizing_trial import audit, compare_reference


def generate(
    report_path, plan_path, output, reference_path=None, reference_plan_path=None, version="v1"
):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    raw, plan_raw = report_path.read_bytes(), plan_path.read_bytes()
    report, plan = json.loads(raw), json.loads(plan_raw)
    checked = audit(report, plan)
    comparison = None
    reference_report = report
    if reference_path:
        reference_report = json.loads(reference_path.read_bytes())
        comparison = compare_reference(
            report, plan, reference_report, json.loads(reference_plan_path.read_bytes())
        )
    output.mkdir(parents=True, exist_ok=True)
    stems = ["right-sizing-cost-" + version, "right-sizing-reference-" + version]
    paths = [output / (stem + ext) for stem in stems for ext in (".png", ".svg")]
    paths += [output / (stems[0] + ".csv"), output / ("right-sizing-figures-" + version + ".json")]
    if any(p.exists() for p in paths):
        raise FileExistsError("Use a new output directory; preserve existing artifacts")
    colors = {"static": "#4C5664", "random": "#0068B8", "qlognei": "#AF4A00"}
    markers = {"static": "s", "random": "o", "qlognei": "^"}
    styles = {"static": "-", "random": "--", "qlognei": ":"}
    curves = checked["cumulative_measured_cost"]
    with (output / (stems[0] + ".csv")).open("x", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(curves[0]))
        writer.writeheader()
        writer.writerows(curves)
    style = {
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "svg.fonttype": "none",
    }
    with plt.rc_context(style):
        fig, axes = plt.subplots(3, 2, figsize=(11, 9), layout="constrained")
        metrics = [
            ("wall_seconds", "Cumulative latency cost (s)"),
            ("device_seconds", "GPU reservation (s)"),
            ("cpu_core_seconds", "CPU reservation (core-s)"),
        ]
        for col, workload in enumerate(("W1", "W2")):
            for row, (metric, label) in enumerate(metrics):
                ax = axes[row, col]
                for arm in ("static", "random", "qlognei"):
                    points = sorted(
                        [p for p in curves if p["workload"] == workload and p["arm"] == arm],
                        key=lambda p: p["N"],
                    )
                    if points:
                        ax.plot(
                            [p["N"] for p in points],
                            [p[metric] for p in points],
                            color=colors[arm],
                            marker=markers[arm],
                            linestyle=styles[arm],
                            label=arm,
                        )
                    else:
                        ax.text(0.02, 0.95, arm + ": abstained", transform=ax.transAxes, va="top")
                ax.set(
                    xlabel="Actual main executions (N)",
                    ylabel=label,
                    xticks=[1, 2, 3],
                    ylim=(0, None),
                )
                ax.grid(axis="y", alpha=0.2)
                if row == 0:
                    ax.set_title(
                        workload
                        + (
                            ": synchronized matmul"
                            if workload == "W1"
                            else ": CNN with input preparation"
                        )
                    )
                    ax.legend(loc="best")
        fig.suptitle(
            "Qualification + profiling/confirmation + measured main executions", fontsize=14
        )
        for ext in ("png", "svg"):
            fig.savefig(
                output / (stems[0] + "." + ext), dpi=160, facecolor="white", transparent=False
            )
        plt.close(fig)
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), layout="constrained")
        for ax, workload in zip(axes, ("W1", "W2"), strict=True):
            reference = next(
                s
                for s in reference_report["studies"]
                if s["workload"] == workload and s["strategy"] == "grid_characterization"
            )
            grouped = {}
            for o in reference["study"]["observations"]:
                if o["mode"] == "confirmation" and o["outcome"] == "COMPLETED":
                    grouped.setdefault(o["candidate_ref"], []).append(
                        o["measurements"]["elapsed_seconds"]
                    )
            labels = sorted(grouped)
            complete = (
                reference["study"]["state"] == "COMPLETED"
                and len(grouped) == len(reference["study"]["spec"]["candidates"])
                and all(len(v) >= 3 for v in grouped.values())
            )
            if not complete:
                ax.text(
                    0.02,
                    0.95,
                    "Incomplete reference: no regret claim",
                    transform=ax.transAxes,
                    va="top",
                )
            for x, candidate in enumerate(labels):
                values = grouped[candidate]
                ax.scatter(
                    [x] * len(values),
                    values,
                    color="#0068B8",
                    marker="x",
                    label="Fresh Job observations" if x == 0 else None,
                )
                ax.scatter(
                    x,
                    sum(values) / len(values),
                    color="#30343B",
                    marker="D",
                    label="Mean" if x == 0 else None,
                )
            ax.set(
                xticks=range(len(labels)),
                xticklabels=[c.replace("-mem", "\n") for c in labels],
                ylabel="Measured objective (s)",
                ylim=(0, None),
                title=workload + ": later finite reference",
            )
            if grouped:
                ax.set_ylim(0, max(v for values in grouped.values() for v in values) * 1.12)
            ax.tick_params(axis="x", labelsize=9)
            ax.grid(axis="y", alpha=0.2)
            ax.legend(loc="best")
        fig.suptitle("Later fresh confirmation measurements; not a known oracle", fontsize=12)
        for ext in ("png", "svg"):
            fig.savefig(
                output / (stems[1] + "." + ext), dpi=160, facecolor="white", transparent=False
            )
        plt.close(fig)
    input_paths = [report_path, plan_path] + (
        [reference_path, reference_plan_path] if reference_path else []
    )
    manifest = {
        "schema_version": "right-sizing-figure-provenance-v1",
        "destination": "portfolio web/docs; no journal specification",
        "experiment_id": report["experiment_id"],
        "matplotlib_version": matplotlib.__version__,
        "inputs": [
            {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in input_paths
        ],
        "reference_comparison": comparison,
        "transformations": [
            "Run strict trial auditor first",
            "Sum actual qualification/study/main costs as defined in raw capture",
            "No projection beyond N=3",
            "Reference plot shows every valid fresh confirmation, with arithmetic means",
        ],
        "uncertainty": "Reference plot shows raw spread; no confidence/predictive bars or significance claim",
        "missing_data": "Abstained arms are omitted from lines and explicitly labeled; failed/censored costs remain in audit",
        "cost_semantics": report["cost_semantics"],
        "color_redundancy": "markers and line styles identify every arm",
        "export": {
            "size_inches": {"cost": [11, 9], "reference": [11, 4.8]},
            "png_dpi": 160,
            "opaque_white": True,
            "svg_fonts": "text; depends on viewer font availability",
        },
        "outputs": [
            {"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in paths
            if p.exists()
        ],
    }
    (output / ("right-sizing-figures-" + version + ".json")).open("x").write(
        json.dumps(manifest, indent=2) + "\n"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--reference-plan", type=Path)
    parser.add_argument("--version", choices=["v1", "v2"], default="v1")
    args = parser.parse_args()
    if bool(args.reference) != bool(args.reference_plan):
        parser.error("--reference and --reference-plan must be supplied together")
    print(
        json.dumps(
            generate(
                args.report,
                args.plan,
                args.output,
                args.reference,
                args.reference_plan,
                args.version,
            ),
            indent=2,
        )
    )
