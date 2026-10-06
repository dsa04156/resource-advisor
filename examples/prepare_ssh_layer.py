"""Prepare a versioned SSH payload inside a disposable Debian 13 amd64 container.

Uses signed apt indexes and download-only dependency resolution. No host packages,
credentials, SSH server or Docker socket are needed. The resulting OCI layer must
still pass client/runtime acceptance before deployment.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tarfile
from pathlib import Path


def prepare(output, version, base_digest):
    if (
        os.environ.get("RA_SSH_IMAGE_PREP") != "disposable-container"
        or os.geteuid() != 0
        or platform.machine() != "x86_64"
        or "VERSION_CODENAME=trixie" not in Path("/etc/os-release").read_text()
        or not re.fullmatch(r"[0-9][A-Za-z0-9:.+~-]+", version)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", base_digest)
    ):
        raise ValueError("explicit disposable Debian 13 amd64 image preparation required")
    output.mkdir(parents=True, exist_ok=False)
    for directory in ["lists/partial", "debs/partial", "root"]:
        (output / directory).mkdir(parents=True)
    common = [
        "apt-get",
        "-o",
        "APT::Sandbox::User=root",
        "-o",
        "Dir::State::lists=" + str(output / "lists"),
        "-o",
        "Dir::Cache::archives=" + str(output / "debs"),
    ]
    for label, command in [
        ("index", [*common, "update"]),
        (
            "download",
            [
                *common,
                "--download-only",
                "--no-install-recommends",
                "-y",
                "install",
                "openssh-client=" + version,
            ],
        ),
    ]:
        with (output / (label + ".log")).open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
    packages = []
    for archive in sorted((output / "debs").glob("*.deb")):
        metadata = subprocess.check_output(
            [
                "dpkg-deb",
                "--show",
                "--showformat=${Package}|${Version}|${Architecture}",
                str(archive),
            ],
            text=True,
        ).split("|")
        if metadata[2] not in {"amd64", "all"}:
            raise ValueError("unexpected package architecture")
        packages.append(
            {
                "name": metadata[0],
                "version": metadata[1],
                "architecture": metadata[2],
                "archive": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )
        subprocess.run(["dpkg-deb", "--extract", str(archive), str(output / "root")], check=True)
    if not any(p["name"] == "openssh-client" and p["version"] == version for p in packages):
        raise ValueError("the requested client archive was not prepared")
    # The original service image uses a numeric UID with no passwd entry.
    # OpenSSH needs that identity; it grants no interactive account or server.
    for name, entry in [
        ("passwd", "ra-inventory:x:10001:10001:Resource Advisor inventory:/tmp:/usr/sbin/nologin"),
        ("group", "ra-inventory:x:10001:"),
    ]:
        source = Path("/etc", name).read_text()
        if any(
            line.split(":")[2] == "10001" or line.startswith("ra-inventory:")
            for line in source.splitlines()
        ):
            raise ValueError("runtime UID/GID already has a different owner")
        target = output / "root/etc" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.rstrip("\n") + "\n" + entry + "\n")
        target.chmod(0o644)
    manifest = {}
    layer = output / "ssh-layer.tar"
    with tarfile.open(layer, "w", format=tarfile.PAX_FORMAT) as target:
        for path in sorted((output / "root").rglob("*")):
            relative = path.relative_to(output / "root").as_posix()
            if path.is_symlink():
                info = tarfile.TarInfo(relative)
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, os.readlink(path), 0o777
                target.addfile(info)
                manifest[relative] = {"symlink": info.linkname}
            elif path.is_file():
                data = path.read_bytes()
                info = tarfile.TarInfo(relative)
                info.size, info.mode = len(data), path.stat().st_mode & 0o777
                target.addfile(info, io.BytesIO(data))
                manifest[relative] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "mode": oct(info.mode),
                }
            elif not path.is_dir():
                raise ValueError("package contains an unexpected file type")
    report = {
        "schema_version": "v1",
        "base_digest": base_digest,
        "openssh_version": version,
        "platform": "linux/amd64",
        "packages": packages,
        "files": manifest,
        "layer_sha256": hashlib.sha256(layer.read_bytes()).hexdigest(),
        "preparer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "mode": "signed-apt dependency payload; not dpkg-installed; runtime acceptance pending",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-digest", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output.resolve(), args.version, args.base_digest)))
