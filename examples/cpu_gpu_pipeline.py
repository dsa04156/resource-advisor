"""Actual CPU compute followed by GPU compute through existing API launchers.

The two native workloads have distinct validated results and stable run keys.
This is an execution dependency, not dataset/artifact transfer or model training.
"""

from kfp import compiler, dsl, kubernetes
from pipeline import launch


def configure(task, token_secret):
    task.set_caching_options(False)
    kubernetes.add_node_selector(task, "kubernetes.io/arch", "amd64")
    kubernetes.use_secret_as_volume(
        task, secret_name=token_secret, mount_path="/var/run/resource-advisor"
    )
    task.set_env_variable("SSL_CERT_FILE", "/var/run/resource-advisor/ca.crt")
    task.set_cpu_request("100m").set_cpu_limit("1")
    task.set_memory_request("128Mi").set_memory_limit("256Mi")
    kubernetes.use_secret_as_env(
        task, secret_name=token_secret, secret_key_to_env={"token": "RA_API_TOKEN"}
    )


@dsl.pipeline(name="hairp-cpu-then-gpu-compute")
def cpu_then_gpu(
    api_url: str,
    launcher_image: str,
    cpu_workload: str,
    cpu_candidate: str,
    gpu_workload: str,
    gpu_candidate: str,
    token_secret: str,
    cpu_run_key: str,
    gpu_run_key: str,
    owner_lease_seconds: int = 60,
):
    cpu = launch(
        api_url=api_url,
        image=launcher_image,
        workload=cpu_workload,
        candidate=cpu_candidate,
        run_key=cpu_run_key,
        owner_lease_seconds=owner_lease_seconds,
    )
    cpu.set_display_name("CPU compute · validate result")
    configure(cpu, token_secret)
    gpu = launch(
        api_url=api_url,
        image=launcher_image,
        workload=gpu_workload,
        candidate=gpu_candidate,
        run_key=gpu_run_key,
        owner_lease_seconds=owner_lease_seconds,
    )
    gpu.set_display_name("GPU compute · validate result")
    gpu.after(cpu)
    configure(gpu, token_secret)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    compiler.Compiler().compile(cpu_then_gpu, args.output)
