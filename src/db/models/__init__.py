"""
src/db/models/__init__.py

SQLAlchemy ORM models for DATA-SERVICE 2.0 v2 production schema.

Import all models through this package so Alembic autogenerate picks them up:

    from src.db.models import Base, EquityCandle, FuturesCandle, ...
"""

from src.db.models.base import Base
from src.db.models.instruments import (
    InstrumentMaster,
    InstrumentProviderMapping,
    InstrumentIdentityHistory,
)
from src.db.models.candles import (
    EquityCandle,
    FuturesCandle,
    OptionsCandle,
)
from src.db.models.live import (
    MarketTick,
    MarketQuote,
)
from src.db.models.option_chain import (
    OptionChainSnapshot,
    OptionChainContract,
    OptionGreeksSnapshot,
)
from src.db.models.calendar import (
    ExchangeCalendar,
    MarketSession,
    FnoUniverseMembership,
)
from src.db.models.operations import (
    IngestionJob,
    IngestionCheckpoint,
    CandleBarQuarantine,
)
from src.db.models.fo_universe import FoUniverse
from src.db.models.reconciliation import (
    MarketDepth,
    ReconciliationRecord,
    DataIncident,
    ClosingAuctionSnapshot,
)

__all__ = [
    "Base",
    # Instruments
    "InstrumentMaster",
    "InstrumentProviderMapping",
    "InstrumentIdentityHistory",
    # Candles
    "EquityCandle",
    "FuturesCandle",
    "OptionsCandle",
    # Live
    "MarketTick",
    "MarketQuote",
    # Option chain
    "OptionChainSnapshot",
    "OptionChainContract",
    "OptionGreeksSnapshot",
    # Calendar
    "ExchangeCalendar",
    "MarketSession",
    "FnoUniverseMembership",
    # Operations
    "IngestionJob",
    "IngestionCheckpoint",
    "CandleBarQuarantine",
    # Reconciliation, depth, incidents, CAS
    "MarketDepth",
    "ReconciliationRecord",
    "DataIncident",
    "ClosingAuctionSnapshot",
    # F&O universe master
    "FoUniverse",
]
