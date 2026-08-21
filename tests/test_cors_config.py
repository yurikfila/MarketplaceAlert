"""Tests for CORS origin configuration (`Settings.cors_allowed_origins`).

Never a real integration test against a live browser - just proving the
parsing/default behavior config.py promises: unset -> no cross-origin
access allowed, set -> parsed into an explicit list, never "*".
"""

from marketplace_alert.config import Settings


def test_cors_allowed_origins_defaults_to_empty() -> None:
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins == []


def test_cors_allowed_origins_parses_comma_separated_string() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins="http://localhost:3000,https://app.example.com")
    assert settings.cors_allowed_origins == ["http://localhost:3000", "https://app.example.com"]


def test_cors_allowed_origins_strips_whitespace_around_entries() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins=" http://localhost:3000 , https://app.example.com ")
    assert settings.cors_allowed_origins == ["http://localhost:3000", "https://app.example.com"]


def test_cors_allowed_origins_ignores_empty_entries() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins="http://localhost:3000,,")
    assert settings.cors_allowed_origins == ["http://localhost:3000"]


def test_cors_allowed_origins_accepts_a_real_list_directly() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins=["http://localhost:3000"])
    assert settings.cors_allowed_origins == ["http://localhost:3000"]


def test_cors_default_never_allows_wildcard() -> None:
    settings = Settings(_env_file=None)
    assert "*" not in settings.cors_allowed_origins


def test_app_does_not_send_cors_headers_for_an_unconfigured_origin(client) -> None:
    """Integration check against the real, already-constructed `app`: since
    the developer's real .env has no CORS_ALLOWED_ORIGINS set, a
    cross-origin browser request must not receive an
    access-control-allow-origin header - CORS is off by default."""
    response = client.get("/api/v1/status", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers
