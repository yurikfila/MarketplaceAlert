import pytest
from fastapi.testclient import TestClient

from marketplace_alert.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _legacy_routes_enabled(with_legacy_routes_enabled) -> None:
    """Most of this file exercises the legacy dashboard/`/search` surface,
    which is now opt-in-disabled by default - see config.py's
    `legacy_routes_enabled` docstring. Harmless for `test_app_loads`,
    which touches no route at all."""


def test_app_loads() -> None:
    assert app.title == "Marketplace Alert"


def test_root_endpoint_serves_the_dashboard(client) -> None:
    # Uses the isolated-DB `client` fixture (see conftest.py), not the raw
    # module-level client above - / now reads saved searches from the
    # database, which the raw client would hit for real.
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Marketplace Alert" in response.text


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_search_endpoint_returns_matching_mock_listings() -> None:
    response = client.get("/search", params={"q": "Maccabi"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert "Maccabi" in body[0]["title"]
    assert body[0]["marketplace"] == "mock"


def test_search_endpoint_is_case_insensitive() -> None:
    response = client.get("/search", params={"q": "maccabi"})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_search_endpoint_returns_empty_list_for_no_matches() -> None:
    response = client.get("/search", params={"q": "Nonexistent Item Zyxwvut"})
    assert response.status_code == 200
    assert response.json() == []


def test_search_endpoint_requires_query_param() -> None:
    response = client.get("/search")
    assert response.status_code == 422
