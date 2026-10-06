# One-shot Orin CUDA device-path diagnostic

After [limits v1 detected actual unreserved GPU execution](slurm-limits-results.md),
submit exactly one independent diagnostic Job using the same pinned driver-only
kernel and positive limit probe. Reserve one typed Orin GPU, one CPU, 512 MiB RAM
and at most two minutes under the existing lab QOS. Maximum additional cost is
120 GPU reservation seconds; record the actual interval from `sacct`.

The worker already has `strace`. Run the published positive probe under
`strace -qq -f -e trace=openat`, writing the trace to an attempt-specific node-local
file. Capture successful/failed opens of `/dev/` paths and the 4,096-element CUDA
result. Retain the private full trace and publish only sanitized device-path
evidence. Do not expose internal hostnames, credentials or unrelated filesystem
paths. Require source hashes to match limits v1 and an idle empty lab queue.

This is a reserved diagnostic of driver device paths, not a benchmark or an
isolation pass. It makes no GRES/driver changes and submits no unreserved GPU
work. If it fails, preserve that failure and inspect the same scheduler ID.
Any subsequent mapping correction requires its own bounded positive/negative
acceptance and must retain all predecessor costs.
