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
    assert "kubernetes.io/arch" in output.read_text()
    assert "SSL_CERT_FILE" in output.read_text()
    assert "/var/run/resource-advisor/ca.crt" in output.read_text()
    assert "--owner-lease-seconds" in container["args"]
    assert (
        pipeline["root"]["inputDefinitions"]["parameters"]["owner_lease_seconds"]["defaultValue"]
        == 60
    )


def test_server_requires_both_tls_files(monkeypatch):
    from resource_advisor.cli import main

    monkeypatch.setattr(
        "sys.argv",
        ["resource-advisor", "serve", "--credentials", "/unused", "--ssl-certfile", "/unused"],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cpu_gpu_workflow_has_uncached_execution_dependency(tmp_path, monkeypatch):
    pytest.importorskip("kfp.kubernetes")
    import yaml
    from kfp import compiler

    folder = Path(__file__).parents[1] / "examples"
    monkeypatch.syspath_prepend(str(folder))
    import cpu_gpu_pipeline

    output = tmp_path / "cpu-gpu.yaml"
    compiler.Compiler().compile(cpu_gpu_pipeline.cpu_then_gpu, str(output))
    pipeline = list(yaml.safe_load_all(output.read_text()))[0]
    tasks = pipeline["root"]["dag"]["tasks"]
    first = next(k for k, v in tasks.items() if not v.get("dependentTasks"))
    second = next(k for k, v in tasks.items() if v.get("dependentTasks"))
    assert tasks[second]["dependentTasks"] == [first]
    assert all(not v.get("cachingOptions", {}).get("enableCache", False) for v in tasks.values())
    assert len(tasks) == 2
    assert {
        tasks[k]["inputs"]["parameters"]["run_key"]["componentInputParameter"] for k in tasks
    } == {"cpu_run_key", "gpu_run_key"}
    for executor in pipeline["deploymentSpec"]["executors"].values():
        assert "accelerator" not in executor["container"]["resources"]
