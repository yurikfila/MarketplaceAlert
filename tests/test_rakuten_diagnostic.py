"""HTTP tests for the TEMPORARY `GET /api/v1/diagnostics/rakuten` route
(`marketplace_alert/api/v1/rakuten_diagnostic.py`). Covers the same
authorization ladder every `require_admin`-gated route needs (see
tests/test_admin_api.py), the not-configured/success/error response
shapes, and - the one property that actually matters for a route that
touches real credentials - that RAKUTEN_ACCESS_KEY never appears anywhere
in a response body or a log line, under any outcome.
"""

import httpx

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.connectors import retry as retry_module


def _signup(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": password})


def _promote_to_admin(db_session, email: str) -> None:
    user = db_session.query(User).filter_by(email=email.lower()).one()
    user.is_admin = True
    db_session.commit()


def _signup_admin(client, db_session, email="admin@example.com", password="a-strong-password") -> str:
    body = _signup(client, email=email, password=password).json()
    _promote_to_admin(db_session, email)
    return body["tokens"]["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _formatversion2_body(items: list[dict]) -> dict:
    return {"items": items, "count": len(items), "page": 1}


# =====================================================================
# Authorization ladder - identical rules to every other require_admin route
# =====================================================================


def test_rakuten_diagnostic_without_authorization_header_returns_401(client) -> None:
    response = client.get("/api/v1/diagnostics/rakuten")
    assert response.status_code == 401


def test_rakuten_diagnostic_with_a_normal_users_token_returns_403(client) -> None:
    body = _signup(client).json()
    token = body["tokens"]["access_token"]

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 403


# =====================================================================
# Not configured
# =====================================================================


def test_returns_not_configured_when_env_vars_are_unset(client, db_session, monkeypatch) -> None:
    monkeypatch.delenv("RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("RAKUTEN_ACCESS_KEY", raising=False)
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_formatversion2_body([]))

    monkeypatch.setattr(httpx, "get", fake_get)
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["success"] is False
    assert called is False


# =====================================================================
# Success
# =====================================================================


def test_success_returns_sanitized_items(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-access-key")
    captured = {}

    def fake_get(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        raw_item = {
            "itemCode": "shop:12345",
            "itemName": "Bosch Professional Drill",
            "itemPrice": 8990,
            "itemUrl": "https://item.rakuten.co.jp/shop/12345/",
            "mediumImageUrls": [{"imageUrl": "https://image.rakuten.co.jp/shop/cabinet/12345.jpg"}],
            "shopName": "Example Tools Shop",
        }
        return httpx.Response(200, json=_formatversion2_body([raw_item]))

    monkeypatch.setattr(httpx, "get", fake_get)
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["success"] is True
    assert body["http_status"] == 200
    assert body["item_count"] == 1
    assert body["items"][0] == {
        "item_code": "shop:12345",
        "item_name": "Bosch Professional Drill",
        "item_price": 8990,
        "item_url": "https://item.rakuten.co.jp/shop/12345/",
        "image_url": "https://image.rakuten.co.jp/shop/cabinet/12345.jpg",
        "shop_name": "Example Tools Shop",
    }
    # Exact verified request shape.
    assert captured["url"] == "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
    assert captured["params"]["keyword"] == "Bosch"
    assert captured["params"]["hits"] == 3
    assert captured["params"]["formatVersion"] == 2
    assert captured["params"]["applicationId"] == "test-application-id"
    assert captured["params"]["accessKey"] == "test-access-key"


def test_image_url_falls_back_to_a_plain_string_shape(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-access-key")

    def fake_get(url, params, timeout):
        raw_item = {"itemCode": "shop:1", "mediumImageUrls": ["https://image.rakuten.co.jp/plain.jpg"]}
        return httpx.Response(200, json=_formatversion2_body([raw_item]))

    monkeypatch.setattr(httpx, "get", fake_get)
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.json()["items"][0]["image_url"] == "https://image.rakuten.co.jp/plain.jpg"


# =====================================================================
# Errors (including the already-observed live CLIENT_IP_NOT_ALLOWED case)
# =====================================================================


def test_client_ip_not_allowed_error_is_surfaced(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-access-key")

    def fake_get(url, params, timeout):
        return httpx.Response(
            403, json={"error": "CLIENT_IP_NOT_ALLOWED", "error_description": "Client IP address is not allowed."}
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 200  # the diagnostic route itself always succeeds; Rakuten's own failure is data
    body = response.json()
    assert body["configured"] is True
    assert body["success"] is False
    assert body["http_status"] == 403
    assert body["error"] == "CLIENT_IP_NOT_ALLOWED"
    assert body["error_description"] == "Client IP address is not allowed."
    assert body["items"] == []


def test_non_json_response_is_handled_safely(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-access-key")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=b"not json"))
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["success"] is False


def test_network_failure_is_handled_safely(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-access-key")

    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["success"] is False


def test_429_is_retried_via_the_shared_retry_helper(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-access-key")
    monkeypatch.setattr(retry_module.time, "sleep", lambda seconds: None)
    responses = [httpx.Response(429), httpx.Response(200, json=_formatversion2_body([]))]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["success"] is True


# =====================================================================
# The one property that actually matters: the credential never leaks
# =====================================================================


def test_access_key_never_appears_in_the_response_body(client, db_session, monkeypatch) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "super-secret-access-key-value")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(403, json={"error": "CLIENT_IP_NOT_ALLOWED"}))
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert "super-secret-access-key-value" not in response.text


def test_access_key_never_appears_in_log_output(client, db_session, monkeypatch, caplog) -> None:
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "super-secret-access-key-value")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(403, json={"error": "CLIENT_IP_NOT_ALLOWED"}))
    token = _signup_admin(client, db_session)

    with caplog.at_level("DEBUG"):
        client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert "super-secret-access-key-value" not in caplog.text


def test_access_key_never_appears_on_network_failure(client, db_session, monkeypatch) -> None:
    """Confirms the exception path specifically - a raw httpx exception's
    own message can embed the request URL, which would include the
    credential if ever logged/returned via str(exc)."""
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "test-application-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "super-secret-access-key-value")

    def raise_connect_error(*args, **kwargs):
        raise httpx.ConnectError(
            "simulated failure for url containing accessKey=super-secret-access-key-value"
        )

    monkeypatch.setattr(httpx, "get", raise_connect_error)
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/diagnostics/rakuten", headers=_auth_headers(token))

    assert "super-secret-access-key-value" not in response.text
