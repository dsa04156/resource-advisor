"""Live recovery fixture: hold a CUDA context before the measured matmul.

The fixed 30-second preparation wait allows an operator to interrupt the separate
control worker. It is not benchmark time or evidence of useful GPU utilization.
The compute Job itself must not be killed by the worker-recovery test.
"""

import time


def run():
    import torch

    from resource_advisor.gpu_benchmark import run as benchmark

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("recovery fixture requires one allocated CUDA GPU")
    torch.cuda.init()
    print("RA_RECOVERY_PREPARATION_WAIT_SECONDS 30", flush=True)
    time.sleep(30)
    benchmark()


if __name__ == "__main__":
    run()
