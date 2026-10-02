# Existing Kubeflow storage recovery

The first live workflow was blocked before Resource Advisor ran: the existing
Kubeflow MinIO image could not be pulled, and the pipeline API could not start
without its object store. Recovery was verified on 2026-10-02 with Kubernetes
1.31.14, Kubeflow manifests 1.10.2 and KFP API server 2.5.0. No cluster upgrade
or change to the edge runtime was required.

## Recovery performed

1. Inspect events and node conditions. Disposable user build-cache cleanup
   allowed DiskPressure to clear naturally. No pressure taint or eviction
   threshold was overridden.
2. While MinIO had no running writer, archive its existing PVC files and save
   the Deployment for rollback. Read every regular file from the archive and
   verify its hash against the source. The private backup contains 41 files;
   it is not distributed with this repository.
3. Obtain the **same release** from the [official MinIO release assets](https://github.com/minio/minio/releases/tag/RELEASE.2025-04-22T22-12-26Z).
   Verify the Debian package against the official checksum; extract it without
   installing or changing system packages.
4. Package the static executable, CA bundle and source/license notice into a
   scratch OCI image. Push a separate recovery repository in the lab registry,
   read back its manifest, and pin the image by digest. An isolated Pod first
   verified the actual image pull and executable version (exit code 0).
5. Change only the MinIO container image. Preserve its existing Recreate
   strategy, arguments, Secret, PVC, mount/subPath, Service and node placement.
6. Verify MinIO readiness (HTTP 200), then restart only the pipeline API.
   Both Deployments became available. All four existing object files were
   byte-identical to the backup; all four objects were readable through S3.
   Internal MinIO metadata and temporary files changed during startup, so
   full-PVC byte equality is neither asserted nor expected.

## Reproduce the image input

```sh
release=RELEASE.2025-04-22T22-12-26Z
package=minio_20250422221226.0.0_amd64.deb
curl -fLO "https://github.com/minio/minio/releases/download/$release/$package"
curl -fLO "https://github.com/minio/minio/releases/download/$release/$package.sha256sum"
sha256sum -c "$package.sha256sum"
dpkg-deb -x "$package" package
package/usr/local/bin/minio --version
```

Verified package SHA-256:
`d8898351831761e083e7ba6b74ea7ec478b9a6680794fe92e5e838145341c9c5`.

Verified executable SHA-256:
`53e2a2cb16c5366ea6fbbc479c19ddb4c6a0948273e752f740fb1fbf27bb817c`.

The executable reports commit `0d7408fc9969caf07de6a8c3a84f9fbb10a6739e`.
This is a compatibility recovery, not a version upgrade or a production
storage availability/backup policy. Private registry addresses, PVC paths,
object names, deployment backups and credentials stay outside this repository.
An operator must verify their own backup, image and rollback plan before using
this procedure. The real Resource Advisor pipeline chain is a separate gate.
