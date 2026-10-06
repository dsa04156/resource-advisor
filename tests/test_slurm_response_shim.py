import runpy
from pathlib import Path

import pytest

matching_submission = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "examples/hold_slurm_submit_response.py")
)["matching_submission"]


@pytest.mark.parametrize(
    "command, target, lines, expected",
    [
        ("sbatch --parsable", "attempt-one", "#SBATCH --job-name=attempt-one", True),
        ("squeue", "attempt-one", "#SBATCH --job-name=attempt-one", False),
        ("sbatch --parsable", "attempt-two", "#SBATCH --job-name=attempt-one", False),
        ("sbatch --parsable", None, "#SBATCH --job-name=attempt-one", False),
        ("sbatch --parsable", "attempt-one", "#SBATCH --job-name=attempt-one-extra", False),
        (
            "sbatch --parsable",
            "attempt-one",
            "#SBATCH --job-name=attempt-one\n#SBATCH --job-name=other",
            False,
        ),
        ("sbatch --parsable --wrap=other", "attempt-one", "#SBATCH --job-name=attempt-one", False),
    ],
)
def test_response_fault_is_bound_to_one_platform_attempt(command, target, lines, expected):
    script = "#!/bin/bash\n" + lines + "\n#SBATCH --comment=resource-advisor:job-one\n"
    assert matching_submission(["target-alias", command], script, target) is expected
