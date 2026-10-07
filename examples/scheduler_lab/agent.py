"""Single bounded native experiment runner; external config/auth, no user commands.

Run with a private JSON config. Kubernetes RBAC should be scoped to the lab
namespace. Slurm transport accepts JSON argv/stdin and returns JSON stdout/code.
A separate transport can hold SSH credentials; neither API nor UI receive them.
"""

import argparse
import fcntl
import json
import random
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


class Canceled(Exception):
    pass


class Agent:
    def __init__(self, config):
        self.c = config
        self.ref = None
        self.snapshot = {}
        self.previous = None
        self.deadline = 0
        self.context = ssl.create_default_context(cafile=config.get("ca_file"))
        self.slurm_ids = []
        self.reservation = None
        self.pending_report = None

    def api(self, path, body=None, key=None):
        credential = json.loads(Path(self.c["credentials_file"]).read_text())
        req = urllib.request.Request(
            credential["api_url"].rstrip("/") + "/api/v1/compute" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + credential["operator_token"],
                **({"Idempotency-Key": key} if key else {}),
            },
        )
        # Job acceptance retries keep their identical key in registered_workloads.
        # Reads and idempotent agent reports survive a transient TLS disconnect.
        attempts = 1 if path == "/jobs" else 6
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    req, context=self.context, timeout=60 if path == "/jobs" else 15
                ) as response:
                    return json.load(response)
            except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
                if (
                    isinstance(exc, urllib.error.HTTPError) and exc.code < 500
                ) or attempt == attempts - 1:
                    raise
                time.sleep(2)

    def heartbeat(self):
        return self.api(
            "/scheduler-lab-agent/heartbeat",
            {
                "scenarios": ["burst"]
                + (["pool_batch", "gang_batch"] if self.c.get("multi_gpu") else [])
                + (["fleet_batch", "mixed_batch"] if self.c.get("mixed") else [])
                + (
                    ["priority_batch", "adaptive_batch"]
                    if self.c.get("priorities") and self.c.get("multi_gpu")
                    else []
                ),
                "gpu_pool": self.c.get("gpu_pool", {}),
                "heterogeneous": self.c.get("heterogeneous", []),
                "mixed": self.c.get("mixed", []),
                "multi_gpu": self.c.get("multi_gpu", {}),
                "kubernetes": "Kueue native admission + hostname topology",
                "slurm": "Native sched/backfill + bounded reservation window",
                "nodes": self.c["nodes"],
                "workload": "CUDA numerical correctness probe",
            },
        )["items"]

    def command(self, argv, stdin=None):
        result = subprocess.run(
            ["rtk", "proxy", *argv], input=stdin, text=True, capture_output=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-700:])
        return result.stdout

    def kube(self, *args, value=None):
        return self.command(
            [
                self.c.get("kubectl", "kubectl"),
                *(["--kubeconfig", self.c["kubeconfig"]] if self.c.get("kubeconfig") else []),
                "-n",
                self.c["namespace"],
                *args,
            ],
            json.dumps(value) if value is not None else None,
        )

    def slurm(self, *args, stdin=None):
        result = self.command(
            self.c["slurm_transport"], json.dumps({"argv": list(args), "stdin": stdin})
        )
        result = json.loads(result)
        if result["returncode"]:
            raise RuntimeError(result["stderr"][-700:])
        return result["stdout"]

    def report(self, title=None, state="RUNNING"):
        self.api(
            "/scheduler-lab-agent/" + self.ref,
            {
                "state": state,
                "snapshot": self.snapshot,
                "event": {"title": title} if title else None,
            },
        )

    def tick(self, title=None):
        if self.c.get("gpu_pool"):
            ref = (
                self.c["gpu_pool"].get("comparison_queue", "hairp-scheduling-lab")
                if self.snapshot.get("queue_scope", {}).get("purpose") == "quota comparison"
                else self.c["gpu_pool"]["ref"]
            )
            queue = json.loads(self.kube("get", "clusterqueue", ref, "-o", "json"))
            status = queue.get("status", {})
            self.snapshot["pool_observation"] = {
                "ref": ref,
                "admitted": status.get("admittedWorkloads"),
                "pending": status.get("pendingWorkloads"),
                "flavors": queue["spec"]["resourceGroups"],
                "reservation": status.get("flavorsReservation"),
            }
        rows = self.heartbeat()
        if any(r["ref"] == self.ref and r["state"] == "CANCEL_REQUESTED" for r in rows):
            raise Canceled()
        if time.monotonic() > self.deadline:
            raise TimeoutError("native scheduler experiment deadline exceeded")
        key = json.dumps(self.snapshot, sort_keys=True)
        if key != self.previous:
            self.report(title or "네이티브 스케줄러 상태 변경")
            self.previous = key
        else:
            self.report()  # Fresh observation without adding an identical replay event.
        time.sleep(3)

    def job(
        self,
        suffix,
        count,
        duration,
        required=False,
        priority=None,
        fail=False,
        exclude_nodes=(),
        sustained=False,
        target_node=None,
    ):
        pool = self.c.get("multi_gpu", {}) if self.snapshot.get("multi_gpu") else {}
        name = self.ref + "-" + suffix
        labels = {
            "hairp.io/lab-run": self.ref,
            "kueue.x-k8s.io/queue-name": pool.get("queue", self.c["queue"]),
        }
        if priority:
            labels["kueue.x-k8s.io/priority-class"] = priority
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "labels": {"hairp.io/lab-run": self.ref}},
            "spec": {
                "clusterIP": "None",
                "publishNotReadyAddresses": True,
                "selector": {"job-name": name},
                "ports": [{"port": 23456}],
            },
        }
        self.kube("apply", "-f", "-", value=service)
        annotations = {"kueue.x-k8s.io/podset-unconstrained-topology": "true"}
        if required:
            annotations = {"kueue.x-k8s.io/podset-required-topology": "kubernetes.io/hostname"}
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "suspend": True,
                "parallelism": count,
                "completions": count,
                "completionMode": "Indexed",
                "backoffLimit": 0,
                "activeDeadlineSeconds": 180,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "annotations": annotations,
                        "labels": {"hairp.io/lab-run": self.ref},
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "subdomain": name,
                        "runtimeClassName": self.c["runtime_class"],
                        "terminationGracePeriodSeconds": 5,
                        "nodeSelector": pool.get(
                            "node_selector", {"hairp.io/scheduling-lab": "gpu"}
                        ),
                        "containers": [
                            {
                                "name": "gpu",
                                "image": self.c["image"],
                                "command": ["python3", "-u", "/lab/gpu_task.py"],
                                "env": [
                                    {"name": "WORLD_SIZE", "value": str(count)},
                                    {"name": "DURATION", "value": str(duration)},
                                    {
                                        "name": "COMPUTE_MODE",
                                        "value": "sustained" if sustained else "probe",
                                    },
                                    {"name": "RENDEZVOUS", "value": name + "-0." + name},
                                ],
                                "resources": {
                                    "requests": {
                                        "cpu": "500m",
                                        "memory": "256Mi",
                                        "nvidia.com/gpu": 1,
                                    },
                                    "limits": {"cpu": "1", "memory": "512Mi", "nvidia.com/gpu": 1},
                                },
                                "volumeMounts": [
                                    {"name": "code", "mountPath": "/lab", "readOnly": True}
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                        "volumes": [{"name": "code", "configMap": {"name": "hairp-lab-probe"}}],
                    },
                },
            },
        }
        if fail:
            job["spec"]["template"]["spec"]["containers"][0]["command"] = [
                "python3",
                "-u",
                "-c",
                'import json,sys; print(json.dumps({"phase":"EXPECTED_FAILURE","exit_code":42}),flush=True); sys.exit(42)',
            ]
        if exclude_nodes:
            job["spec"]["template"]["spec"]["affinity"] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "NotIn",
                                        "values": list(exclude_nodes),
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        if target_node:
            job["spec"]["template"]["spec"]["nodeSelector"] = {
                **job["spec"]["template"]["spec"]["nodeSelector"],
                "kubernetes.io/hostname": target_node,
            }
        self.kube("apply", "-f", "-", value=job)
        return name

    def observe_kube(self, update_snapshot=True):
        jobs = json.loads(
            self.kube("get", "jobs", "-l", "hairp.io/lab-run=" + self.ref, "-o", "json")
        )["items"]
        pods = json.loads(
            self.kube("get", "pods", "-l", "hairp.io/lab-run=" + self.ref, "-o", "json")
        )["items"]
        # Job labels do not automatically propagate to pod templates.
        if not pods:
            pods = json.loads(self.kube("get", "pods", "-o", "json"))["items"]
            pods = [
                p
                for p in pods
                if p["metadata"].get("labels", {}).get("job-name", "").startswith(self.ref)
            ]
        workloads = json.loads(self.kube("get", "workloads.kueue.x-k8s.io", "-o", "json"))["items"]
        result = []
        for job in jobs:
            name = job["metadata"]["name"]
            w = next(
                (
                    w
                    for w in workloads
                    if any(o["name"] == name for o in w["metadata"].get("ownerReferences", []))
                ),
                {},
            )
            conditions = w.get("status", {}).get("conditions", [])
            admitted = any(c["type"] == "Admitted" and c["status"] == "True" for c in conditions)
            js = job.get("status", {})
            complete = any(
                c["type"] == "Complete" and c["status"] == "True" for c in js.get("conditions", [])
            )
            failed = any(
                c["type"] == "Failed" and c["status"] == "True" for c in js.get("conditions", [])
            )
            jp = []
            for p in pods:
                if p["metadata"].get("labels", {}).get("job-name") != name:
                    continue
                item = {
                    "name": p["metadata"]["name"],
                    "rank": p["metadata"]
                    .get("annotations", {})
                    .get("batch.kubernetes.io/job-completion-index"),
                    "node": p["spec"].get("nodeName"),
                    "state": p.get("status", {}).get("phase", "Pending"),
                    "exit_codes": [
                        c["state"]["terminated"]["exitCode"]
                        for c in p.get("status", {}).get("containerStatuses", [])
                        if "terminated" in c.get("state", {})
                    ],
                }
                if item["state"] in {"Running", "Succeeded", "Failed"}:
                    lines = self.kube("logs", item["name"], "--tail=32").splitlines()
                    item["events"] = [
                        json.loads(line) for line in lines if line.startswith('{"phase"')
                    ]
                jp.append(item)
            reason = next(
                (
                    c.get("message", c.get("reason"))
                    for c in reversed(conditions)
                    if c["status"] == "False" and c["type"] in {"Admitted", "QuotaReserved"}
                ),
                None,
            )
            expected = job["spec"].get("completions", job["spec"]["parallelism"])
            finalizing = len(jp) >= expected and all(p["state"] == "Succeeded" for p in jp)
            result.append(
                {
                    "id": name,
                    "submitted_at": job["metadata"].get("creationTimestamp"),
                    "label": name.removeprefix(self.ref + "-"),
                    "state": "FAILED"
                    if failed
                    else "SUCCEEDED"
                    if complete
                    else "RUNNING"
                    if any(p["state"] == "Running" for p in jp)
                    else "FINALIZING"
                    if finalizing
                    else "ADMITTED"
                    if admitted
                    else "PENDING",
                    "reason": reason,
                    "gpu": job["spec"]["parallelism"],
                    "expected_completions": expected,
                    "priority": w.get("spec", {}).get("priority"),
                    "pods": jp,
                    "admission": w.get("status", {}).get("admission"),
                    "conditions": conditions,
                    "native_uid": w.get("metadata", {}).get("uid"),
                    "workload_name": w.get("metadata", {}).get("name"),
                }
            )
        result.extend(self.snapshot.get("retired_jobs", []))
        if update_snapshot:
            self.snapshot["jobs"] = result
        return {j["label"]: j for j in result}

    def wait_kube(self, predicate, title, expected_failures=()):
        while True:
            result = self.observe_kube()
            self.tick(title)
            if any(
                j["state"] == "FAILED" and j["label"] not in expected_failures
                for j in result.values()
            ):
                raise RuntimeError("GPU probe job failed; inspect native pod logs")
            if predicate(result):
                return result

    def gang(self):
        self.snapshot.update(
            backend="Kueue",
            policy="Gang admission",
            phase="BLOCKER",
            explanation="먼저 GPU 1개를 사용합니다. GPU 2개 그룹은 전체 quota가 확보될 때까지 기다립니다.",
        )
        self.job("blocker", 1, 35)
        self.wait_kube(lambda r: r["blocker"]["state"] == "RUNNING", "선행 GPU 작업 상태 관측")
        self.job("group", 2, 15)
        self.snapshot["phase"] = "WAIT_ALL"
        pending = self.wait_kube(
            lambda r: r.get("group", {}).get("state") == "PENDING" and bool(r["group"]["reason"]),
            "GPU 2개 그룹 입장 상태 관측",
        )
        self.snapshot["queue_evidence"] = pending["group"]
        self.snapshot["phase"] = "GROUP_EXECUTION"
        result = self.wait_kube(
            lambda r: r.get("group", {}).get("state") == "SUCCEEDED",
            "그룹의 승인·실행 상태 관측",
        )
        pods = result["group"]["pods"]
        if len(pods) != 2 or not all(
            any(e["phase"] == "BARRIER_RELEASED" for e in p.get("events", [])) for p in pods
        ):
            raise RuntimeError("missing two-worker barrier evidence")
        self.snapshot["verdict"] = (
            "GPU 2개 전체 quota 승인 후 두 worker가 barrier를 통과하고 CUDA 계산 완료"
        )

    def multi_gpu(self):
        count = self.snapshot["requested_gpus"]
        pool = self.c["multi_gpu"]
        if not 1 <= count <= pool["max_gpus"]:
            raise RuntimeError("requested GPU count exceeds configured PoC capacity")
        self.snapshot.update(
            backend="Kueue",
            policy="Multi-GPU PoC",
            phase="REQUEST",
            nodes=pool["nodes"],
            pool_gpus=pool["max_gpus"],
            explanation=f"하나의 작업이 GPU {count}개를 요청합니다. Worker마다 물리 GPU 1개를 배정하고 전체 준비 후 CUDA 검증을 실행합니다.",
        )
        if count > 1:
            self.job("blocker", pool["max_gpus"] - count + 1, 25)
            self.wait_kube(
                lambda r: r.get("blocker", {}).get("state") == "RUNNING",
                "선행 작업의 물리 GPU 할당 관측",
            )
        self.job("workers", count, 20)
        if count > 1:
            self.snapshot["phase"] = "WAIT_ALL"
            queued = self.wait_kube(
                lambda r: (
                    r.get("workers", {}).get("state") == "PENDING" and bool(r["workers"]["reason"])
                ),
                "다중 GPU 요청 전체 입장 대기",
            )
            self.snapshot["queue_evidence"] = queued["workers"]
        self.snapshot["phase"] = "PARALLEL_COMPUTE"
        result = self.wait_kube(
            lambda r: r.get("workers", {}).get("state") == "SUCCEEDED",
            "GPU별 worker 승인·준비·실행 관측",
        )
        pods = result["workers"]["pods"]
        finished = [
            next((e for e in p.get("events", []) if e["phase"] == "COMPUTE_FINISHED"), {})
            for p in pods
        ]
        ranks = {e.get("rank") for e in finished}
        if (
            len(pods) != count
            or ranks != set(range(count))
            or not all(e.get("correctness") for e in finished)
        ):
            raise RuntimeError("missing distinct successful CUDA workers")
        if count > 1 and not all(
            any(e["phase"] == "BARRIER_RELEASED" for e in p["events"]) for p in pods
        ):
            raise RuntimeError("missing all-worker startup barrier evidence")
        self.snapshot["result"] = {
            "requested_gpus": count,
            "workers_passed": len(finished),
            "nodes_used": len({p["node"] for p in pods}),
            "models": [e["report"]["accelerator_model"] for e in finished],
            "architectures": sorted({e["report"]["arch"] for e in finished}),
            "semantics": "Independent CUDA probes after a shared barrier; not DDP or a speedup benchmark",
        }
        self.snapshot["verdict"] = (
            f"물리 GPU {count}개에서 {count}개 worker 모두 CUDA 계산 완료 · 결과 {count}/{count} 검증"
        )

    def heterogeneous(self):
        return self.registered_workloads(self.c["heterogeneous"], "GPU + NPU")

    def npu(self):
        return self.registered_workloads(
            [t for t in self.c["heterogeneous"] if t["device_class"] == "npu"], "NPU"
        )

    def mixed(self):
        return self.registered_workloads(self.c["mixed"], "Kubernetes + Slurm", mixed=True)

    def observe_registered_job(self, task):
        view = self.api("/jobs/" + task["job_id"] + "/view")
        return {
            "id": task["job_id"],
            "label": task["name"],
            "state": view["state"],
            "device_class": task["device_class"],
            "workload_ref": task["workload_ref"],
            "backend": view.get("backend"),
            "reason": view.get("scheduler_reason") or view.get("error"),
            "native_id": view.get("external_id"),
            "node": view.get("node_ref"),
            "requested": view.get("requested_resources"),
            "tracking": view.get("tracking"),
            "result": view.get("result"),
            "started_at": view.get("started_at"),
            "finished_at": view.get("finished_at"),
            "lifecycle_events": view.get("lifecycle_events", []),
            "pods": [],
        }

    def registered_workloads(self, tasks, title, mixed=False):
        if not tasks:
            raise RuntimeError("no qualified registered workloads configured")
        self.snapshot.update(
            backend="Platform adapters",
            policy=title,
            heterogeneous=True,
            phase="SUBMIT",
            platform_jobs=[],
            jobs=[
                {
                    "id": f"{self.ref}-registered-{i}",
                    "label": task["name"],
                    "state": "REQUESTED",
                    "device_class": task["device_class"],
                    "workload_ref": task["workload_ref"],
                }
                for i, task in enumerate(tasks)
            ],
            request_batch={"count": len(tasks), "submission": "concurrent"},
            explanation=title
            + " 등록 작업을 각각의 네이티브 큐에 제출합니다. 작업별 실제 backend·장치·결과를 관측합니다.",
        )

        self.report(f"독립 요청 {len(tasks)}개 · 플랫폼 접수 시작")

        def submit(item):
            i, task = item
            for attempt in range(3):
                try:
                    return (
                        i,
                        task,
                        self.api(
                            "/jobs",
                            {
                                "workload_ref": task["workload_ref"],
                                "scheduling_profile_ref": task["profile_ref"],
                                "mode": "observe",
                            },
                            key=f"{self.ref}-registered-{i}",
                        ),
                    )
                except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
                    if (isinstance(exc, urllib.error.HTTPError) and exc.code < 500) or attempt == 2:
                        raise
                    time.sleep(0.5)

        # The native adapters remain independent; only acceptance is concurrent.
        failures = []
        with ThreadPoolExecutor(max_workers=min(len(tasks), 10)) as pool:
            futures = {pool.submit(submit, item): item for item in enumerate(tasks)}
            for future in as_completed(futures):
                try:
                    i, task, result = future.result()
                    self.snapshot["platform_jobs"].append(
                        {**task, "job_id": result["job_id"], "request_index": i}
                    )
                    self.snapshot["jobs"][i].update(id=result["job_id"], state=result["state"])
                    # Show native execution while remaining requests are still
                    # accepted; a receipt alone does not indicate the current state.
                    try:
                        for accepted in self.snapshot["platform_jobs"]:
                            self.snapshot["jobs"][accepted["request_index"]] = (
                                self.observe_registered_job(accepted)
                            )
                    except (OSError, RuntimeError, ValueError) as exc:
                        self.snapshot["acceptance_observation_error"] = type(exc).__name__
                    self.report("등록된 " + task["device_class"].upper() + " 작업 접수")
                except Exception as exc:  # noqa: BLE001 — retain successful receipts for cleanup
                    index, task = futures[future]
                    reason = str(exc)[:300]
                    if isinstance(exc, urllib.error.HTTPError):
                        try:
                            reason += (
                                " · " + str(json.loads(exc.read(4096)).get("detail", ""))[:700]
                            )
                        except (ValueError, OSError):
                            pass
                    self.snapshot["jobs"][index].update(
                        state="FAILED", reason=reason, submission_rejected=True
                    )
                    failures.append(reason)
                    self.report("요청 거절 · 다른 접수 작업은 계속 실행")
        self.snapshot["submission_failures"] = failures
        self.snapshot["phase"] = "EXECUTION"
        while True:
            jobs = list(self.snapshot["jobs"])
            for task in sorted(self.snapshot["platform_jobs"], key=lambda t: t["request_index"]):
                jobs[task["request_index"]] = self.observe_registered_job(task)
            self.snapshot["jobs"] = jobs
            accepted_jobs = [j for j in jobs if not j.get("submission_rejected")]
            self.tick("GPU·NPU 네이티브 작업 상태와 결과 관측")
            if any(j["state"] in {"FAILED", "CANCELED", "RESULT_INVALID"} for j in accepted_jobs):
                raise RuntimeError(
                    "이기종 작업 중 실패가 있습니다. 작업 상세의 실제 오류를 확인하세요"
                )
            if all(j["state"] == "SUCCEEDED" for j in accepted_jobs):
                if (
                    not failures
                    and mixed
                    and {j["backend"] for j in accepted_jobs} != {"kubernetes", "slurm"}
                ):
                    raise RuntimeError(
                        "mixed execution completed without evidence of both backends"
                    )
                aliases = self.c.get("gpu_pool", {}).get("node_aliases", {})
                self.snapshot["execution_coverage"] = {
                    device: sorted(
                        {
                            aliases.get(j["node"], j["node"])
                            for j in jobs
                            if j.get("node") and j["device_class"] == device
                        }
                    )
                    for device in {j["device_class"] for j in jobs}
                }
                if failures:
                    raise RuntimeError(
                        f"접수 작업 {len(accepted_jobs)}개 완료; 요청 {len(failures)}개 거절: "
                        + "; ".join(failures)
                    )
                self.snapshot["verdict"] = (
                    f"{title} {len(jobs)}개 등록 작업 모두 실제 실행 성공 · 작업별 결과와 MLflow 기록 확인 가능"
                )
                return

    def retire(self, label, reason):
        record = self.observe_kube()[label]
        self.kube("delete", "job", record["id"], "--wait=true", "--timeout=25s")
        record = {**record, "state": "CANCELED", "reason": reason, "native_deleted": True}
        self.snapshot.setdefault("retired_jobs", []).append(record)
        self.observe_kube()
        self.report(reason)

    def occupy_pool(self, policy, explanation):
        self.snapshot.update(
            backend="Kueue", policy=policy, phase="OCCUPY", explanation=explanation
        )
        self.job("blocker", 2, 120)
        self.wait_kube(
            lambda r: r["blocker"]["state"] == "RUNNING" and len(r["blocker"]["pods"]) == 2,
            "선행 작업의 2 GPU 점유 관측",
        )

    def quota(self):
        self.occupy_pool(
            "Quota backlog",
            "2 GPU quota를 점유한 뒤 1 GPU 작업 3개를 접수합니다. 대기 사유를 기록하고 선행 작업을 종료합니다.",
        )
        labels = ["request-a", "request-b", "request-c"]
        for label in labels:
            self.job(label, 1, 20)
            self.observe_kube()
            self.tick("큐에 추가 접수: " + label)
        result = self.wait_kube(
            lambda r: (
                all(r.get(label, {}).get("state") == "PENDING" for label in labels)
                and any("quota" in (r[label].get("reason") or "").lower() for label in labels)
            ),
            "세 요청의 동시 대기와 네이티브 quota 부족 근거 관측",
        )
        self.snapshot["queue_evidence"] = [result[label] for label in labels]
        self.snapshot["phase"] = "RELEASE"
        self.retire("blocker", "시나리오가 선행 작업을 종료하여 quota 반환")
        self.wait_kube(
            lambda r: all(r[label]["state"] == "SUCCEEDED" for label in labels),
            "반환된 quota에 요청들이 입장·실행",
        )
        self.snapshot["verdict"] = (
            "3개 요청의 quota 대기를 기록한 뒤 선행 작업 종료, 모든 GPU 요청 완료"
        )

    def pool_batch(self):
        self.snapshot["multi_gpu"] = True
        return self.burst()

    def gang_batch(self):
        self.snapshot.update(multi_gpu=True, gang_batch=True)
        return self.burst()

    def priority_batch(self):
        self.snapshot.update(multi_gpu=True, priority_batch=True)
        return self.burst()

    def fleet_batch(self):
        self.snapshot["scope"] = "registered_heterogeneous_pool"
        return self.mixed_batch()

    def promote_pending(self, record):
        """Update only this run's unreserved Workload, guarded against races."""
        name = record.get("workload_name")
        if not name or record["state"] != "PENDING":
            return False
        w = json.loads(self.kube("get", "workload", name, "-o", "json"))
        if not any(o.get("name") == record["id"] for o in w["metadata"].get("ownerReferences", [])):
            raise RuntimeError("priority change owner mismatch")
        if any(
            c["type"] == "QuotaReserved" and c["status"] == "True"
            for c in w.get("status", {}).get("conditions", [])
        ):
            return False
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": w["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": w["metadata"]["resourceVersion"],
            },
            {"op": "replace", "path": "/spec/priority", "value": 100},
        ]
        try:
            self.kube("patch", "workload", name, "--type=json", "-p", json.dumps(patch))
        except RuntimeError:
            # A concurrent admission wins; never modify admitted/running work.
            current = json.loads(self.kube("get", "workload", name, "-o", "json"))
            if current["metadata"]["resourceVersion"] != w["metadata"]["resourceVersion"]:
                return False
            raise
        self.snapshot.setdefault("priority_changes", []).append(
            {
                "request_id": record["id"],
                "workload_uid": w["metadata"]["uid"],
                "before": w["spec"].get("priority"),
                "after": 100,
                "reason": "대기 20초 이상 · 시나리오 aging 정책",
                "observed_at": time.time(),
            }
        )
        return True

    @staticmethod
    def arrival_plan(seed):
        rng = random.Random(seed)
        offset = 0
        tasks = []
        for i in range(10):
            if i:
                offset += rng.randint(3, 9)
            tasks.append(
                {
                    "request_id": f"request-{i + 1:02d}",
                    "arrival_seconds": offset,
                    "gpu": 2 if i in {0, 2, 5} else 1,
                    "duration": 60 if i < 2 else 30,
                    "priority_class": "high" if i in {4, 7} else "low",
                    "attempt": 0,
                    "attempts": [],
                    "excluded_nodes": [],
                }
            )
        return tasks

    def adaptive_batch(self):
        seed = random.SystemRandom().randrange(2**31)
        tasks = self.arrival_plan(seed)
        self.snapshot.update(
            multi_gpu=True,
            adaptive=True,
            phase="ARRIVALS",
            policy="Staggered arrivals + aging + bounded failover",
            backend="Kueue",
            pool_gpus=self.c["multi_gpu"]["max_gpus"],
            request_batch={"count": 10, "submission": "staggered", "seed": seed},
            explanation="10개 변동 도착 · 1/2 GPU 요청 · 대기 우선순위 승격 · 실험 장애 1회 후 다른 노드 재실행",
            queue_scope={"purpose": "common GPU pool"},
            arrival_plan=tasks,
            jobs=[
                {
                    "id": self.ref + ":" + t["request_id"],
                    "label": t["request_id"],
                    "state": "NOT_SUBMITTED",
                    "gpu": t["gpu"],
                    "arrival_seconds": t["arrival_seconds"],
                }
                for t in tasks
            ],
        )
        started = time.monotonic()
        archived = {}
        while True:
            elapsed = time.monotonic() - started
            for task in tasks:
                if task["attempt"] == 0 and elapsed >= task["arrival_seconds"]:
                    task["attempt"] = 1
                    task["submitted_elapsed"] = time.monotonic() - started
                    task["native_label"] = task["request_id"] + "-a1"
                    self.job(
                        task["native_label"],
                        task["gpu"],
                        task["duration"],
                        priority=self.c["priorities"][task["priority_class"]],
                        fail=task["request_id"] == "request-02",
                        sustained=True,
                    )
                    self.report("새 요청 도착 · " + task["request_id"])
            native = self.observe_kube(update_snapshot=False)
            rows = []
            for task in tasks:
                record = native.get(task.get("native_label"))
                if record and record["state"] == "FAILED" and task["attempt"] == 1:
                    failed_nodes = sorted({p["node"] for p in record["pods"] if p.get("node")})
                    expected = task["request_id"] == "request-02" and any(
                        42 in p.get("exit_codes", []) for p in record["pods"]
                    )
                    if failed_nodes and expected:
                        nodes = json.loads(self.kube("get", "nodes", "-o", "json"))["items"]
                        qualified = {n["id"] for n in self.c["multi_gpu"]["nodes"]}
                        available = sorted(
                            n["metadata"]["name"]
                            for n in nodes
                            if n["metadata"]["name"] in qualified
                            and n["metadata"]["name"] not in failed_nodes
                            and not n["spec"].get("unschedulable")
                            and any(
                                c["type"] == "Ready" and c["status"] == "True"
                                for c in n["status"]["conditions"]
                            )
                            and int(n["status"].get("allocatable", {}).get("nvidia.com/gpu", 0))
                            >= task["gpu"]
                        )
                        if not available:
                            raise RuntimeError("다른 검증된 GPU 노드 없음 · 실패 근거 보존")
                        task["attempts"].append(record)
                        archived[record["label"]] = record
                        task["excluded_nodes"] = failed_nodes
                        task["attempt"] = 2
                        task["native_label"] = task["request_id"] + "-a2"
                        self.job(
                            task["native_label"],
                            task["gpu"],
                            task["duration"],
                            priority=self.c["priorities"][task["priority_class"]],
                            exclude_nodes=failed_nodes,
                            sustained=True,
                            target_node=available[0],
                        )
                        record = None
                        self.report(
                            "실패 기록 보존 · 다른 GPU 후보로 재접수 · " + task["request_id"]
                        )
                if (
                    record
                    and record["state"] == "PENDING"
                    and not task.get("promoted")
                    and task["priority_class"] == "low"
                    and elapsed - task["submitted_elapsed"] >= 20
                ):
                    task["promoted"] = self.promote_pending(record)
                    if task["promoted"]:
                        self.report("대기 우선순위 승격 · " + task["request_id"])
                row = {
                    **(record or {}),
                    "id": self.ref + ":" + task["request_id"],
                    "native_id": record["id"] if record else None,
                    "label": task["request_id"],
                    "device_class": "gpu",
                    "backend": "kubernetes",
                    "state": record["state"]
                    if record
                    else "REQUEUED"
                    if task["attempt"] == 2
                    else "SUBMITTED"
                    if task["attempt"]
                    else "NOT_SUBMITTED",
                    "gpu": task["gpu"],
                    "attempt": task["attempt"],
                    "attempts": task["attempts"],
                    "excluded_nodes": task["excluded_nodes"],
                    "arrival_seconds": task["arrival_seconds"],
                    "duration": task["duration"],
                    "submitted_elapsed": task.get("submitted_elapsed"),
                    "priority": record.get("priority")
                    if record
                    else (100 if task["priority_class"] == "high" else 10),
                }
                rows.append(row)
                if record and record["state"] in {"FAILED", "SUCCEEDED"}:
                    archived[record["label"]] = record
            self.snapshot["jobs"] = rows
            self.snapshot["elapsed_seconds"] = round(elapsed, 1)
            self.snapshot["native_attempt_evidence"] = list(archived.values())
            self.tick("도착·대기 순서·GPU 그룹·재할당 관측")
            if all(r["state"] in {"SUCCEEDED", "FAILED"} for r in rows):
                if any(r["state"] == "FAILED" for r in rows):
                    raise RuntimeError(
                        "작업 실패 · 재시도 제한 또는 복구 불가 · 나머지 독립 작업 결과 보존"
                    )
                for row in rows:
                    finished = [
                        e
                        for p in row["pods"]
                        for e in p.get("events", [])
                        if e["phase"] == "COMPUTE_FINISHED" and e.get("correctness")
                    ]
                    if len(finished) != row["gpu"]:
                        raise RuntimeError("GPU worker numerical results missing")
                    if row["gpu"] > 1 and not all(
                        any(e["phase"] == "BARRIER_RELEASED" for e in p.get("events", []))
                        for p in row["pods"]
                    ):
                        raise RuntimeError("multi-worker startup barrier evidence missing")
                    if row["attempt"] > 1 and set(row["excluded_nodes"]) & {
                        p["node"] for p in row["pods"]
                    }:
                        raise RuntimeError("retry used an excluded failed node")
                self.snapshot["verdict"] = (
                    "10개 실제 요청 완료 · 1/2 GPU worker 결과와 재시도 노드 근거 보존"
                )
                return

    def mixed_batch(self):
        templates = self.c["mixed"]
        cuda = next(
            t
            for t in templates
            if t["workload_ref"] in {"cuda-smoke-auto-v1", "cuda-sustained-auto-v1"}
        )
        cnn = next(t for t in templates if t["workload_ref"] == "e5-kernel-cache-v2")
        npu = next(t for t in templates if t["device_class"] == "npu")
        slurm = next(t for t in templates if t["workload_ref"] == "slurm-orin-cnn-api-v1")
        selected = [cuda] * 5 + [cnn, npu, npu, slurm, slurm]
        tasks = [
            {**task, "name": f"요청 {index + 1:02d} · " + task["name"]}
            for index, task in enumerate(selected)
        ]
        return self.registered_workloads(tasks, "공통 GPU 풀 + NPU · 10개 요청", mixed=True)

    def burst(self):
        capacity = self.c["multi_gpu"]["max_gpus"] if self.snapshot.get("multi_gpu") else 2
        self.snapshot["pool_gpus"] = capacity
        self.snapshot["queue_scope"] = {
            "local_queue": self.c["multi_gpu"]["queue"]
            if self.snapshot.get("multi_gpu")
            else self.c["queue"],
            "purpose": "common GPU pool" if self.snapshot.get("multi_gpu") else "quota comparison",
            "qualified_probe_gpu_nodes": capacity,
        }
        labels = [f"request-{index:02d}" for index in range(1, 11)]
        self.snapshot.update(
            phase="SUBMIT",
            explanation=f"독립 요청 10개 · {capacity}개 검증된 CUDA 노드 · 큐 대기와 실제 자원 할당을 관측합니다.",
            request_batch={"count": 10, "labels": labels, "submission": "concurrent"},
            submitted_requests=[
                {
                    "id": self.ref + "-" + label,
                    "label": label,
                    "state": "SUBMITTED",
                    "gpu": 2 if self.snapshot.get("gang_batch") and index < 3 else 1,
                }
                for index, label in enumerate(labels)
            ],
        )
        self.report("동시 요청 10개 접수 · 네이티브 Job 생성 시작")
        # Independent native requests, not a ten-worker gang or an expanded fixture.
        # Wait for every submission before cleanup, including partial-create failure.
        with ThreadPoolExecutor(max_workers=10) as pool:
            submitted = [
                pool.submit(
                    self.job,
                    label,
                    2 if self.snapshot.get("gang_batch") and index < 3 else 1,
                    12,
                    priority=(
                        self.c["priorities"]["high" if index >= 5 else "low"]
                        if self.snapshot.get("priority_batch")
                        else None
                    ),
                )
                for index, label in enumerate(labels)
            ]
            for future in submitted:
                future.result()
        self.snapshot["phase"] = "QUEUE"
        result = self.wait_kube(
            lambda rows: (
                all(label in rows for label in labels)
                and any(rows[label]["state"] == "PENDING" for label in labels)
                and any(rows[label]["state"] == "RUNNING" for label in labels)
                and any("quota" in (rows[label].get("reason") or "").lower() for label in labels)
            ),
            "10개 네이티브 요청 · 실행과 quota 대기 동시 관측",
        )
        self.snapshot["queue_evidence"] = [result[label] for label in labels]
        self.snapshot["phase"] = "DRAIN"
        self.wait_kube(
            lambda rows: all(
                rows.get(label, {}).get("state") == "SUCCEEDED"
                and len(rows[label].get("pods", [])) == rows[label]["gpu"]
                and all(
                    any(
                        e.get("phase") == "COMPUTE_FINISHED" and e.get("correctness") is True
                        for e in pod.get("events", [])
                    )
                    and (
                        rows[label]["gpu"] == 1
                        or any(e.get("phase") == "BARRIER_RELEASED" for e in pod.get("events", []))
                    )
                    for pod in rows[label].get("pods", [])
                )
                for label in labels
            ),
            "완료된 작업의 quota 반환 → 대기 요청 입장·실행",
        )
        self.snapshot["verdict"] = "동시 요청 10개, 실제 quota 대기와 GPU 실행, 10개 CUDA 검증 완료"

    def priority(self):
        self.occupy_pool(
            "Workload priority",
            "2 GPU 풀을 점유한 상태에서 낮은 우선순위 → 높은 우선순위 순서로 같은 크기 요청을 넣습니다.",
        )
        for label in ["low", "high"]:
            self.job(label, 2, 20, priority=self.c["priorities"][label])
        queued = self.wait_kube(
            lambda r: (
                all(
                    r.get(k, {}).get("state") == "PENDING" and r[k].get("priority") is not None
                    for k in ["low", "high"]
                )
                and any("quota" in (r[k].get("reason") or "").lower() for k in ["low", "high"])
            ),
            "두 우선순위 요청의 동시 대기 관측",
        )
        if (
            queued["high"].get("priority") is None
            or queued["low"].get("priority") is None
            or queued["high"]["priority"] <= queued["low"]["priority"]
        ):
            raise RuntimeError("native high/low workload priority evidence missing")
        self.snapshot["queue_evidence"] = [queued["low"], queued["high"]]
        self.retire("blocker", "선행 작업 종료 · Kueue가 다음 입장 요청 결정")
        result = self.wait_kube(
            lambda r: any(r[k].get("admission") for k in ["low", "high"]),
            "실제 우선순위 입장 순서 관측",
        )
        if not result["high"].get("admission") or result["low"].get("admission"):
            raise RuntimeError("higher priority first admission was not observed")
        self.snapshot["priority_evidence"] = {
            "first": "high",
            "high": result["high"],
            "low": result["low"],
        }
        self.report("나중에 제출한 높은 우선순위 작업이 먼저 입장")
        self.wait_kube(
            lambda r: all(r[k]["state"] == "SUCCEEDED" for k in ["low", "high"]),
            "높은 우선순위 완료 후 낮은 우선순위 실행",
        )
        self.snapshot["verdict"] = (
            "낮은 우선순위를 먼저 제출했지만 높은 우선순위가 먼저 승인·실행; 두 작업 모두 완료"
        )

    def cancel(self):
        self.occupy_pool(
            "Queued cancellation",
            "quota를 점유한 상태에서 요청을 대기시킨 뒤 그 요청만 취소하고 후속 요청을 실행합니다.",
        )
        self.job("withdraw", 1, 15)
        result = self.wait_kube(
            lambda r: r.get("withdraw", {}).get("state") == "PENDING" and r["withdraw"]["reason"],
            "취소할 요청의 실제 대기 관측",
        )
        self.snapshot["queue_evidence"] = result["withdraw"]
        self.retire("withdraw", "대기 요청 취소 · 해당 Job 삭제 확인; 점유 quota는 없었음")
        self.retire("blocker", "선행 작업 종료 · 후속 요청을 위한 quota 반환")
        self.job("replacement", 1, 15)
        self.wait_kube(
            lambda r: r.get("replacement", {}).get("state") == "SUCCEEDED",
            "후속 GPU 요청 실행 관측",
        )
        self.snapshot["verdict"] = "대기 요청 취소를 확인하고 선행 점유 해제 후 후속 요청 완료"

    def recovery(self):
        self.snapshot.update(
            backend="Kueue",
            policy="Failure and resubmission",
            phase="EXPECTED_FAILURE",
            explanation="GPU 자원을 요청한 테스트 컨테이너 하나를 종료 코드 42로 끝냅니다. 실패를 기록한 뒤 새 CUDA 검증 Job을 제출합니다.",
        )
        self.job("expected-failure", 1, 1, fail=True)
        result = self.wait_kube(
            lambda r: r.get("expected-failure", {}).get("state") == "FAILED",
            "의도한 컨테이너 실패 관측",
            expected_failures={"expected-failure"},
        )
        failed = result["expected-failure"]
        if not any(42 in p.get("exit_codes", []) for p in failed["pods"]):
            raise RuntimeError("failure occurred without expected exit code 42")
        self.snapshot["failure_evidence"] = failed
        self.snapshot["phase"] = "RESUBMIT"
        self.job("recovered", 1, 15)
        result = self.wait_kube(
            lambda r: r.get("recovered", {}).get("state") == "SUCCEEDED",
            "새 GPU Job의 정상 CUDA 실행 확인",
            expected_failures={"expected-failure"},
        )
        if not any(
            e.get("correctness") for p in result["recovered"]["pods"] for e in p.get("events", [])
        ):
            raise RuntimeError("successful recovery missing CUDA correctness evidence")
        self.snapshot["verdict"] = (
            "종료 코드 42 실패를 보존하고 새 Job에서 CUDA 검증 성공 · 자동 체크포인트 복구는 아님"
        )

    def topology(self):
        self.snapshot.update(
            backend="Kueue",
            policy="Topology-aware scheduling",
            phase="SAME_NODE",
            explanation="GPU 총 2개가 있어도 각 노드에 1개씩이면 동일 노드 2 GPU 조건을 만족하지 못합니다.",
        )
        name = self.job("same-node", 2, 15, required=True)
        result = self.wait_kube(
            lambda r: bool(r["same-node"]["reason"]) and r["same-node"]["state"] == "PENDING",
            "동일 노드 topology 조건의 실제 대기 사유",
        )
        evidence = result["same-node"]
        if not any(t in (evidence["reason"] or "").lower() for t in ["topology", "domain", "fit"]):
            raise RuntimeError("pending observed, but native topology constraint evidence missing")
        self.snapshot["queue_evidence"] = evidence
        self.report("동일 노드 조건 불충족을 기록; 비교 실행으로 전환")
        self.kube("delete", "job", name, "--wait=true", "--timeout=25s")
        self.snapshot.update(
            phase="CROSS_NODE",
            explanation="같은 자원 요청에서 동일 노드 제약을 해제해 실제 두 노드 배치를 비교합니다.",
        )
        self.job("cross-node", 2, 15)
        result = self.wait_kube(
            lambda r: r.get("cross-node", {}).get("state") == "SUCCEEDED",
            "Kueue topologyAssignment와 두 노드 실행 관측",
        )
        if len({p["node"] for p in result["cross-node"]["pods"]}) != 2:
            raise RuntimeError("two-node placement evidence missing")
        self.snapshot["verdict"] = (
            "동일 노드 배치는 대기, 제약 해제 후 두 GPU 노드에 실제 배치·계산 완료"
        )

    def observe_slurm(self):
        raw = self.slurm(
            "sacct",
            "-X",
            "-n",
            "-P",
            "-j",
            ",".join(self.slurm_ids),
            "--format=JobID,JobName,State,Reason,Start,End,Elapsed,Timelimit,NodeList",
        )
        active = {}
        live = self.slurm("squeue", "-h", "-j", ",".join(self.slurm_ids), "-o", "%i|%T|%r|%N")
        for line in live.splitlines():
            values = line.split("|")
            if len(values) == 4:
                active[values[0]] = values[1:]
        jobs = []
        for line in raw.splitlines():
            fields = line.split("|")
            if len(fields) < 9 or fields[0] not in self.slurm_ids:
                continue
            i, name, state, reason, start, end, elapsed, limit, node = fields[:9]
            if i in active:
                state, reason, node = active[i]
            jobs.append(
                {
                    "id": i,
                    "label": name.removeprefix(self.ref + "-"),
                    "state": state,
                    "reason": reason,
                    "start": start,
                    "end": end,
                    "elapsed": elapsed,
                    "time_limit": limit,
                    "gpu": 1,
                    "pods": [{"name": i, "node": node, "state": state}],
                }
            )
        self.snapshot["jobs"] = jobs
        return {j["label"]: j for j in jobs}

    def backfill(self):
        self.snapshot.update(
            backend="Slurm",
            policy="sched/backfill",
            phase="RESERVATION_WINDOW",
            explanation="곧 시작할 1분 예약 앞에서 긴 요청은 대기합니다. 그 빈 시간에 들어갈 수 있는 짧은 GPU 작업을 뒤에 제출합니다.",
        )
        self.reservation = self.ref
        self.slurm(
            "scontrol",
            "create",
            "reservation",
            "ReservationName=" + self.reservation,
            "StartTime=now+2minutes",
            "Duration=1",
            "Nodes=" + self.c["slurm_node"],
            "Users=root",
        )
        self.snapshot["reservation"] = self.slurm(
            "scontrol", "show", "reservation", self.reservation
        ).strip()
        self.snapshot["sdiag_before"] = self.slurm("sdiag")
        probe = Path(__file__).resolve().parents[2] / "src/resource_advisor/cuda_probe.py"
        code = (
            probe.read_text().split("if __name__")[0]
            + "\nimport time\nfor _ in range(30):\n print(measure(), flush=True)\n time.sleep(.3)\n"
        )
        for label, limit, nice in [("long", "00:02:00", "0"), ("short", "00:01:00", "100")]:
            script = "#!/bin/bash\nset -e\npython3 - <<\x27PY\x27\n" + code + "\nPY\n"
            ident = (
                self.slurm(
                    "sbatch",
                    "--parsable",
                    "--job-name=" + self.ref + "-" + label,
                    "--partition=" + self.c["slurm_partition"],
                    "--account=" + self.c["slurm_account"],
                    "--qos=" + self.c["slurm_qos"],
                    "--nodelist=" + self.c["slurm_node"],
                    "--gres=gpu:1",
                    "--cpus-per-task=1",
                    "--mem=256M",
                    "--time=" + limit,
                    "--nice=" + nice,
                    "--output=/tmp/" + self.ref + "-%j.log",
                    stdin=script,
                )
                .strip()
                .split(";")[0]
            )
            if not ident.isdigit():
                raise RuntimeError("invalid native Slurm ID")
            self.slurm_ids.append(ident)
            self.snapshot["native_ids"] = self.slurm_ids[:]
            self.report("실제 sbatch 제출: " + label)
        seen_backfill = False
        self.snapshot["phase"] = "BACKFILL"
        while True:
            jobs = self.observe_slurm()
            if (
                jobs.get("short", {}).get("state") in {"RUNNING", "COMPLETED"}
                and jobs.get("long", {}).get("state") == "PENDING"
            ):
                if not seen_backfill:
                    self.snapshot["queue_evidence"] = jobs["long"]
                    self.snapshot["sdiag_during"] = self.slurm("sdiag")
                seen_backfill = True
            self.tick("Slurm 실제 시작 순서와 예약 창 관측")
            if any(
                j["state"] in {"FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "CANCELLED"}
                for j in jobs.values()
            ):
                raise RuntimeError("native Slurm GPU probe failed")
            if len(jobs) == 2 and all(j["state"] == "COMPLETED" for j in jobs.values()):
                if not seen_backfill or not jobs["short"]["start"] < jobs["long"]["start"]:
                    raise RuntimeError("jobs completed but backfill ordering was not observed")
                self.snapshot["sdiag_after"] = self.slurm("sdiag")
                counts = []
                for key in ("sdiag_before", "sdiag_during"):
                    match = re.search(
                        r"Total backfilled jobs \(since last slurm start\):\s*(\d+)",
                        self.snapshot.get(key, ""),
                    )
                    if not match:
                        raise RuntimeError("missing native backfill counter evidence")
                    counts.append(int(match.group(1)))
                if counts[1] <= counts[0]:
                    raise RuntimeError("start ordering observed without backfill counter increment")
                self.snapshot["backfill_counter"] = {"before": counts[0], "during": counts[1]}
                self.snapshot["verdict"] = (
                    "늦게 제출한 짧은 GPU 작업이 예약 전 빈 시간에 먼저 완료; 긴 작업은 예약 창 이후 실행"
                )
                return

    def cleanup(self):
        # Attempt both backends even if one is unavailable. Never delete by a broad name.
        errors = []
        pending = []
        for task in self.snapshot.get("platform_jobs", []):
            try:
                view = self.api("/jobs/" + task["job_id"] + "/view")
                if view["state"] not in {"SUCCEEDED", "FAILED", "CANCELED", "RESULT_INVALID"}:
                    self.api("/jobs/" + task["job_id"] + "/cancel", {})
                    pending.append(task["job_id"])
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(str(exc)[:300])
        until = time.monotonic() + 25
        while pending and time.monotonic() < until:
            self.heartbeat()
            pending = [
                ident
                for ident in pending
                if self.api("/jobs/" + ident + "/view")["state"]
                not in {"SUCCEEDED", "FAILED", "CANCELED", "RESULT_INVALID"}
            ]
            if pending:
                time.sleep(2)
        if pending:
            errors.append("Cancellation pending; inspect platform jobs: " + ",".join(pending))
        try:
            self.kube(
                "delete",
                "job,service",
                "-l",
                "hairp.io/lab-run=" + self.ref,
                "--ignore-not-found=true",
                "--wait=false",
            )
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc)[:300])
        ids = self.slurm_ids or self.snapshot.get("native_ids", [])
        if ids:
            try:
                self.slurm("scancel", *ids)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc)[:300])
        if self.reservation or self.snapshot.get("reservation"):
            try:
                reservations = self.slurm("scontrol", "show", "reservation")
                if "ReservationName=" + self.ref + " " in reservations:
                    self.slurm("scontrol", "delete", "ReservationName=" + self.ref)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc)[:300])
        if errors:
            raise RuntimeError("; ".join(errors))

    def execute(self, row):
        self.ref, self.snapshot = row["ref"], row["body"].get("snapshot", {})
        self.slurm_ids, self.reservation, self.previous = [], None, None
        self.deadline = time.monotonic() + 480
        state, title = "FAILED", "실험 중단"
        try:
            if row["state"] != "REQUESTED":
                raise (
                    Canceled()
                    if row["state"] == "CANCEL_REQUESTED"
                    else RuntimeError("runner restarted; bounded resources cleaned up")
                )
            if row["scenario"] == "multi_gpu":
                self.snapshot.update(multi_gpu=True, requested_gpus=row["body"]["gpu_count"])
            self.report("네이티브 스케줄러 실험 시작")
            getattr(self, row["scenario"])()
            state, title = "SUCCEEDED", self.snapshot["verdict"]
        except Canceled:
            state, title = "CANCELED", "사용자 취소: 이 실험의 자원 정리"
        except Exception as exc:  # noqa: BLE001 — persist failure and clean owned resources
            title = str(exc)[:800]
            self.snapshot["error"] = title
        finally:
            try:
                self.cleanup()
                self.snapshot["cleanup"] = "completed"
            except Exception as exc:  # noqa: BLE001 — persist failure and clean owned resources
                state, title = "FAILED", "실험 자원 정리 확인 필요"
                self.snapshot["cleanup_error"] = str(exc)[:500]
            self.snapshot["phase"] = "FINISHED"
            self.pending_report = (title, state)
            self.report(title, state)
            self.pending_report = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    lock = open(config["lock_file"], "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = Agent(config)
    while True:
        try:
            if runner.pending_report:
                runner.report(*runner.pending_report)
                runner.pending_report = None
            rows = runner.heartbeat()
            if rows:
                runner.execute(rows[0])
        except (TimeoutError, ConnectionError, urllib.error.URLError):
            # Preserve the in-memory final report; do not restart and cancel a
            # run merely because its final report acknowledgement was lost.
            print("Agent API temporarily unavailable; retrying without process restart", flush=True)
        time.sleep(4)


if __name__ == "__main__":
    main()
