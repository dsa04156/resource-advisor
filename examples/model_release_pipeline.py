"""Compile a candidate workflow; production approval stays outside KFP."""

from kfp import compiler, dsl, kubernetes


@dsl.container_component
def train(
    api_url: str,
    image: str,
    policy_ref: str,
    key: str,
    deadline_seconds: int,
    lifecycle_ref: dsl.OutputPath(str),
):
    return dsl.ContainerSpec(
        image=str(image),
        command=["python", "-m", "resource_advisor.release_launcher"],
        args=[
            "train",
            "--api-url",
            api_url,
            "--policy-ref",
            policy_ref,
            "--idempotency-key",
            key,
            "--deadline-seconds",
            deadline_seconds,
            "--output",
            lifecycle_ref,
        ],
    )


@dsl.container_component
def stage(
    api_url: str,
    image: str,
    lifecycle_ref: str,
    policy_ref: str,
    key: str,
    deadline_seconds: int,
    release_ref: dsl.OutputPath(str),
):
    return dsl.ContainerSpec(
        image=str(image),
        command=["python", "-m", "resource_advisor.release_launcher"],
        args=[
            "stage",
            "--api-url",
            api_url,
            "--lifecycle-ref",
            lifecycle_ref,
            "--policy-ref",
            policy_ref,
            "--idempotency-key",
            key,
            "--deadline-seconds",
            deadline_seconds,
            "--output",
            release_ref,
        ],
    )


def configure(task, user_secret):
    task.set_caching_options(False)
    task.set_retry(
        num_retries=2, backoff_duration="10s", backoff_factor=2, backoff_max_duration="60s"
    )
    kubernetes.add_node_selector(task, "kubernetes.io/arch", "amd64")
    kubernetes.use_secret_as_volume(
        task, secret_name=user_secret, mount_path="/var/run/resource-advisor"
    )
    kubernetes.use_secret_as_env(
        task, secret_name=user_secret, secret_key_to_env={"token": "RA_API_TOKEN"}
    )
    task.set_env_variable("SSL_CERT_FILE", "/var/run/resource-advisor/ca.crt")
    task.set_cpu_request("100m").set_cpu_limit("1")
    task.set_memory_request("128Mi").set_memory_limit("256Mi")


@dsl.pipeline(name="resource-advisor-model-candidate")
def candidate(
    api_url: str,
    launcher_image: str,
    training_policy: str,
    release_policy: str,
    user_secret: str,
    training_key: str,
    release_key: str,
    training_deadline_seconds: int = 3900,
    staging_deadline_seconds: int = 600,
) -> str:
    trained = train(
        api_url=api_url,
        image=launcher_image,
        policy_ref=training_policy,
        key=training_key,
        deadline_seconds=training_deadline_seconds,
    )
    configure(trained, user_secret)
    staged = stage(
        api_url=api_url,
        image=launcher_image,
        lifecycle_ref=trained.outputs["lifecycle_ref"],
        policy_ref=release_policy,
        key=release_key,
        deadline_seconds=staging_deadline_seconds,
    )
    configure(staged, user_secret)
    return staged.outputs["release_ref"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    compiler.Compiler().compile(candidate, args.output)
