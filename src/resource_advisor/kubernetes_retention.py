"""Opt-in Pod retention until terminal scheduler evidence is committed to SQL."""

import json

FINALIZER = "resource-advisor.io/termination-evidence"


def observe(namespace, job, obj, pods):
    from .backends import BackendError, memory_mib, number
    from .contracts import State

    body = job["body"]
    uid = body.get("backend_uid")
    if obj is not None:
        meta = obj["metadata"]
        annotations = meta.get("annotations", {})
        if (
            meta.get("namespace") != namespace
            or meta.get("name") != body["external_id"]
            or annotations.get("resource-advisor/job-id") != job["id"]
            or annotations.get("resource-advisor/context-signature") != body["context_signature"]
            or not meta.get("uid")
            or (uid and uid != meta["uid"])
        ):
            raise BackendError("retained Job ownership mismatch")
        uid = meta["uid"]
    if len(pods) > 1:
        raise BackendError("retention requires one non-retrying Pod")
    retained = {"backend_uid": uid}
    if not pods:
        return retained, None
    pod = pods[0]
    meta, status = pod["metadata"], pod.get("status", {})
    if (
        not uid
        or not meta.get("uid")
        or meta.get("namespace") != namespace
        or not any(
            o.get("uid") == uid and o.get("kind") == "Job" for o in meta.get("ownerReferences", [])
        )
    ):
        raise BackendError("retained Pod ownership mismatch")
    if status.get("phase") not in {"Succeeded", "Failed"}:
        if obj is not None and any(
            c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
            for c in obj.get("status", {}).get("conditions", [])
        ):
            # Older controllers may mark the Job terminal before the Pod is stopped.
            return retained, {"state": State.RUNNING}
        return retained, None
    container = next(
        (c for c in status.get("containerStatuses", []) if c["name"] == "workload"), {}
    )
    terminated = container.get("state", {}).get("terminated")
    # A terminal Pod that never started can be released, but has no invented times.
    terminated = terminated or {}
    scheduled = next(
        (
            c.get("lastTransitionTime")
            for c in status.get("conditions", [])
            if c["type"] == "PodScheduled" and c["status"] == "True"
        ),
        None,
    )
    requests = next(
        (
            c.get("resources", {}).get("requests", {})
            for c in pod["spec"]["containers"]
            if c["name"] == "workload"
        ),
        {},
    )
    cpu = requests.get("cpu")
    cpu = (
        number(cpu[:-1]) / 1000
        if isinstance(cpu, str) and cpu.endswith("m") and number(cpu[:-1]) is not None
        else number(cpu)
    )
    allocation = (
        {
            "source": "kubernetes:scheduled workload container requests",
            "accelerator_count": number(requests.get(body["capability"]["resource_key"])),
            "cpu": cpu,
            "memory_mib": memory_mib(requests.get("memory")),
        }
        if scheduled
        else None
    )
    receipt = {
        "namespace": namespace,
        "job_uid": uid,
        "pod_name": meta["name"],
        "pod_uid": meta["uid"],
        "phase": status["phase"],
        "scheduled_at": scheduled,
        "container": {
            k: terminated.get(k)
            for k in ("startedAt", "finishedAt", "exitCode", "signal", "reason")
        },
        "allocation": allocation,
        "retention_finalizer": FINALIZER if FINALIZER in meta.get("finalizers", []) else None,
    }
    retained["termination"] = receipt
    if obj is None or obj["metadata"].get("deletionTimestamp"):
        state = (
            State.COLLECTING
            if status["phase"] == "Succeeded" and terminated.get("exitCode") == 0
            else State.CANCELED
            if job["state"] == State.CANCEL_REQUESTED
            else State.FAILED
        )
        return retained, {
            "state": state,
            "started_at": scheduled,
            "finished_at": terminated.get("finishedAt"),
            "execution_started_at": terminated.get("startedAt"),
            "allocation": allocation,
            "error": "JOB_DISAPPEARED" if state == State.FAILED else None,
        }
    return retained, None


def release(backend, receipt):
    from .backends import BackendError

    if receipt["namespace"] != backend.namespace:
        raise BackendError("retention namespace mismatch")
    raw = backend.execute(
        backend.prefix + ["get", "pod", receipt["pod_name"], "--ignore-not-found", "-o", "json"]
    )
    if not raw.strip():
        return  # An accepted patch may have removed the Pod before its response arrived.
    pod = json.loads(raw)
    meta, status = pod["metadata"], pod.get("status", {})
    if (
        meta.get("uid") != receipt["pod_uid"]
        or meta.get("namespace") != receipt["namespace"]
        or not any(
            o.get("uid") == receipt["job_uid"] and o.get("kind") == "Job"
            for o in meta.get("ownerReferences", [])
        )
        or status.get("phase") not in {"Succeeded", "Failed"}
    ):
        raise BackendError("refusing to release unowned or nonterminal Pod")
    finalizers = meta.get("finalizers", [])
    if FINALIZER not in finalizers:
        return
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": receipt["pod_uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {
            "op": "replace",
            "path": "/metadata/finalizers",
            "value": [f for f in finalizers if f != FINALIZER],
        },
    ]
    backend.execute(
        backend.prefix
        + ["patch", "pod", receipt["pod_name"], "--type=json", "-p", json.dumps(patch)]
    )
