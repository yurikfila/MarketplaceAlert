import logging

from marketplace_alert.core.logging_config import configure_logging


def test_configure_logging_suppresses_httpx_info_logging() -> None:
    """httpx logs 'HTTP Request: {method} {url} ...' at INFO by default, and
    our only httpx caller (TelegramNotificationProvider) puts the bot token
    in that URL - configure_logging must keep it from reaching our logs."""
    configure_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_configure_logging_sets_requested_level_on_root() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    configure_logging("INFO")  # restore default for any tests that follow
