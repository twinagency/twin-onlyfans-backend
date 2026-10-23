"""Scoped ledger investigation helpers; no startup, route or worker registration.

Import helpers from ``diagnostics`` explicitly. Keeping this package import free
also lets the ledger parser depend on ``validation`` without an import cycle.
"""
