"""Render site settings; credential values are provisioned separately as Secrets."""

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--tls-server-name", required=True)
    parser.add_argument("--api-namespace", default="resource-advisor-lab")
    parser.add_argument("--monitoring-namespace", default="kube-system")
    parser.add_argument("--project-ref", default="ra-lab")
    parser.add_argument("--service-ref", action="append", required=True)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", args.image)
        or not re.fullmatch(r"[a-zA-Z0-9.-]+", args.tls_server_name)
        or not all(
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", n)
            for n in (
                args.api_namespace,
                args.monitoring_namespace,
            )
        )
        or not all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", n)
            for n in (
                args.project_ref,
                *args.service_ref,
            )
        )
    ):
        parser.error("pinned image, TLS identity, namespaces and references required")
    folder = Path(__file__).parent
    probe = yaml.safe_load((folder / "probe.yaml").read_text())
    scrape = yaml.safe_load((folder / "scrape.yaml").read_text())
    rules = yaml.safe_load((folder / "rules.yaml").read_text())
    probe["metadata"]["namespace"] = args.api_namespace
    container = probe["spec"]["template"]["spec"]["containers"][0]
    container["image"] = args.image
    origin = f"https://ra-api.{args.api_namespace}.svc:8443"
    container["args"] = [
        "--api-url",
        origin,
        "--tls-server-name",
        args.tls_server_name,
        "--interval-seconds",
        "30",
        "--heartbeat-path",
        "/tmp/heartbeat",
    ]
    for ref in dict.fromkeys(args.service_ref):
        container["args"].extend(["--service-ref", ref])
    for obj in (scrape, rules):
        obj["metadata"]["namespace"] = args.monitoring_namespace
    scrape["spec"]["staticConfigs"] = [
        {
            "targets": [urlsplit(origin).netloc],
            "labels": {"project_ref": args.project_ref},
        }
    ]
    scrape["spec"]["tlsConfig"]["serverName"] = args.tls_server_name
    print(yaml.safe_dump_all([probe, scrape, rules], sort_keys=False), end="")


if __name__ == "__main__":
    main()
