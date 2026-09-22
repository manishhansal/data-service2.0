"""
DATA-SERVICE 2.0 — scheduler entry point.

Scheduled jobs:
- 08:45 IST daily  : F&O universe refresh (Requirement 11.1)
- 23:55 IST daily  : Angel One JWT rotation (Requirement 19.7)
- Every ≤30 seconds: NTP clock-skew sample (Requirement 18.8)

Run: ``python -m src.scheduler``
"""

from __future__ import annotations

import asyncio
import signal
import sys
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

IST = ZoneInfo("Asia/Kolkata")


async def _run_scheduler() -> None:
    """Initialise APScheduler and run until SIGTERM/SIGINT."""
    from src.core.settings import get_settings  # noqa: PLC0415
    from src.observability.logging import configure_logging  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    logger = structlog.get_logger(__name__)

    await logger.ainfo("scheduler_starting", component="scheduler", version="2.0.0")

    scheduler = AsyncIOScheduler(timezone=IST)

    # ── Job: F&O universe refresh at 08:45 IST (Req 11.1) ───────────────
    scheduler.add_job(
        _fno_universe_refresh,
        trigger=CronTrigger(hour=8, minute=45, timezone=IST),
        id="fno_universe_refresh",
        name="F&O Universe Refresh",
        replace_existing=True,
        misfire_grace_time=300,  # allow up to 5-min late start
    )

    # ── Job: OHLCV catch-up at 17:30 IST (post-market close) ────────────
    # Runs a full EOD + intraday catch-up for all instruments.
    # This is a safety net — the worker process runs continuously and handles
    # catch-up in near-real-time. This job fires in case the worker was down.
    scheduler.add_job(
        _ohlcv_eod_catchup,
        trigger=CronTrigger(hour=17, minute=30, timezone=IST),
        id="ohlcv_eod_catchup",
        name="OHLCV EOD Catch-up",
        replace_existing=True,
        misfire_grace_time=1800,  # allow up to 30-min late start
    )

    # ── Job: OHLCV intraday refresh every 4 hours ────────────────────────
    # Keeps intraday candles current throughout the trading day and overnight.
    scheduler.add_job(
        _ohlcv_intraday_refresh,
        trigger="interval",
        hours=4,
        id="ohlcv_intraday_refresh",
        name="OHLCV Intraday Refresh",
        replace_existing=True,
    )

    # ── Job: Angel One JWT rotation at 23:55 IST (Req 19.7) ─────────────
    scheduler.add_job(
        _angel_one_jwt_rotation,
        trigger=CronTrigger(hour=23, minute=55, timezone=IST),
        id="angel_one_jwt_rotation",
        name="Angel One JWT Rotation",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # ── Job: NTP clock-skew monitor every 30 seconds (Req 18.8) ─────────
    scheduler.add_job(
        _clock_skew_sample,
        trigger="interval",
        seconds=30,
        id="clock_skew_monitor",
        name="NTP Clock Skew Monitor",
        replace_existing=True,
    )

    scheduler.start()
    await logger.ainfo("scheduler_ready", component="scheduler", jobs=len(scheduler.get_jobs()))

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        asyncio.get_event_loop().call_soon_threadsafe(stop_event.set)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await stop_event.wait()
    finally:
        await logger.ainfo("scheduler_shutting_down", component="scheduler")
        scheduler.shutdown(wait=True)
        await logger.ainfo("scheduler_stopped", component="scheduler")


# ── Job implementations (stubs — detailed logic added in later tasks) ────────

async def _fno_universe_refresh() -> None:
    """Refresh F&O universe at 08:45 IST — stub (implemented in Task 3.3)."""
    logger = structlog.get_logger(__name__)
    await logger.ainfo(
        "fno_universe_refresh_triggered",
        component="scheduler",
        event="fno_universe_refresh",
    )
    try:
        from src.engines.fno_universe import FnoUniverseService  # noqa: PLC0415

        await FnoUniverseService.refresh()
    except (ImportError, ModuleNotFoundError):
        await logger.awarning("fno_universe_service_not_yet_implemented", component="scheduler")
    except Exception as exc:  # noqa: BLE001
        await logger.aerror(
            "fno_universe_refresh_failed",
            component="scheduler",
            error=str(exc),
        )


async def _angel_one_jwt_rotation() -> None:
    """Rotate Angel One JWT at 23:55 IST — stub (implemented in Task 13.5)."""
    logger = structlog.get_logger(__name__)
    await logger.ainfo(
        "angel_one_jwt_rotation_triggered",
        component="scheduler",
        event="angel_one_jwt_rotation",
    )
    try:
        from src.scheduler_jobs.angel_one_jwt_rotation import rotate_jwt  # noqa: PLC0415

        await rotate_jwt()
    except (ImportError, ModuleNotFoundError):
        await logger.awarning(
            "angel_one_jwt_rotation_not_yet_implemented", component="scheduler"
        )
    except Exception as exc:  # noqa: BLE001
        await logger.aerror(
            "angel_one_jwt_rotation_failed",
            component="scheduler",
            error=str(exc),
        )


async def _clock_skew_sample() -> None:
    """Sample NTP clock offset — stub (implemented in Task 9.8)."""
    try:
        from src.observability.clock_monitor import sample_clock_skew  # noqa: PLC0415

        await sample_clock_skew()
    except (ImportError, ModuleNotFoundError):
        pass  # Not yet implemented — silent until Task 9.8
    except Exception:  # noqa: BLE001
        pass  # Never let clock monitor crash the scheduler


async def _ohlcv_eod_catchup() -> None:
    """Run EOD OHLCV catch-up for all instruments (1d, 1w, 1M).

    Fired at 17:30 IST daily — post-market close safety net.
    The worker process handles continuous catch-up; this job fires when
    the worker was temporarily down or missed an update.
    """
    logger = structlog.get_logger(__name__)
    await logger.ainfo(
        "ohlcv_eod_catchup_triggered",
        component="scheduler",
        event="ohlcv_eod_catchup",
    )
    try:
        from src.core.settings import get_settings  # noqa: PLC0415
        from src.db.engine import create_async_engine_from_settings  # noqa: PLC0415
        from src.cache.redis_client import create_redis_pool  # noqa: PLC0415
        from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415
        from src.worker_tasks.ohlcv_catchup import _load_instruments, _run_pass  # noqa: PLC0415

        settings = get_settings()
        db_engine = await create_async_engine_from_settings(settings)
        redis     = await create_redis_pool(settings.redis_url)
        engine    = HistoricalEngine()

        if (settings.angel_one_api_key and settings.angel_one_client_id
                and settings.angel_one_totp_secret):
            try:
                from src.providers.adapters.angel_one import AngelOneAdapter  # noqa: PLC0415
                angel = AngelOneAdapter(
                    api_key=settings.angel_one_api_key,
                    client_id=settings.angel_one_client_id,
                    totp_secret=settings.angel_one_totp_secret,
                    mpin=settings.angel_one_mpin,
                )
                await angel.ensure_authenticated()
                engine._angel_one_adapter = angel
            except Exception as exc:  # noqa: BLE001
                await logger.awarning("scheduler_ohlcv_angel_auth_failed", error=str(exc))

        if settings.upstox_api_key and (settings.upstox_access_token
                                          or settings.upstox_analytics_key):
            try:
                from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
                upstox = UpstoxAdapter(
                    api_key=settings.upstox_api_key,
                    api_secret=settings.upstox_api_secret or "",
                    redirect_uri=(settings.upstox_redirect_uri
                                  or "http://localhost:8200/v1/auth/upstox/callback"),
                )
                if settings.upstox_analytics_key:
                    await upstox.set_analytics_token(settings.upstox_analytics_key)
                if settings.upstox_access_token:
                    await upstox.set_access_token(settings.upstox_access_token)
                engine._upstox_adapter = upstox
            except Exception as exc:  # noqa: BLE001
                await logger.awarning("scheduler_ohlcv_upstox_init_failed", error=str(exc))

        engine._db_engine = db_engine
        instruments = await _load_instruments(db_engine)
        stop_event  = asyncio.Event()  # never set — runs to completion

        await _run_pass(
            pass_name="EOD_SCHEDULER",
            intervals=["1d", "1w", "1M"],
            instruments=instruments,
            hist_engine=engine,
            db_engine=db_engine,
            redis_client=redis,
            stop_event=stop_event,
            concurrency=2,
            chunk_delay_s=0.3,
            instrument_delay_s=0.2,
        )

        if hasattr(engine, "_angel_one_adapter") and engine._angel_one_adapter:
            await engine._angel_one_adapter.close()
        if hasattr(engine, "_upstox_adapter") and engine._upstox_adapter:
            await engine._upstox_adapter.aclose()
        await redis.aclose()
        await db_engine.dispose()

        await logger.ainfo("ohlcv_eod_catchup_completed", component="scheduler")

    except Exception as exc:  # noqa: BLE001
        await logger.aerror("ohlcv_eod_catchup_failed", component="scheduler", error=str(exc))


async def _ohlcv_intraday_refresh() -> None:
    """Run intraday OHLCV refresh for all instruments (1m–1h).

    Fired every 4 hours. Supplements the worker's continuous loop.
    """
    logger = structlog.get_logger(__name__)
    await logger.ainfo(
        "ohlcv_intraday_refresh_triggered",
        component="scheduler",
        event="ohlcv_intraday_refresh",
    )
    try:
        from src.core.settings import get_settings  # noqa: PLC0415
        from src.db.engine import create_async_engine_from_settings  # noqa: PLC0415
        from src.cache.redis_client import create_redis_pool  # noqa: PLC0415
        from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415
        from src.worker_tasks.ohlcv_catchup import _load_instruments, _run_pass  # noqa: PLC0415

        settings  = get_settings()
        db_engine = await create_async_engine_from_settings(settings)
        redis     = await create_redis_pool(settings.redis_url)
        engine    = HistoricalEngine()

        if (settings.angel_one_api_key and settings.angel_one_client_id
                and settings.angel_one_totp_secret):
            try:
                from src.providers.adapters.angel_one import AngelOneAdapter  # noqa: PLC0415
                angel = AngelOneAdapter(
                    api_key=settings.angel_one_api_key,
                    client_id=settings.angel_one_client_id,
                    totp_secret=settings.angel_one_totp_secret,
                    mpin=settings.angel_one_mpin,
                )
                await angel.ensure_authenticated()
                engine._angel_one_adapter = angel
            except Exception as exc:  # noqa: BLE001
                await logger.awarning("scheduler_intraday_angel_failed", error=str(exc))

        if settings.upstox_api_key and (settings.upstox_access_token
                                          or settings.upstox_analytics_key):
            try:
                from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
                upstox = UpstoxAdapter(
                    api_key=settings.upstox_api_key,
                    api_secret=settings.upstox_api_secret or "",
                    redirect_uri=(settings.upstox_redirect_uri
                                  or "http://localhost:8200/v1/auth/upstox/callback"),
                )
                if settings.upstox_analytics_key:
                    await upstox.set_analytics_token(settings.upstox_analytics_key)
                if settings.upstox_access_token:
                    await upstox.set_access_token(settings.upstox_access_token)
                engine._upstox_adapter = upstox
            except Exception as exc:  # noqa: BLE001
                await logger.awarning("scheduler_intraday_upstox_failed", error=str(exc))

        engine._db_engine = db_engine
        instruments = await _load_instruments(db_engine)
        stop_event  = asyncio.Event()

        await _run_pass(
            pass_name="INTRADAY_SCHEDULER",
            intervals=["1m", "5m", "10m", "15m", "30m", "1h"],
            instruments=instruments,
            hist_engine=engine,
            db_engine=db_engine,
            redis_client=redis,
            stop_event=stop_event,
            concurrency=2,
            chunk_delay_s=0.4,
            instrument_delay_s=0.2,
        )

        if hasattr(engine, "_angel_one_adapter") and engine._angel_one_adapter:
            await engine._angel_one_adapter.close()
        if hasattr(engine, "_upstox_adapter") and engine._upstox_adapter:
            await engine._upstox_adapter.aclose()
        await redis.aclose()
        await db_engine.dispose()

        await logger.ainfo("ohlcv_intraday_refresh_completed", component="scheduler")

    except Exception as exc:  # noqa: BLE001
        await logger.aerror("ohlcv_intraday_refresh_failed", component="scheduler", error=str(exc))


def main() -> None:
    """CLI entry point for ``data-service-scheduler`` script."""
    try:
        asyncio.run(_run_scheduler())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
