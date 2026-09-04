"""HTTP-level tests for `GET`/`PUT /api/v1/notification-preferences/me` -
real signed-up users, real bearer tokens, never a dependency override that
would weaken auth for the test (matching the same convention
`test_ownership_enforcement_api.py` established for saved searches).
"""


def _signup(client, email: str, password: str = "a-strong-password") -> dict:
    response = client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201
    return response.json()


def _auth_headers(signup_body: dict) -> dict:
    return {"Authorization": f"Bearer {signup_body['tokens']['access_token']}"}


# =====================================================================
# Unauthenticated -> 401
# =====================================================================


def test_unauthenticated_get_returns_401(client) -> None:
    assert client.get("/api/v1/notification-preferences/me").status_code == 401


def test_unauthenticated_put_returns_401(client) -> None:
    response = client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "123456"})
    assert response.status_code == 401


# =====================================================================
# Authenticated: get / set my own preference
# =====================================================================


def test_get_with_no_preference_configured_returns_null(client) -> None:
    user = _signup(client, "fresh@example.com")

    response = client.get("/api/v1/notification-preferences/me", headers=_auth_headers(user))

    assert response.status_code == 200
    assert response.json() == {"telegram_chat_id": None}


def test_put_sets_the_chat_id_and_get_reflects_it(client) -> None:
    user = _signup(client, "setter@example.com")
    headers = _auth_headers(user)

    put_response = client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "123456"}, headers=headers
    )
    assert put_response.status_code == 200
    assert put_response.json() == {"telegram_chat_id": "123456"}

    get_response = client.get("/api/v1/notification-preferences/me", headers=headers)
    assert get_response.json() == {"telegram_chat_id": "123456"}


def test_put_again_updates_rather_than_duplicating(client) -> None:
    user = _signup(client, "updater@example.com")
    headers = _auth_headers(user)

    client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "111111"}, headers=headers)
    second = client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "222222"}, headers=headers)

    assert second.status_code == 200
    assert second.json() == {"telegram_chat_id": "222222"}
    assert client.get("/api/v1/notification-preferences/me", headers=headers).json() == {
        "telegram_chat_id": "222222"
    }


def test_put_null_clears_a_previously_set_chat_id(client) -> None:
    user = _signup(client, "clearer@example.com")
    headers = _auth_headers(user)

    client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "123456"}, headers=headers)
    cleared = client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": None}, headers=headers)

    assert cleared.status_code == 200
    assert cleared.json() == {"telegram_chat_id": None}


def test_put_accepts_a_negative_chat_id_for_a_group_chat(client) -> None:
    user = _signup(client, "group-chat@example.com")
    response = client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "-1001234567890"}, headers=_auth_headers(user)
    )
    assert response.status_code == 200
    assert response.json() == {"telegram_chat_id": "-1001234567890"}


# =====================================================================
# User A cannot read/change user B's preference
# =====================================================================


def test_user_a_never_sees_user_bs_preference(client) -> None:
    user_a = _signup(client, "a@example.com")
    user_b = _signup(client, "b@example.com")

    client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "111111"}, headers=_auth_headers(user_a))
    client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "222222"}, headers=_auth_headers(user_b))

    response_a = client.get("/api/v1/notification-preferences/me", headers=_auth_headers(user_a))
    response_b = client.get("/api/v1/notification-preferences/me", headers=_auth_headers(user_b))

    assert response_a.json() == {"telegram_chat_id": "111111"}
    assert response_b.json() == {"telegram_chat_id": "222222"}


def test_user_a_cannot_change_user_bs_preference_via_their_own_token(client) -> None:
    """There is no user id anywhere in the request - user A's PUT can only
    ever affect user A's own row, never B's, regardless of intent."""
    user_a = _signup(client, "attacker@example.com")
    user_b = _signup(client, "victim@example.com")

    client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "999999"}, headers=_auth_headers(user_b)
    )
    client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "666666"}, headers=_auth_headers(user_a)
    )

    b_after = client.get("/api/v1/notification-preferences/me", headers=_auth_headers(user_b))
    assert b_after.json() == {"telegram_chat_id": "999999"}  # untouched by A's own PUT


# =====================================================================
# Malformed input validation
# =====================================================================


def test_put_rejects_non_numeric_chat_id(client) -> None:
    user = _signup(client, "malformed@example.com")
    response = client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "not-a-chat-id"}, headers=_auth_headers(user)
    )
    assert response.status_code == 422


def test_put_rejects_a_chat_id_with_embedded_letters(client) -> None:
    user = _signup(client, "malformed2@example.com")
    response = client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "12ab34"}, headers=_auth_headers(user)
    )
    assert response.status_code == 422


def test_put_treats_a_blank_chat_id_as_clearing_it(client) -> None:
    """A blank string is a friendly equivalent to `null` (clear it), not
    a validation error - distinct from a non-numeric, genuinely malformed value."""
    user = _signup(client, "blank@example.com")
    client.put("/api/v1/notification-preferences/me", json={"telegram_chat_id": "123456"}, headers=_auth_headers(user))

    response = client.put(
        "/api/v1/notification-preferences/me", json={"telegram_chat_id": "   "}, headers=_auth_headers(user)
    )

    assert response.status_code == 200
    assert response.json() == {"telegram_chat_id": None}
