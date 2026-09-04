"""Tests for `Settings.legacy_routes_enabled` (secure-by-default) and the
`main.py` gate it drives - see config.py's docstring for the full
reasoning. Complements `test_ownership_enforcement_api.py`'s explicit
disabled-state test with the specific proofs this security-hardening
change requires: the *default* (no override at all) is disabled, an
explicit opt-in restores the legacy surface exactly as before, and
neither `/health` nor `/api/v1` are ever affected either way.
"""

from marketplace_alert.config import Settings

# (method, path, kwargs) for every route `_require_legacy_routes_enabled`
# gates - see main.py. Every one of these, when the gate is disabled,
# responds with the exact same body (`{"detail": "Not Found"}`) - asserted
# on directly below rather than just "status 404", so a route's own
# ordinary not-found response (e.g. a nonexistent saved-search id) could
# never be mistaken for the gate firing.
_LEGACY_ROUTE_CALLS = [
    ("get", "/", {}),
    ("get", "/listings", {}),
    ("get", "/search", {"params": {"q": "x"}}),
    ("get", "/scan", {"params": {"q": "x"}}),
    (
        "post",
        "/saved-searches",
        {"json": {"query": "x", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}},
    ),
    ("get", "/saved-searches", {}),
    ("get", "/saved-searches/1", {}),
    ("patch", "/saved-searches/1", {"json": {"query": "y"}}),
    ("delete", "/saved-searches/1", {}),
    ("post", "/saved-searches/1/run", {}),
]


def test_legacy_routes_enabled_defaults_to_false() -> None:
    """The declared default, isolated from any local `.env` - secure by
    default regardless of what a given environment's configuration does
    or doesn't set."""
    settings = Settings(_env_file=None)
    assert settings.legacy_routes_enabled is False


def test_legacy_routes_are_disabled_by_default(client) -> None:
    """No override at all - the `client` fixture doesn't touch this
    setting, so it reflects the real, secure default for every one of
    the ten gated legacy routes."""
    for method, path, kwargs in _LEGACY_ROUTE_CALLS:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, f"{method.upper()} {path} was not disabled by default"
        assert response.json()["detail"] == "Not Found", f"{method.upper()} {path} did not 404 via the gate"


def test_legacy_routes_work_when_explicitly_enabled(client, with_legacy_routes_enabled) -> None:
    """Explicit opt-in (a test's own isolated settings, or an operator's
    environment) restores the legacy surface exactly as it always
    worked - none of these should be gate-blocked anymore."""
    for method, path, kwargs in _LEGACY_ROUTE_CALLS:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code != 404 or response.json().get("detail") != "Not Found", (
            f"{method.upper()} {path} was still gate-blocked despite being explicitly enabled"
        )


def test_health_is_unaffected_by_legacy_routes_enabled(client) -> None:
    assert client.get("/health").status_code == 200


def test_health_is_unaffected_when_legacy_routes_explicitly_enabled(client, with_legacy_routes_enabled) -> None:
    assert client.get("/health").status_code == 200


def test_api_v1_is_unaffected_by_legacy_routes_enabled(client) -> None:
    assert client.get("/api/v1/status").status_code == 200
    assert client.get("/api/v1/marketplaces").status_code == 200


def test_api_v1_is_unaffected_when_legacy_routes_explicitly_enabled(client, with_legacy_routes_enabled) -> None:
    assert client.get("/api/v1/status").status_code == 200
    assert client.get("/api/v1/marketplaces").status_code == 200
