"""Extract the pinned ImageNetV2 10-class / 100-image subset and its manifest.

Reads the existing local matched-frequency archive (no download). Classes and
images are chosen by seed from sorted names; 8 train and 2 holdout per class.
Prints the canonical manifest digest used as `--dataset-digest`.

    python examples/build_vision_training_dataset.py ARCHIVE OUT_DIR
"""

import argparse
import hashlib
import json
import random
import tarfile
from pathlib import Path

from resource_advisor.vision_training import signature

PREFIX = "imagenetv2-matched-frequency-format-val/"
SOURCE = {
    "name": "ImageNetV2 matched-frequency",
    "huggingface": "vaishaal/ImageNetV2",
    "revision": "d626240be2538720e83103a0e1178d24aca8b12c",
}


def select(names, seed, classes, per_class, train_per_class):
    by_class = {}
    for name in names:
        parts = name[len(PREFIX) :].split("/")
        if name.startswith(PREFIX) and len(parts) == 2 and parts[1]:
            by_class.setdefault(int(parts[0]), []).append(name)
    rng = random.Random(seed)
    chosen = sorted(rng.sample(sorted(by_class), classes))
    picks = []
    for label, index in enumerate(chosen):
        files = sorted(by_class[index])
        rng.shuffle(files)
        for position, name in enumerate(sorted(files[:per_class])):
            picks.append((name, index, label, "train" if position < train_per_class else "holdout"))
    return chosen, picks


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("archive")
    p.add_argument("out")
    p.add_argument("--seed", type=int, default=20261029)
    p.add_argument("--classes", type=int, default=10)
    p.add_argument("--per-class", type=int, default=10)
    p.add_argument("--train-per-class", type=int, default=8)
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    h = hashlib.sha256()
    with open(args.archive, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            h.update(block)
    with tarfile.open(args.archive) as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        chosen, picks = select(members, args.seed, args.classes, args.per_class, args.train_per_class)
        items = []
        for name, index, label, split in picks:
            data = tar.extractfile(members[name]).read()
            rel = f"{index:04d}/{Path(name).name}"
            (out / rel).parent.mkdir(exist_ok=True)
            (out / rel).write_bytes(data)
            items.append(
                {
                    "path": rel,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "imagenet_class": index,
                    "label": label,
                    "split": split,
                }
            )
    manifest = {
        "schema": "ra-vision-dataset-v1",
        "source": {**SOURCE, "archive_sha256": h.hexdigest()},
        "selection": {
            "seed": args.seed,
            "classes": args.classes,
            "per_class": args.per_class,
            "train_per_class": args.train_per_class,
        },
        "classes": chosen,
        "items": items,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1))
    print(json.dumps({"digest": signature(manifest), "items": len(items), "classes": chosen,
                      "bytes": sum(i["bytes"] for i in items)}))


if __name__ == "__main__":
    main()
