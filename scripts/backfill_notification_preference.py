"""One-off cutover script: seed ONE explicit, existing user's Telegram
notification preference from the current global `TELEGRAM_CHAT_ID`.

See `marketplace_alert/core/notifications/preference_backfill.py` for the
full design and the exact rules governing it - this script is a thin CLI
wrapper around that module's `run_preference_backfill()`; no business
logic lives here.

**What this does:** finds the named user by (normalized) email - never
creates one - and, only if they have no `NotificationPreference` row at
all yet, sets it to the current `TELEGRAM_CHAT_ID` value. Never touches an
account that already has a preference row, regardless of its value -
idempotent, and safe to re-run.

**Deliberately a manual script, not wired into the app** - same "reviewed
and run by a human, not automatic" posture as `scripts/create_bootstrap_
admin.py`, `scripts/cleanup_historical_listings.py`, and `scripts/
backfill_listing_metadata.py`.

**Never logs, prints, or otherwise exposes the chat id value itself** -
only aggregate outcome (found/not found, already had a preference,
applied or not).

Usage (from the project root, with the project's virtualenv active):

    python scripts/backfill_notification_preference.py --email user@example.com
        # dry run - reports what WOULD happen, writes nothing

    python scripts/backfill_notification_preference.py --email user@example.com --apply
        # actually writes (only if the account exists and has no preference row yet)

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app
itself uses.
"""

import argparse
import sys

from marketplace_alert.config import settings
from marketplace_alert.core.notifications.preference_backfill import PreferenceBackfillReport, run_preference_backfill
from marketplace_alert.core.persistence.database import SessionLocal


def _print_report(report: PreferenceBackfillReport, *, apply: bool) -> None:
    print(f"Mode: {'APPLIED' if apply else 'DRY RUN (nothing written)'}")

    if not report.user_found:
        print(f"No existing user found for email {report.email!r} - nothing to migrate.")
        return

    print(f"Target account: {report.email} (id={report.user_id})")
    if report.already_had_a_preference:
        print("  Already has a notification preference row - left unchanged (idempotent, never overwritten).")
        return

    verb = "Set" if report.applied else "Would set"
    print(f"  {verb} the Telegram destination from the current TELEGRAM_CHAT_ID (value not printed).")
    if not apply:
        print("  Dry run only - nothing was written. Re-run with --apply to actually make this change.")


def main() -> int:
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        # Windows consoles often default to a legacy codepage - replace
        # rather than crash mid-report (same as create_bootstrap_admin.py).
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--email",
        required=True,
        help="The existing account's email to migrate. Never guessed or defaulted - must name a real, already-signed-up account.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the preference. Without this flag, always a dry run.",
    )
    args = parser.parse_args()

    if not settings.telegram_chat_id:
        print(
            "TELEGRAM_CHAT_ID is not set in this environment - nothing to migrate from.",
            file=sys.stderr,
        )
        return 2

    session = SessionLocal()
    try:
        report = run_preference_backfill(
            session, email=args.email, telegram_chat_id=settings.telegram_chat_id, apply=args.apply
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _print_report(report, apply=args.apply)
    return 0 if report.user_found else 1


if __name__ == "__main__":
    sys.exit(main())
