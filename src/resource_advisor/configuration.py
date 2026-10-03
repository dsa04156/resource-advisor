"""Local deployment checks; never contact schedulers, storage, MLflow or the DB."""

import importlib
import json
import re

from .api import Principal
from .backends import KubernetesBackend, SlurmBackend


class ConfigurationError(ValueError):
    """Messages contain fixed error codes, never supplied credentials or options."""


def require(condition, code):
    if not condition:
        raise ConfigurationError(code)


def read_config(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "DUPLICATE_JSON_KEY")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError("CONFIG_UNREADABLE_OR_INVALID_JSON") from None
    require(isinstance(value, dict), "CONFIG_OBJECT_REQUIRED")
    return value


def valid_ref(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value)


def principals(credentials):
    require(isinstance(credentials, dict) and bool(credentials), "CREDENTIALS_REQUIRED")
    result = {}
    for digest, identity in credentials.items():
        require(
            isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest),
            "TOKEN_SHA256_REQUIRED",
        )
        require(
            isinstance(identity, dict)
            and set(identity) <= {"project", "operator"}
            and valid_ref(identity.get("project"))
            and isinstance(identity.get("operator", False), bool),
            "INVALID_PRINCIPAL",
        )
        result[digest] = Principal(**identity)
    return result


def artifact_buckets(config, projects):
    if config is None:
        return None
    require(
        isinstance(config, dict) and set(config) <= {"buckets", "endpoint_url", "region_name"},
        "INVALID_ARTIFACT_CONFIG",
    )
    buckets = config.get("buckets")
    require(
        isinstance(buckets, dict)
        and bool(buckets)
        and all(
            valid_ref(project)
            and isinstance(bucket, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            for project, bucket in buckets.items()
        ),
        "INVALID_ARTIFACT_BUCKET_MAPPING",
    )
    require(set(projects) <= buckets.keys(), "ARTIFACT_PROJECT_MAPPING_MISSING")
    return buckets


def api_configuration(credentials, artifacts=None):
    identities = principals(credentials)
    projects = {p.project for p in identities.values()}
    artifact_buckets(artifacts, projects)
    return identities


def worker_configuration(config):
    require(isinstance(config, dict), "CONFIG_OBJECT_REQUIRED")
    optimizer_required = config.get("optimizer_required", False)
    require(type(optimizer_required) is bool, "INVALID_OPTIMIZER_REQUIREMENT")
    if optimizer_required:
        try:
            importlib.import_module("torch")
            importlib.import_module("botorch")
        except (ImportError, OSError, RuntimeError):
            raise ConfigurationError("OPTIMIZER_RUNTIME_UNAVAILABLE") from None
    routes = config.get("routes")
    require(isinstance(routes, list), "WORKER_ROUTES_REQUIRED")
    keys, projects = set(), set()
    for route in routes:
        require(
            isinstance(route, dict)
            and set(route) == {"project", "cluster", "backend", "options"}
            and valid_ref(route.get("project"))
            and valid_ref(route.get("cluster"))
            and route.get("backend") in ("kubernetes", "slurm")
            and isinstance(route.get("options"), dict),
            "INVALID_WORKER_ROUTE",
        )
        key = (route["project"], route["cluster"])
        require(key not in keys, "DUPLICATE_WORKER_ROUTE")
        keys.add(key)
        projects.add(route["project"])
    artifact_buckets(config.get("artifacts"), projects)
    if config.get("mlflow_url"):
        require(isinstance(config["mlflow_url"], str), "INVALID_MLFLOW_URL")
        experiments = config.get("mlflow_experiments")
        require(
            isinstance(experiments, dict)
            and projects <= experiments.keys()
            and all(isinstance(experiments[p], str) and experiments[p].strip() for p in projects),
            "MLFLOW_PROJECT_MAPPING_MISSING",
        )
    backends = {}
    for route in routes:
        cls = {"kubernetes": KubernetesBackend, "slurm": SlurmBackend}[route["backend"]]
        try:
            backends[(route["project"], route["cluster"])] = cls(**route["options"])
        except (TypeError, ValueError, AttributeError):
            raise ConfigurationError("INVALID_BACKEND_OPTIONS") from None
    return backends


def check_configuration(*, credentials=None, artifacts=None, worker=None):
    require(credentials is not None or worker is not None, "API_OR_WORKER_CONFIG_REQUIRED")
    require(credentials is not None or artifacts is None, "ARTIFACT_CONFIG_REQUIRES_CREDENTIALS")
    identities = api_configuration(credentials, artifacts) if credentials is not None else None
    backends = worker_configuration(worker) if worker is not None else None
    api_projects = {p.project for p in identities.values()} if identities is not None else None
    worker_projects = {p for p, _ in backends} if backends is not None else None
    if api_projects is not None and worker_projects is not None:
        require(worker_projects <= api_projects, "WORKER_PROJECT_WITHOUT_API_IDENTITY")
        api_buckets = artifact_buckets(artifacts, api_projects)
        worker_buckets = artifact_buckets(worker.get("artifacts"), worker_projects)
        require(
            (api_buckets is None) == (worker_buckets is None),
            "API_WORKER_ARTIFACT_ENABLEMENT_MISMATCH",
        )
        if api_buckets is not None:
            require(
                all(api_buckets[p] == worker_buckets[p] for p in worker_projects),
                "API_WORKER_BUCKET_MAPPING_MISMATCH",
            )
    return {
        "status": "PASS",
        "scope": "local configuration only; no connectivity or hardware qualification",
        "api_projects": len(api_projects) if api_projects is not None else None,
        "worker_projects": len(worker_projects) if worker_projects is not None else None,
        "routes": len(backends) if backends is not None else None,
        "api_artifacts_enabled": artifacts is not None if identities is not None else None,
        "worker_artifacts_enabled": worker.get("artifacts") is not None
        if worker is not None
        else None,
        "mlflow_enabled": bool(worker.get("mlflow_url")) if worker is not None else None,
        "optimizer_required": worker.get("optimizer_required", False)
        if worker is not None
        else None,
    }
