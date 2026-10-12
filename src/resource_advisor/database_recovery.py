"""Recover at durable cycle boundaries; never replay SQL or remote submissions."""

import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.exc import DBAPIError, OperationalError

from .health import write_heartbeat


@contextmanager
def database_cycle(store, heartbeat_path=None, *, once=False):
    path = Path(heartbeat_path) if heartbeat_path else None
    try:
        yield
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None)
        disconnected = exc.connection_invalidated or (
            isinstance(exc, OperationalError)
            and (code is None or code.startswith("08") or code in {"57P01", "57P02", "57P03"})
        )
        if once or store.engine.dialect.name != "postgresql" or not disconnected:
            raise
        # Transactions have unwound before reaching this boundary. The next
        # polling cycle reads durable intent/leases and reconciles uncertain
        # submissions instead of immediately retrying the failed remote action.
        store.engine.dispose()
        if path:
            path.unlink(missing_ok=True)
        # Exception text can contain SQL parameters, endpoints or credentials.
        print(json.dumps({"status": "database_unavailable"}), flush=True)
    else:
        if path:
            write_heartbeat(path)
