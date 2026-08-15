import pytest

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications import service as notification_service_module
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.service import NotificationService


def _listing(external_id: str = "mock-001") -> Listing:
    return Listing(
        marketplace="mock",
        external_listing_id=external_id,
        title="Maccabi Vintage Shirt",
        listing_url=f"https://mock-marketplace.example.com/listing/{external_id}",
    )


class RecordingProvider(NotificationProvider):
    """A fake provider used only to test NotificationService in isolation."""

    def __init__(self, enabled: bool = True) -> None:
        self.sent: list[Listing] = []
        self._enabled = enabled

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def send_listing_alert(self, listing: Listing) -> None:
        self.sent.append(listing)


class AlwaysFailingProvider(NotificationProvider):
    """A fake provider that always fails, to test that failures don't propagate."""

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        raise NotificationError("simulated failure")


class SometimesFailingProvider(NotificationProvider):
    """Fails for specific listing IDs only, succeeds for the rest."""

    def __init__(self, failing_ids: set[str]) -> None:
        self.sent: list[Listing] = []
        self._failing_ids = failing_ids

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        if listing.external_listing_id in self._failing_ids:
            raise NotificationError("simulated failure")
        self.sent.append(listing)


def test_notify_sends_alert_for_each_new_listing() -> None:
    provider = RecordingProvider()
    NotificationService(provider).notify_new_listings([_listing("mock-001"), _listing("mock-002")])
    assert len(provider.sent) == 2


def test_notify_does_nothing_when_no_new_listings() -> None:
    provider = RecordingProvider()
    NotificationService(provider).notify_new_listings([])
    assert provider.sent == []


def test_notify_skips_silently_when_provider_disabled() -> None:
    provider = RecordingProvider(enabled=False)
    NotificationService(provider).notify_new_listings([_listing()])
    assert provider.sent == []


def test_notify_does_not_raise_when_provider_fails() -> None:
    service = NotificationService(AlwaysFailingProvider())
    # Must not raise - one failed notification must never crash the caller (a scan).
    service.notify_new_listings([_listing()])


def test_one_failed_notification_does_not_stop_delivery_for_other_listings() -> None:
    provider = SometimesFailingProvider(failing_ids={"mock-002"})
    listings = [_listing("mock-001"), _listing("mock-002"), _listing("mock-003")]

    NotificationService(provider).notify_new_listings(listings)

    assert {listing.external_listing_id for listing in provider.sent} == {"mock-001", "mock-003"}


# --- rate-controlled delivery under bursts ---------------------------------


def test_burst_of_listings_are_paced_between_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(notification_service_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    provider = RecordingProvider()
    listings = [_listing(f"mock-{i:03d}") for i in range(5)]

    NotificationService(provider, send_delay_seconds=0.75).notify_new_listings(listings)

    assert len(provider.sent) == 5
    # 5 listings -> 4 gaps between sends, never a delay before the first one.
    assert sleep_calls == [0.75, 0.75, 0.75, 0.75]


def test_single_new_listing_is_never_delayed(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(notification_service_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    NotificationService(RecordingProvider(), send_delay_seconds=2.0).notify_new_listings([_listing()])

    assert sleep_calls == []


def test_zero_delay_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(notification_service_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    listings = [_listing(f"mock-{i:03d}") for i in range(5)]
    NotificationService(RecordingProvider(), send_delay_seconds=0.0).notify_new_listings(listings)

    assert sleep_calls == []


def test_negative_delay_is_clamped_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(notification_service_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    listings = [_listing("a"), _listing("b")]
    NotificationService(RecordingProvider(), send_delay_seconds=-5.0).notify_new_listings(listings)

    assert sleep_calls == []


def test_sends_preserve_listing_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notification_service_module.time, "sleep", lambda seconds: None)

    provider = RecordingProvider()
    listings = [_listing(f"mock-{i:03d}") for i in range(10)]

    NotificationService(provider, send_delay_seconds=0.01).notify_new_listings(listings)

    assert [listing.external_listing_id for listing in provider.sent] == [
        listing.external_listing_id for listing in listings
    ]
