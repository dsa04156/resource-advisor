"""Compile only: site URL, qualified image and Secret name are runtime inputs."""

from kfp import compiler, dsl, kubernetes


@dsl.container_component
def launch(api_url: str, image: str, workload: str, candidate: str, run_key: str):
    return dsl.ContainerSpec(
        image=str(image),
        command=["python", "-m", "resource_advisor.launcher"],
        args=[
            "--api-url",
            api_url,
            "--workload",
            workload,
            "--candidate",
            candidate,
            "--idempotency-key",
            run_key,
        ],
    )


@dsl.pipeline(name="resource-advisor-observe")
def observe(
    api_url: str,
    launcher_image: str,
    workload: str,
    candidate: str,
    token_secret: str,
    run_key: str,
):
    task = launch(
        api_url=api_url,
        image=launcher_image,
        workload=workload,
        candidate=candidate,
        run_key=run_key,
    )
    task.set_caching_options(False)
    task.set_cpu_request("100m").set_cpu_limit("1")
    task.set_memory_request("128Mi").set_memory_limit("256Mi")
    kubernetes.use_secret_as_env(
        task, secret_name=token_secret, secret_key_to_env={"token": "RA_API_TOKEN"}
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    compiler.Compiler().compile(observe, args.output)
