"""Tests for authentication configuration (`Settings.jwt_secret_key` and the
token-lifetime settings) - `marketplace_alert/config.py`.

Mirrors `tests/test_cors_config.py`'s pattern: construct `Settings`
directly with `_env_file=None` (never read the developer's real `.env`) to
exercise each combination in isolation, rather than depending on process
environment variables.
"""

import pytest
from pydantic import ValidationError

import marketplace_alert.config as config_module
from marketplace_alert.config import Settings

_FAKE_DATABASE_URL = "postgresql://appuser:secret@db.render.com:5432/marketplacealert_db"
_VALID_SECRET = "a" * 32  # exactly the minimum accepted length


@pytest.fixture(autouse=True)
def _reset_ephemeral_jwt_secret_key_cache():
    """The ephemeral secret is cached at module scope, deliberately - one
    per *process*, not one per `Settings()` call (see config.py). Reset it
    around every test in this file so tests can't leak a generated value
    into each other via that shared cache."""
    config_module._ephemeral_jwt_secret_key = None
    yield
    config_module._ephemeral_jwt_secret_key = None


# --- No DATABASE_URL (local SQLite dev / the test suite itself) ------------


def test_jwt_secret_key_is_auto_generated_when_unset_and_no_database_url() -> None:
    settings = Settings(_env_file=None)
    assert settings.jwt_secret_key
    assert len(settings.jwt_secret_key) >= 32


def test_auto_generated_jwt_secret_key_is_shared_across_instances_in_the_same_process() -> None:
    """The actual correctness requirement: a token signed via one
    `Settings` instance's secret must still verify via another instance's
    secret in the same process - which is only true if they share exactly
    one generated value, not a fresh one each."""
    first = Settings(_env_file=None)
    second = Settings(_env_file=None)
    assert first.jwt_secret_key == second.jwt_secret_key


def test_ephemeral_jwt_secret_key_is_freshly_random_per_process() -> None:
    """Proves the shared value is still genuinely random/ephemeral, not a
    hidden hard-coded fallback masquerading as "generated" - simulates a
    new process by clearing the module-level cache between two
    generations, which must then differ."""
    first = Settings(_env_file=None).jwt_secret_key

    config_module._ephemeral_jwt_secret_key = None
    second = Settings(_env_file=None).jwt_secret_key

    assert first != second


def test_explicit_jwt_secret_key_is_used_as_is_without_database_url() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=_VALID_SECRET)
    assert settings.jwt_secret_key == _VALID_SECRET


# --- DATABASE_URL set (any real deployment, including production) ----------


def test_missing_jwt_secret_key_fails_fast_when_database_url_is_set() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY is required"):
        Settings(_env_file=None, database_url=_FAKE_DATABASE_URL)


def test_explicit_jwt_secret_key_is_accepted_when_database_url_is_set() -> None:
    settings = Settings(_env_file=None, database_url=_FAKE_DATABASE_URL, jwt_secret_key=_VALID_SECRET)
    assert settings.jwt_secret_key == _VALID_SECRET


# --- Minimum length is enforced regardless of DATABASE_URL -----------------


def test_too_short_jwt_secret_key_is_rejected_without_database_url() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(_env_file=None, jwt_secret_key="too-short")


def test_too_short_jwt_secret_key_is_rejected_with_database_url() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(_env_file=None, database_url=_FAKE_DATABASE_URL, jwt_secret_key="too-short")


def test_jwt_secret_key_exactly_at_minimum_length_is_accepted() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=_VALID_SECRET)
    assert len(settings.jwt_secret_key) == 32
    assert settings.jwt_secret_key == _VALID_SECRET


# --- Token lifetime settings: defaults and overrides ------------------------


def test_token_lifetime_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.access_token_expire_minutes == 30
    assert settings.refresh_token_expire_days == 30
    assert settings.password_reset_token_expire_minutes == 30


def test_token_lifetime_settings_are_overridable() -> None:
    settings = Settings(
        _env_file=None,
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        password_reset_token_expire_minutes=60,
    )
    assert settings.access_token_expire_minutes == 15
    assert settings.refresh_token_expire_days == 7
    assert settings.password_reset_token_expire_minutes == 60
