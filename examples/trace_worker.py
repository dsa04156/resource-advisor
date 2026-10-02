"""Qualification-only worker trace, mounted into isolated test worker Pods.

Run with the ordinary worker CLI arguments. It logs identifiers and action
boundaries, never credentials, workload bodies or artifact payloads. This wrapper
does not change leases, sleep, retry or replace production transport calls.
"""

import json
import os
import runpy
import sys
from datetime import datetime, timezone

from resource_advisor.backends import KubernetesBackend
from resource_advisor.store import Store


def emit(action, **values):
    print(
        "RA_CONCURRENCY "
        + json.dumps(
            {
                "action": action,
                "worker": os.environ["RA_TEST_WORKER_ID"],
                "at": datetime.now(timezone.utc).isoformat(),
                **values,
            }
        ),
        flush=True,
    )


def main():
    claim = Store.claim
    submit = KubernetesBackend.submit
    status = KubernetesBackend.status

    def traced_claim(self, kind, *args, **kwargs):
        event = claim(self, kind, *args, **kwargs)
        if event:
            emit("claim", kind=kind, event_id=event["id"], tries=event["tries"])
        return event

    def traced_submit(self, row):
        emit("submit_started", attempt_id=row["body"]["attempt_id"])
        value = submit(self, row)
        emit("submit_returned", attempt_id=row["body"]["attempt_id"], external_id=value)
        return value

    def traced_status(self, row):
        value = status(self, row)
        emit("observed", attempt_id=row["body"]["attempt_id"], state=str(value.state))
        return value

    Store.claim = traced_claim
    KubernetesBackend.submit = traced_submit
    KubernetesBackend.status = traced_status
    emit("started", pid=os.getpid())
    sys.argv[0] = "resource-advisor"
    runpy.run_module("resource_advisor.cli", run_name="__main__")
    emit("stopped")


if __name__ == "__main__":
    main()
