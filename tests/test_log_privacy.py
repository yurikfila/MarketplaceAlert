"""Regression tests: a saved search's keyword (``query``) is user-entered text and
must never appear in application logs - not in INFO/WARNING/ERROR messages, not in
exception messages or tracebacks that logging would format, and not via the
database layer's error text.

Every check formats the captured records the same way production does
(``JSONFormatter``, which includes ``exc_info``) and looks for a unique marker
string, so a leak through a message, its arguments, or an exception is caught.
"""

import ast
import logging
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from marketplace_alert.connectors.bonanza.connector import BonanzaMarketplaceConnector
from marketplace_alert.connectors.ebay.connector import EbayMarketplaceConnector
from marketplace_alert.connectors.etsy.connector import EtsyMarketplaceConnector
from marketplace_alert.connectors.reverb.connector import ReverbMarketplaceConnector
from marketplace_alert.connectors.tradera.connector import TraderaMarketplaceConnector
from marketplace_alert.core.connectors import retry as retry_module
from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.logging_config import JSONFormatter, configure_logging, exception_summary
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.relevance import filter_relevant_listings
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner

# Unique, unmistakable, and not a substring of anything the app logs on its own.
KEYWORD = "zq-private-keyword-7431"

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _logged_text(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the records would emit in production: message + args + exc_info."""
    formatter = JSONFormatter()
    parts: list[str] = []
    for record in caplog.records:
        parts.append(formatter.format(record))
        parts.append(repr(record.args))
        parts.append(record.getMessage())
    return "\n".join(parts)


def _assert_no_keyword(caplog: pytest.LogCaptureFixture) -> str:
    logged = _logged_text(caplog)
    assert KEYWORD not in logged, "the saved-search keyword leaked into the logs"
    return logged


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_module.time, "sleep", lambda seconds: None)


def _irrelevant_listing() -> Listing:
    return Listing(
        marketplace="mock",
        external_listing_id="rej-1",
        title="Completely unrelated garden hose",
        listing_url="https://example.com/rej-1",
    )


# --- connectors -------------------------------------------------------------


def test_ebay_search_log_has_no_keyword_but_keeps_a_diagnostic(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"access_token": "t", "expires_in": 7200}))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"itemSummaries": []}))

    with caplog.at_level(logging.DEBUG):
        EbayMarketplaceConnector(app_id="a", cert_id="c").search(KEYWORD)

    logged = _assert_no_keyword(caplog)
    assert "eBay search started" in logged


_CONNECTOR_BUILDERS = {
    "ebay": lambda: EbayMarketplaceConnector(app_id="a", cert_id="c"),
    "etsy": lambda: EtsyMarketplaceConnector(api_key="k", shared_secret="s"),
    "reverb": lambda: ReverbMarketplaceConnector(api_token="t"),
    "bonanza": lambda: BonanzaMarketplaceConnector(dev_name="d"),
    "tradera": lambda: TraderaMarketplaceConnector(app_id="a", app_key="k"),
}


def _install_failure(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """Make every outbound marketplace call fail in a way that echoes the keyword."""
    token_response = httpx.Response(200, json={"access_token": "t", "expires_in": 7200})

    def fail(*args, **kwargs):
        if failure == "transport":
            # httpx transport errors routinely quote the full request URL.
            raise httpx.ConnectError(f"cannot reach https://api.example.test/search?query={KEYWORD}")
        if failure == "http_400":
            return httpx.Response(
                400, json={"error": f"bad query {KEYWORD}", "errors": [{"code": "InvalidQuery", "message": KEYWORD}]}
            )
        if failure == "errors_on_200":
            return httpx.Response(
                200,
                json={
                    "errors": [{"code": "InvalidQuery", "message": f"Could not search for {KEYWORD}"}],
                    "ack": f"Failure for {KEYWORD}",
                    "findItemsByKeywordsResponse": [{"ack": [f"Failure {KEYWORD}"]}],
                },
            )
        raise AssertionError(failure)

    def fake_post(url, *args, **kwargs):
        # eBay's OAuth token request succeeds so the *search* is what fails; every
        # other POST (Bonanza's search endpoint) fails like the GETs do.
        return token_response if "oauth2/token" in str(url) else fail()

    monkeypatch.setattr(httpx, "get", fail)
    monkeypatch.setattr(httpx, "post", fake_post)


@pytest.mark.parametrize("failure", ["transport", "http_400", "errors_on_200"])
@pytest.mark.parametrize("name", sorted(_CONNECTOR_BUILDERS))
def test_connector_failures_never_log_the_keyword(
    name: str, failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_failure(monkeypatch, failure)
    connector = _CONNECTOR_BUILDERS[name]()

    with caplog.at_level(logging.DEBUG):
        try:
            connector.search(KEYWORD)
        except MarketplaceConnectorError as exc:
            # The error that propagates to the runner (and is logged there) must be clean too.
            assert KEYWORD not in str(exc)

    _assert_no_keyword(caplog)


def test_tradera_error_log_keeps_the_count_and_codes_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    body = {"errors": [{"code": "InvalidQuery", "message": f"no results for {KEYWORD}"}, {"code": f"has space {KEYWORD}"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    with caplog.at_level(logging.ERROR), pytest.raises(MarketplaceConnectorError):
        TraderaMarketplaceConnector(app_id="a", app_key="k").search(KEYWORD)

    logged = _assert_no_keyword(caplog)
    assert "2 error(s)" in logged
    assert "InvalidQuery" in logged  # the safe, machine-readable code survives


# --- relevance filtering ----------------------------------------------------


def test_relevance_rejection_log_keeps_internal_ids_but_not_the_keyword(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        result = filter_relevant_listings(
            query=KEYWORD, listings=[_irrelevant_listing()], marketplace="mock", saved_search_id=42
        )

    assert result.rejected_count == 1  # the logging path really ran
    logged = _assert_no_keyword(caplog)
    for expected in ("Relevance: rejected", "mock", "rej-1", "42"):
        assert expected in logged


# --- runner / scanner (exceptions) ------------------------------------------


class _HostileConnector:
    """Fails with an exception whose message quotes the keyword, as an HTTP
    library's error for a '?query=...' URL realistically would."""

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        raise RuntimeError(f"unexpected failure while requesting https://api.example.test/?query={query}")


class _ErroringConnector:
    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        raise MarketplaceConnectorError("Marketplace API returned HTTP 500")


def test_runner_unexpected_exception_logs_type_and_location_not_the_keyword(
    db_session, caplog: pytest.LogCaptureFixture
) -> None:
    saved_search = SavedSearchRepository(db_session).create(
        query=KEYWORD, marketplaces=["hostile"], scan_interval_seconds=60, is_active=True
    )
    runner = SavedSearchRunner(resolve_connector=lambda name: _HostileConnector())

    with caplog.at_level(logging.DEBUG):
        result = runner.run(db_session, saved_search)

    assert result.results[0].error == "An unexpected error occurred"
    logged = _assert_no_keyword(caplog)
    # Still diagnosable: which search/marketplace, what kind of failure, and where.
    for expected in (str(saved_search.id), "hostile", "failed unexpectedly", "RuntimeError", "test_log_privacy.py"):
        assert expected in logged
    assert all(record.exc_info is None for record in caplog.records if record.levelno >= logging.ERROR)


def test_runner_connector_error_log_keeps_the_controlled_message(db_session, caplog: pytest.LogCaptureFixture) -> None:
    saved_search = SavedSearchRepository(db_session).create(
        query=KEYWORD, marketplaces=["broken"], scan_interval_seconds=60, is_active=True
    )
    runner = SavedSearchRunner(resolve_connector=lambda name: _ErroringConnector())

    with caplog.at_level(logging.DEBUG):
        runner.run(db_session, saved_search)

    logged = _assert_no_keyword(caplog)
    assert "Marketplace API returned HTTP 500" in logged


def test_scheduled_scan_end_to_end_never_logs_the_keyword(session_factory, caplog: pytest.LogCaptureFixture) -> None:
    setup = session_factory()
    repository = SavedSearchRepository(setup)
    repository.create(query=KEYWORD, marketplaces=["hostile"], scan_interval_seconds=60, is_active=True)
    repository.create(query=f"{KEYWORD} two", marketplaces=["ok"], scan_interval_seconds=60, is_active=True)
    setup.commit()
    setup.close()

    class _Irrelevant:
        def search(self, query: str, filters: dict | None = None) -> list[Listing]:
            return [_irrelevant_listing()]

    runner = SavedSearchRunner(
        resolve_connector=lambda name: _HostileConnector() if name == "hostile" else _Irrelevant()
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    with caplog.at_level(logging.DEBUG):
        scanner.run_due_searches()

    logged = _assert_no_keyword(caplog)
    assert "Relevance: rejected" in logged and "failed unexpectedly" in logged  # both paths ran


# --- database error text ----------------------------------------------------


def test_database_errors_do_not_include_bound_parameters(db_engine, caplog: pytest.LogCaptureFixture) -> None:
    """A failed insert of a saved search carries the keyword as a bound parameter;
    SQLAlchemy normally appends it to the exception message ('[parameters: ...]')."""
    logger = logging.getLogger("tests.log_privacy.db")
    with db_engine.connect() as connection, caplog.at_level(logging.DEBUG):
        try:
            # scan_interval_seconds is NOT NULL -> IntegrityError; the keyword is a bound parameter.
            connection.execute(
                text("INSERT INTO saved_searches (query, is_active) VALUES (:query, 1)"), {"query": KEYWORD}
            )
        except SQLAlchemyError as exc:
            assert KEYWORD not in str(exc)
            logger.exception("insert failed")  # a full traceback, formatted like production does

    assert caplog.records, "expected the failing insert to have been logged"
    _assert_no_keyword(caplog)


# --- helpers & configuration ------------------------------------------------


def _raise_in_helper() -> None:
    raise ValueError(f"contains {KEYWORD}")


def test_exception_summary_has_type_and_location_but_never_the_message() -> None:
    try:
        _raise_in_helper()
    except ValueError as exc:
        summary = exception_summary(exc)

    assert summary.startswith("ValueError at test_log_privacy.py:")
    assert "in _raise_in_helper" in summary  # the innermost frame
    assert KEYWORD not in summary


def test_exception_summary_for_an_exception_that_was_never_raised() -> None:
    assert exception_summary(RuntimeError(KEYWORD)) == "RuntimeError at unknown location"


def test_configure_logging_keeps_http_libraries_quiet_even_at_debug() -> None:
    configure_logging("DEBUG")
    try:
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        configure_logging("INFO")  # restore the default level, as tests/test_logging_config.py does


# --- static guard against reintroducing the leak ----------------------------

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_FORBIDDEN_NAMES = {"query", "keyword", "keywords"}


def _logging_calls_referencing_a_keyword(source: str, filename: str) -> list[int]:
    offending: list[int] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS):
            continue
        receiver = node.func.value
        if not (isinstance(receiver, ast.Name) and receiver.id in {"logger", "logging", "log"}):
            continue
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            for inner in ast.walk(argument):
                name = inner.id if isinstance(inner, ast.Name) else inner.attr if isinstance(inner, ast.Attribute) else None
                if name in _FORBIDDEN_NAMES:
                    offending.append(node.lineno)
    return offending


def test_no_logging_call_in_the_application_references_a_search_keyword() -> None:
    offenders = {
        str(path.relative_to(_PROJECT_ROOT)): lines
        for path in sorted((_PROJECT_ROOT / "marketplace_alert").rglob("*.py"))
        if (lines := _logging_calls_referencing_a_keyword(path.read_text(encoding="utf-8"), str(path)))
    }
    assert offenders == {}, f"logging calls that reference a search keyword (never log it): {offenders}"


def test_the_static_guard_itself_detects_a_leak() -> None:
    leaky = "import logging\nlogger = logging.getLogger(__name__)\ndef f(query):\n    logger.info('x %r', query)\n"
    f_string = "def g(saved_search):\n    logger.error(f'failed {saved_search.query}')\n"
    clean = "def h(saved_search):\n    logger.info('ok %s', saved_search.id)\n"

    assert _logging_calls_referencing_a_keyword(leaky, "leaky.py") == [4]
    assert _logging_calls_referencing_a_keyword(f_string, "fstring.py") == [2]
    assert _logging_calls_referencing_a_keyword(clean, "clean.py") == []
