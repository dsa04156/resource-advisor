"""Extract hash-pinned NVIDIA userspace archives into a fresh isolated directory.

No drivers, package installation, global loader configuration or GPU operations.
The resulting directory is an unqualified candidate until actual runtime tests.
"""

import argparse
import hashlib
import json
import platform
import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

COMPONENTS = {
    "cuda_cudart",
    "cuda_cupti",
    "cuda_nvrtc",
    "cuda_nvtx",
    "cuda_cuobjdump",
    "libcublas",
    "libcufft",
    "libcurand",
    "libcusolver",
    "libcusparse",
    "libnvjitlink",
    "cudnn",
    "libcusparse_lt",
}


def validate_manifest(manifest):
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= len(COMPONENTS):
        raise ValueError("bounded userspace component list required")
    seen = set()
    for item in manifest:
        url = urllib.parse.urlsplit(item["url"])
        if (
            item["component"] not in COMPONENTS
            or item["component"] in seen
            or url.scheme != "https"
            or url.netloc != "developer.download.nvidia.com"
            or not re.fullmatch(
                r"/compute/(cuda|cudnn|cusparselt)/redist/"
                r"[A-Za-z0-9_/-]+/linux-aarch64/[A-Za-z0-9_.-]+\.tar\.xz",
                url.path,
            )
            or url.query
            or url.fragment
            or url.path.split("/")[4] != item["component"]
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or type(item["size"]) is not int
            or not 0 < item["size"] <= 4 * 1024**3
        ):
            raise ValueError("only unique pinned NVIDIA ARM64 userspace archives allowed")
        seen.add(item["component"])


def prepare(manifest, output):
    validate_manifest(manifest)
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise RuntimeError("this candidate is fixed to Linux aarch64")
    output.mkdir(parents=True, exist_ok=False)
    (output / "archives").mkdir()
    (output / "root").mkdir()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    completed = []
    try:
        for item in manifest:
            archive = output / "archives" / Path(urllib.parse.urlsplit(item["url"]).path).name
            digest, size = hashlib.sha256(), 0
            with (
                urllib.request.urlopen(item["url"], timeout=90) as response,
                archive.open("xb") as f,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > item["size"]:
                        raise ValueError("archive exceeds the published size")
                    f.write(chunk)
                    digest.update(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("archive differs from the published size/hash")
            with tarfile.open(archive) as bundle:
                bundle.extractall(output / "root", filter="data")
            completed.append(item["component"])
            (output / "progress.json").write_text(json.dumps({"completed": completed}))
        result = {"exit": 0, "completed": completed, "qualification": "pending"}
    except Exception as error:
        result = {"exit": 1, "completed": completed, "error": str(error)}
        (output / "status.json").write_text(json.dumps(result))
        raise
    (output / "status.json").write_text(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(json.loads(args.manifest.read_text()), args.output.resolve())))
