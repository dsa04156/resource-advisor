# Central lab telemetry acceptance plan

Starting state: two authorized ARM workers run the pinned dedicated exporter;
existing Kubernetes monitoring and application services are live.

1. Preserve Slurm/driver file hashes, boot IDs and slurmd identities, existing
   Prometheus version/configuration sources and service Pods.
2. Keep loopback as the default Ansible transport. Explicit mTLS mode may bind
   only a configured local host IPv4 address on port 19100. Require a private
   server key, scoped CA, server identity and verified client certificate.
3. Keep server keys root-only; pass them to the dynamic user through systemd
   credentials. Client keys stay only on the operator and in the monitoring
   Secret. Publish no keys, site addresses or credentials.
4. Require verified real scrapes and all eight collectors on both hosts. Missing
   or foreign client certificates, plaintext and wrong server identity must fail.
5. Deploy the new ScrapeConfig through a separate pinned, manual-sync Argo CD
   Application/AppProject permitting only ScrapeConfig in the lab namespace.
   It must not manage Secrets, Jobs, Pods or existing applications. No pruning.
6. Require actual Prometheus targets up, advancing source timestamps and valid
   CPU rate/total-memory/available-memory samples. Exporter reachability confers
   neither scheduler readiness nor GPU/NPU qualification.
7. Stop only one dedicated exporter; require up=0 and health-qualified values
   becoming unknown while the other host stays fresh. Restore and verify
   recovery. Compare protected identities; repeat Ansible with zero changes.
8. Publish evidence and reproduction/rotation instructions. Backend inventory
   and console consumption remain a separate required integration gate until
   their own end-to-end evidence exists.
