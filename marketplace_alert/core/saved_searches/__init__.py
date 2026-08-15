"""Saved searches: persisted search definitions the background scanner
runs on a recurring interval, without requiring a manual /scan request.

Depends only on `MarketplaceConnector` (via an injected resolver function),
`ListingDiscoveryService`, and `NotificationService` - never a concrete
connector. See `marketplace_alert.connectors.registry` and
`marketplace_alert.core.scheduler`.
"""
