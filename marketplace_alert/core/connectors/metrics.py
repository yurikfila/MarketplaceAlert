"""Per-scan-pass counters of the HTTP requests marketplace connectors actually make.

Counts real HTTP *attempts* - a request that is retried after a 429/5xx counts
once per attempt - so the numbers can be compared directly with a marketplace's
daily quota. eBay's OAuth token requests are counted under their own label
(`ebay_oauth`) because that endpoint has a separate limit.

Only numbers are recorded: never a URL, query, header, token, credential or any
response content, so the summary is safe to log.

The collector is scoped with a `ContextVar`, so it follows the thread that runs a
scan pass; requests made outside any `collect_request_metrics()` block (e.g. a
manual "Run now" in a web worker) are simply not counted.
"""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

OAUTH_LABEL = "ebay_oauth"


class RequestMetrics:
    """Mutable counters for one scan pass."""

    def __init__(self) -> None:
        self.attempts: Counter[str] = Counter()
        self.http_429: Counter[str] = Counter()
        self.network_errors: Counter[str] = Counter()

    def record(self, label: str, status_code: int | None) -> None:
        label = label.lower()
        self.attempts[label] += 1
        if status_code is None:
            self.network_errors[label] += 1
        elif status_code == 429:
            self.http_429[label] += 1

    @property
    def total_attempts(self) -> int:
        return sum(self.attempts.values())

    @property
    def total_429(self) -> int:
        return sum(self.http_429.values())

    def summary(self) -> str:
        """e.g. `ebay=3 etsy=2 ebay_oauth=1 | http_429=1 network_errors=0 total=6` - counts only."""
        per_label = " ".join(f"{label}={count}" for label, count in sorted(self.attempts.items())) or "none"
        return (
            f"{per_label} | http_429={self.total_429} "
            f"network_errors={sum(self.network_errors.values())} total={self.total_attempts}"
        )


_active: ContextVar[RequestMetrics | None] = ContextVar("marketplace_request_metrics", default=None)


@contextmanager
def collect_request_metrics() -> Iterator[RequestMetrics]:
    metrics = RequestMetrics()
    token = _active.set(metrics)
    try:
        yield metrics
    finally:
        _active.reset(token)


def record_request(label: str, status_code: int | None) -> None:
    """Record one HTTP attempt (`status_code=None` for a transport failure) if a collector is active."""
    metrics = _active.get()
    if metrics is not None:
        metrics.record(label, status_code)
