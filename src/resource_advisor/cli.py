"""Explicit local configuration; no discovery of another application's secrets."""

import argparse
import json
import os
import signal
from pathlib import Path
from threading import Event

from .api import create_app
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
    sub.add_parser("backfill-tracking")
    snapshot_check = sub.add_parser(
        "verify-db-snapshot", help="compare a restored DB with its saved backup snapshot"
    )
    snapshot_check.add_argument("--manifest", type=Path, required=True)
    snapshot_check.add_argument("--archive", type=Path, required=True)
    config_check = sub.add_parser(
        "check-config", help="local deployment checks without network or DB"
    )
    config_check.add_argument("--credentials", type=Path)
    config_check.add_argument("--artifacts-config", type=Path)
    config_check.add_argument("--worker-config", type=Path)
    mfkg = sub.add_parser(
        "mfkg-analyze", help="numerical analysis only; cannot authorize execution"
    )
    mfkg.add_argument("--input", type=Path, required=True)
    mfkg.add_argument("--output", type=Path, required=True)
    verify_copy = sub.add_parser("verify-db-copy")
    verify_copy.add_argument(
        "--target-env",
        default="RA_RESTORED_DATABASE_URL",
        help="environment variable containing the restored database URL",
    )
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
    worker.add_argument(
        "--heartbeat-path", help="readiness timestamp after a complete worker cycle"
    )
    inventory = sub.add_parser("collect-inventory")
    inventory.add_argument("--config", required=True, help="private read-only inventory sources")
    inventory.add_argument("--once", action="store_true")
    inventory.add_argument("--interval-seconds", type=int, default=30)
    inventory.add_argument(
        "--heartbeat-path", help="private readiness timestamp after a saved snapshot"
    )
    slurm_inventory = sub.add_parser("collect-slurm-inventory")
    slurm_inventory.add_argument(
        "--config", required=True, help="private Slurm observation sources"
    )
    slurm_inventory.add_argument("--once", action="store_true")
    slurm_inventory.add_argument("--interval-seconds", type=int, default=30)
    slurm_inventory.add_argument(
        "--heartbeat-path", help="readiness timestamp after a saved snapshot"
    )
    heartbeat = sub.add_parser("check-heartbeat")
    heartbeat.add_argument("--path", required=True)
    heartbeat.add_argument("--max-age", type=float, default=120)
    args = parser.parse_args()
    if args.action in {"check-config", "serve", "worker"}:
        from .configuration import (
            ConfigurationError,
            api_configuration,
            check_configuration,
            read_config,
            worker_configuration,
        )

        try:
            if args.action == "check-config":
                report = check_configuration(
                    credentials=read_config(args.credentials) if args.credentials else None,
                    artifacts=read_config(args.artifacts_config) if args.artifacts_config else None,
                    worker=read_config(args.worker_config) if args.worker_config else None,
                )
                print(json.dumps(report))
                return
            if args.action == "serve":
                artifact_config = (
                    read_config(Path(args.artifacts_config)) if args.artifacts_config else None
                )
                credentials = api_configuration(
                    read_config(Path(args.credentials)), artifact_config
                )
            else:
                config = read_config(Path(args.config))
                backends = worker_configuration(config)
        except ConfigurationError as exc:
            parser.error(str(exc))
    if args.action == "mfkg-analyze":
        from .mfkg import MFKernelInput, ask_mfkg

        if args.output.exists():
            parser.error("output already exists; choose a new report path")
        problem = MFKernelInput.model_validate_json(args.input.read_text())
        report = ask_mfkg(problem)
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"reason": report["reason"], "execution_authorized": False}))
        return
    if args.action == "check-heartbeat":
        from .health import heartbeat_fresh

        raise SystemExit(0 if heartbeat_fresh(Path(args.path), args.max_age) else 1)
    if (
        args.action in {"collect-inventory", "collect-slurm-inventory"}
        and args.interval_seconds < 5
    ):
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
    if args.action == "verify-db-snapshot":
        from .backup import verify_snapshot

        try:
            report = verify_snapshot(store, json.loads(args.manifest.read_text()), args.archive)
            print(json.dumps(report, indent=2))
        finally:
            store.engine.dispose()
        raise SystemExit(0 if report["matches"] else 1)
    if args.action == "verify-db-copy":
        from .backup import compare

        if not os.environ.get(args.target_env):
            parser.error("restored database environment variable is missing")
        target = Store(os.environ[args.target_env])
        try:
            report = compare(store, target)
            print(json.dumps(report, indent=2))
        finally:
            target.engine.dispose()
        raise SystemExit(0 if report["matches"] else 1)
    if args.action in {"collect-inventory", "collect-slurm-inventory"}:
        from .inventory import InventoryCollector, InventoryConfig, save_inventory

        if args.action == "collect-slurm-inventory":
            from .slurm_inventory import SlurmInventoryCollector, SlurmInventoryConfig

            config = SlurmInventoryConfig.model_validate_json(Path(args.config).read_text())
            collector = SlurmInventoryCollector(config)
        else:
            config = InventoryConfig.model_validate_json(Path(args.config).read_text())
            collector = InventoryCollector(config)
        stopping = Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        if args.heartbeat_path:
            Path(args.heartbeat_path).unlink(missing_ok=True)
        while not stopping.is_set():
            snapshot = collector.collect()
            save_inventory(store, config.project_ref, snapshot)
            if args.heartbeat_path:
                from .health import write_heartbeat

                write_heartbeat(Path(args.heartbeat_path))
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
            stopping.wait(args.interval_seconds)
    if args.action == "backfill-usage":
        print(json.dumps({"inserted": store.backfill_usage()}))
        return
    if args.action == "backfill-tracking":
        print(json.dumps({"enqueued": store.backfill_tracking()}))
        return
    if args.action == "serve":
        import uvicorn

        artifact_storage = None
        if artifact_config is not None:
            from .artifacts import S3Artifacts

            artifact_storage = S3Artifacts(**artifact_config)
        uvicorn.run(
            create_app(service, credentials, artifact_storage=artifact_storage),
            host=args.host,
            port=args.port,
            ssl_keyfile=args.ssl_keyfile,
            ssl_certfile=args.ssl_certfile,
        )
    elif args.action == "worker":
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
        stopping = Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        if args.heartbeat_path:
            Path(args.heartbeat_path).unlink(missing_ok=True)
        while not stopping.is_set():
            if config.get("coordinate_studies", True):
                study_runner.tick_all()
            runner.submit_one()
            runner.cancel_one()
            runner.reconcile_all()
            runner.release_one()
            if artifacts:
                artifacts.deliver_one()
            if delivery:
                delivery.deliver_one()
                delivery.deliver_artifact_one()
            if args.heartbeat_path:
                from .health import write_heartbeat

                write_heartbeat(Path(args.heartbeat_path))
            if args.once:
                break
            stopping.wait(5)


if __name__ == "__main__":
    main()
