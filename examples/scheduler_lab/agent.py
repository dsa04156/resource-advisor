"""Single bounded native experiment runner; external config/auth, no user commands.

Run with a private JSON config. Kubernetes RBAC should be scoped to the lab
namespace. Slurm transport accepts JSON argv/stdin and returns JSON stdout/code.
A separate transport can hold SSH credentials; neither API nor UI receive them.
"""

import argparse
import fcntl
import json
import re
import ssl
import subprocess
import time
import urllib.request
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
        with urllib.request.urlopen(req, context=self.context, timeout=15) as response:
            return json.load(response)

    def heartbeat(self):
        return self.api(
            "/scheduler-lab-agent/heartbeat",
            {
                "scenarios": ["backfill", "gang", "topology"]
                + (["multi_gpu"] if self.c.get("multi_gpu") else [])
                + (["heterogeneous"] if self.c.get("heterogeneous") else []),
                "heterogeneous": self.c.get("heterogeneous", []),
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

    def job(self, suffix, count, duration, required=False):
        pool = self.c.get("multi_gpu", {}) if self.snapshot.get("multi_gpu") else {}
        name = self.ref + "-" + suffix
        labels = {
            "hairp.io/lab-run": self.ref,
            "kueue.x-k8s.io/queue-name": pool.get("queue", self.c["queue"]),
        }
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
        self.kube("apply", "-f", "-", value=job)
        return name

    def observe_kube(self):
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
                }
                if item["state"] in {"Running", "Succeeded", "Failed"}:
                    lines = self.kube("logs", item["name"], "--tail=8").splitlines()
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
            result.append(
                {
                    "id": name,
                    "label": name.removeprefix(self.ref + "-"),
                    "state": "FAILED"
                    if failed
                    else "SUCCEEDED"
                    if complete
                    else "RUNNING"
                    if js.get("active")
                    else "ADMITTED"
                    if admitted
                    else "PENDING",
                    "reason": reason,
                    "gpu": job["spec"]["parallelism"],
                    "pods": jp,
                    "admission": w.get("status", {}).get("admission"),
                    "conditions": conditions,
                    "native_uid": w.get("metadata", {}).get("uid"),
                }
            )
        self.snapshot["jobs"] = result
        return {j["label"]: j for j in result}

    def wait_kube(self, predicate, title):
        while True:
            result = self.observe_kube()
            self.tick(title)
            if any(j["state"] == "FAILED" for j in result.values()):
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
        self.snapshot.update(
            backend="Platform adapters",
            policy="GPU + NPU workloads",
            heterogeneous=True,
            phase="SUBMIT",
            platform_jobs=[],
            jobs=[],
            explanation="GPU용 CUDA/CNN 작업과 NPU용 ResNet-50을 각 검증된 실행 경로로 제출합니다. 공통 큐가 아니라 장치별 네이티브 큐에서 실행합니다.",
        )
        for i, task in enumerate(self.c["heterogeneous"]):
            result = self.api(
                "/jobs",
                {
                    "workload_ref": task["workload_ref"],
                    "scheduling_profile_ref": task["profile_ref"],
                    "mode": "observe",
                },
                key=f"{self.ref}-registered-{i}",
            )
            self.snapshot["platform_jobs"].append({**task, "job_id": result["job_id"]})
            self.report("등록된 " + task["device_class"].upper() + " 작업 접수")
        self.snapshot["phase"] = "EXECUTION"
        while True:
            jobs = []
            for task in self.snapshot["platform_jobs"]:
                view = self.api("/jobs/" + task["job_id"] + "/view")
                jobs.append(
                    {
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
                )
            self.snapshot["jobs"] = jobs
            self.tick("GPU·NPU 네이티브 작업 상태와 결과 관측")
            if any(j["state"] in {"FAILED", "CANCELED", "RESULT_INVALID"} for j in jobs):
                raise RuntimeError(
                    "이기종 작업 중 실패가 있습니다. 작업 상세의 실제 오류를 확인하세요"
                )
            if all(j["state"] == "SUCCEEDED" for j in jobs):
                self.snapshot["verdict"] = (
                    f"GPU·NPU {len(jobs)}개 등록 작업 모두 실제 실행 성공 · 작업별 결과와 MLflow 기록 확인 가능"
                )
                return

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
            self.report(title, state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    lock = open(config["lock_file"], "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = Agent(config)
    while True:
        rows = runner.heartbeat()
        if rows:
            runner.execute(rows[0])
        time.sleep(4)


if __name__ == "__main__":
    main()
