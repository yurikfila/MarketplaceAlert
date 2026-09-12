"""Business logic for `scripts/set_admin.py` - promotes exactly one
already-existing account to `is_admin=True`. See that script's own
docstring for the full usage/safety rationale; this module is the thin
core it wraps, same division of responsibility as
`core/notifications/preference_backfill.py` and
`scripts/backfill_notification_preference.py`.

**Deliberately unrelated to `core/auth/bootstrap.py`/`scripts/
create_bootstrap_admin.py`.** That mechanism decides which account owns
pre-authentication legacy data (saved searches/listings with no owner
yet) - a data-attribution concept. This module is purely about the
`is_admin` authorization flag added for the read-only admin
user-management API (`api/v1/admin.py`). The two share the word "admin"
but are otherwise unrelated; conflating them would be a real design
mistake, not just a naming coincidence.

**Never creates an account.** A promotion target must already exist -
this module only ever sets a field on a row that's already there.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from marketplace_alert.core.auth.repository import UserRepository, normalize_email


@dataclass
class AdminPromotionReport:
    """What `scripts/set_admin.py` reports back to the operator - no
    password, hash, or token ever appears here, only the outcome of
    setting one boolean field."""

    email: str
    user_found: bool
    already_admin: bool
    applied: bool


def promote_to_admin(session: Session, *, email: str, apply: bool) -> AdminPromotionReport:
    """Finds an EXISTING user by (normalized) email - the same lookup
    `UserRepository.get_by_email` already uses everywhere else in this
    codebase - and sets `is_admin=True`.

    - Unknown email: reports `user_found=False`, changes nothing.
    - Already an admin: reports `already_admin=True`, changes nothing -
      idempotent, safe to run more than once against the same account.
    - `apply=False` (the default posture every script in this repo uses -
      see `scripts/backfill_notification_preference.py`): always a dry
      run, reports what WOULD happen, writes nothing.
    - `apply=True` and not already an admin: sets `is_admin=True` and
      commits immediately - this function owns that commit (unlike the
      request-scoped repositories under `core/auth/repository.py`, which
      only flush and let a FastAPI request's session commit at the end),
      the same convention `run_preference_backfill` already uses for this
      exact kind of one-off, non-request-scoped CLI operation.
    """
    normalized = normalize_email(email)
    user = UserRepository(session).get_by_email(normalized)
    if user is None:
        return AdminPromotionReport(email=normalized, user_found=False, already_admin=False, applied=False)

    already_admin = user.is_admin
    will_apply = apply and not already_admin
    if will_apply:
        user.is_admin = True
        session.commit()

    return AdminPromotionReport(
        email=normalized, user_found=True, already_admin=already_admin, applied=will_apply
    )
