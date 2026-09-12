"""Read-only admin user-management reporting.

Everything here exists to answer one question for the application owner:
"who is registered, and how much of the app are they using" - never to
let an admin (or anyone else) view a password, a password hash, a reset
code, a token, or any other authentication secret. See `repository.py`'s
`AdminUserRow` for exactly which fields exist and why each one is safe,
and `api/v1/admin.py` for the HTTP layer on top of this (gated by
`core/auth/dependencies.py:require_admin`).

Deliberately its own package, not folded into `core/auth/` (scoped to
authentication persistence only) or `core/saved_searches/` (scoped to
saved-search persistence only) - this reporting view joins across both,
which neither of those should take on as a new responsibility. Also
unrelated to `core/auth/bootstrap.py`/`scripts/create_bootstrap_admin.py`
("bootstrap admin" = which account owns pre-authentication legacy data) -
see `core/auth/admin_promotion.py`'s own docstring for why that's a
different concept entirely, despite the shared word "admin".
"""
