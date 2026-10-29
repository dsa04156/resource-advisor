"""Render a task-owned qualification Job for vision_training on one Orin GPU slot.

Qualification only: the in-Pod wrapper observes the device name and runtime
versions, derives the seeded initial checkpoint digest, then calls the same
`vision_training.run` used by the Resource Advisor variant with an environment
built from those observations. A PASS here is evidence for registering a
TRAINING_VERIFIED variant; it is not an end-to-end Resource Advisor run.

    python examples/vision_training_qualification.py --image IMAGE --model mobilenet_v3_small \
        --dataset-digest sha256:... --steps 50 --global-batch 16 --name NAME > job.json
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


def job(args):
    name = args.name
    command = [
        "python3", "-c", WRAPPER,
        "--model", args.model, "--data", "/opt/ra-train/data",
        "--dataset-digest", args.dataset_digest, "--initial-checkpoint-digest", "pending",
        "--global-batch", str(args.global_batch), "--steps", str(args.steps),
        "--lr", str(args.lr), "--seed", str(args.seed), "--output", "/out",
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
    args = p.parse_args(argv)
    if not 1 <= args.steps <= 100 or args.deadline > 1800:
        raise SystemExit("bounded pilot: <=100 steps and <=1800 s")
    manifest = job(args)
    if args.runtime_class:
        manifest["spec"]["template"]["spec"]["runtimeClassName"] = args.runtime_class
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
