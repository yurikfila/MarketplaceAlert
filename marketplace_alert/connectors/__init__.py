"""Concrete marketplace connectors.

Each marketplace (real or mock) gets its own isolated subpackage here. This
package itself must stay empty of logic - it exists only as the namespace
real and mock connectors live under, so adding or removing one never touches
another.
"""
