"""Merchant platform integrations.

Each module here is a thin, injectable bridge to one commerce platform:
pull its catalog in, push our orders and refunds out. Nothing in this package
holds policy — bounds, gates, and the ledger stay in `kernel/` exactly where
they were. A connector that misbehaves can at worst sync wrong data or fail a
push; it can never widen a discount or skip a veto.
"""
