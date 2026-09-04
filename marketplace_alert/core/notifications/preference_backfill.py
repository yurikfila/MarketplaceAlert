"""One-time (and safely re-runnable) cutover: give ONE explicit, already-
existing user a `NotificationPreference` seeded from the current, legacy
global `TELEGRAM_CHAT_ID`, so their Telegram alerts continue working
identically once per-user notification routing goes live and `core/
notifications/outbox.py` stops using that global value as a runtime
fallback at all (see that module's "SECURITY RULE").

Pure logic only - no `argparse`, no `getpass`, no `print`. The CLI wrapper
(`scripts/backfill_notification_preference.py`) owns all of that; this
module is plain functions/dataclasses a test can call directly against a
session, matching this codebase's established split (see `core/auth/
bootstrap.py` and its own CLI wrapper, `scripts/create_bootstrap_admin.py`).

**Never creates a user** - unlike `core/auth/bootstrap.py`, this script
has no legitimate reason to create an account; it exists solely to seed
one *specific, already-real* account's preference. A misspelled or
nonexistent email is reported and aborted, never silently treated as
"nothing to do" or "create one".

**Idempotent, and never overwrites.** If the target user already has a
`NotificationPreference` row - from a previous run of this same script,
or because they've since set/cleared their own preference via `PUT
/api/v1/notification-preferences/me` - this is a strict no-op, regardless
of that row's current value. A second run can never clobber a user's own
later choice, and re-running after a first successful run changes
nothing.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from marketplace_alert.core.auth.repository import UserRepository
from marketplace_alert.core.notifications.preferences_repository import NotificationPreferenceRepository


@dataclass
class PreferenceBackfillReport:
    """The complete result of one `run_preference_backfill` call - enough
    for the CLI wrapper to print a report in both dry-run and apply modes,
    without ever including the chat id value itself."""

    email: str
    user_found: bool
    user_id: int | None
    already_had_a_preference: bool
    applied: bool


def run_preference_backfill(
    session: Session, *, email: str, telegram_chat_id: str, apply: bool
) -> PreferenceBackfillReport:
    """Finds the named, already-existing user (never creates one) and, if
    they have no `NotificationPreference` row at all yet, sets its
    `telegram_chat_id` to the given value.

    `telegram_chat_id` is passed in by the caller (the CLI wrapper reads
    it from `settings.telegram_chat_id`) rather than read from settings
    here - this module never touches configuration directly, keeping it
    trivially testable with an arbitrary value and keeping "where the
    legacy global value is allowed to be read from" to exactly one place.

    Commits only when `apply=True` *and* a write actually happens (i.e.
    never when the account doesn't exist, and never when it already has a
    preference row) - a dry run, or a no-op idempotent rerun, never opens
    a write transaction at all.
    """
    user = UserRepository(session).get_by_email(email)
    if user is None:
        return PreferenceBackfillReport(
            email=email, user_found=False, user_id=None, already_had_a_preference=False, applied=False
        )

    preferences = NotificationPreferenceRepository(session)
    existing = preferences.get_by_user_id(user.id)
    already_had_a_preference = existing is not None

    will_apply = apply and not already_had_a_preference
    if will_apply:
        preferences.upsert_telegram_chat_id(user.id, telegram_chat_id)
        session.commit()

    return PreferenceBackfillReport(
        email=email,
        user_found=True,
        user_id=user.id,
        already_had_a_preference=already_had_a_preference,
        applied=will_apply,
    )
