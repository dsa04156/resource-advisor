"""Explicit local configuration; no discovery of another application's secrets."""

import argparse
import json
import os
import time
from pathlib import Path

from .api import Principal, create_app
from .backends import KubernetesBackend, SlurmBackend
from .service import Service
from .store import Store
from .worker import MLflowDelivery, Worker


def main():
    parser = argparse.ArgumentParser(prog="resource-advisor")
    parser.add_argument(
        "--database", default=os.getenv("RA_DATABASE_URL", "sqlite:///.state/advisor.sqlite")
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init-db")
    sub.add_parser("backfill-usage")
    serve = sub.add_parser("serve")
    serve.add_argument(
        "--credentials", required=True, help="private JSON file of token SHA-256 hashes"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18040)
    serve.add_argument("--ssl-keyfile", help="private TLS key for direct HTTPS serving")
    serve.add_argument("--ssl-certfile", help="TLS certificate chain for direct HTTPS serving")
    serve.add_argument("--artifacts-config", help="private S3 endpoint and project bucket map")
    worker = sub.add_parser("worker")
    worker.add_argument("--config", required=True, help="private backend routes")
    worker.add_argument("--once", action="store_true")
    inventory = sub.add_parser("collect-inventory")
    inventory.add_argument("--config", required=True, help="private read-only inventory sources")
    inventory.add_argument("--once", action="store_true")
    inventory.add_argument("--interval-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.action == "collect-inventory" and args.interval_seconds < 5:
        parser.error("inventory polling interval must be at least 5 seconds")
    if args.action == "serve" and bool(args.ssl_keyfile) != bool(args.ssl_certfile):
        parser.error("--ssl-keyfile and --ssl-certfile must be provided together")
    if args.database.startswith("sqlite:///."):
        Path(".state").mkdir(mode=0o700, exist_ok=True)
    store = Store(args.database)
    if args.action == "init-db":
        store.initialize()
        print("Independent Resource Advisor schema initialized.")
        return
    service = Service(store)
    if args.action == "collect-inventory":
        from .inventory import InventoryCollector, InventoryConfig, save_inventory

        config = InventoryConfig.model_validate_json(Path(args.config).read_text())
        collector = InventoryCollector(config)
        while True:
            snapshot = collector.collect()
            save_inventory(store, config.project_ref, snapshot)
            print(
                json.dumps(
                    {
                        "ref": snapshot["ref"],
                        "status": snapshot["status"],
                        "node_count": len(snapshot["nodes"]),
                    }
                ),
                flush=True,
            )
            if args.once:
                return
            time.sleep(args.interval_seconds)
    if args.action == "backfill-usage":
        print(json.dumps({"inserted": store.backfill_usage()}))
        return
    if args.action == "serve":
        import uvicorn

        credentials = {
            h: Principal(**p) for h, p in json.loads(Path(args.credentials).read_text()).items()
        }
        artifact_storage = None
        if args.artifacts_config:
            from .artifacts import S3Artifacts

            artifact_storage = S3Artifacts(**json.loads(Path(args.artifacts_config).read_text()))
        uvicorn.run(
            create_app(service, credentials, artifact_storage=artifact_storage),
            host=args.host,
            port=args.port,
            ssl_keyfile=args.ssl_keyfile,
            ssl_certfile=args.ssl_certfile,
        )
    elif args.action == "worker":
        config = json.loads(Path(args.config).read_text())
        backends = {}
        for route in config["routes"]:
            kind = route["backend"]
            cls = {"kubernetes": KubernetesBackend, "slurm": SlurmBackend}[kind]
            key = (route["project"], route["cluster"])
            if key in backends:
                raise ValueError("duplicate route")
            backends[key] = cls(**route["options"])
        runner = Worker(service, backends)
        from .study import Studies

        study_runner = Studies(service)
        artifacts = None
        if config.get("artifacts"):
            from .artifacts import ArtifactDelivery, S3Artifacts

            artifacts = ArtifactDelivery(store, S3Artifacts(**config["artifacts"]))
        delivery = (
            MLflowDelivery(
                store,
                config["mlflow_url"],
                experiments=config["mlflow_experiments"],
                token=os.getenv("RA_MLFLOW_TOKEN"),
                artifact_storage=artifacts.storage if artifacts else None,
            )
            if config.get("mlflow_url")
            else None
        )
        while True:
            study_runner.tick_all()
            runner.submit_one()
            runner.cancel_one()
            runner.reconcile_all()
            if artifacts:
                artifacts.deliver_one()
            if delivery:
                delivery.deliver_one()
                delivery.deliver_artifact_one()
            if args.once:
                break
            time.sleep(5)


if __name__ == "__main__":
    main()
