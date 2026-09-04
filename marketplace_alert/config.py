"""Application configuration, loaded from environment variables / .env."""

import secrets
import threading

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Minimum accepted length for an explicitly-provided JWT_SECRET_KEY - not a
# real entropy measurement, just a cheap guard against an obviously-weak
# placeholder value ("changeme", "secret", ...) ever being accepted. 32
# matches PyJWT's own documented minimum recommended HMAC key length for
# HS256 (RFC 7518 §3.2) - confirmed directly: PyJWT emits its own
# `InsecureKeyLengthWarning` below this length.
_MIN_JWT_SECRET_LENGTH = 32

# Module-level, not per-`Settings`-instance: exactly one ephemeral secret
# per Python process, generated lazily on first use (see
# `_get_or_create_ephemeral_jwt_secret_key`), never one per `Settings()`
# call. A token signed with one instance's secret must still verify
# against another instance's secret within the same process - this is what
# makes that true.
_ephemeral_jwt_secret_key: str | None = None
_ephemeral_jwt_secret_key_lock = threading.Lock()


def _get_or_create_ephemeral_jwt_secret_key() -> str:
    """Return this process's ephemeral JWT secret, creating it on first call.

    Double-checked locking: the lock is only ever taken on the first call
    (when the module-level cache is still `None`) - every call after that
    hits the fast, lock-free path. Never persisted anywhere (not written to
    disk, `.env`, or the environment) - it exists only as this module-level
    Python value for the lifetime of the process, and a fresh one is
    generated the next time the process starts.
    """
    global _ephemeral_jwt_secret_key
    if _ephemeral_jwt_secret_key is None:
        with _ephemeral_jwt_secret_key_lock:
            if _ephemeral_jwt_secret_key is None:
                _ephemeral_jwt_secret_key = secrets.token_urlsafe(64)
    return _ephemeral_jwt_secret_key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Marketplace Alert"
    environment: str = "development"
    log_level: str = "INFO"

    # Used for local persistence and duplicate detection (Phase 3), and for
    # PostgreSQL support in production (Phase 8). Unset (None) -> the app
    # falls back to the local SQLite default; set -> that URL is used as-is
    # (Render's PostgreSQL URLs, both `postgres://` and `postgresql://`
    # forms, are normalized automatically - see
    # `core/persistence/database.py:resolve_database_url`, the single place
    # this decision is made). Never hard-code a real value here - this is
    # read from the environment / `.env` only.
    database_url: str | None = None

    # Telegram notifications for newly discovered listings (Phase 4).
    # Optional - if either is unset, notifications are disabled and the app
    # still starts normally. Never hard-code real values here.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Robust delivery under bursts (many new listings discovered in one
    # scan) - a fixed pacing delay between sends plus bounded retries with
    # backoff for transient failures. Defaults are safe out of the box:
    # ~1 msg/sec is Telegram's own documented guideline for messages to the
    # same chat, and 3 retries is enough to ride out a brief rate-limit or
    # 5xx blip without retrying forever. See
    # `marketplace_alert/notifications/telegram/provider.py` and
    # `marketplace_alert/core/notifications/service.py`.
    telegram_send_delay_seconds: float = 1.0
    telegram_max_retries: int = 3
    telegram_retry_base_seconds: float = 2.0

    # Notification outbox (`core/persistence/models.py:PendingNotification`,
    # `core/notifications/outbox.py`) - decouples scanning/persistence from
    # Telegram delivery entirely (see PROJECT_CONTEXT.md's outbox decision).
    # `notification_outbox_batch_size`: how many pending/reclaimed rows one
    # drain run claims at a time. `notification_lease_seconds`: how long a
    # claimed (`processing`) row is considered "someone is actively
    # delivering this" before a later drain run treats it as an abandoned/
    # crashed claim and reclaims it - comfortably longer than a Telegram
    # send's own worst-case duration (up to `telegram_max_retries` retries
    # with backoff), short enough that a genuine crash doesn't leave a
    # notification stuck for long. `notification_max_attempts`: how many
    # times total (first attempt + reclaims) a row is retried before it's
    # marked permanently `failed` rather than retried forever.
    notification_outbox_batch_size: int = 20
    notification_lease_seconds: float = 120.0
    notification_max_attempts: int = 10

    # A "Case A" no-destination row (owner resolved, but no Telegram
    # destination configured yet - see `core/persistence/models.py:
    # NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG` and
    # `core/notifications/outbox.py`'s "SECURITY RULE" section) is never
    # counted against `notification_max_attempts`, so without a separate
    # throttle it would otherwise be reclaimed on every single drain
    # cycle forever. This is that throttle: such a row is only eligible
    # for reclaim once its `last_attempted_at` is at least this many
    # seconds old. 900s (15 minutes) is comfortably longer than any
    # plausible drain cadence (so it actually stops the hot-reclaim
    # loop) while still being short enough that a user who configures
    # Telegram shortly after signing up sees their backlog delivered
    # promptly, not after a multi-hour wait.
    notification_no_destination_retry_seconds: float = 900.0

    # How often the background scanner checks for due saved searches
    # (polling granularity, not a per-search interval - see
    # core/saved_searches/schemas.py for the per-search minimum).
    scheduler_tick_seconds: float = 5.0

    # Whether the Web Service process runs the saved-search scanner
    # in-process (a background thread, started in `main.py`'s `lifespan`)
    # or leaves scanning entirely to an external trigger (a Render Cron
    # Job running `scripts/run_due_scans.py` on a schedule). Defaults to
    # `True` (today's behavior, and what every existing test/local-dev
    # setup already assumes) - production sets this to `False` once the
    # Cron Job is confirmed working, so the Web Service becomes API/
    # dashboard/mobile-only. See PROJECT_CONTEXT.md's Render-reliability
    # decision for why: a Render Free Web Service can be suspended during
    # idle periods, which an in-process background thread cannot survive.
    run_scanner_in_process: bool = True

    # Gates the entire legacy, unauthenticated surface: the server-rendered
    # dashboard (`GET /`, `GET /listings`), its backing `/saved-searches*`
    # CRUD/run routes (`main.py` - `/static/dashboard.js` is the only thing
    # that calls these), and the temporary mock-only `/search`/`/scan`
    # endpoints. Secure by default: `False`, so a fresh deploy/environment
    # never accidentally exposes this fully unauthenticated bypass around
    # /api/v1's ownership enforcement (no ownership scoping, one operator-
    # facing tool, never meant to be ownership-aware - see ARCHITECTURE.md
    # "The management dashboard"). Set to `True` explicitly (env var, or a
    # test's own isolated settings) only where the legacy dashboard is
    # actually wanted - e.g. a local/dev environment. `GET /health` and
    # every other harmless global-metadata route (`/docs`, `/openapi.json`,
    # `/api/v1/status`, `/api/v1/marketplaces`) are never gated by this flag.
    legacy_routes_enabled: bool = False

    # Automatic PostgreSQL migrations at app startup
    # (core/persistence/migrations.py) - added because Render's Free plan
    # has no Pre-Deploy Command to run `alembic upgrade head` as a
    # separate step. How long to wait to acquire the Postgres advisory
    # lock that guards a migration run before giving up and failing
    # startup, rather than waiting forever for a stuck/crashed prior
    # instance to release it. Irrelevant for local SQLite - that backend
    # never takes this lock at all.
    migration_lock_timeout_seconds: float = 30.0

    # Etsy Open API v3 credentials (Phase 2 - first real connector).
    # Optional - if either is unset, the Etsy connector reports a clear
    # configuration error when actually used, but the app still starts
    # normally and other connectors are unaffected. Never hard-code real
    # values here. Both are required: Etsy's x-api-key header is
    # "<ETSY_API_KEY>:<ETSY_SHARED_SECRET>", not the API key alone.
    etsy_api_key: str | None = None
    etsy_shared_secret: str | None = None

    # Safe, configurable page size for Etsy searches (Etsy's own max is
    # 100; this connector fetches one page, not all of them, for now).
    etsy_result_limit: int = 25

    # eBay Buy Browse API credentials (Phase 2 - second real connector).
    # Optional - if either is unset, the eBay connector reports a clear
    # configuration error when actually used, but the app still starts
    # normally and other connectors are unaffected. Never hard-code real
    # values here. EBAY_DEV_ID is not used - the Browse API's client_credentials
    # OAuth flow only needs the App ID (client_id) and Cert ID (client_secret).
    ebay_app_id: str | None = None
    ebay_cert_id: str | None = None

    # Safe, configurable page size for eBay searches (eBay's own max is
    # 200; this connector fetches one page, not all of them, for now).
    ebay_result_limit: int = 25

    # Reverb API v3 (musical instruments/gear marketplace) - a single
    # static personal access token (Authorization: Bearer <token>), not a
    # client id/secret pair or OAuth flow. Optional - if unset, the
    # Reverb connector reports a clear configuration error when actually
    # used, but the app still starts normally and other connectors are
    # unaffected. Never hard-code a real value here.
    reverb_api_token: str | None = None

    # Safe, configurable result limit for Reverb searches - unlike Etsy/
    # eBay, this connector does paginate (via Reverb's own `_links.next`),
    # bounded by both this limit and an internal max-pages safety cap
    # (see connectors/reverb/connector.py).
    reverb_result_limit: int = 25

    # Bonanza's "Bonapitit" API - a single developer name (Bonanza's own
    # X-BONANZLE-API-DEV-NAME header), not a secret token or client
    # id/secret pair. Optional - if unset, the Bonanza connector reports a
    # clear configuration error when actually used, but the app still
    # starts normally and other connectors are unaffected. Never
    # hard-code a real value here.
    bonanza_dev_name: str | None = None

    # Safe, configurable result limit for Bonanza searches - paginates via
    # pageNumber (bounded by both this limit and an internal max-pages
    # safety cap - see connectors/bonanza/connector.py).
    bonanza_result_limit: int = 25

    # Historical listing metadata backfill
    # (core/persistence/backfill.py, scripts/backfill_listing_metadata.py) -
    # re-fetches individual pre-existing listings from their source
    # marketplace (by id, via each connector's `get_listing_by_id()`) to
    # fill in fields that were missing before rich metadata persistence
    # existed (see PROJECT_CONTEXT.md). Deliberately conservative
    # defaults - this is a slow, rate-limit-aware maintenance task run
    # manually via the CLI script, never automatically, and never a bulk
    # operation: `listing_backfill_batch_size` bounds how many rows one
    # invocation processes; `listing_backfill_delay_ms` paces the gap
    # between consecutive marketplace requests within a run.
    listing_backfill_batch_size: int = 25
    listing_backfill_delay_ms: int = 500

    # CORS for future web/dev tooling (Phase 9 mobile-API prep). Native
    # mobile apps don't need browser CORS at all - this exists only in
    # case future web-based tooling (e.g. an Expo/React Native web
    # preview) needs to call this API from a browser. Comma-separated
    # list of allowed origins, e.g. "http://localhost:3000,https://app.example.com".
    # Empty/unset by default - no cross-origin browser access is allowed
    # until explicitly configured. Never set this to "*" - see main.py.
    cors_allowed_origins: list[str] = []

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_allowed_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # --- Authentication (JWT access token + rotated refresh token) ---
    #
    # `jwt_secret_key` signs/verifies access tokens (HS256 - one backend
    # both issues and verifies tokens, so there's no reason for asymmetric
    # keys). Deliberately left unset here, with no literal default value at
    # all: `_require_or_generate_jwt_secret_key` below is the one place
    # that decides what happens when it's missing, and the decision is not
    # "fall back to something" - see that validator's docstring. Never log
    # this value, and never accept a hard-coded value here or anywhere else
    # - only from the environment.
    jwt_secret_key: str | None = None

    # Access tokens are short-lived by design - minimizes the exposure
    # window if one leaks. A client silently exchanges an expired one for a
    # new one via its refresh token rather than being logged out.
    access_token_expire_minutes: int = 30

    # Refresh tokens are long-lived but revocable (stored hashed, rotated on
    # every use - not implemented until a later phase, this is just the TTL
    # config for it). 30 days balances "doesn't force a mobile user to
    # re-login constantly" against "a lost/forgotten device's token doesn't
    # stay valid forever."
    refresh_token_expire_days: int = 30

    # Password-reset tokens are short-lived and single-use - long enough for
    # someone to receive and act on a reset link, short enough that a
    # leaked link (e.g. via a proxy log) stops being useful quickly. Email
    # delivery itself is a separate, later phase (this backend cannot send
    # email yet) - this setting exists now so the token/TTL model can be
    # built and tested ahead of that, per the approved authentication
    # design.
    password_reset_token_expire_minutes: int = 30

    # Brute-force login protection (Phase 2 - `core/auth/service.py`,
    # `User.failed_login_attempts`/`locked_until`). After this many
    # consecutive wrong-password attempts against one account, further
    # login/refresh attempts are rejected outright (password correctness
    # never even checked) until the lockout window passes - a DB-backed
    # counter, no new infrastructure (e.g. Redis) needed at this scale.
    # Deliberately per-account only, not per-IP, in this phase - see
    # AuthService's module docstring for that tradeoff.
    max_failed_login_attempts: int = 5
    account_lockout_minutes: float = 15.0

    @model_validator(mode="after")
    def _require_or_generate_jwt_secret_key(self) -> "Settings":
        """Decide what a missing `JWT_SECRET_KEY` means - and never guess.

        Gated on `database_url`, not `environment`: this codebase already
        uses "is `DATABASE_URL` set?" as its one existing, reliable signal
        for "is this a real deployment, not local SQLite dev?" (see
        `core/persistence/database.py`'s docstring) - production
        (including Render) always has it set, so reusing that signal means
        this check is correctly strict there without depending on a second,
        easy-to-forget variable (`ENVIRONMENT`) also being set correctly.
        `environment` itself stays purely descriptive/log-only, unchanged.

        - Provided and long enough: used as-is.
        - Provided but too short: rejected outright, in every environment -
          a deliberately weak or placeholder value is exactly the "insecure
          default" this exists to prevent, and there's no legitimate reason
          a real secret would ever be this short.
        - Missing, and `DATABASE_URL` is set (any real deployment): fails
          fast at startup with a clear error, the same "don't start in a
          broken/insecure state" philosophy already used for the
          migration-lock startup check. There is no insecure fallback here
          - this is the whole point of this validator.
        - Missing, and `DATABASE_URL` is unset (local SQLite dev, or the
          test suite): every `Settings` instance in this process shares one
          ephemeral secret (`_get_or_create_ephemeral_jwt_secret_key` -
          generated once per process, not once per instance, so a token
          signed via one instance still verifies via another), so no
          `.env` entry is required to run the app or the tests locally. It
          is never persisted and a fresh one is generated the next time the
          process starts - sessions simply don't survive a restart, which
          is an acceptable, expected local-dev tradeoff, never an
          acceptable production one.
        """
        if self.jwt_secret_key:
            if len(self.jwt_secret_key) < _MIN_JWT_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET_KEY must be at least {_MIN_JWT_SECRET_LENGTH} characters. "
                    'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
                )
            return self

        if self.database_url:
            raise ValueError(
                "JWT_SECRET_KEY is required whenever DATABASE_URL is set (i.e. any real "
                "deployment, including production) - there is no insecure default. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )

        self.jwt_secret_key = _get_or_create_ephemeral_jwt_secret_key()
        return self


settings = Settings()
