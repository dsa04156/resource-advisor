"""Render a task-owned qualification Job for vision_training on one Orin GPU slot.

Qualification only: the in-Pod wrapper observes the device name and runtime
versions, derives the seeded initial checkpoint digest, then calls the same
`vision_training.run` used by the Resource Advisor variant with an environment
built from those observations. A PASS here is evidence for registering a
TRAINING_VERIFIED variant; it is not an end-to-end Resource Advisor run.

    python examples/vision_training_qualification.py --image IMAGE --model mobilenet_v3_small \
        --dataset-digest sha256:... --steps 50 --global-batch 16 --name NAME > job.json

`--ddp-node NODE=DEVICE` (repeated) renders a two-or-more-rank torchrun DDP qualification
instead: one Indexed Job (a Pod per node, one GPU each, hostname anti-affinity) plus its
headless rendezvous Service. Every rank checks its own device against its node entry,
starts from the pinned initial state and reports its own node/Pod UID/replica digests.
"""

import argparse
import json

WRAPPER = r"""
import json, os, platform, sys, time
sys.path.insert(0, "/opt/ra-train")
start = time.time()
import torch, torchvision
import vision_training as vt
a = vt.parser().parse_args(sys.argv[1:])
manifest = json.load(open(os.path.join(a.data, "manifest.json")))
a.initial_checkpoint_digest = vt.checkpoint_digest(vt.build_model(a.model, len(manifest["classes"]), a.seed))
context = {
    "arch": {"aarch64": "arm64", "x86_64": "amd64"}[platform.machine()],
    "accelerator_model": torch.cuda.get_device_name(0),
    "allocation_mode": "virtual_slot",
    "parameters": {},
    "runtime_versions": {"pytorch": torch.__version__, "torchvision": torchvision.__version__},
    "resources": {"accelerator_count": 1},
}
env = dict(os.environ)
env.update({
    "RA_JOB_ID": os.environ["QUAL_ID"], "RA_ATTEMPT_ID": os.environ["QUAL_ID"] + "-a1", "RA_EPOCH": "0",
    "RA_WORKLOAD_SIGNATURE": "sha256:" + "0" * 64, "RA_CONTEXT_SIGNATURE": vt.signature(context),
    "RA_CONTEXT_JSON": json.dumps(context), "RA_WORK_UNITS": str(a.steps),
    "RA_INPUT_SHAPE": json.dumps([a.global_batch, 3, 224, 224]), "RA_PRECISION": "fp32", "RA_SEED": str(a.seed),
})
print("RA_QUALIFICATION " + json.dumps({"context": context, "initial_checkpoint_digest": a.initial_checkpoint_digest,
      "import_seconds": time.time() - start, "cuda": torch.version.cuda,
      "nccl": torch.cuda.nccl.version() if torch.distributed.is_nccl_available() else None}), flush=True)
vt.run(a, env)
"""

DDP_WRAPPER = r"""
import json, os, platform, sys, time
sys.path.insert(0, "/opt/ra-train")
start = time.time()
import torch, torchvision
import vision_training as vt
a = vt.parser().parse_args(sys.argv[1:])
classes = len(json.load(open(os.path.join(a.data, "manifest.json")))["classes"])
seeded = vt.checkpoint_digest(vt.build_model(a.model, classes, a.seed, a.initial_state))
if a.initial_checkpoint_digest == "pending":
    a.initial_checkpoint_digest = seeded
d = json.loads(os.environ["RA_DISTRIBUTED_JSON"])
world = int(os.environ["WORLD_SIZE"])
context = {
    "arch": {"aarch64": "arm64", "x86_64": "amd64"}[platform.machine()],
    "accelerator_model": "per-node",
    "allocation_mode": "physical_device",
    "parameters": {},
    "runtime_versions": {"pytorch": torch.__version__, "torchvision": torchvision.__version__},
    "resources": {"accelerator_count": int(os.environ["LOCAL_WORLD_SIZE"])},
    "distributed": d,
}
env = dict(os.environ)
env.update({
    "RA_JOB_ID": os.environ["QUAL_ID"], "RA_ATTEMPT_ID": os.environ["QUAL_ID"] + "-a1", "RA_EPOCH": "0",
    "RA_WORKLOAD_SIGNATURE": "sha256:" + "0" * 64, "RA_CONTEXT_SIGNATURE": vt.signature(context),
    "RA_CONTEXT_JSON": json.dumps(context), "RA_WORK_UNITS": str(a.steps),
    "RA_INPUT_SHAPE": json.dumps([a.global_batch // world, 3, 224, 224]), "RA_PRECISION": "fp32",
    "RA_SEED": str(a.seed),
})
print("RA_QUALIFICATION " + json.dumps({"rank": int(os.environ["RANK"]), "node": os.environ["RA_NODE_NAME"],
      "pod_uid": os.environ["RA_POD_UID"], "device": torch.cuda.get_device_name(int(os.environ["LOCAL_RANK"])),
      "import_seconds": time.time() - start, "cuda": torch.version.cuda,
      "nccl": torch.cuda.nccl.version() if torch.distributed.is_nccl_available() else None}), flush=True)
evidence = vt.run(a, env)
if evidence is not None:
    print("RA_DDP_QUALIFICATION " + json.dumps(evidence), flush=True)
"""
RDZV_PORT = 29400


def ddp_resources(args, nodes, name):
    """Headless rendezvous Service + Indexed Job (one Pod and one GPU per node)."""
    d = {
        "schema_version": "v1",
        "strategy": "ddp",
        "launcher": "torchrun",
        "collective": args.collective,
        "rendezvous": "c10d-index0",
        "nodes": [
            {"node_ref": n, "capability_ref": f"{name}-{i}", "gpus": 1, "accelerator_model": m,
             "validation_ref": name}
            for i, (n, m) in enumerate(sorted(nodes))
        ],
        "nproc_per_node": 1,
        "world_size": len(nodes),
        "memory_semantics": "replicated-per-rank",
    }
    manifest = job(args)
    spec = manifest["spec"]
    pod = spec["template"]["spec"]
    spec.update(completionMode="Indexed", completions=len(nodes), parallelism=len(nodes))
    pod["subdomain"] = name
    pod.pop("nodeSelector")
    pod.pop("tolerations")
    pod["affinity"] = {
        "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In",
                                   "values": [n for n, _ in sorted(nodes)]}]}]}},
        "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
            {"labelSelector": {"matchLabels": {"ra/qualification": name}},
             "topologyKey": "kubernetes.io/hostname"}]},
    }
    main = pod["containers"][0]
    main["command"] = [
        "python3", "-m", "torch.distributed.run", f"--nnodes={len(nodes)}", "--nproc-per-node=1",
        "--rdzv-backend=c10d", f"--rdzv-id={name}", f"--rdzv-endpoint={name}-0.{name}:{RDZV_PORT}",
        "--max-restarts=0", "--no-python", "python3", "-c", DDP_WRAPPER, *main["command"][3:],
    ]
    main["env"] += [
        {"name": "RA_DISTRIBUTED_JSON", "value": json.dumps(d, sort_keys=True)},
        {"name": "NCCL_DEBUG", "value": "INFO"},
        {"name": "NCCL_DEBUG_SUBSYS", "value": "INIT,NET"},
    ]
    main["ports"] = [{"name": "rdzv", "containerPort": RDZV_PORT}]
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": args.namespace,
                     "labels": {"app.kubernetes.io/part-of": "ra-vision-training"}},
        "spec": {"clusterIP": "None", "publishNotReadyAddresses": True,
                 "selector": {"ra/qualification": name}},
    }
    return {"apiVersion": "v1", "kind": "List", "items": [service, manifest]}


def job(args):
    name = args.name
    command = [
        "python3", "-c", WRAPPER,
        "--model", args.model, "--data", "/opt/ra-train/data",
        "--dataset-digest", args.dataset_digest, "--initial-checkpoint-digest", args.initial_checkpoint_digest,
        "--global-batch", str(args.global_batch), "--steps", str(args.steps),
        "--lr", str(args.lr), "--seed", str(args.seed), "--output", "/out",
        *(("--initial-state", args.initial_state) if args.initial_state else ()),
        *(("--strategy", "ddp") if args.ddp_node else ()),
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": args.namespace,
            "labels": {"app.kubernetes.io/part-of": "ra-vision-training", "ra/qualification": name},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": args.deadline,
            "template": {
                "metadata": {"labels": {"ra/qualification": name}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "nodeSelector": {"kubernetes.io/hostname": args.node},
                    # Edge heartbeats lapse under heavy eMMC writes; keep this own Pod bound.
                    "tolerations": [
                        {"key": f"node.kubernetes.io/{k}", "operator": "Exists",
                         "effect": "NoExecute", "tolerationSeconds": args.deadline}
                        for k in ("unreachable", "not-ready")
                    ],
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532},
                    "containers": [
                        {
                            "name": "train",
                            "image": args.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": command,
                            "env": [
                                {"name": "QUAL_ID", "value": name},
                                {"name": "HOME", "value": "/tmp"},
                                {"name": "RA_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                                {"name": "RA_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                            ],
                            "resources": {
                                "requests": {"cpu": "2", "memory": "3Gi", args.gpu_resource: "1"},
                                "limits": {"cpu": "4", "memory": "5Gi", args.gpu_resource: "1"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "out", "mountPath": "/out"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "out", "emptyDir": {"sizeLimit": "1Gi"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                    ],
                },
            },
        },
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--dataset-digest", required=True)
    p.add_argument("--steps", type=int, required=True)
    p.add_argument("--global-batch", type=int, required=True)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--seed", type=int, default=20261029)
    p.add_argument("--name", required=True)
    p.add_argument("--namespace", default="ra-vision-training")
    p.add_argument("--node", default="etri-dev0009-jetorn")
    p.add_argument("--deadline", type=int, default=1800)
    p.add_argument("--gpu-resource", default="nvidia.com/gpu.shared")
    p.add_argument("--runtime-class")
    p.add_argument("--initial-state", help="pinned state file in the image")
    p.add_argument("--initial-checkpoint-digest", default="pending", help="DDP: expected digest")
    p.add_argument("--ddp-node", action="append", default=[], help="NODE=DEVICE NAME")
    p.add_argument("--collective", choices=("nccl", "gloo"), default="nccl")
    args = p.parse_args(argv)
    if not 1 <= args.steps <= 100 or args.deadline > 1800:
        raise SystemExit("bounded pilot: <=100 steps and <=1800 s")
    if args.ddp_node:
        nodes = [tuple(v.split("=", 1)) for v in args.ddp_node]
        if len(nodes) < 2 or len({n for n, _ in nodes}) != len(nodes):
            raise SystemExit("DDP qualification needs two or more distinct nodes")
        manifest = ddp_resources(args, nodes, args.name)
        job_manifest = manifest["items"][1]
    else:
        manifest = job_manifest = job(args)
    if args.runtime_class:
        job_manifest["spec"]["template"]["spec"]["runtimeClassName"] = args.runtime_class
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
