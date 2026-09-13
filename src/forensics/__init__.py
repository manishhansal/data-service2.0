"""
Trade forensics sub-package.

Provides immutable per-trade provenance recording that links every trade
execution back to the exact market-data observations and quality-gate state
that informed it.

Usage::

    from src.forensics import TradeForensicsRecord, TradeForensicsStore

Requirements: 8.7 (trade forensics provenance recording)
"""

from .trade_forensics import TradeForensicsRecord, TradeForensicsStore

__all__ = ["TradeForensicsRecord", "TradeForensicsStore"]
