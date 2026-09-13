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


def main() -> None:
    """CLI entry point for ``data-service-scheduler`` script."""
    try:
        asyncio.run(_run_scheduler())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
