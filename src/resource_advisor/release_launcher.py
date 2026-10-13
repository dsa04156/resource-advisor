"""CPU-only KFP candidate orchestration through authenticated platform APIs."""

import argparse
import os
import signal
import time
from pathlib import Path

import httpx

PREFIX = "/api/v1/compute"
TRANSIENT = {429, 502, 503, 504}


def deadline(seconds, poll_seconds):
    if seconds <= 0 or poll_seconds < 0:
        raise ValueError("positive deadline and nonnegative polling interval required")
    return time.monotonic() + seconds


def call(client, method, path, end, poll_seconds, **kwargs):
    while time.monotonic() < end:
        try:
            response = client.request(
                method, PREFIX + path, timeout=min(20, max(0.1, end - time.monotonic())), **kwargs
            )
            if response.status_code not in TRANSIENT:
                response.raise_for_status()
                return response.json()
        except httpx.TransportError:
            pass
        time.sleep(min(poll_seconds, max(0, end - time.monotonic())))
    raise TimeoutError("candidate launcher deadline exceeded")


def train(client, policy_ref, key, deadline_seconds, *, poll_seconds=5):
    end = deadline(deadline_seconds, poll_seconds)
    value = call(
        client,
        "POST",
        "/model-lifecycles",
        end,
        poll_seconds,
        json={"policy_ref": policy_ref},
        headers={"Idempotency-Key": key},
    )
    while time.monotonic() < end:
        if value["state"] == "REGISTERED":
            return value["ref"]
        if value["state"] in {
            "QUALITY_REJECTED",
            "TRAINING_FAILED",
            "REGISTRY_RECONCILIATION_REQUIRED",
        }:
            raise RuntimeError("model lifecycle terminated: " + value["state"])
        time.sleep(min(poll_seconds, max(0, end - time.monotonic())))
        value = call(client, "GET", "/model-lifecycles/" + value["ref"], end, poll_seconds)
    raise TimeoutError("model lifecycle registration deadline exceeded")


def stage(client, lifecycle_ref, policy_ref, key, deadline_seconds, *, poll_seconds=5):
    end = deadline(deadline_seconds, poll_seconds)
    value = call(
        client,
        "POST",
        "/model-releases",
        end,
        poll_seconds,
        json={"policy_ref": policy_ref, "lifecycle_ref": lifecycle_ref},
        headers={"Idempotency-Key": key},
    )
    path = "/model-releases/" + value["ref"]
    value = call(client, "POST", path + "/staging", end, poll_seconds)
    while time.monotonic() < end:
        if (
            value["state"] in {"REJECTED", "SUPERSEDED"}
            or value.get("staging", {}).get("state") == "SUPERSEDED"
        ):
            raise RuntimeError("model release requires investigation: " + value["state"])
        if value.get("staging") and value["staging"]["state"] == "READY":
            verified = call(client, "POST", path + "/verification", end, poll_seconds)
            if not verified.get("verification"):
                raise RuntimeError("staging verification receipt is missing")
            return verified["ref"]
        time.sleep(min(poll_seconds, max(0, end - time.monotonic())))
        value = call(client, "GET", path, end, poll_seconds)
    raise TimeoutError("model staging verification deadline exceeded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["train", "stage"])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--policy-ref", required=True)
    parser.add_argument("--lifecycle-ref")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=3900)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "stage" and not args.lifecycle_ref:
        parser.error("stage requires --lifecycle-ref")

    def terminated(_signum, _frame):
        raise KeyboardInterrupt("candidate launcher terminated")

    signal.signal(signal.SIGTERM, terminated)
    with httpx.Client(
        base_url=args.api_url.rstrip("/"),
        headers={"Authorization": "Bearer " + os.environ["RA_API_TOKEN"]},
        timeout=20,
    ) as client:
        ref = (
            train(client, args.policy_ref, args.idempotency_key, args.deadline_seconds)
            if args.action == "train"
            else stage(
                client,
                args.lifecycle_ref,
                args.policy_ref,
                args.idempotency_key,
                args.deadline_seconds,
            )
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(ref, encoding="utf-8")
    print(ref)


if __name__ == "__main__":
    main()
