"""Application configuration, loaded from environment variables / .env."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # How often the background scanner checks for due saved searches
    # (polling granularity, not a per-search interval - see
    # core/saved_searches/schemas.py for the per-search minimum).
    scheduler_tick_seconds: float = 5.0

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


settings = Settings()
