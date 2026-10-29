import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "deploy/slurm-accounting-virtual-140/enable-accounting.sh"

# Stub Docker: the controller answers only after an explicit start (crash-loop otherwise).
DOCKER = r"""#!/bin/bash
echo "$*" >>"$STUB/calls"
case "$*" in
  *"show cluster"*) echo sim ;;
  *"show account"*) echo ra-lab ;;
  *"show assoc"*) echo slurm ;;
  "exec slurmctld cat /var/spool/slurmctld/clustername") printf 'sim|623' ;;
  "exec slurmctld scontrol ping") [[ -e $STUB/started ]] ;;
  "start slurmctld") touch "$STUB/started" ;;
  cp\ *) cp "$2" "$STUB/restored-clustername" ;;
esac
"""


def test_failed_switch_restores_identity_without_controller_rpc(tmp_path):
    stub, cluster = tmp_path / "stub", tmp_path / "cluster"
    (cluster / "shared").mkdir(parents=True)
    stub.mkdir()
    conf = cluster / "shared/slurm.conf"
    conf.write_text("ClusterName=sim\n")
    for name, body in [("docker", DOCKER), ("sleep", "#!/bin/bash\n")]:
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", STUB=str(stub))
    env["CLUSTER_DIR"] = str(cluster)
    run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert run.returncode == 1 and "restored slurm.conf and clustername" in run.stderr
    assert conf.read_text() == "ClusterName=sim\n"
    assert (stub / "restored-clustername").read_text() == "sim|623"
    calls = (stub / "calls").read_text().splitlines()
    tail = calls[calls.index("restart slurmctld") :]
    assert [c for c in tail if not c.endswith("scontrol ping")] == [
        "restart slurmctld",
        "stop slurmctld",
        next(c for c in tail if c.startswith("cp ")),
        "start slurmctld",
        "exec slurmctld chown slurm:slurm /var/spool/slurmctld/clustername",
    ]
    assert next(c for c in tail if c.startswith("cp ")).endswith(
        "slurmctld:/var/spool/slurmctld/clustername"
    )
