# PostgreSQL connection recovery

Inventory and worker CLI loops recover from a PostgreSQL disconnect at the
boundary of a complete cycle. An interrupted cycle rolls back/unwinds first,
discards the connection pool, removes its readiness heartbeat and logs only
`{"status": "database_unavailable"}`. The next ordinary polling cycle reads
durable state again. Inventory waits its configured interval; workers wait five
seconds. Recovery never prints the exception, database URL or SQL parameters.

Connection validation (`pool_pre_ping`) replaces stale pooled connections before
use. PostgreSQL connections also set libpq `connect_timeout=5`, TCP keepalives
enabled with idle/interval/count of 5 seconds/2 seconds/3, and
`tcp_user_timeout=15000` milliseconds. Pool checkout waits at most five seconds.
These are transport settings, not an end-to-end recovery SLO: DNS/multiple host
addresses and kernel/platform support affect elapsed time. Unix-domain sockets
ignore TCP settings. An application-level peer that acknowledges TCP but stops
responding is not covered by these network failure limits.

The cycle handles invalidated connections, PostgreSQL connection SQLSTATE class
08, shutdown/recovery codes 57P01–57P03, and OperationalError without a SQLSTATE
(including libpq connection failures). Other database errors and SQLite errors
still propagate. `--once` also propagates failures; it never claims a failed
one-shot collection or worker cycle succeeded. Readiness is restored only after
the entire subsequent cycle finishes.

There is no transparent SQL/commit retry. In particular, after a scheduler has
accepted work but its acknowledgement cannot be persisted, the committed
SUBMITTING intent and outbox lease remain. Once that lease expires, the worker
reconciles the same native attempt instead of submitting another job. Recovery
may therefore take longer than the next polling interval. Result and accounting
collection keep their existing immutable identities and transition rules.

## Regression checks

`tests/test_database_recovery.py` runs only with a dedicated `ra_test_*`
PostgreSQL database, using the existing destructive test-fixture guard:

```sh
rtk proxy uv run pytest -q tests/test_database_recovery.py tests/test_health.py
```

Set `RA_TEST_DATABASE_URL` privately before invoking the command. The checks
terminate actual PostgreSQL sessions, exercise the real worker/inventory CLI
loops, enforce failure semantics for `--once`, and use a TCP endpoint that
accepts but never completes PostgreSQL connection setup. Scheduler acceptance is
an explicit test double: it verifies same-attempt reconciliation, successful
result ingestion and idempotent request replay without another submission.
This is not a new native GPU/Slurm crash experiment.

The October 12 readiness incident involved a worker connection to an old
headless-Service Pod address while fresh database connections succeeded. These
checks cover stale pooled connections and mid-operation disconnect recovery;
they do not reproduce that entire node/DNS/network incident. Actual packet
blackholing, indefinite server-side SQL, arbitrary blocked external dependencies
and a process watchdog remain separate qualification work. No dependency-driven
liveness restart loop is added.

References: [SQLAlchemy disconnect handling](https://docs.sqlalchemy.org/en/20/core/pooling.html#dealing-with-disconnects),
[libpq transport parameters](https://www.postgresql.org/docs/17/libpq-connect.html#LIBPQ-PARAMKEYWORDS).
