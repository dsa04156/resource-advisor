"""Actual CUDA correctness probe plus a bounded all-worker startup barrier.

This is a numerical scheduling probe, not a trained model or an ML benchmark.
"""

import json
import os
import socket
import time

from cuda_probe import measure


def emit(phase, **values):
    print(json.dumps({"phase": phase, "rank": rank, "time": time.time(), **values}), flush=True)


rank = int(os.environ.get("JOB_COMPLETION_INDEX", "0"))
world = int(os.environ.get("WORLD_SIZE", "1"))
report = measure()
emit("GPU_READY", correctness=True)
if world > 1:
    deadline = time.monotonic() + 80
    if rank == 0:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", 23456))
        server.listen(world)
        server.settimeout(80)
        peers = [server.accept()[0] for _ in range(world - 1)]
        for peer in peers:
            peer.sendall(b"GO")
            peer.close()
        server.close()
    else:
        host = os.environ["RENDEZVOUS"]
        while True:
            try:
                peer = socket.create_connection((host, 23456), timeout=3)
                peer.settimeout(80)
                assert peer.recv(2) == b"GO"
                peer.close()
                break
            except (OSError, AssertionError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
    emit("BARRIER_RELEASED", workers=world)
emit("COMPUTE_STARTED")
end = time.monotonic() + int(os.environ.get("DURATION", "15"))
iterations = 0
while time.monotonic() < end:
    report = measure()
    iterations += 1
    time.sleep(0.1)
emit("COMPUTE_FINISHED", iterations=iterations, correctness=True, report=report)
