# Actual server interruption: retained incomplete trial

This is **INCOMPLETE**, not a pass of the
[prospective server-outage protocol](mlflow-server-outage-plan.md).
[Sanitized evidence](evidence/mlflow-server-outage-v1.json) retains the failures,
one accepted compute slot and its actual cost. No replacement compute was run.

The existing lab MLflow3.16.1 Deployment was scaled1→0→1 in two administrative
episodes. Prerequisites independently found zero RUNNING MLflow runs, no pending
tracking deliveries and no active Kubernetes compute. The unrelated original
Slurm cancellation remained unresolved and was not resubmitted.

1. Before submission, an empty EndpointSlice encoded `endpoints: null`; the
   verifier incorrectly iterated it. Its finally restoration completed. SQL
   verified zero Jobs for the frozen key. The report was retained, the null
   predicate fixed, and continuation of the one UNUSED compute slot was frozen
   before the second administrative episode.
2. One API Job was accepted after zero serving Pods/endpoints were observed.
   The verifier then attempted to serialize a SQL RowMapping and stopped;
   service restoration still occurred before that reporting error propagated.
   The original compute was retained and followed by read-only reconciliation.
3. That native GPU allocation failed with `ModuleNotFoundError` before the
   runner imported: its new variant ID had no configured runtime/source mounts.
   This is an environment-binding failure, not a model quality measurement or
   proof of useful GPU execution during the outage. The
   [explicit runtime-bundle contract](kubernetes-runtime-bundles.md) addresses it.

The original attempt is FAILED with exactly one ledger and1GPU reservation
second. Exactly one FAILED MLflow run exists after recovery; its original outbox
completed in two delivery attempts. No result, profile, model/performance metrics
or result artifact was invented. The transient first delivery error was cleared
on successful delivery, so its precise type is not claimed from final state.

Independent reads verified the original MLflow Deployment UID/specification,
Service and PVC were restored with1/1 Ready, all prior MLflow run bodies remained
unchanged, and preexisting immutable entities, ledgers and terminal Jobs matched.
The first report, original accepted Job, raw logs, failed allocation and later
read-only reconciliation are preserved in private state.

The verifier now normalizes SQL mappings before saving. It still stops on
unexpected compute outcomes, restores service on failure and refuses an existing
output directory. This trial consumes its one compute slot; a later successful
server-outage test requires a separately frozen complete environment and must
include this failure's cost. No broad E7/platform completion is inferred.
