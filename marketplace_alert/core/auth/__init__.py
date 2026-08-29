"""Authentication: user accounts, JWT access tokens, and rotated, revocable
refresh tokens - the approved design in PROJECT_CONTEXT.md's authentication
decision.

- **Phase 1**: schema. `models.py` defines `User`, `RefreshToken`, and
  `PasswordResetToken`, and `SavedSearch` gained a nullable `user_id`
  column (see `core/saved_searches/models.py`).
- **Phase 2** (this phase): the core/service layer. `security.py` -
  password hashing/verification, JWT access-token creation/validation,
  refresh-token generation/hashing. `repository.py` - `UserRepository`,
  `RefreshTokenRepository`. `service.py` - `AuthService`: signup, login
  (with failed-attempt tracking and temporary lockout), refresh (rotation
  + reuse detection), logout.

No FastAPI routes, no auth dependency, no route protection, and no
multi-tenancy enforcement exist yet - `AuthService` is fully usable and
fully tested on its own, but nothing in the running application calls it
yet. See later phases for those.
"""
