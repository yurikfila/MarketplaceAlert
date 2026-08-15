"""Shared pytest fixtures.

Tests must never touch the developer's real local SQLite file (the
`DATABASE_URL` default, `marketplace_alert.db`) - every fixture here builds
its own throwaway engine bound to a `tmp_path` file that pytest deletes
automatically, and wires it in via FastAPI's dependency override rather
than mutating any global state.

Tests must also never send real Telegram messages, even though a real
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` may be present in the developer's
`.env` - the `client` fixture always overrides the notification provider
with `FakeNotificationProvider`, which only records what it was asked to
send.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationProvider
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.database import Base, create_db_engine, get_db_session
from marketplace_alert.main import app, get_notification_service
from marketplace_alert.main import _saved_search_run_guard as saved_search_run_guard


class FakeNotificationProvider(NotificationProvider):
    """Records alerts instead of sending them anywhere. Always enabled."""

    def __init__(self) -> None:
        self.sent_listings: list[Listing] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        self.sent_listings.append(listing)


@pytest.fixture()
def db_engine(tmp_path) -> Iterator[Engine]:
    """An isolated SQLite engine backed by a temp file, with tables created."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(db_engine: Engine) -> sessionmaker:
    """A sessionmaker bound to the isolated test engine - for code (like
    BackgroundScanner) that opens more than one session itself."""
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db_session(session_factory: sessionmaker) -> Iterator[Session]:
    """A session bound to the isolated test engine, for testing persistence directly."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake_notification_provider() -> FakeNotificationProvider:
    """The fake provider used by the `client` fixture; inspect `.sent_listings`."""
    return FakeNotificationProvider()


@pytest.fixture()
def client(db_engine: Engine, fake_notification_provider: FakeNotificationProvider) -> Iterator[TestClient]:
    """A TestClient whose /scan endpoint is wired to the isolated test engine
    and a fake notification provider - never the real database or Telegram."""
    session_factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def override_get_db_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_notification_service] = lambda: NotificationService(
        fake_notification_provider
    )
    # The manual /saved-searches/{id}/run overlap guard is a module-level
    # singleton (main.py), not request-scoped - reset it so a test that
    # exercises the "already running" 409 case can't leak state into a
    # later test whose isolated DB happens to reuse the same saved-search id.
    saved_search_run_guard.reset()
    try:
        yield TestClient(app)
    finally:
        saved_search_run_guard.reset()
        app.dependency_overrides.clear()
