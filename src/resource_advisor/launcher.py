"""CPU-only workflow launcher. Remote compute belongs to the Compute API."""

import argparse
import json
import os
import signal
import time

import httpx

from .contracts import TERMINAL


def execute(api_url, token, request, key, deadline_seconds, *, client=None, poll_seconds=5):
    if not key or deadline_seconds <= 0:
        raise ValueError("stable run idempotency key and positive deadline required")
    owner_lease = request.get("owner_lease_seconds")
    if owner_lease is not None and not 0 <= poll_seconds < owner_lease / 3:
        raise ValueError("heartbeat polling must be faster than one third of the owner lease")
    client = client or httpx.Client(
        base_url=api_url.rstrip("/"), headers={"Authorization": "Bearer " + token}, timeout=20
    )
    prefix = "/api/v1/compute/jobs"
    job_id = None
    try:
        # Caller retains this key when restarting after an uncertain HTTP response.
        response = client.post(prefix, json=request, headers={"Idempotency-Key": key})
        response.raise_for_status()
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            response = (
                client.post(prefix + "/" + job_id + "/heartbeat")
                if owner_lease is not None
                else client.get(prefix + "/" + job_id)
            )
            response.raise_for_status()
            job = response.json()
            if job["state"] in TERMINAL:
                if job["state"] != "SUCCEEDED":
                    raise RuntimeError("compute job terminated: " + job["state"])
                return job
            time.sleep(poll_seconds)
        raise TimeoutError("launcher deadline exceeded")
    except (KeyboardInterrupt, TimeoutError, httpx.HTTPError):
        if job_id:
            # Cancellation is a request, not a claim that resource release was confirmed.
            try:
                client.post(prefix + "/" + job_id + "/cancel").raise_for_status()
            except httpx.HTTPError:
                pass  # Backend-side deadlines remain the last line of cleanup.
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=3900)
    parser.add_argument("--owner-lease-seconds", type=int, default=60)
    args = parser.parse_args()

    def terminated(_signum, _frame):
        raise KeyboardInterrupt("workflow launcher terminated")

    signal.signal(signal.SIGTERM, terminated)
    result = execute(
        args.api_url,
        os.environ["RA_API_TOKEN"],
        {
            "workload_ref": args.workload,
            "candidate_ref": args.candidate,
            "mode": "observe",
            "owner_lease_seconds": args.owner_lease_seconds,
        },
        args.idempotency_key,
        args.deadline_seconds,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
