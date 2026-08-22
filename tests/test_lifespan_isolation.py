"""Regression test: using the API TestClient must never trigger real
background scanning, real outbound network calls, or real database writes
- not even via the deliberately dangerous `with TestClient(app) as ...:`
form.

**This is exactly what went wrong once during development.** An ad-hoc
verification script (run outside pytest, so tests/conftest.py's fixtures
never applied) used `with TestClient(app) as client:` instead of plain
`TestClient(app)`. That triggers FastAPI's real `lifespan()`
(`marketplace_alert/main.py`), which starts `_background_scanner` - a
singleton wired directly to the real `SessionLocal` and real Etsy/eBay/
Telegram credentials, entirely bypassing `app.dependency_overrides`. The
result was real Etsy searches against the developer's real database and
real Telegram notifications sent, unintentionally.

`tests/conftest.py`'s `_never_run_real_lifespan` (autouse - applies to
every test in the suite, not just this one) now replaces the app's real
lifespan with a no-op. These tests exist specifically to prove that
safety net actually works, using the exact dangerous pattern that caused
the original incident - not just avoiding it by convention.
"""

import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from marketplace_alert.main import app

_SCANNER_THREAD_NAME = "saved-search-scanner"


def _scanner_thread_running() -> bool:
    return any(t.name == _SCANNER_THREAD_NAME for t in threading.enumerate())


def test_context_manager_test_client_does_not_start_the_real_scanner() -> None:
    """The exact pattern that caused the original incident: `with
    TestClient(app) as client:` normally starts the real background
    scanner thread. It must not, inside the test suite."""
    assert not _scanner_thread_running()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/status").status_code == 200
        assert not _scanner_thread_running()

    assert not _scanner_thread_running()


def test_context_manager_test_client_makes_no_real_outbound_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every real network call this app ever makes (Telegram, Etsy, eBay -
    including eBay's OAuth token request) goes through the module-level
    `httpx.get`/`httpx.post` functions (never a persistent client - see
    ARCHITECTURE.md). If the real scanner/notification stack were
    reachable, one of these would fire almost immediately given the
    developer's saved searches; fail loudly if either is ever called."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "A real outbound HTTP call was attempted during a test - this "
            "would have hit real Etsy/eBay/Telegram infrastructure"
        )

    monkeypatch.setattr(httpx, "get", _fail_if_called)
    monkeypatch.setattr(httpx, "post", _fail_if_called)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/marketplaces").status_code == 200
        assert client.get("/api/v1/status").status_code == 200


def test_context_manager_test_client_never_runs_startup_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even more directly than the above: main.py's own startup calls
    (run_pending_migrations, init_db, migrate_legacy_marketplace_column,
    and starting the scanner) must never execute - proving the real
    lifespan body truly never runs at all, not just that its effects
    happen to be harmless this time. All of these run against the real,
    production-bound `engine` (see main.py), so "never called" is also
    the "never mutates the real database" guarantee - including never
    attempting a real Alembic migration against it."""
    import marketplace_alert.main as main_module

    calls: list[str] = []
    monkeypatch.setattr(
        main_module, "run_pending_migrations", lambda *a, **k: calls.append("run_pending_migrations")
    )
    monkeypatch.setattr(main_module, "init_db", lambda *a, **k: calls.append("init_db"))
    monkeypatch.setattr(
        main_module, "migrate_legacy_marketplace_column", lambda *a, **k: calls.append("migrate")
    )
    monkeypatch.setattr(
        main_module._background_scanner, "start", lambda *a, **k: calls.append("scanner_start")
    )
    monkeypatch.setattr(
        main_module._background_scanner, "stop", lambda *a, **k: calls.append("scanner_stop")
    )

    with TestClient(app) as client:
        client.get("/health")

    assert calls == []


def test_plain_test_client_without_context_manager_also_never_starts_scanner(client) -> None:
    """The pattern the `client` fixture (tests/conftest.py) actually uses
    day to day - plain `TestClient(app)`, no `with` - never triggers
    lifespan on its own either (this was already true before the fix; this
    test just keeps it covered alongside the `with`-form regression tests
    above, using the real project-wide `client` fixture rather than a
    fresh, unfixtured one)."""
    assert not _scanner_thread_running()
    assert client.get("/health").status_code == 200
    assert not _scanner_thread_running()
