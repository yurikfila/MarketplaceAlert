"""OAuth client_credentials token management for the eBay Buy Browse API.

Verified before implementation against eBay's own official sources, not
guessed:
  - the OAuth client credentials grant guide
    (https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html),
    confirming the token endpoint, the `Authorization: Basic
    <base64(client_id:client_secret)>` header, and the
    `grant_type=client_credentials&scope=...` form-encoded body;
  - manually verified by the developer beforehand: the exact same request
    shape (App ID as client_id, Cert ID as client_secret, this token URL)
    already returns HTTP 200 with a real application access token.

Endpoint: ``POST https://api.ebay.com/identity/v1/oauth2/token``. Scope:
``https://api.ebay.com/oauth/api_scope`` - the base scope, sufficient for
public, read-only Browse API search; no user OAuth login is involved, this
is the application (client_credentials), not user, grant.

An Application Access Token is valid for `expires_in` seconds (eBay
currently issues these for 7200s / 2 hours) and has no refresh token -
getting a new one just means repeating this same request. This module
exists so that repeating it happens only when actually needed (no cached
token yet, or the cached one is about to expire) - never once per search.
"""

import base64
import logging
import threading
import time
from typing import Any

import httpx

from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.connectors.metrics import OAUTH_LABEL, record_request

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Refresh a little before actual expiry so a request already in flight
# never races the token going stale mid-call.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60


class EbayTokenManager:
    """Fetches and caches an eBay Application Access Token (client_credentials).

    One token is shared across every search - a new one is only requested
    when none is cached yet or the cached one is within
    `_EXPIRY_SAFETY_MARGIN_SECONDS` of expiring. The token value itself is
    never logged, persisted, or exposed to callers beyond `get_token()`'s
    return value - not even at DEBUG level.
    """

    def __init__(self, app_id: str | None, cert_id: str | None, timeout: float = 10.0) -> None:
        self._app_id = app_id
        self._cert_id = cert_id
        self._timeout = timeout
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        # One manager is now shared by every eBay connector in the process (see
        # `get_shared_token_manager`), possibly across threads (the in-process
        # scanner and web requests), so refresh/invalidate must not interleave.
        self._lock = threading.RLock()

    @property
    def is_configured(self) -> bool:
        return bool(self._app_id) and bool(self._cert_id)

    def get_token(self) -> str:
        """Return a currently-valid access token, fetching/refreshing one if needed."""
        if not self.is_configured:
            raise MarketplaceConnectorError(
                "eBay connector is not configured: EBAY_APP_ID and/or EBAY_CERT_ID are not set"
            )
        with self._lock:
            if self._access_token is None or time.monotonic() >= self._expires_at:
                self._refresh()
            assert self._access_token is not None  # _refresh() always sets it or raises
            return self._access_token

    def invalidate(self) -> None:
        """Drop the cached token, forcing a fresh one on the next `get_token()` call.

        Used after a search request comes back 401/403, in case the cached
        token was revoked or otherwise went bad before its tracked expiry.
        """
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def _refresh(self) -> None:
        credentials = base64.b64encode(f"{self._app_id}:{self._cert_id}".encode()).decode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        }
        data = {"grant_type": "client_credentials", "scope": _SCOPE}

        try:
            response = httpx.post(_TOKEN_URL, headers=headers, data=data, timeout=self._timeout)
        except httpx.HTTPError as exc:
            record_request(OAUTH_LABEL, None)
            logger.error("eBay OAuth token request failed (%s)", type(exc).__name__)
            raise MarketplaceConnectorError("eBay OAuth token request failed") from None
        record_request(OAUTH_LABEL, response.status_code)

        if response.status_code != 200:
            logger.error("eBay OAuth token request returned HTTP %s", response.status_code)
            raise MarketplaceConnectorError(
                f"eBay OAuth token request returned HTTP {response.status_code}"
            )

        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            logger.error("eBay OAuth token response was not JSON")
            raise MarketplaceConnectorError("eBay OAuth token response was malformed") from None

        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, (int, float)):
            logger.error("eBay OAuth token response was missing access_token/expires_in")
            raise MarketplaceConnectorError("eBay OAuth token response was malformed")

        self._access_token = access_token
        self._expires_at = time.monotonic() + expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS
        logger.info("eBay OAuth application access token acquired (expires_in=%s seconds)", expires_in)


_shared_managers: dict[tuple[str, str, float], EbayTokenManager] = {}
_shared_managers_lock = threading.Lock()


def get_shared_token_manager(app_id: str | None, cert_id: str | None, timeout: float = 10.0) -> EbayTokenManager:
    """The process-wide token manager for exactly these credentials.

    `registry.get_connector` builds a fresh connector for every search, and each used to
    own a private `EbayTokenManager` - so every single eBay search first requested a brand
    new OAuth token. Connectors now share one manager per (app id, cert id, timeout), so a
    token is fetched once and reused until it is within the safety margin of expiring.
    Different credentials never share a manager (and therefore never share a token).
    Unconfigured credentials get an unshared manager that never makes a request.
    """
    if not app_id or not cert_id:
        return EbayTokenManager(app_id=app_id, cert_id=cert_id, timeout=timeout)
    key = (app_id, cert_id, timeout)
    with _shared_managers_lock:
        manager = _shared_managers.get(key)
        if manager is None:
            manager = EbayTokenManager(app_id=app_id, cert_id=cert_id, timeout=timeout)
            _shared_managers[key] = manager
        return manager


def reset_shared_token_managers() -> None:
    """Forget every shared manager (and its cached token). For tests and credential rotation."""
    with _shared_managers_lock:
        _shared_managers.clear()
