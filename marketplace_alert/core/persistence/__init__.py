"""Local persistence and duplicate-detection for discovered listings.

Works with the normalized ``Listing`` model
(``marketplace_alert.core.models.listing.Listing``) only, so it never needs
to know which connector - mock or real - a listing came from.
"""
