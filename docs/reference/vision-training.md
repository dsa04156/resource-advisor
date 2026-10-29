# Vision training runtime (torchvision, real weight updates)

`resource_advisor.vision_training` trains `mobilenet_v3_small` or `resnet18` from a pinned
initial state with AdamW for a fixed number of optimizer steps at a fixed global batch. The
data is a manifest-pinned ImageNetV2 subset: 10 classes, 80 train and 20 holdout images, with
per-file sha256. The images are not synthetic, and no step is forward-only.
`resource_advisor.vision_joint_training` (separate owner) adds Mobilint ARIES teacher logits
for knowledge distillation (KD) on top of the same helpers.

## Identity

- **Initial weights:** seeded init differs between torch 2.8/torchvision 0.23 and torch
  2.9.1/torchvision 0.24. The image therefore carries the state file
  `/opt/ra-train/data/init/<model>-c10-s20261029.pt`. Its tensor digest (`checkpoint_digest`)
  must equal `--initial-checkpoint-digest`.
- **Isolation binding:**
  - `checkpoint.json` is the init descriptor (`init_descriptor`).
  - `input.json` is the dataset manifest.
  - Both are well under the 1 MiB bound.
  - The receipt `output_checkpoint` carries the initial/final tensor digests and the losses,
    under 16 KiB. No weights go into `/tmp`.
- **Before any work** the runtime refuses when any of these differ from the signed job:
  steps vs `RA_WORK_UNITS`, input shape, fp32, seed, context parameters, device name, or
  runtime versions.
- **DDP:** under torchrun with a typed `RA_DISTRIBUTED_JSON`, the runtime validates its own
  node entry before `init_process_group`. Only rank 0 emits the result. DDP has not been
  executed on hardware.

## Build, qualify, register

- `examples/build_vision_training_dataset.py`: builds the subset from the local archive.
- `examples/build_vision_training_image.py`: adds a deterministic layer (wheels/site, data,
  minimal package) over a pinned base through the registry v2 API.
- `examples/vision_training_qualification.py`: renders a direct, task-owned qualification Job
  (qualification only).
- `examples/register_vision_training.py`: builds and posts one workload per setting, with every
  qualified candidate, variant, capability and TrainingIsolation.
- `deploy/vision-training/`: per-node cohort-less Kueue queues, plus the `ra-vision-training-worker`
  that owns only the `lab-vt-*` routes.

Keep multi-GB image pulls off etri-ser0001. It is the shell and cloudcore host, and it sits
near the kubelet disk-eviction threshold.

## Evidence and readiness limits

- Registry entries are immutable, so a variant's `validation_refs` cannot be corrected in
  place. `vt-kd-mnv3s-b16s50-gb10-v1` cites the vanilla GB10 qualification. Its actual KD
  evidence is native job `j-3ebb9237cc7748b9ba2c4828a0d8f1a7`, through its training receipt.
- No field expresses a dependency on the teacher Pod. After the teacher is removed, the
  catalog still reports the KD candidate as submission-compatible. Execution fails closed in
  two places:
  - the worker route has no training source for it;
  - the runner preflights teacher health and the pinned Pod UID.
- JCT in `jct-report` covers native scheduler submit → backend finish (17 s for the joint job).
  RA state SUCCEEDED adds worker collection (≈21.9 s in total). No JctProfile exists yet for
  these workloads.
