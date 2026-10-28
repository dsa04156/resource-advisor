#!/bin/bash
# Submit a bounded CPU numpy job as user ra and wait for its accounting record.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

ssh -F "$SSH_CONFIG" "$SLURM_VM_NAME" 'bash -s' <<'GUEST'
set -euo pipefail
cd /srv/ra/work
cat > smoke.py <<'PY'
import numpy as np, platform, time
rng = np.random.default_rng(0)
x = rng.normal(size=(4096, 16)); w_true = rng.normal(size=16); y = x @ w_true
w = np.zeros(16); start = time.time()
for step in range(300):
    w -= 0.05 * x.T @ (x @ w - y) / len(x)
print(f"numpy={np.__version__} python={platform.python_version()} steps=300 mse={np.mean((x @ w - y) ** 2):.3e} seconds={time.time() - start:.2f}")
PY
id=$(sbatch --parsable --job-name=ra-vm-smoke --account=ra-project --qos=ra-normal --partition=compute \
  --cpus-per-task=1 --mem=256M --time=00:05:00 --output=/srv/ra/work/smoke-%j.out \
  --wrap='srun /usr/bin/python3 -I /srv/ra/work/smoke.py')
echo "submitted job $id"
for _ in $(seq 120); do squeue -h -j "$id" | grep -q . || break; sleep 2; done
for _ in $(seq 30); do
  state=$(sacct -n -X -P -j "$id" -o State); [[ $state == COMPLETED || $state == FAILED* || $state == CANCELLED* || $state == TIMEOUT ]] && break; sleep 2
done
sacct -j "$id" -P -o JobID,JobName,Account,QOS,Partition,NodeList,AllocCPUS,ReqMem,State,ExitCode,Elapsed,Start,End
cat "/srv/ra/work/smoke-$id.out"
[[ $state == COMPLETED ]]
GUEST
