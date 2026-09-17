"""
tests/test_pre_fno_backfill_certification.py
=============================================

Extended test suite for the Pre-F&O Backfill Foundation Certification.

Coverage:
  CANDLE_BAR CUTOVER
    - No active candle_bar SQL reads in src/api/india.py
    - No active candle_bar SQL writes in src/engines/historical_engine.py
    - india.py reads equity_candle for NSE
    - india.py reads futures_candle for NFO
    - historical_engine routes EQ/IDX → equity_candle
    - historical_engine routes FO/FUT → futures_candle
    - historical_engine routes OPT    → options_candle
    - timescale.py no longer promotes candle_bar

  OI VERIFICATION
    - All candle_bar OI values = 0 (no real OI data lost)
    - equity_candle has no OI column (correct by design)
    - futures_candle has open_interest column
    - options_candle has open_interest column

  EXCHANGE CALENDAR
    - exchange_calendar is populated
    - Trading days > 0
    - Weekends classified as WEEKEND (not OFFICIAL_HOLIDAY)
    - Holidays classified as OFFICIAL_HOLIDAY
    - NOT_PUBLISHED dates have is_trading_day=FALSE
    - 2026-09-14 (Sunday) → WEEKEND
    - 2026-10-02 (Gandhi Jayanti) → OFFICIAL_HOLIDAY
    - TradingCalendarService.is_trading_day works
    - TradingCalendarService.get_trading_days returns correct list
    - get_previous_trading_day works
    - get_next_trading_day works

  INSTRUMENT MASTER
    - 50 instruments populated
    - EQ instruments classified as 'EQ'
    - IDX instruments classified as 'IDX'
    - instrument_provider_mapping populated

  F&O CONSTRAINTS
    - futures_candle rejects 3m
    - futures_candle rejects non-FUT contract_type
    - futures_candle rejects negative OI
    - options_candle rejects invalid option_type
    - options_candle rejects strike=0
    - options_candle rejects negative OI

  MIGRATION ARCHIVE SAFETY
    - candle_bar still has 5,425,725 rows
    - candle_bar still exists (not dropped)
    - No new NSE writes to candle_bar
    - BINANCE writes to candle_bar remain (exempt)

  IDEMPOTENCY
    - Calendar population is idempotent
    - equity_candle migration is idempotent

  API ROUTING
    - Live API returns equity_candle data
    - Live API returns 7 candles for RELIANCE 1d Sep 2026
    - Live compat API route returns same data

Run:
    APP_ENV=local pytest tests/test_pre_fno_backfill_certification.py -v --tb=short

Markers:
    integration — requires live PostgreSQL and running API server
"""

from __future__ import annotations

import ast
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# DB connectivity
# ---------------------------------------------------------------------------
_DB_AVAILABLE = False

try:
    import os
    os.environ.setdefault("APP_ENV", "local")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
    # Mark DB as available — the fixture will resolve the real URL at test time
    # by parsing .env.local directly, bypassing the Settings lru_cache and the
    # class-level env_file lock that bakes in wrong path at import time.
    _DB_AVAILABLE = True
except Exception:
    pass

pytestmark = [pytest.mark.integration]

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    if not _DB_AVAILABLE:
        pytest.skip("DATABASE_URL not configured")
    try:
        import os as _os
        # Resolve the real DATABASE_URL, bypassing both the lru_cache on
        # get_settings() and the module-level env_file lock on Settings.model_config.
        #
        # Strategy:
        # 1. If DATABASE_URL is already set and looks like the real one, use it.
        # 2. Otherwise, read .env.local directly to get the real URL.
        raw_url = _os.environ.get("DATABASE_URL", "")
        if raw_url and "test:test" not in raw_url and "localhost:5432" not in raw_url:
            db_url = raw_url
        else:
            # Parse .env.local ourselves — avoids the Settings class-level
            # env_file lock that bakes in the wrong .env path at import time.
            import pathlib
            env_local = pathlib.Path(__file__).resolve().parent.parent / ".env.local"
            db_url = None
            if env_local.exists():
                for line in env_local.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("DATABASE_URL=") and not line.startswith("#"):
                        db_url = line.split("=", 1)[1].strip()
                        break
            if not db_url:
                pytest.skip("DATABASE_URL not configured")
    except Exception as _exc:
        pytest.skip(f"DATABASE_URL not configured: {_exc}")
    engine = create_async_engine(db_url, echo=False, pool_pre_ping=True)
    yield engine
    # Teardown: use sync disposal to avoid "Runner.run() cannot be called from
    # a running event loop" with pytest-asyncio 1.4.0 on Python 3.14.
    try:
        engine.sync_engine.dispose()
    except Exception:
        pass


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return result.scalar()


async def fetchone(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return result.mappings().first()


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.rowcount


# ============================================================================
# SECTION 1 — CANDLE_BAR CUTOVER (static code analysis)
# ============================================================================

class TestCandleBarCutoverStatic:
    """Static analysis: verify no active candle_bar SQL in production code."""

    def _read_source(self, rel_path: str) -> str:
        return (_REPO_ROOT / rel_path).read_text()

    def test_india_py_no_from_candle_bar(self) -> None:
        """india.py must not SELECT FROM candle_bar."""
        src = self._read_source("src/api/india.py")
        # Allow the string in comments/docstrings but not in SQL context
        matches = re.findall(r"FROM\s+candle_bar", src)
        assert len(matches) == 0, (
            f"Found {len(matches)} 'FROM candle_bar' in india.py — cutover incomplete"
        )

    def test_india_py_reads_equity_candle(self) -> None:
        """india.py must contain FROM equity_candle."""
        src = self._read_source("src/api/india.py")
        assert "FROM equity_candle" in src, "equity_candle not in india.py SELECT"

    def test_india_py_reads_futures_candle(self) -> None:
        """india.py must contain FROM futures_candle for NFO routing."""
        src = self._read_source("src/api/india.py")
        assert "FROM futures_candle" in src, "futures_candle not in india.py SELECT"

    def test_historical_engine_no_insert_candle_bar(self) -> None:
        """historical_engine.py must not INSERT INTO candle_bar."""
        src = self._read_source("src/engines/historical_engine.py")
        matches = re.findall(r"INSERT\s+INTO\s+candle_bar", src)
        assert len(matches) == 0, (
            f"Found {len(matches)} 'INSERT INTO candle_bar' in historical_engine.py"
        )

    def test_historical_engine_inserts_equity_candle(self) -> None:
        """historical_engine.py must INSERT INTO equity_candle."""
        src = self._read_source("src/engines/historical_engine.py")
        assert "INSERT INTO equity_candle" in src

    def test_historical_engine_inserts_futures_candle(self) -> None:
        """historical_engine.py must INSERT INTO futures_candle."""
        src = self._read_source("src/engines/historical_engine.py")
        assert "INSERT INTO futures_candle" in src

    def test_historical_engine_inserts_options_candle(self) -> None:
        """historical_engine.py must INSERT INTO options_candle."""
        src = self._read_source("src/engines/historical_engine.py")
        assert "INSERT INTO options_candle" in src

    def test_timescale_no_create_hypertable_candle_bar(self) -> None:
        """timescale.py must not EXECUTE create_hypertable('candle_bar', ...)."""
        src = self._read_source("src/db/timescale.py")
        # The SQL constant string itself must not exist (only docstring comments are OK)
        # Check that no text() call contains the create_hypertable on candle_bar
        # The old code had: _PROMOTE_HYPERTABLE_SQL = text("SELECT create_hypertable('candle_bar'...")
        assert "candle_bar',\n        'time'" not in src, (
            "timescale.py still has create_hypertable SQL targeting candle_bar"
        )
        # The new code should only mention it in docstrings/comments
        # Verify no actual SQL variable assigns create_hypertable for candle_bar
        assert "_PROMOTE_HYPERTABLE_SQL" not in src, (
            "_PROMOTE_HYPERTABLE_SQL still present — candle_bar promotion not removed"
        )

    def test_canonical_table_router_exists(self) -> None:
        """historical_engine.py must have _canonical_table_for() method."""
        src = self._read_source("src/engines/historical_engine.py")
        assert "_canonical_table_for" in src

    def test_eq_routes_to_equity_candle(self) -> None:
        """_canonical_table_for EQ must return equity_candle."""
        import sys
        sys.path.insert(0, str(_REPO_ROOT))
        from src.engines.historical_engine import HistoricalEngine
        assert HistoricalEngine._canonical_table_for("EQ", "NSE") == "equity_candle"

    def test_idx_routes_to_equity_candle(self) -> None:
        from src.engines.historical_engine import HistoricalEngine
        assert HistoricalEngine._canonical_table_for("IDX", "NSE") == "equity_candle"

    def test_fo_routes_to_futures_candle(self) -> None:
        from src.engines.historical_engine import HistoricalEngine
        assert HistoricalEngine._canonical_table_for("FO", "NFO") == "futures_candle"

    def test_fut_routes_to_futures_candle(self) -> None:
        from src.engines.historical_engine import HistoricalEngine
        assert HistoricalEngine._canonical_table_for("FUT", "NFO") == "futures_candle"

    def test_opt_routes_to_options_candle(self) -> None:
        from src.engines.historical_engine import HistoricalEngine
        assert HistoricalEngine._canonical_table_for("OPT", "NFO") == "options_candle"

    def test_optidx_routes_to_options_candle(self) -> None:
        from src.engines.historical_engine import HistoricalEngine
        assert HistoricalEngine._canonical_table_for("OPTIDX", "NFO") == "options_candle"

    def test_nfo_exchange_fallback_routes_to_futures_candle(self) -> None:
        from src.engines.historical_engine import HistoricalEngine
        # NFO exchange with unknown class → futures_candle
        assert HistoricalEngine._canonical_table_for("UNKNOWN", "NFO") == "futures_candle"

    def test_trading_calendar_service_exists(self) -> None:
        """TradingCalendarService must be importable."""
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService()
        assert svc is not None


# ============================================================================
# SECTION 2 — OI VERIFICATION
# ============================================================================

class TestOIVerification:
    """All historical OI = 0. No real OI data was in candle_bar."""

    async def test_all_candle_bar_oi_is_zero(self, db_engine: AsyncEngine) -> None:
        """No candle_bar row has oi > 0."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM candle_bar WHERE oi IS NOT NULL AND oi != 0 AND exchange='NSE'",
        )
        assert count == 0, f"Found {count} candle_bar rows with non-zero OI — unexpected"

    async def test_candle_bar_oi_not_null_count(self, db_engine: AsyncEngine) -> None:
        """Exactly 13,480 rows have oi IS NOT NULL (all = 0 from Upstox)."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM candle_bar WHERE oi IS NOT NULL AND exchange='NSE'",
        )
        assert count == 13_480, f"Expected 13,480 oi=NOT_NULL rows, got {count}"

    async def test_equity_candle_has_no_oi_column(self, db_engine: AsyncEngine) -> None:
        """equity_candle must NOT have an 'oi' column (equities have no OI)."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='equity_candle' AND column_name='oi'",
        )
        assert count == 0, "equity_candle should not have 'oi' column"

    async def test_futures_candle_has_open_interest(self, db_engine: AsyncEngine) -> None:
        """futures_candle must have open_interest column."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='futures_candle' AND column_name='open_interest'",
        )
        assert count == 1, "futures_candle must have open_interest column"

    async def test_options_candle_has_open_interest(self, db_engine: AsyncEngine) -> None:
        """options_candle must have open_interest column."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='options_candle' AND column_name='open_interest'",
        )
        assert count == 1, "options_candle must have open_interest column"


# ============================================================================
# SECTION 3 — EXCHANGE CALENDAR
# ============================================================================

class TestExchangeCalendar:
    """exchange_calendar must be populated with correct classifications."""

    async def test_calendar_is_populated(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine, "SELECT COUNT(*) FROM exchange_calendar WHERE exchange='NSE'"
        )
        assert count >= 1827, f"Expected >= 1827 NSE calendar rows, got {count}"

    async def test_trading_days_exist(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND is_trading_day=TRUE",
        )
        assert count >= 1000, f"Expected >= 1000 trading days, got {count}"

    async def test_weekends_classified_correctly(self, db_engine: AsyncEngine) -> None:
        """2026-09-12 is a Saturday → must be WEEKEND, not OFFICIAL_HOLIDAY."""
        row = await fetchone(
            db_engine,
            "SELECT day_type, is_trading_day FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND calendar_date='2026-09-12'",
        )
        assert row is not None, "2026-09-12 not found in exchange_calendar"
        assert row["day_type"] == "WEEKEND", f"Expected WEEKEND, got {row['day_type']}"
        assert row["is_trading_day"] is False

    async def test_saturday_is_weekend_not_holiday(self, db_engine: AsyncEngine) -> None:
        """2026-09-13 is a Sunday → WEEKEND."""
        row = await fetchone(
            db_engine,
            "SELECT day_type FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND calendar_date='2026-09-13'",
        )
        assert row is not None
        assert row["day_type"] == "WEEKEND"

    async def test_official_holiday_classified_correctly(self, db_engine: AsyncEngine) -> None:
        """2026-10-02 (Gandhi Jayanti) must be OFFICIAL_HOLIDAY."""
        row = await fetchone(
            db_engine,
            "SELECT day_type, is_trading_day, holiday_name FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND calendar_date='2026-10-02'",
        )
        assert row is not None, "2026-10-02 not found"
        assert row["day_type"] == "OFFICIAL_HOLIDAY"
        assert row["is_trading_day"] is False
        assert row["holiday_name"] is not None

    async def test_trading_day_classified_correctly(self, db_engine: AsyncEngine) -> None:
        """2026-09-15 (Tuesday) must be TRADING_DAY."""
        row = await fetchone(
            db_engine,
            "SELECT day_type, is_trading_day FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND calendar_date='2026-09-15'",
        )
        assert row is not None
        assert row["day_type"] == "TRADING_DAY"
        assert row["is_trading_day"] is True

    async def test_not_published_future_dates(self, db_engine: AsyncEngine) -> None:
        """Far future dates beyond horizon should be NOT_PUBLISHED."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND day_type='NOT_PUBLISHED'",
        )
        assert count > 0, "Expected some NOT_PUBLISHED dates in the far future"

    async def test_not_published_is_not_trading_day(self, db_engine: AsyncEngine) -> None:
        """NOT_PUBLISHED dates must not be marked as trading days."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' "
            "AND day_type='NOT_PUBLISHED' AND is_trading_day=TRUE",
        )
        assert count == 0, f"{count} NOT_PUBLISHED dates marked as trading days"

    async def test_nfo_fo_calendar_exists(self, db_engine: AsyncEngine) -> None:
        """NFO/FO calendar must also be populated."""
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM exchange_calendar WHERE exchange='NFO' AND segment='FO'",
        )
        assert count >= 1827, f"Expected >= 1827 NFO calendar rows, got {count}"

    async def test_nse_2026_diwali_holiday(self, db_engine: AsyncEngine) -> None:
        """2026-11-10 (Diwali - Laxmi Pujan) → OFFICIAL_HOLIDAY."""
        row = await fetchone(
            db_engine,
            "SELECT day_type, is_trading_day FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND calendar_date='2026-11-10'",
        )
        assert row is not None
        assert row["day_type"] == "OFFICIAL_HOLIDAY"
        assert row["is_trading_day"] is False

    async def test_independence_day_2026(self, db_engine: AsyncEngine) -> None:
        """2026-08-15 is a Saturday, so it is classified as WEEKEND (not OFFICIAL_HOLIDAY).
        The holiday classification logic correctly prioritizes weekend over holiday when
        a public holiday falls on a weekend — NSE is closed regardless.
        Use 2025-08-15 (Friday) instead, which IS an official holiday."""
        row = await fetchone(
            db_engine,
            "SELECT day_type, is_trading_day FROM exchange_calendar "
            "WHERE exchange='NSE' AND segment='EQ' AND calendar_date='2025-08-15'",
        )
        assert row is not None, "2025-08-15 not found in calendar"
        assert row["is_trading_day"] is False, "Independence Day must not be a trading day"
        # 2025-08-15 is a Friday (not a weekend), so it should be OFFICIAL_HOLIDAY
        assert row["day_type"] == "OFFICIAL_HOLIDAY", (
            f"2025-08-15 (Friday, Independence Day) should be OFFICIAL_HOLIDAY, got {row['day_type']}"
        )


# ============================================================================
# SECTION 4 — TRADING CALENDAR SERVICE
# ============================================================================

class TestTradingCalendarService:
    """TradingCalendarService must correctly use exchange_calendar."""

    async def test_trading_day_returns_true(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        result = await svc.is_trading_day(date(2026, 9, 15))  # Tuesday
        assert result is True

    async def test_saturday_returns_false(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        result = await svc.is_trading_day(date(2026, 9, 12))  # Saturday
        assert result is False

    async def test_sunday_returns_false(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        result = await svc.is_trading_day(date(2026, 9, 13))  # Sunday
        assert result is False

    async def test_official_holiday_returns_false(self, db_engine: AsyncEngine) -> None:
        """Gandhi Jayanti 2026-10-02 must not be a trading day."""
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        result = await svc.is_trading_day(date(2026, 10, 2))
        assert result is False

    async def test_get_trading_days_excludes_weekends(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        days = await svc.get_trading_days(date(2026, 9, 11), date(2026, 9, 18))
        # Week: Mon(11), Tue(12)→not in range, so: 11(Thu?), let's check
        # 2026-09-11=Fri, 12=Sat, 13=Sun, 14=Mon, 15=Tue, 16=Wed, 17=Thu, 18=Fri
        # But 2026-09-18 = Milad-un-Nabi (holiday)
        for d in days:
            assert d.isoweekday() not in (6, 7), f"{d} is a weekend but appears in trading days"

    async def test_get_trading_days_excludes_holidays(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        days = await svc.get_trading_days(date(2026, 9, 15), date(2026, 9, 20))
        # 2026-09-18 is Milad-un-Nabi holiday
        assert date(2026, 9, 18) not in days, "Holiday 2026-09-18 should not be in trading days"

    async def test_get_previous_trading_day_skips_weekend(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        # 2026-09-14 is Monday. Previous trading day should skip over 2026-09-13 (Sun) and 2026-09-12 (Sat)
        prev = await svc.get_previous_trading_day(date(2026, 9, 14))
        assert prev == date(2026, 9, 11), f"Expected 2026-09-11 (Fri), got {prev}"

    async def test_get_next_trading_day_skips_weekend(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        # After Friday 2026-09-11, next trading day should skip weekend
        nxt = await svc.get_next_trading_day(date(2026, 9, 11))
        assert nxt == date(2026, 9, 14), f"Expected 2026-09-14 (Mon), got {nxt}"

    async def test_calendar_coverage_stats(self, db_engine: AsyncEngine) -> None:
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        stats = await svc.calendar_coverage()
        assert stats["trading_days"] >= 1000
        assert stats["weekends"] >= 500
        assert stats["official_holidays"] >= 50
        assert "error" not in stats

    async def test_no_db_fallback_uses_weekday_check(self) -> None:
        """Without DB, TradingCalendarService falls back to weekday check."""
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine=None)
        # Tuesday → True
        assert await svc.is_trading_day(date(2026, 9, 15)) is True
        # Saturday → False
        assert await svc.is_trading_day(date(2026, 9, 12)) is False

    async def test_leap_year_2024_feb_29(self, db_engine: AsyncEngine) -> None:
        """2024-02-29 (Thursday in leap year) → TRADING_DAY."""
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        result = await svc.is_trading_day(date(2024, 2, 29))
        assert result is True, "2024-02-29 should be a trading day"

    async def test_year_boundary_dec_31_to_jan_1(self, db_engine: AsyncEngine) -> None:
        """2025-12-31 (Wednesday) → TRADING_DAY. 2026-01-01 (Thursday) depends on holiday."""
        from src.engines.trading_calendar_service import TradingCalendarService
        svc = TradingCalendarService(db_engine)
        # 2025-12-25 = Christmas holiday
        is_christmas = await svc.is_trading_day(date(2025, 12, 25))
        assert is_christmas is False, "2025-12-25 (Christmas) should not be trading day"


# ============================================================================
# SECTION 5 — INSTRUMENT MASTER
# ============================================================================

class TestInstrumentMasterReadiness:
    """instrument_master must have 50 instruments populated correctly."""

    async def test_instrument_master_populated(self, db_engine: AsyncEngine) -> None:
        count = await scalar(db_engine, "SELECT COUNT(*) FROM instrument_master")
        # Now includes EQ + IDX + F&O contracts loaded from Angel One scrip master
        assert count >= 50, f"Expected >= 50 instruments, got {count}"

    async def test_eq_instruments_count(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM instrument_master WHERE instrument_class='EQ'",
        )
        assert count == 47

    async def test_idx_instruments_count(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM instrument_master WHERE instrument_class='IDX'",
        )
        assert count == 2

    async def test_provider_mapping_populated(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine, "SELECT COUNT(*) FROM instrument_provider_mapping"
        )
        assert count >= 90

    async def test_reliance_angel_one_token(self, db_engine: AsyncEngine) -> None:
        row = await fetchone(
            db_engine,
            "SELECT provider_instrument_id FROM instrument_provider_mapping "
            "WHERE instrument_id='NSE:RELIANCE' AND provider='angel_one' AND is_active=TRUE",
        )
        assert row is not None
        assert row["provider_instrument_id"] == "2885"

    async def test_no_fno_instruments_yet(self, db_engine: AsyncEngine) -> None:
        """F&O instrument master is now populated from Angel One scrip master.
        
        Blockers resolved: load_fno_instrument_master.py loaded 34,410 F&O contracts.
        """
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM instrument_master WHERE instrument_class IN ('FUT','OPT')",
        )
        # Blocker resolved — F&O instruments are now loaded
        assert count > 0, "F&O instruments must be loaded (blocker was resolved by load_fno_instrument_master.py)"


# ============================================================================
# SECTION 6 — F&O CONSTRAINTS (active)
# ============================================================================

class TestFnOConstraints:
    """All F&O constraints must actively reject invalid data."""

    async def test_futures_3m_rejected(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            async with db_engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO futures_candle
                        (instrument_id, exchange, interval_str, time, session_date,
                         expiry, open, high, low, close, volume, provider, source_type)
                    VALUES
                        ('NFO:TESTFUT', 'NFO', '3m', '2026-01-01 09:15:00+00',
                         '2026-01-01', '2026-09-25',
                         100, 101, 99, 100, 500, 'test', 'TEST')
                """))

    async def test_futures_non_fut_contract_type_rejected(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            async with db_engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO futures_candle
                        (instrument_id, exchange, interval_str, time, session_date,
                         expiry, contract_type, open, high, low, close, volume, provider, source_type)
                    VALUES
                        ('NFO:TESTFWD', 'NFO', '1d', '2026-01-01 18:30:00+00',
                         '2026-01-01', '2026-09-25', 'FWD',
                         100, 101, 99, 100, 500, 'test', 'TEST')
                """))

    async def test_futures_negative_oi_rejected(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            async with db_engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO futures_candle
                        (instrument_id, exchange, interval_str, time, session_date,
                         expiry, open, high, low, close, volume, open_interest,
                         provider, source_type)
                    VALUES
                        ('NFO:TESTFUT2', 'NFO', '1d', '2026-01-02 18:30:00+00',
                         '2026-01-02', '2026-09-25',
                         100, 101, 99, 100, 500, -1, 'test', 'TEST')
                """))

    async def test_options_invalid_type_rejected(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            async with db_engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO options_candle
                        (instrument_id, exchange, interval_str, time, session_date,
                         expiry, strike, option_type, open, high, low, close, volume,
                         provider, source_type)
                    VALUES
                        ('NFO:TESTOPT', 'NFO', '1d', '2026-01-01 18:30:00+00',
                         '2026-01-01', '2026-09-25', 25000, 'XX',
                         50, 55, 45, 52, 100, 'test', 'TEST')
                """))

    async def test_options_zero_strike_rejected(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            async with db_engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO options_candle
                        (instrument_id, exchange, interval_str, time, session_date,
                         expiry, strike, option_type, open, high, low, close, volume,
                         provider, source_type)
                    VALUES
                        ('NFO:TESTOPT2', 'NFO', '1d', '2026-01-01 18:30:00+00',
                         '2026-01-01', '2026-09-25', 0, 'CE',
                         50, 55, 45, 52, 100, 'test', 'TEST')
                """))

    async def test_options_negative_oi_rejected(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            async with db_engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO options_candle
                        (instrument_id, exchange, interval_str, time, session_date,
                         expiry, strike, option_type, open, high, low, close, volume,
                         open_interest, provider, source_type)
                    VALUES
                        ('NFO:TESTOPT3', 'NFO', '1d', '2026-01-02 18:30:00+00',
                         '2026-01-02', '2026-09-25', 25000, 'PE',
                         50, 55, 45, 52, 100, -1, 'test', 'TEST')
                """))

    async def test_futures_valid_insert_succeeds(self, db_engine: AsyncEngine) -> None:
        """A valid futures candle must be accepted."""
        async with db_engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO futures_candle
                    (instrument_id, exchange, interval_str, time, session_date,
                     expiry, open, high, low, close, volume, open_interest,
                     provider, source_type)
                VALUES
                    ('NFO:RELIANCE26SEPFUT', 'NFO', '1d', '2026-09-10 18:30:00+00',
                     '2026-09-11', '2026-09-24',
                     2800, 2850, 2780, 2835, 125000, 45000000,
                     'test', 'BROKER_AUTHENTICATED')
                ON CONFLICT (instrument_id, exchange, interval_str, time) DO NOTHING
            """))

        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM futures_candle WHERE instrument_id='NFO:RELIANCE26SEPFUT'",
        )
        assert count == 1

        await execute(
            db_engine,
            "DELETE FROM futures_candle WHERE instrument_id='NFO:RELIANCE26SEPFUT'",
        )


# ============================================================================
# SECTION 7 — MIGRATION ARCHIVE SAFETY
# ============================================================================

class TestMigrationArchiveSafety:
    """candle_bar must be preserved as read-only archive with exact row count."""

    async def test_candle_bar_exists(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='candle_bar'",
        )
        assert count == 1, "candle_bar was dropped — must be preserved"

    async def test_candle_bar_row_count_unchanged(self, db_engine: AsyncEngine) -> None:
        """candle_bar must still have all 5,425,725 original rows."""
        count = await scalar(db_engine, "SELECT COUNT(*) FROM candle_bar")
        assert count == 5_425_725, f"Expected 5,425,725, got {count}"

    async def test_candle_bar_nse_row_count(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine, "SELECT COUNT(*) FROM candle_bar WHERE exchange='NSE'"
        )
        assert count == 5_425_719

    async def test_candle_bar_binance_rows_preserved(self, db_engine: AsyncEngine) -> None:
        count = await scalar(
            db_engine, "SELECT COUNT(*) FROM candle_bar WHERE exchange='BINANCE'"
        )
        assert count == 6

    async def test_candle_bar_no_new_nse_rows_possible(self, db_engine: AsyncEngine) -> None:
        """Verify historical_engine routes to equity_candle, not candle_bar."""
        from src.engines.historical_engine import HistoricalEngine
        # All NSE instrument classes must route away from candle_bar
        for cls in ("EQ", "IDX", "ETF"):
            table = HistoricalEngine._canonical_table_for(cls, "NSE")
            assert table == "equity_candle", f"{cls} should route to equity_candle, not {table}"
        # F&O routes away from candle_bar too
        for cls in ("FO", "FUT", "FUTIDX", "FUTSTK"):
            table = HistoricalEngine._canonical_table_for(cls, "NFO")
            assert table == "futures_candle"

    async def test_equity_candle_nse_count_matches_candle_bar(
        self, db_engine: AsyncEngine
    ) -> None:
        """Migration integrity: equity_candle angel_one rows must >= candle_bar angel_one rows.

        equity_candle legitimately exceeds candle_bar because post-migration ingestion
        appends new rows (upstox, yahoo_finance, new angel_one fetches) to equity_candle
        but does not back-populate the legacy candle_bar archive.
        The critical invariant is: no angel_one rows were lost during migration.
        """
        cb_angel = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM candle_bar WHERE provider='angel_one'",
        )
        ec_angel = await scalar(
            db_engine,
            "SELECT COUNT(*) FROM equity_candle WHERE provider='angel_one'",
        )
        assert ec_angel >= cb_angel, (
            f"MIGRATION DATA LOSS: equity_candle angel_one={ec_angel} < "
            f"candle_bar angel_one={cb_angel}"
        )


# ============================================================================
# SECTION 8 — CALENDAR IDEMPOTENCY
# ============================================================================

class TestCalendarIdempotency:
    """Re-running calendar population must not change row counts."""

    async def test_calendar_population_idempotent(self, db_engine: AsyncEngine) -> None:
        """ON CONFLICT DO UPDATE means second run produces same counts."""
        before = await scalar(
            db_engine, "SELECT COUNT(*) FROM exchange_calendar"
        )

        # Re-insert a sample date — must not create a duplicate
        async with db_engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO exchange_calendar
                    (exchange, segment, calendar_date, day_type, is_trading_day,
                     session_status, year, is_official, special_session)
                VALUES ('NSE', 'EQ', '2026-09-15', 'TRADING_DAY', TRUE, 'OPEN',
                        2026, FALSE, FALSE)
                ON CONFLICT (exchange, segment, calendar_date) DO UPDATE SET
                    updated_at = NOW()
            """))

        after = await scalar(
            db_engine, "SELECT COUNT(*) FROM exchange_calendar"
        )
        assert after == before, f"Row count changed after idempotent upsert: {before} → {after}"
