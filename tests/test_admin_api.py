"""HTTP tests for `/api/v1/admin/*` (`marketplace_alert/api/v1/admin.py`).

Uses the same `client`/`db_session` fixtures as every other API test
(`tests/conftest.py`) - an isolated temp database, never the developer's
real one. Covers the full authorization ladder (unauthenticated -> 401,
authenticated non-admin -> 403, admin -> 200), safe serialization (no
secret ever reaches a response body), and the aggregate/per-user counts
this API reports.
"""

from marketplace_alert.core.auth.models import User


def _signup(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": password})


def _promote_to_admin(db_session, email: str) -> None:
    user = db_session.query(User).filter_by(email=email.lower()).one()
    user.is_admin = True
    db_session.commit()


def _signup_admin(client, db_session, email="admin@example.com", password="a-strong-password") -> str:
    """Signs up a fresh account, promotes it to admin directly at the
    database layer (never through any API - see `admin_promotion.py`'s
    own module docstring), and returns its access token."""
    body = _signup(client, email=email, password=password).json()
    _promote_to_admin(db_session, email)
    return body["tokens"]["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# =====================================================================
# Authorization ladder
# =====================================================================


def test_list_users_without_authorization_header_returns_401(client) -> None:
    response = client.get("/api/v1/admin/users")
    assert response.status_code == 401


def test_stats_without_authorization_header_returns_401(client) -> None:
    response = client.get("/api/v1/admin/stats")
    assert response.status_code == 401


def test_list_users_with_a_normal_users_token_returns_403(client) -> None:
    body = _signup(client).json()
    token = body["tokens"]["access_token"]

    response = client.get("/api/v1/admin/users", headers=_auth_headers(token))

    assert response.status_code == 403


def test_stats_with_a_normal_users_token_returns_403(client) -> None:
    body = _signup(client).json()
    token = body["tokens"]["access_token"]

    response = client.get("/api/v1/admin/stats", headers=_auth_headers(token))

    assert response.status_code == 403


def test_list_users_with_an_admin_token_returns_200(client, db_session) -> None:
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/admin/users", headers=_auth_headers(token))

    assert response.status_code == 200


def test_stats_with_an_admin_token_returns_200(client, db_session) -> None:
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/admin/stats", headers=_auth_headers(token))

    assert response.status_code == 200


def test_list_users_rejects_a_tampered_token_with_401_not_403(client) -> None:
    """A malformed/tampered bearer token must fail authentication (401),
    never be treated as "authenticated but not admin" (403) - the two
    failure modes must never be conflated."""
    response = client.get("/api/v1/admin/users", headers=_auth_headers("not-a-real-token"))
    assert response.status_code == 401


# =====================================================================
# Safe serialization - the core security guarantee of this API
# =====================================================================


def test_admin_users_response_contains_only_approved_fields(client, db_session) -> None:
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/admin/users", headers=_auth_headers(token))
    body = response.json()

    assert set(body.keys()) == {"total_users", "users"}
    for user in body["users"]:
        assert set(user.keys()) == {
            "id",
            "email",
            "created_at",
            "is_admin",
            "is_active",
            "saved_search_count",
        }


def test_admin_users_response_never_contains_a_password_hash_or_token(client, db_session) -> None:
    token = _signup_admin(client, db_session)
    _signup(client, email="other-user@example.com")

    response = client.get("/api/v1/admin/users", headers=_auth_headers(token))
    flattened = str(response.json()).lower()

    for forbidden in ("password_hash", "token_hash", "failed_login_attempts", "locked_until"):
        assert forbidden not in flattened


def test_admin_stats_response_contains_only_approved_fields(client, db_session) -> None:
    token = _signup_admin(client, db_session)

    response = client.get("/api/v1/admin/stats", headers=_auth_headers(token))

    assert set(response.json().keys()) == {"total_users", "total_saved_searches"}


# =====================================================================
# Counts
# =====================================================================


def test_total_users_matches_the_number_of_signed_up_accounts(client, db_session) -> None:
    token = _signup_admin(client, db_session)  # the admin itself
    _signup(client, email="second@example.com")
    _signup(client, email="third@example.com")

    response = client.get("/api/v1/admin/users", headers=_auth_headers(token))
    body = response.json()

    assert body["total_users"] == 3
    assert len(body["users"]) == 3

    stats = client.get("/api/v1/admin/stats", headers=_auth_headers(token)).json()
    assert stats["total_users"] == 3


def test_admin_users_list_reports_each_users_own_saved_search_count(client, db_session) -> None:
    token = _signup_admin(client, db_session, email="admin@example.com")
    other_body = _signup(client, email="searcher@example.com").json()
    other_user_id = other_body["user"]["id"]

    for i in range(3):
        client.post(
            "/api/v1/saved-searches",
            json={"query": f"item {i}", "marketplaces": ["etsy"], "scan_interval_seconds": 300, "is_active": True},
            headers=_auth_headers(other_body["tokens"]["access_token"]),
        )

    response = client.get("/api/v1/admin/users", headers=_auth_headers(token))
    users_by_id = {user["id"]: user for user in response.json()["users"]}

    assert users_by_id[other_user_id]["saved_search_count"] == 3
    assert users_by_id[other_user_id]["is_admin"] is False


def test_total_saved_searches_counts_across_all_users(client, db_session) -> None:
    token = _signup_admin(client, db_session, email="admin@example.com")
    other_body = _signup(client, email="searcher@example.com").json()
    client.post(
        "/api/v1/saved-searches",
        json={"query": "widget", "marketplaces": ["etsy"], "scan_interval_seconds": 300, "is_active": True},
        headers=_auth_headers(other_body["tokens"]["access_token"]),
    )

    stats = client.get("/api/v1/admin/stats", headers=_auth_headers(token)).json()

    assert stats["total_saved_searches"] == 1


# =====================================================================
# is_admin cannot be self-granted, and existing auth behavior is intact
# =====================================================================


def test_new_signup_defaults_to_non_admin_and_cannot_reach_admin_api(client) -> None:
    body = _signup(client, email="new-account@example.com").json()

    assert body["user"]["is_admin"] is False

    response = client.get("/api/v1/admin/users", headers=_auth_headers(body["tokens"]["access_token"]))
    assert response.status_code == 403


def test_client_supplied_is_admin_header_is_ignored(client, db_session) -> None:
    """There is no header, query param, or request body shape that can
    grant admin access - only the database row `require_admin` reads
    server-side matters."""
    body = _signup(client, email="spoofer@example.com").json()
    token = body["tokens"]["access_token"]

    response = client.get(
        "/api/v1/admin/users",
        headers={**_auth_headers(token), "X-Is-Admin": "true", "X-Admin": "1"},
    )

    assert response.status_code == 403


def test_existing_login_and_me_behavior_is_unaffected_by_the_admin_api(client) -> None:
    """Regression guard: adding the admin API must not change unrelated
    auth behavior at all."""
    signup_body = _signup(client).json()
    access_token = signup_body["tokens"]["access_token"]

    me_response = client.get("/api/v1/auth/me", headers=_auth_headers(access_token))
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "person@example.com"

    login_response = client.post(
        "/api/v1/auth/login", json={"email": "person@example.com", "password": "a-strong-password"}
    )
    assert login_response.status_code == 200
