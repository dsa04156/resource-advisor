"""Read-only collectors for heterogeneous recommendation evidence.

Every subcommand only reads (kubectl get, sinfo, local query tools, a stored
database, or one explicitly permitted bounded object read) and prints JSON for an
operator to review and import. Nothing submits jobs or changes cluster state.

  inventory  --nodes-json FILE | --kubectl [--sinfo-text FILE] [--local]
  queue      --pods-json FILE | --kubectl  --route ... (RouteQueueSnapshot)
  profile    --database-url URL --job-id ... (JctProfile from completed RA jobs)
  network    --database-url URL --job-id ... --node N (NetworkProfile from staging receipts)
  link       --url URL --source EP --target EP --evidence-ref REF [--execute]
  probes     (existing probe hooks and what each establishes)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from resource_advisor.contracts import now
from resource_advisor.hetero_collect import (
    PROBE_HOOKS,
    dump,
    link_command,
    link_measurement,
    local_tools,
    node_fingerprints,
    slurm_fingerprints,
)


def kubectl(*args):
    done = subprocess.run(
        ["kubectl", "get", *args, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(done.stdout)


def inventory(args):
    stamp = now().isoformat()
    nodes = kubectl("nodes") if args.kubectl else json.loads(Path(args.nodes_json).read_text())
    report = {
        "collected_at": stamp,
        "method": "read-only: kubectl get nodes" if args.kubectl else "file: " + args.nodes_json,
        "kubernetes": node_fingerprints(nodes),
        "semantics": "advertised resources/labels are inventory, not runtime or model qualification",
    }
    if args.sinfo_text:
        report["slurm"] = slurm_fingerprints(Path(args.sinfo_text).read_text())
    if args.local:
        report["local_tools"] = local_tools()
    print(dump(report))


def queue(args):
    from resource_advisor.recommendation import queue_snapshot_from_pods

    pods = kubectl("pods", "-A") if args.kubectl else json.loads(Path(args.pods_json).read_text())
    occupancy = (
        json.loads(Path(args.expected_occupancy).read_text()) if args.expected_occupancy else {}
    )
    snapshot = queue_snapshot_from_pods(
        pods["items"],
        ref=args.ref,
        project_ref=args.project,
        cluster_ref=args.cluster,
        route_ref=args.route,
        nodes=set(args.node),
        resource_key=args.resource_key,
        capacity=args.capacity,
        capacity_unit=args.capacity_unit,
        observed_at=now(),
        evidence_ref=args.evidence_ref,
        expected_occupancy=occupancy,
    )
    print(dump(snapshot.model_dump(mode="json")))


def stored_rows(store, conn, job_id):
    """A job with its stored result, rank-0 staging receipt and every rank's own receipt."""
    from sqlalchemy import select

    from resource_advisor.store import entities

    row = store.job(conn, job_id)
    if row is None:
        sys.exit(f"job {job_id} not found")
    attempt = row["body"]["attempt_id"]
    result = store.get(conn, "result", attempt)
    if result is None:
        sys.exit(f"job {job_id} has no stored result")
    value = {"id": row["id"], "state": row["state"], "body": row["body"], "result": result["body"]}
    staging = store.get(conn, "staging_receipt", attempt)
    if staging is not None:
        value["staging"] = staging["body"]
    # Every stored rank receipt of this attempt (id "<attempt>:<rank>"), not only the
    # expected indexes: member_receipts() rejects extra, missing or foreign-Pod ones.
    members = conn.execute(
        select(entities.c.body)
        .where(
            entities.c.kind == "staging_member_receipt",
            entities.c.project == row["project"],
            entities.c.ref.startswith(attempt + ":", autoescape=True),
        )
        .order_by(entities.c.ref)
    ).scalars()
    members = list(members)
    if members:
        value["staging_members"] = members
    return value


def profile(args):
    from resource_advisor.recommendation import HardwareFingerprint, jct_profile_from_jobs
    from resource_advisor.store import Store

    store = Store(args.database_url)
    with store.transaction() as conn:
        rows = [stored_rows(store, conn, job_id) for job_id in args.job_id]
    hardware = HardwareFingerprint.model_validate(json.loads(Path(args.hardware_json).read_text()))
    made = jct_profile_from_jobs(
        rows, ref=args.ref, valid_for_seconds=args.valid_for_seconds, hardware=hardware
    )
    print(dump(made.model_dump(mode="json")))


def network_rows(rows, node):
    """Staging receipts measured on ``node``: a single job's own, or that DDP rank's own."""
    found = []
    for row in rows:
        if (row["body"].get("placement") or {}).get("distributed"):
            from resource_advisor.recommendation import member_rows

            found += [m for m in member_rows(row) if m["body"]["placement"]["node_name"] == node]
        elif (row["body"].get("placement") or {}).get("node_name") == node and row.get("staging"):
            found.append(row)
    return found


def network(args):
    from resource_advisor.recommendation import network_profile_from_receipts
    from resource_advisor.store import Store

    store = Store(args.database_url)
    with store.transaction() as conn:
        rows = network_rows([stored_rows(store, conn, j) for j in args.job_id], args.node)
    made = network_profile_from_receipts(
        rows, ref=args.ref, valid_for_seconds=args.valid_for_seconds
    )
    print(dump(made.model_dump(mode="json")))


def link(args):
    command = link_command(
        args.url,
        max_bytes=args.max_bytes,
        max_seconds=args.max_seconds,
        limit_rate_bytes=args.limit_rate_bytes,
    )
    if not args.execute:
        print(dump({"dry_run": True, "command": command, "concurrency": 1}))
        return
    done = subprocess.run(
        command, capture_output=True, text=True, timeout=args.max_seconds + 5, check=True
    )
    print(
        dump(
            link_measurement(
                done.stdout,
                source=args.source,
                target=args.target,
                evidence_ref=args.evidence_ref,
                limit_rate_bytes=args.limit_rate_bytes,
            )
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inventory")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--nodes-json")
    group.add_argument("--kubectl", action="store_true")
    p.add_argument("--sinfo-text", help="output of: sinfo -N -h -o '%%N|%%G|%%f|%%c|%%m|%%T'")
    p.add_argument("--local", action="store_true")
    p.set_defaults(func=inventory)
    p = sub.add_parser("queue")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--pods-json")
    group.add_argument("--kubectl", action="store_true")
    for name in ("ref", "project", "cluster", "route", "resource-key", "evidence-ref"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--node", action="append", required=True)
    p.add_argument("--capacity", type=int, required=True)
    p.add_argument("--capacity-unit", choices=["physical_device", "logical_slot"], required=True)
    p.add_argument("--expected-occupancy", help="JSON {ra_job_id: seconds} from frozen plans")
    p.set_defaults(func=queue)
    p = sub.add_parser("profile")
    p.add_argument("--database-url", required=True)
    p.add_argument("--job-id", action="append", required=True)
    p.add_argument("--hardware-json", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--valid-for-seconds", type=int, default=86400)
    p.set_defaults(func=profile)
    p = sub.add_parser("network")
    p.add_argument("--database-url", required=True)
    p.add_argument("--job-id", action="append", required=True)
    p.add_argument("--node", required=True, help="destination node the receipts were staged on")
    p.add_argument("--ref", required=True)
    p.add_argument("--valid-for-seconds", type=int, default=3600)
    p.set_defaults(func=network)
    p = sub.add_parser("link")
    p.add_argument("--url", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--evidence-ref", required=True)
    p.add_argument("--max-bytes", type=int, default=64 * 2**20)
    p.add_argument("--max-seconds", type=int, default=20)
    p.add_argument("--limit-rate-bytes", type=int, default=200 * 2**20)
    p.add_argument("--execute", action="store_true", help="perform the permitted read")
    p.set_defaults(func=link)
    p = sub.add_parser("probes")
    p.set_defaults(func=lambda _: print(dump(PROBE_HOOKS)))
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
