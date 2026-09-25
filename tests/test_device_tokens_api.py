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

    assert response.status_code == 200
    assert response.json() == {"platform": "android", "notification_channel_id": None}
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
    """The response body (`DeviceRegisterResponse`) is deliberately
    minimal - `platform`/`notification_channel_id` only, never the
    `expo_push_token` itself, the row id, or any timestamp."""
    token = _access_token(client)

    response = client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[secret-ish]"}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"platform", "notification_channel_id"}
    assert "ExponentPushToken[secret-ish]" not in response.text


# =====================================================================
# Notification sound selection (Phase 1: backend + database)
# =====================================================================


def test_new_device_registration_with_no_channel_id_succeeds_with_a_null_column(client, db_session) -> None:
    token = _access_token(client)

    response = client.post(
        "/api/v1/devices", json={"expo_push_token": "ExponentPushToken[no-sound-yet]"}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    assert response.json()["notification_channel_id"] is None
    row = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[no-sound-yet]").one()
    assert row.notification_channel_id is None


def test_registering_with_a_valid_explicit_channel_id_persists_it(client, db_session) -> None:
    token = _access_token(client)

    response = client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[radar]", "notification_channel_id": "listing-alerts-radar-v1"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["notification_channel_id"] == "listing-alerts-radar-v1"
    row = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[radar]").one()
    assert row.notification_channel_id == "listing-alerts-radar-v1"


def test_registering_with_an_invalid_channel_id_returns_422(client) -> None:
    token = _access_token(client)

    response = client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[bad-sound]", "notification_channel_id": "not-a-real-channel"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 422


def test_normal_startup_reregistration_with_channel_id_omitted_does_not_erase_an_existing_preference(
    client, db_session
) -> None:
    """The critical persistence-safety case: the mobile app's automatic
    startup registration never sends `notification_channel_id` at all -
    re-registering the same token that way must never reset an already-
    chosen sound preference back to NULL."""
    token = _access_token(client)
    client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[keep-radar]", "notification_channel_id": "listing-alerts-radar-v1"},
        headers=_auth_headers(token),
    )

    response = client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[keep-radar]", "platform": "android"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["notification_channel_id"] == "listing-alerts-radar-v1"
    row = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[keep-radar]").one()
    assert row.notification_channel_id == "listing-alerts-radar-v1"


def test_explicit_system_default_overwrites_a_previously_chosen_preference(client, db_session) -> None:
    token = _access_token(client)
    client.post(
        "/api/v1/devices",
        json={"expo_push_token": "ExponentPushToken[back-to-default]", "notification_channel_id": "listing-alerts-radar-v1"},
        headers=_auth_headers(token),
    )

    response = client.post(
        "/api/v1/devices",
        json={
            "expo_push_token": "ExponentPushToken[back-to-default]",
            "notification_channel_id": "listing-alerts-default-v1",
        },
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["notification_channel_id"] == "listing-alerts-default-v1"
    row = db_session.query(DeviceToken).filter_by(expo_push_token="ExponentPushToken[back-to-default]").one()
    assert row.notification_channel_id == "listing-alerts-default-v1"
