"""Notification abstraction: alerting on newly discovered listings.

Mirrors the connector pattern (see `marketplace_alert.core.connectors`):
the core system depends only on `NotificationProvider` and `Listing`, never
on a specific provider's SDK or API. Concrete providers (Telegram, and any
added later) live under `marketplace_alert/notifications/<provider>/`.
"""
