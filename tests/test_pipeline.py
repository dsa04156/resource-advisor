import importlib.util
from pathlib import Path

import pytest


def test_compiled_launcher_does_not_request_gpu_or_cache(tmp_path):
    pytest.importorskip("kfp.kubernetes")
    import yaml
    from kfp import compiler

    path = Path(__file__).parents[1] / "examples" / "pipeline.py"
    spec = importlib.util.spec_from_file_location("example_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "pipeline.yaml"
    compiler.Compiler().compile(module.observe, str(output))
    documents = list(yaml.safe_load_all(output.read_text()))
    pipeline = documents[0]
    task = pipeline["root"]["dag"]["tasks"]["launch"]
    assert not task.get("cachingOptions", {}).get("enableCache", False)
    container = pipeline["deploymentSpec"]["executors"]["exec-launch"]["container"]
    assert "accelerator" not in container["resources"]
    assert container["command"] == ["python", "-m", "resource_advisor.launcher"]
    assert "RA_API_TOKEN" in output.read_text()
    assert "secretNameParameter" in output.read_text()
