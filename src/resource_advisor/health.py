"""Process progress readiness, separate from the availability of metric sources."""

import math
import time


def write_heartbeat(path):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(str(time.time()))
    temporary.replace(path)


def heartbeat_fresh(path, max_age=120, *, timestamp=None):
    if not math.isfinite(max_age) or max_age <= 0:
        return False
    try:
        age = (time.time() if timestamp is None else timestamp) - float(path.read_text())
        return math.isfinite(age) and 0 <= age <= max_age
    except (OSError, ValueError):
        return False
