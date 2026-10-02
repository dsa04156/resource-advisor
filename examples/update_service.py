"""Append only application source to a qualified service image with the same lock.

The runtime report must come from build_launcher.py (or this tool). This does not
install dependencies or qualify an arbitrary image. Registry auth stays external.
"""

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


def update(runtime_report, destination, workdir, crane="crane"):
    repo = Path(__file__).resolve().parents[1]
    runtime = json.loads(runtime_report.read_text())
    base = runtime["image"]
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", base):
        raise ValueError("runtime image must be pinned by SHA-256")
    lock = hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()
    if runtime["lock_sha256"] != lock:
        raise ValueError("dependency lock changed; rebuild the runtime first")
    if runtime["platform"] != "linux/amd64" or runtime["python"] != "3.11":
        raise ValueError("only the qualified Python 3.11 amd64 runtime is supported")
    if workdir.resolve().is_relative_to(repo):
        raise ValueError("build directory must be outside the repository")
    source = repo / "src/resource_advisor"
    paths = sorted(
        p for p in source.rglob("*") if "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    if any(p.is_symlink() for p in paths):
        raise ValueError("source symlinks are not allowed in service layers")
    workdir.mkdir(parents=True, exist_ok=False)
    layer = workdir / "source-layer.tar"
    prefix = "opt/resource-advisor/resource_advisor"
    manifest = {}
    with tarfile.open(layer, "w", format=tarfile.PAX_FORMAT) as archive:
        # OCI opaque whiteout removes old package children, including deleted
        # source and cached bytecode, while retaining sibling dependencies.
        marker = tarfile.TarInfo(prefix + "/.wh..wh..opq")
        marker.mode = 0o644
        archive.addfile(marker, io.BytesIO(b""))
        for path in paths:
            relative = path.relative_to(source).as_posix()
            info = tarfile.TarInfo(prefix + "/" + relative)
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                info.mode, info.size = 0o644, len(data)
                archive.addfile(info, io.BytesIO(data))
                manifest[relative] = hashlib.sha256(data).hexdigest()
            else:
                raise ValueError("source contains a non-regular file")
    image = subprocess.check_output(
        [
            crane,
            "mutate",
            base,
            "--platform",
            "linux/amd64",
            "--append",
            str(layer),
            "--tag",
            destination,
        ],
        text=True,
    ).strip()
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("registry did not return an immutable image reference")
    report = {
        "image": image,
        "base": base,
        "platform": "linux/amd64",
        "python": "3.11",
        "lock_sha256": lock,
        "extras": runtime["extras"],
        "kubectl_sha256": runtime.get("kubectl_sha256"),
        "layer_sha256": hashlib.sha256(layer.read_bytes()).hexdigest(),
        "layer_bytes": layer.stat().st_size,
        "source_tree_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        "source_files": manifest,
        "mode": "source-only; inherited locked dependencies",
    }
    (workdir / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-report", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--crane", default="crane")
    args = parser.parse_args()
    print(json.dumps(update(args.runtime_report, args.destination, args.workdir, args.crane)))


if __name__ == "__main__":
    main()
