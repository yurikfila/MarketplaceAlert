import pytest

from marketplace_alert.connectors.ebay.connector import EbayMarketplaceConnector
from marketplace_alert.connectors.etsy.connector import EtsyMarketplaceConnector
from marketplace_alert.connectors.mock.connector import MockMarketplaceConnector
from marketplace_alert.connectors.reverb.connector import ReverbMarketplaceConnector
from marketplace_alert.connectors.registry import (
    UnsupportedMarketplaceError,
    display_name_for,
    get_connector,
    is_marketplace_supported,
    list_supported_marketplaces,
)


def test_get_connector_returns_mock_connector_for_mock() -> None:
    connector = get_connector("mock")
    assert isinstance(connector, MockMarketplaceConnector)


def test_get_connector_returns_etsy_connector_for_etsy() -> None:
    connector = get_connector("etsy")
    assert isinstance(connector, EtsyMarketplaceConnector)
    assert connector.marketplace_name == "etsy"


def test_get_connector_returns_ebay_connector_for_ebay() -> None:
    connector = get_connector("ebay")
    assert isinstance(connector, EbayMarketplaceConnector)
    assert connector.marketplace_name == "ebay"


def test_get_connector_returns_reverb_connector_for_reverb() -> None:
    connector = get_connector("reverb")
    assert isinstance(connector, ReverbMarketplaceConnector)
    assert connector.marketplace_name == "reverb"


def test_get_connector_raises_for_unregistered_marketplace() -> None:
    with pytest.raises(UnsupportedMarketplaceError):
        get_connector("vinted")


def test_is_marketplace_supported_true_for_mock_etsy_ebay_and_reverb() -> None:
    assert is_marketplace_supported("mock") is True
    assert is_marketplace_supported("etsy") is True
    assert is_marketplace_supported("ebay") is True
    assert is_marketplace_supported("reverb") is True


def test_is_marketplace_supported_false_for_not_yet_implemented_marketplaces() -> None:
    assert is_marketplace_supported("vinted") is False
    assert is_marketplace_supported("nonsense") is False


def test_list_supported_marketplaces_includes_reverb() -> None:
    assert "reverb" in list_supported_marketplaces()


def test_display_name_for_reverb_is_brand_cased() -> None:
    assert display_name_for("reverb") == "Reverb"


def test_display_name_for_unknown_marketplace_falls_back_to_title_case() -> None:
    assert display_name_for("vinted") == "Vinted"
