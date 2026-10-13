"""Bounded live prediction probes, using a project user credential and verified TLS."""

import argparse
import json
import os
import signal
import ssl
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .api import PREFIX
from .configuration import valid_ref


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--service-ref", action="append", required=True)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--tls-server-name")
    parser.add_argument("--heartbeat-path")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    url = urlsplit(args.api_url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or not all(valid_ref(ref) for ref in args.service_ref)
        or not 5 <= args.interval_seconds <= 3600
    ):
        parser.error("HTTPS origin, valid service refs and interval 5–3600 seconds required")
    token = os.environ.get("RA_API_TOKEN", "")
    if not token:
        parser.error("RA_API_TOKEN is required")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    extensions = {"sni_hostname": args.tls_server_name} if args.tls_server_name else {}
    context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    with httpx.Client(
        base_url=args.api_url.rstrip("/"),
        verify=context,
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
        timeout=10,
    ) as client:
        while not stop.is_set():
            succeeded = True
            reachable = True
            for ref in dict.fromkeys(args.service_ref):
                result = {"service_ref": ref, "success": False}
                try:
                    response = client.request(
                        "POST",
                        PREFIX + "/model-services/" + ref + "/probe",
                        extensions=extensions,
                    )
                    response.raise_for_status()
                    observation = response.json()
                    result.update(observation.get("probe") or {"reason": "DEPLOYMENT_CHANGED"})
                except (httpx.HTTPError, ValueError):
                    result["reason"] = "API_UNAVAILABLE"
                    reachable = False
                succeeded &= bool(result["success"])
                print(json.dumps(result, sort_keys=True), flush=True)
            if args.heartbeat_path and reachable:
                Path(args.heartbeat_path).touch()
            if args.once:
                return 0 if succeeded else 1
            stop.wait(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
