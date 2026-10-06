import runpy
from pathlib import Path

import pytest


check = runpy.run_path(
    str(Path(__file__).parents[1] / "examples/verify_mlflow_server_outage.py")
)["assert_gpu_requests"]


@pytest.mark.parametrize("memory", ["2048Mi", "2Gi", "2147483648"])
def test_canonical_memory_quantity_preserves_the_frozen_allocation(memory):
    check({"cpu": "1000m", "memory": memory, "nvidia.com/gpu": "1"})


@pytest.mark.parametrize(
    "change",
    [
        {"cpu": "500m"},
        {"memory": "2G"},
        {"memory": "1Gi"},
        {"nvidia.com/gpu": "2"},
        {"example.com/npu": "1"},
    ],
)
def test_different_allocation_is_still_rejected(change):
    with pytest.raises(AssertionError):
        check({"cpu": "1", "memory": "2Gi", "nvidia.com/gpu": "1", **change})
