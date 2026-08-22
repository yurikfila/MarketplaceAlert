"""Applies pending PostgreSQL schema migrations automatically at
application startup - see `run_pending_migrations()`.

**Why this exists**: the production Render Web Service runs on Render's
Free plan, which has no "Pre-Deploy Command" - the mechanism this
project's Alembic setup originally assumed migrations would run through
(see PROJECT_CONTEXT.md decision #13 / ARCHITECTURE.md "Database
selection and PostgreSQL support"). Without either a Pre-Deploy Command
or *some* automatic mechanism, a new migration (like the
`first_discovered_at` index added in the previous hardening pass) would
never actually get applied to the live production database - the app
would keep running against an increasingly stale schema indefinitely,
with no way to fix it short of the kind of manual shell access Render
Free doesn't offer for a routine deploy. See PROJECT_CONTEXT.md decision
#20 and ARCHITECTURE.md "Automatic migrations on Render Free" for the
full reasoning, including why this is scoped to PostgreSQL only - SQLite
(local dev/tests) keeps using `init_db()`'s existing `create_all()`
bootstrap, completely unchanged.

**Design constraints, each satisfied deliberately, not incidentally:**
- Runs once, synchronously, from `main.py`'s `lifespan()`, strictly
  before `init_db()`, the legacy marketplace-column migration, and the
  background scanner start - the app must never begin serving requests,
  or start scanning marketplaces, against a database whose schema might
  not match what the running code expects.
- **Fails fast, on purpose.** Any failure here - a bad migration, a lock
  timeout, a connectivity problem - propagates out of this function, out
  of `lifespan()`, and fails FastAPI/uvicorn startup outright. Render
  then simply keeps serving the previous successful deploy instead of
  ever letting a broken migration or a schema/code mismatch go live - a
  failed startup is the *safe* outcome here, not a bug to work around.
- **Idempotent.** `alembic upgrade head` against an already-current
  database is a documented no-op (see `tests/test_alembic_migrations.py`)
  - restarts, redeploys, and manual restarts on Render are all safe to
  run this on repeatedly.
- **Never runs a downgrade** - only ever `command.upgrade(cfg, "head")`,
  matching every other Alembic usage in this project.
- **A Postgres advisory lock guards the actual migration run**, acquired
  with a bounded wait (`pg_try_advisory_lock`, polled from Python -
  deliberately not the blocking `pg_advisory_lock` combined with
  `lock_timeout`, since that GUC's interaction with advisory-lock
  functions isn't consistent/documented clearly enough across Postgres
  versions to depend on). Render Free only ever runs a single instance of
  this service (no horizontal scaling on that plan), so true concurrent
  *instances* racing to migrate at once isn't realistically possible -
  but a brief overlap during a deploy's old-instance-shutting-down/
  new-instance-starting-up window, or two manual restarts triggered close
  together, are both real enough to guard against cheaply. If the lock
  can't be acquired within `settings.migration_lock_timeout_seconds`,
  this fails fast rather than letting two processes run DDL concurrently.
- **Never logs `DATABASE_URL` or any credential** - same rule as every
  other module that touches it (`core/persistence/database.py`,
  `alembic/env.py`). Only the dialect name and generic progress/failure
  messages are logged.
"""

import logging
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from marketplace_alert.config import settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI_PATH = _PROJECT_ROOT / "alembic.ini"

# Arbitrary, fixed key for the Postgres session-level advisory lock this
# module uses to serialize migration runs. Must stay stable (changing it
# would just mean two different-vintage app instances no longer contend
# for the same lock, not a correctness bug on its own) and must not
# collide with an advisory lock key used elsewhere in this codebase -
# nothing else takes one today.
_MIGRATION_ADVISORY_LOCK_KEY = 738_291_064_501

_LOCK_POLL_INTERVAL_SECONDS = 0.5


def run_pending_migrations(bind: Engine) -> None:
    """Bring the database up to the latest Alembic revision.

    PostgreSQL only - a deliberate no-op for SQLite and any other
    dialect (see this module's docstring and `init_db()`'s).

    Raises on any failure. Callers must let this propagate (see
    `main.py`'s `lifespan()`) rather than catching and continuing -
    starting the app against a database that might not match the current
    schema is worse than failing to start at all.
    """
    if bind.dialect.name != "postgresql":
        logger.info("Skipping automatic Alembic migration for %s - not PostgreSQL", bind.dialect.name)
        return

    logger.info("Applying pending Alembic migrations for PostgreSQL")
    connection = bind.connect()
    lock_acquired = False
    try:
        _acquire_advisory_lock(connection, settings.migration_lock_timeout_seconds)
        lock_acquired = True
        _upgrade_to_head()
    except Exception:
        logger.exception("Alembic migration failed - refusing to continue startup")
        raise
    finally:
        if lock_acquired:
            _release_advisory_lock(connection)
        connection.close()
    logger.info("Alembic migrations applied successfully")


def _acquire_advisory_lock(connection: Connection, timeout_seconds: float) -> None:
    """Poll `pg_try_advisory_lock` (non-blocking) until it succeeds or
    `timeout_seconds` elapses. Bounded on purpose - a stuck or crashed
    prior instance must never hang this one's startup forever."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK_KEY}
        )
        acquired = bool(result.scalar())
        connection.commit()
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Could not acquire the database migration lock within {timeout_seconds:.0f}s - "
                "another process may already be migrating, or is stuck holding the lock"
            )
        time.sleep(_LOCK_POLL_INTERVAL_SECONDS)


def _release_advisory_lock(connection: Connection) -> None:
    """Released on the exact same connection/session that acquired it -
    required for correctness: PostgreSQL's session-level advisory locks
    are tied to the session that took them, so calling
    `pg_advisory_unlock` from a *different* connection is a silent no-op,
    not a real release."""
    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK_KEY})
    connection.commit()


def _upgrade_to_head() -> None:
    cfg = Config(str(_ALEMBIC_INI_PATH))
    command.upgrade(cfg, "head")
