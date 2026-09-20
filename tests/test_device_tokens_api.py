"""HTTP tests for `/api/v1/devices` (`marketplace_alert/api/v1/devices.py`).

Uses the same `client`/`db_session` fixtures as every other API test
(`tests/conftest.py`). Covers ownership scoping (a token always belongs
to the authenticated caller, never a request-supplied user id),
idempotent re-registration, ownership transfer on re-registration under
a different user, and that a user can never unregister someone else's
device.
"""

from marketplace_alert.core.notifications.models import DeviceToken


def _signup(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": password})


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _access_token(client, email="person@example.com", password="a-strong-password") -> str:
    return _signup(client, email=email, password=password).json()["tokens"]["access_token"]


def test_register_device_without_authorization_header_returns_401(client) -> None:
    response = client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[abc]"})
    assert response.status_code == 401


def test_register_device_persists_a_row_owned_by_the_caller(client, db_session) -> None:
    token = _access_token(client)

    response = client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[abc]", "platform": "android"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 204
    row = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[abc]").one()
    user = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[abc]").one().user_id
    assert row.platform == "android"
    assert user is not None


def test_registering_the_same_token_twice_is_idempotent_not_a_duplicate(client, db_session) -> None:
    token = _access_token(client)

    client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[dup]"}, headers=_auth_headers(token))
    client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[dup]"}, headers=_auth_headers(token))

    rows = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[dup]").all()
    assert len(rows) == 1


def test_registering_an_existing_token_under_a_different_user_transfers_ownership(client, db_session) -> None:
    token_a = _access_token(client, email="user-a@example.com")
    token_b = _access_token(client, email="user-b@example.com")

    client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[shared]"}, headers=_auth_headers(token_a)
    )
    client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[shared]"}, headers=_auth_headers(token_b)
    )

    rows = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[shared]").all()
    assert len(rows) == 1

    me_b = client.get("/api/v1/auth/me", headers=_auth_headers(token_b)).json()
    assert rows[0].user_id == me_b["id"]


def test_unregister_device_without_authorization_header_returns_401(client) -> None:
    response = client.request(
        "DELETE", "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[abc]"}
    )
    assert response.status_code == 401


def test_unregister_device_removes_the_callers_own_token(client, db_session) -> None:
    token = _access_token(client)
    client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[gone]"}, headers=_auth_headers(token)
    )

    response = client.request(
        "DELETE",
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[gone]"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 204
    assert db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[gone]").one_or_none() is None


def test_unregister_is_idempotent_for_an_already_removed_token(client) -> None:
    token = _access_token(client)

    response = client.request(
        "DELETE",
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[never-registered]"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 204


def test_user_cannot_unregister_another_users_device(client, db_session) -> None:
    token_a = _access_token(client, email="owner@example.com")
    token_b = _access_token(client, email="attacker@example.com")
    client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[owned-by-a]"}, headers=_auth_headers(token_a)
    )

    response = client.request(
        "DELETE",
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[owned-by-a]"},
        headers=_auth_headers(token_b),
    )

    assert response.status_code == 204  # idempotent response, never reveals whether it existed
    assert db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[owned-by-a]").one_or_none() is not None


def test_device_registration_never_exposes_a_token_in_the_response_body(client) -> None:
    token = _access_token(client)

    response = client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[secret-ish]"}, headers=_auth_headers(token)
    )

    assert response.status_code == 204
    assert response.text == ""


# --- POST /api/v1/devices/test-push - temporary Phase 1 manual-verification
# diagnostic (see api/v1/devices.py:send_test_push's own docstring). Uses the
# `fake_expo_push_provider` fixture (tests/conftest.py) - never a real Expo
# push from this test file.


def test_test_push_without_authorization_header_returns_401(client) -> None:
    response = client.post("/api/v1/devices/test-push")
    assert response.status_code == 401


def test_test_push_with_no_registered_device_returns_not_sent_and_zero_count(client, fake_expo_push_provider) -> None:
    token = _access_token(client)

    response = client.post("/api/v1/devices/test-push", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json() == {"sent": False, "device_count": 0}
    assert fake_expo_push_provider.sent_listings == []


def test_test_push_sends_only_to_the_callers_own_tokens(client, fake_expo_push_provider) -> None:
    token_a = _access_token(client, email="owner-a@example.com")
    token_b = _access_token(client, email="owner-b@example.com")
    client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[a-1]"}, headers=_auth_headers(token_a))
    client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[a-2]"}, headers=_auth_headers(token_a))
    client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[b-1]"}, headers=_auth_headers(token_b))

    response = client.post("/api/v1/devices/test-push", headers=_auth_headers(token_b))

    assert response.status_code == 200
    assert response.json() == {"sent": True, "device_count": 1}
    # Exactly one send attempt - proves user A's two tokens were never
    # touched by user B's test-push call, not just that B's own succeeded.
    assert len(fake_expo_push_provider.sent_listings) == 1


def test_test_push_successful_provider_call_returns_sent_true(client, fake_expo_push_provider) -> None:
    token = _access_token(client)
    client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[abc]"}, headers=_auth_headers(token))

    response = client.post("/api/v1/devices/test-push", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json() == {"sent": True, "device_count": 1}
    assert len(fake_expo_push_provider.sent_listings) == 1
    sent = fake_expo_push_provider.sent_listings[0]
    assert sent.marketplace == "test"
    assert sent.external_listing_id == "test-push"


def test_test_push_never_exposes_a_token_in_the_response_body(client) -> None:
    token = _access_token(client)
    client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[do-not-leak-me]"},
        headers=_auth_headers(token),
    )

    response = client.post("/api/v1/devices/test-push", headers=_auth_headers(token))

    assert response.status_code == 200
    assert "ExponentPushToken" not in response.text
