"""One-shot entrypoint: run every currently-due saved search once, then exit.

Intended to be invoked by the Render Cron Job "scan" schedule (see
ARCHITECTURE.md) instead of relying on `BackgroundScanner`'s in-process
thread, which only runs while the web service process is alive - a Render
Free web service can be spun down while idle, silently starving any saved
search whose next-due time falls during that window. A Cron Job is started
fresh by the platform on its own schedule, independent of whether the web
service happens to be up.

Reuses `BackgroundScanner.run_due_searches()` (a single pass, never
`.start()`'d as a thread here) rather than duplicating its due-search
lookup / per-search error isolation / logging - this script's only job is
to call that once and exit with a code reflecting whether it completed.

A failure in one saved search is still logged and never stops the others
(see `BackgroundScanner.run_due_searches`'s own docstring) - this script
exits 0 as long as the pass itself completed, even if individual saved
searches failed; a fatal error running the pass itself (e.g. no database
connection at all) exits 1.

Never sends notifications - a newly-discovered listing only ever gets a
`PendingNotification` outbox row here (see `SavedSearchRunner`'s module
docstring). Delivery is `scripts/drain_notification_outbox.py`'s job, run
as a separate Cron Job.

Usage (from the project root, with the project's virtualenv active):

    python scripts/run_due_scans.py

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app itself
uses.
"""

import logging
import sys

from marketplace_alert.config import settings
from marketplace_alert.core.logging_config import configure_logging
from marketplace_alert.core.persistence.database import SessionLocal
from marketplace_alert.core.scheduler.scanner import BackgroundScanner
from marketplace_alert.dependencies import saved_search_run_guard, saved_search_runner

logger = logging.getLogger(__name__)


def main() -> int:
    configure_logging(settings.log_level)

    scanner = BackgroundScanner(
        session_factory=SessionLocal,
        runner=saved_search_runner,
        run_guard=saved_search_run_guard,
    )
    try:
        scanner.run_due_searches()
    except Exception:
        logger.exception("run_due_scans: scan pass failed")
        return 1

    logger.info("run_due_scans: scan pass complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
