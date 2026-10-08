"""Reproduce the three-policy native JCT audit and conditional promotion gate."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from resource_advisor.jct_evaluation import audit_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_capture(args.directory)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "summary.json").write_text(
        json.dumps(report["summary"], indent=2, sort_keys=True) + "\n"
    )
    (args.output / "input-hashes.json").write_text(
        json.dumps(report["input_hashes"], indent=2, sort_keys=True) + "\n"
    )
    for name, rows in (("main.csv", report["main_rows"]), ("cohorts.csv", report["cohort_rows"])):
        with (args.output / name).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(
                {
                    key: json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
                for row in rows
            )
    outputs = {
        name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
        for name in ("summary.json", "input-hashes.json", "main.csv", "cohorts.csv")
    }
    manifest = {
        "outputs": outputs,
        "inputs": report["input_hashes"],
        "auditor_source_sha256": hashlib.sha256(
            Path(
                __import__("resource_advisor.jct_evaluation", fromlist=["__file__"]).__file__
            ).read_bytes()
        ).hexdigest(),
        "reproduce": "PYTHONPATH=src python examples/analyze_jct_comparison.py --directory <archive> --output <new-report-directory>",
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "main_jobs": report["summary"]["main_jobs"],
                "gate_passed": report["summary"]["gate"]["passed"],
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
