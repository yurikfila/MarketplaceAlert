"""One-off operator script: promote an EXISTING account to `is_admin=True`.

See `marketplace_alert/core/auth/admin_promotion.py` for the full design -
this script is a thin CLI wrapper around that module's
`promote_to_admin()`; no business logic lives here.

**What this does:** finds the named user by (normalized) email - never
creates one - and sets `is_admin=True` if it isn't already. Never touches
any other field (password hash, saved searches, tokens, lockout state).
Idempotent - running this twice against an already-admin account changes
nothing and is reported as such.

**Deliberately a manual script, not an API endpoint.** Promoting an
account to admin is a server/operator action in this phase, never
reachable through the mobile app or any authenticated request - see
`marketplace_alert/api/v1/admin.py`'s own module docstring for why no
such endpoint exists.

**Unrelated to `scripts/create_bootstrap_admin.py`** - that script
decides which account owns pre-authentication legacy data (a data-
attribution concept); this one only ever sets the `is_admin`
authorization flag. See `core/auth/admin_promotion.py`'s own docstring.

**Never prints a password, password hash, or any other authentication
secret** - only the target email and the promotion outcome (found or
not, already admin or not, applied or not).

Usage (from the project root, with the project's virtualenv active):

    python scripts/set_admin.py --email owner@example.com
        # dry run - reports what WOULD happen, writes nothing

    python scripts/set_admin.py --email owner@example.com --apply
        # actually sets is_admin=True and commits

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app
itself uses.
"""

import argparse
import sys

from marketplace_alert.core.auth.admin_promotion import AdminPromotionReport, promote_to_admin
from marketplace_alert.core.persistence.database import SessionLocal


def _print_report(report: AdminPromotionReport, *, apply: bool) -> None:
    print(f"Mode: {'APPLIED' if apply else 'DRY RUN (nothing written)'}")
    print(f"Target account: {report.email}")

    if not report.user_found:
        print("  No existing user found with this email - nothing to do. This script never creates an account.")
        return

    if report.already_admin:
        print("  Already an admin - no change made (idempotent).")
        return

    if report.applied:
        print("  Promoted to admin (is_admin=True) and committed.")
    else:
        print("  Would be promoted to admin. Re-run with --apply to actually make this change.")


def main() -> int:
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        # Windows consoles often default to a legacy codepage - replace
        # rather than crash mid-report (same as create_bootstrap_admin.py).
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--email",
        required=True,
        help="The existing account's email to promote. Never guessed or defaulted - must name a real, already-signed-up account.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually set is_admin=True and commit. Without this flag, always a dry run.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        report = promote_to_admin(session, email=args.email, apply=args.apply)
        _print_report(report, apply=args.apply)
        return 0 if report.user_found else 1
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
