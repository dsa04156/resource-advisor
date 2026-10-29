"""Assemble the arm64 vision-training image as one extra layer over a pinned base.

No build daemon and no download: wheels from an existing offline wheelhouse are
unpacked into /opt/ra-train/site, the pinned dataset subset into
/opt/ra-train/data, and vision_training.py into /opt/ra-train. The layer is
deterministic (sorted, mtime 0, root-owned) and pushed with the registry v2 API.

    python examples/build_vision_training_image.py --registry http://HOST:5000 \
        --base node-inventory-python@sha256:... --wheelhouse DIR --dataset DIR \
        --repository resource-advisor/vision-training --tag v1 --work DIR
"""

import argparse
import gzip
import hashlib
import http.client
import io
import json
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
WHEELS = (
    "torch-2.9.1+cu130-",
    "torchvision-0.24.1-",
    "numpy-1.26.4-",
    "pillow-11.3.0-",
    "filelock-",
    "typing_extensions-",
    "sympy-",
    "mpmath-",
    "networkx-",
    "jinja2-",
    "markupsafe-",
    "fsspec-",
    "setuptools-",
    "nvidia_",
)
ROOT = "opt/ra-train"


def get(registry, path, accept=ACCEPT):
    request = urllib.request.Request(registry + path, headers={"Accept": accept})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read(), response.headers


def add(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime, info.uid, info.gid = len(data), mode, 0, 0, 0
    tar.addfile(info, io.BytesIO(data))


def add_dir(tar, name, seen):
    parts = name.split("/")
    for i in range(1, len(parts) + 1):
        path = "/".join(parts[:i])
        if path not in seen:
            seen.add(path)
            info = tarfile.TarInfo(path)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            tar.addfile(info)


def build_layer(wheelhouse, dataset, module, out, prefixes=WHEELS, package=(), site_tar=None,
                root_files=()):
    """Write layer.tar (uncompressed) and layer.tar.gz; return (diff_id, digest, size, wheels)."""
    wheels = sorted(
        p for p in Path(wheelhouse).glob("*.whl") if p.name.startswith(tuple(prefixes))
    ) if wheelhouse else []
    raw = Path(out) / "layer.tar"
    seen = set()
    with tarfile.open(raw, "w", format=tarfile.PAX_FORMAT) as tar:
        for wheel in wheels:
            with zipfile.ZipFile(wheel) as archive:
                for entry in sorted(archive.infolist(), key=lambda e: e.filename):
                    if entry.is_dir() or entry.filename.split("/", 1)[0].endswith(".data"):
                        continue
                    name = f"{ROOT}/site/{entry.filename}"
                    add_dir(tar, name.rsplit("/", 1)[0], seen)
                    mode = 0o755 if (entry.external_attr >> 16) & 0o111 else 0o644
                    add(tar, name, archive.read(entry), mode)
        if site_tar:  # An already-installed site tree (members named site/...).
            with tarfile.open(site_tar) as src:
                for member in src:
                    if not member.name.startswith("site/") or not (member.isfile() or member.issym()):
                        continue
                    name = f"{ROOT}/{member.name}"
                    add_dir(tar, name.rsplit("/", 1)[0], seen)
                    if member.issym():
                        info = tarfile.TarInfo(name)
                        info.type, info.linkname, info.mtime = tarfile.SYMTYPE, member.linkname, 0
                        tar.addfile(info)
                    else:
                        add(tar, name, src.extractfile(member).read(), 0o755 if member.mode & 0o111 else 0o644)
        for path in sorted(Path(dataset).rglob("*")) if dataset else ():
            if path.is_file():
                name = f"{ROOT}/data/{path.relative_to(dataset).as_posix()}"
                add_dir(tar, name.rsplit("/", 1)[0], seen)
                add(tar, name, path.read_bytes())
        add_dir(tar, ROOT, seen)
        add(tar, f"{ROOT}/vision_training.py", Path(module).read_bytes())
        for path in root_files:  # Script-sibling modules (e.g. the teacher client).
            add(tar, f"{ROOT}/{Path(path).name}", Path(path).read_bytes())
        # Minimal package for the RA training-isolation init container.
        for path in package:
            name = f"{ROOT}/ra/resource_advisor/{Path(path).name}"
            add_dir(tar, name.rsplit("/", 1)[0], seen)
            add(tar, name, Path(path).read_bytes())
    diff = hashlib.sha256()
    compressed = Path(out) / "layer.tar.gz"
    with raw.open("rb") as src, compressed.open("wb") as dst:
        with gzip.GzipFile(fileobj=dst, mode="wb", compresslevel=1, mtime=0) as gz:
            for block in iter(lambda: src.read(1 << 22), b""):
                diff.update(block)
                gz.write(block)
    digest = hashlib.sha256()
    with compressed.open("rb") as file:
        for block in iter(lambda: file.read(1 << 22), b""):
            digest.update(block)
    raw.unlink()
    return (
        "sha256:" + diff.hexdigest(),
        "sha256:" + digest.hexdigest(),
        compressed.stat().st_size,
        [w.name for w in wheels],
    )


def push_blob(registry, repository, digest, path=None, data=None):
    url = urllib.parse.urlparse(registry)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=600)
    conn.request("HEAD", f"/v2/{repository}/blobs/{digest}")
    response = conn.getresponse()
    response.read()
    if response.status == 200:
        return "exists"
    conn.request("POST", f"/v2/{repository}/blobs/uploads/")
    response = conn.getresponse()
    response.read()
    location = urllib.parse.urlparse(response.getheader("Location"))
    target = location.path + "?" + (location.query + "&" if location.query else "") + "digest=" + digest
    size = path.stat().st_size if path else len(data)
    conn.putrequest("PUT", target)
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    if path:
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1 << 22), b""):
                conn.send(block)
    else:
        conn.send(data)
    response = conn.getresponse()
    body = response.read()
    if response.status != 201:
        raise RuntimeError(f"blob upload failed {response.status}: {body[:200]!r}")
    return "uploaded"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--registry", required=True)
    p.add_argument("--base", required=True, help="repository@sha256 of a multi-arch index")
    p.add_argument("--wheelhouse")
    p.add_argument("--dataset")
    p.add_argument("--wheel-prefix", action="append", help="default: the torch runtime set")
    p.add_argument("--package-file", action="append", default=[])
    p.add_argument("--site-tar")
    p.add_argument("--root-file", action="append", default=[])
    p.add_argument("--arch", default="arm64")
    p.add_argument("--module", default=str(Path(__file__).parents[1] / "src/resource_advisor/vision_training.py"))
    p.add_argument("--repository", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--work", required=True)
    args = p.parse_args(argv)
    base_repo, base_digest = args.base.split("@")
    index = json.loads(get(args.registry, f"/v2/{base_repo}/manifests/{base_digest}")[0])
    if "manifests" in index:
        arm = next(m for m in index["manifests"] if m["platform"]["architecture"] == args.arch)
        manifest = json.loads(get(args.registry, f"/v2/{base_repo}/manifests/{arm['digest']}")[0])
    else:  # Already a single arm64 image (overlay on an earlier build).
        arm, manifest = {"digest": base_digest}, index
    config = json.loads(get(args.registry, f"/v2/{base_repo}/blobs/{manifest['config']['digest']}", "*/*")[0])
    Path(args.work).mkdir(parents=True, exist_ok=True)
    diff_id, layer_digest, layer_size, wheels = build_layer(
        args.wheelhouse, args.dataset, args.module, args.work,
        args.wheel_prefix or WHEELS, args.package_file, args.site_tar, args.root_file,
    )
    config["rootfs"]["diff_ids"].append(diff_id)
    env = [e for e in config["config"].get("Env", []) if not e.startswith("PYTHONPATH=")]
    config["config"]["Env"] = env + [f"PYTHONPATH=/{ROOT}/site:/{ROOT}/ra"]
    config["config"]["WorkingDir"] = f"/{ROOT}"
    config.setdefault("history", []).append(
        {"created_by": "resource-advisor build_vision_training_image.py", "comment": ",".join(wheels)}
    )
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    # Cross-repository mount of the base layers keeps the push metadata-only for them.
    url = urllib.parse.urlparse(args.registry)
    for layer in manifest["layers"]:
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=120)
        conn.request(
            "POST", f"/v2/{args.repository}/blobs/uploads/?mount={layer['digest']}&from={base_repo}"
        )
        response = conn.getresponse()
        response.read()
        if response.status != 201:
            raise RuntimeError(f"base layer mount failed {response.status} {layer['digest']}")
    layer_state = push_blob(args.registry, args.repository, layer_digest, path=Path(args.work) / "layer.tar.gz")
    push_blob(args.registry, args.repository, config_digest, data=config_bytes)
    new = {
        "schemaVersion": 2,
        "mediaType": manifest.get("mediaType", "application/vnd.oci.image.manifest.v1+json"),
        "config": {"mediaType": manifest["config"]["mediaType"], "size": len(config_bytes), "digest": config_digest},
        "layers": manifest["layers"]
        + [
            {
                "mediaType": manifest["layers"][-1]["mediaType"].replace("+zstd", "+gzip"),
                "size": layer_size,
                "digest": layer_digest,
            }
        ],
    }
    body = json.dumps(new, separators=(",", ":")).encode()
    request = urllib.request.Request(
        f"{args.registry}/v2/{args.repository}/manifests/{args.tag}",
        data=body,
        method="PUT",
        headers={"Content-Type": new["mediaType"]},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        pushed = response.headers["Docker-Content-Digest"]
    if pushed != "sha256:" + hashlib.sha256(body).hexdigest():
        raise RuntimeError("registry manifest digest differs from local digest")
    print(json.dumps({
        "image": f"{urllib.parse.urlparse(args.registry).netloc}/{args.repository}@{pushed}",
        "base_arm64_manifest": arm["digest"],
        "layer_digest": layer_digest,
        "layer_diff_id": diff_id,
        "layer_bytes": layer_size,
        "layer_upload": layer_state,
        "wheels": wheels,
    }, indent=1))


if __name__ == "__main__":
    main()
