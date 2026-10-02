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
    serve = sub.add_parser("serve")
    serve.add_argument(
        "--credentials", required=True, help="private JSON file of token SHA-256 hashes"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18040)
    worker = sub.add_parser("worker")
    worker.add_argument("--config", required=True, help="private backend routes")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.database.startswith("sqlite:///."):
        Path(".state").mkdir(mode=0o700, exist_ok=True)
    store = Store(args.database)
    if args.action == "init-db":
        store.initialize()
        print("Independent Resource Advisor schema initialized.")
        return
    service = Service(store)
    if args.action == "serve":
        import uvicorn

        credentials = {
            h: Principal(**p) for h, p in json.loads(Path(args.credentials).read_text()).items()
        }
        uvicorn.run(create_app(service, credentials), host=args.host, port=args.port)
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
        delivery = (
            MLflowDelivery(store, config["mlflow_url"], token=os.getenv("RA_MLFLOW_TOKEN"))
            if config.get("mlflow_url")
            else None
        )
        while True:
            runner.submit_one()
            runner.cancel_one()
            runner.reconcile_all()
            if delivery:
                delivery.deliver_one()
            if args.once:
                break
            time.sleep(5)


if __name__ == "__main__":
    main()
