"""Deterministic code layer for freight billing reconciliation.

Parsing, discovery, contract clause indexing, rate-spec interpretation, pricing, disposition
policy and report assembly. No module here reads a contract's prose to decide a price: prices
come only from a rate spec (produced and verified upstream) applied to shipment records.
"""

__version__ = "0.1.0"
