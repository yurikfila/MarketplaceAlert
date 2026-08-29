"""One-off cutover script: create (or find) the bootstrap admin account
and attribute pre-existing, pre-authentication data to it.

See `marketplace_alert/core/auth/bootstrap.py` for the full design and
the exact rules governing each step - this script is a thin CLI wrapper
around that module's `run_cutover()`; no business logic lives here.

**What this does, in order:**
1. Finds the bootstrap account by (normalized) email, or creates it if it
   doesn't exist yet - safely re-runnable: a second run against an
   existing account skips straight to step 2, no password needed.
2. Assigns every `saved_searches` row with `user_id IS NULL` to that
   account. Never touches a row that already has an owner.
3. For every `discovered_listings` row with `discovered_by_saved_search_id
   IS NULL`, attributes it to exactly one bootstrap-owned saved search
   *only* when exactly one such search targets that listing's
   marketplace - ambiguous or unmatched rows are left unowned, never
   guessed at.

**Deliberately a manual script, not wired into the app** - same
"reviewed and run by a human, not automatic" posture as
`scripts/cleanup_historical_listings.py` and
`scripts/backfill_listing_metadata.py`.

**Never commits, logs, or prints a password or password hash anywhere,
under any circumstances** - the only credential this script ever handles
is read once (from `BOOTSTRAP_ADMIN_PASSWORD` or an interactive prompt)
and passed directly to `core.auth.security.hash_password`.

Usage (from the project root, with the project's virtualenv active):

    python scripts/create_bootstrap_admin.py --email admin@example.com
        # dry run - reports what WOULD happen, writes nothing

    python scripts/create_bootstrap_admin.py --email admin@example.com --apply
        # actually creates the account (if needed) and backfills, commits

Email can also come from the BOOTSTRAP_ADMIN_EMAIL environment variable
instead of --email - never hard-code a real email into this file or any
wrapper around it. Password, if a new account needs to be created, comes
from the BOOTSTRAP_ADMIN_PASSWORD environment variable if set (useful for
scripted/CI use - be aware this risks exposure via shell history or the
process list, so prefer the interactive prompt below whenever a human is
actually at the keyboard) or otherwise an interactive, masked `getpass`
prompt (asked twice, to catch typos before they lock anyone out).

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app
itself uses.
"""

import argparse
import getpass
import os
import sys

from marketplace_alert.core.auth.bootstrap import (
    BootstrapPasswordTooShortError,
    BootstrapReport,
    find_bootstrap_user,
    run_cutover,
)
from marketplace_alert.core.persistence.database import SessionLocal


def _resolve_email(cli_email: str | None) -> str | None:
    if cli_email:
        return cli_email.strip()
    env_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL")
    if env_email:
        return env_email.strip()
    return None


def _resolve_password(*, needed: bool) -> str | None:
    """Only ever called when `needed` is True (a new account, `--apply`).
    Never echoes, logs, or returns anything derived from the password
    beyond the raw string itself, passed straight through to
    `run_cutover` - this function does not hash it, log it, or persist it
    anywhere."""
    if not needed:
        return None

    env_password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    if env_password:
        print("Using BOOTSTRAP_ADMIN_PASSWORD from the environment (not printed).")
        return env_password

    first = getpass.getpass("New bootstrap account password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("Passwords did not match.", file=sys.stderr)
        return None
    return first


def _print_report(report: BootstrapReport) -> None:
    print(f"Mode: {'APPLIED' if report.applied else 'DRY RUN (nothing written)'}")
    print(f"Bootstrap account: {report.email}")
    if report.user_already_existed:
        print(f"  Already existed (id={report.user_id}) - no account created.")
    elif report.applied:
        print(f"  Created (id={report.user_id}).")
    else:
        print("  Does not exist yet - would be created.")

    backfill = report.saved_search_backfill
    verb = "assigned" if report.applied else "would be assigned"
    print()
    print(f"Saved searches with no owner: {backfill.unowned_before}")
    print(f"  {verb} to this account: {backfill.assigned_count if report.applied else backfill.unowned_before}")

    attribution = report.listing_attribution
    attr_verb = "attributed" if report.applied else "would be attributed"
    print()
    print(f"Listings already attributed to a discovering search: {attribution.already_attributed_count}")
    print(f"Listings with no discovering-search attribution: {attribution.total_unattributed}")
    print(f"  Safely attributable (genuine provenance evidence) - {attr_verb}: {attribution.safely_attributable_count}")
    print(f"  Ambiguous (multiple candidate searches, no real evidence) - left unowned: {attribution.ambiguous_count}")
    print(f"  No provenance - left unowned: {attribution.no_provenance_count}")
    if attribution.safely_attributable_count == 0:
        print(
            "  (Marketplace alone is never sufficient evidence - see core/auth/bootstrap.py's "
            "_find_provenance_backed_saved_search_id docstring. This count is expected to be 0 "
            "given the current schema; that is correct, not a bug.)"
        )

    if not report.applied:
        print()
        print("Dry run only - nothing was written. Re-run with --apply to actually make these changes.")


def main() -> int:
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        # Windows consoles often default to a legacy codepage that can't
        # encode every character a real email address/title might
        # contain - replace rather than crash mid-report.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--email",
        default=None,
        help="Bootstrap account email. Falls back to BOOTSTRAP_ADMIN_EMAIL if not given. Required either way.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the account (if needed), backfill, and commit. Without this flag, always a dry run.",
    )
    args = parser.parse_args()

    email = _resolve_email(args.email)
    if not email:
        print(
            "No email given - pass --email or set BOOTSTRAP_ADMIN_EMAIL. Never hard-code one into this script.",
            file=sys.stderr,
        )
        return 2

    session = SessionLocal()
    try:
        needs_password = args.apply and find_bootstrap_user(session, email) is None
        password = _resolve_password(needed=needs_password)
        if needs_password and password is None:
            print("No usable password provided - aborting without writing anything.", file=sys.stderr)
            return 2

        report = run_cutover(session, email=email, password=password, apply=args.apply)
        _print_report(report)
        return 0
    except BootstrapPasswordTooShortError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
