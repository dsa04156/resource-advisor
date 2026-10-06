# Slurm attempt ownership and current execution gates

The adapter now validates the attempt name, configured account and partition
before using a scheduler record. Previously, live status matched only the numeric
job ID, accounting status did not check ownership, and cancellation sent an
unfiltered ID. A mismatched or reused ID could therefore affect the wrong local
attempt or produce a cancellation outside its intended scope.

## Implemented boundary

`squeue` remains an account/partition-scoped listing so a completed ID purged from
the live cache can still reach `sacct`. Both commands return the ownership fields.
For a requested ID, missing identity fields, foreign owners and duplicate records
are errors. Accounting requests include `--duplicates`: an ambiguous reused ID
is rejected instead of silently choosing a newer record. A valid terminal record
still supplies the actual allocation and UTC submission/start/end boundaries.

Submission-response reconciliation also matches partition, account and attempt.
Result collection requires that same identity, the qualified node, completion and
exit status before reading a node-local log. These checks do not qualify a new
runtime or make an unavailable controller healthy.

Cancellation only accepts one positive numeric allocation ID. It sends
`scancel --ctld --account <account> --partition <partition> --name <attempt> <id>`.
The controller receives all filters together; the worker's earlier status read
is not the sole protection. An acknowledgement remains distinct from terminal
cancellation. Slurm's own user/account authorization must still be configured.
The option behavior is documented in the installed-version
[Slurm 24.11.5 scancel manual](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/doc/man/man1/scancel.1)
and [sacct manual](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/doc/man/man1/sacct.1).

## Verification

`tests/test_slurm_ownership.py` uses explicitly labeled scheduler-output doubles.
It covers foreign attempt/account/partition, duplicate and truncated records,
valid live/terminal records, accounting timing/allocation, transport errors,
filtered cancellation and invalid ID rejection. A durable worker test verifies
that a foreign or unavailable observation leaves cancellation pending, sends no
cancel command and creates no terminal usage row. Existing backend tests cover
node-local result ownership and response-loss reconciliation.

```sh
uv run pytest -q tests/test_slurm_ownership.py tests/test_backends.py
```

These are software regression tests, not a live multi-user Slurm acceptance test.
No fake scheduler output is published as a physical accelerator result.

## Current recovery — 2026-10-06

Controller connectivity and both worker registrations have recovered; the two
allocation-free nodes now report `IDLE`. Fresh MUNGE authentication passed in both
directions. See [the recovery evidence and remaining gates](slurm-recovery.md).
The Pi accelerator is still absent, and full model/API execution is still pending.

## Previous lab observation — 2026-10-03 KST

A read-only recheck found the controller unreachable from both the platform host
and the Slurm Raspberry Pi. Both compute nodes accepted SSH and reported active
`slurmd`; daemon activity alone does not prove scheduler connectivity.

The Orin Nano reports an NVIDIA GPU driver, Ubuntu 24.04.4 and L4T 39.2.1.
The checked system Python has no PyTorch, TensorRT, ONNX Runtime or PyCUDA module.
Docker and NVIDIA container tooling exist, but no container/model combination
was qualified by these reads. Native or isolated container qualification is
still required, preserving the existing driver environment.

The Pi retains PCIe Gen2 configuration. `lspci -nn` shows the onboard bridge/RP1
only; no DEEPX device or `/dev/dx*`/`/dev/hailo*` device file was observed. This is
a missing-device observation, not a diagnosis of which cable, power or board
component is responsible. CPU SSH access is not NPU inference evidence.

The installed Slurm client reports 24.11.5. Read-only `scancel --help` and
`squeue --help` expose the relevant filters. On the Pi, even `sacct --help` exits
with `Slurm accounting storage is disabled`, reflecting that client's local
configuration; this does not establish the unreachable controller's current
accounting state. No accounting configuration was changed during this check.

Remaining live gates: controller recovery, scoped worker credentials and client
configuration, qualified GPU/NPU model environments, full API submission/result
transport, cancellation/recovery, and cross-user authorization. Historical F0
and QOS trials remain dated evidence in [slurm-verification.md](slurm-verification.md)
and [slurm-policy.md](slurm-policy.md); they are not current readiness claims.
