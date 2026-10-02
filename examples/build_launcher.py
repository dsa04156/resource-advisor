"""Build a pinned amd64 launcher without a privileged Docker socket.

Requires uv and crane on PATH. Registry authentication uses crane's standard
credential configuration; site URLs and credentials never enter image layers.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path


def build(base, destination, workdir, crane, *, extras=(), kubectl=None, kubectl_sha256=None):
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", base):
        raise ValueError("base image must be pinned by SHA-256")
    repo = Path(__file__).resolve().parents[1]
    if workdir.is_relative_to(repo):
        raise ValueError("build directory must be outside the source repository")
    if set(extras) - {"artifacts", "optimizer"}:
        raise ValueError("only service runtime extras may be included")
    if bool(kubectl) != bool(kubectl_sha256):
        raise ValueError("kubectl path and independently verified SHA-256 are both required")
    if kubectl and hashlib.sha256(kubectl.read_bytes()).hexdigest() != kubectl_sha256:
        raise ValueError("kubectl checksum mismatch")
    workdir.mkdir(parents=True, exist_ok=False)
    requirements = workdir / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--project",
            str(repo),
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
            *[value for extra in extras for value in ("--extra", extra)],
            *(["--emit-index-url"] if "optimizer" in extras else []),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    target = workdir / "root" / "opt" / "resource-advisor"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--require-hashes",
            # Export loses per-package index scoping. Exact lock hashes still
            # constrain every artifact when the CPU torch index is required.
            *(["--index-strategy", "unsafe-best-match"] if "optimizer" in extras else []),
            "--only-binary=:all:",
            "--python-version",
            "3.11",
            "--python-platform",
            "x86_64-manylinux_2_28",
            "--link-mode",
            "copy",
            "--target",
            str(target),
            "-r",
            str(requirements),
        ],
        check=True,
    )
    shutil.copytree(
        repo / "src/resource_advisor",
        target / "resource_advisor",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if kubectl:
        binary = workdir / "root" / "usr" / "local" / "bin" / "kubectl"
        binary.parent.mkdir(parents=True)
        shutil.copyfile(kubectl, binary)
        binary.chmod(0o755)
    layer = workdir / "launcher-layer.tar"

    def normalize(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    with tarfile.open(layer, "w", format=tarfile.PAX_FORMAT) as archive:
        archive.add(workdir / "root" / "opt", arcname="opt", filter=normalize)
        if kubectl:
            archive.add(workdir / "root" / "usr", arcname="usr", filter=normalize)
    # Build once with the desired environment/user, then push its manifest.
    output = subprocess.check_output(
        [
            crane,
            "mutate",
            base,
            "--platform",
            "linux/amd64",
            "--append",
            str(layer),
            "--env",
            "PYTHONPATH=/opt/resource-advisor",
            "--user",
            "10001:10001",
            "--tag",
            destination,
        ],
        text=True,
    ).strip()
    report = {
        "image": output,
        "base": base,
        "platform": "linux/amd64",
        "python": "3.11",
        "lock_sha256": hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
        "layer_sha256": hashlib.sha256(layer.read_bytes()).hexdigest(),
        "extras": sorted(extras),
        "kubectl_sha256": kubectl_sha256,
    }
    (workdir / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="qualified Python 3.11 amd64 image@sha256")
    parser.add_argument("--destination", required=True, help="authorized registry/repository:tag")
    parser.add_argument(
        "--workdir", type=Path, required=True, help="new directory outside source tree"
    )
    parser.add_argument("--crane", default="crane")
    parser.add_argument("--extra", action="append", choices=["artifacts", "optimizer"], default=[])
    parser.add_argument("--kubectl", type=Path)
    parser.add_argument("--kubectl-sha256")
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.base,
                args.destination,
                args.workdir.resolve(),
                args.crane,
                extras=args.extra,
                kubectl=args.kubectl,
                kubectl_sha256=args.kubectl_sha256,
            )
        )
    )


if __name__ == "__main__":
    main()
