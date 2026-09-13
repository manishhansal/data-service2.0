"""
DATA-SERVICE 2.0 — background worker entry point.

Responsibilities:
- Backfill orchestration and chunked historical OHLCV acquisition
- Gap detection and gap recovery (up to configured max retries)
- Cross-provider OHLCV reconciliation
- Provenance persistence retries
- DataIncident archival

Run: ``python -m src.worker``
"""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog


async def _run_worker() -> None:
    """Initialise all worker subsystems and run until SIGTERM/SIGINT."""
    from src.core.settings import get_settings  # noqa: PLC0415
    from src.observability.logging import configure_logging  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    logger = structlog.get_logger(__name__)

    await logger.ainfo("worker_starting", component="worker", version="2.0.0")

    # Redis
    redis = None
    try:
        from src.cache.redis_client import create_redis_pool  # noqa: PLC0415

        redis = await create_redis_pool(settings.redis_url)
        await logger.ainfo("worker_redis_connected")
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("worker_redis_unavailable", error=str(exc))

    # PostgreSQL
    db_engine = None
    try:
        from src.db.engine import create_async_engine_from_settings  # noqa: PLC0415

        db_engine = await create_async_engine_from_settings(settings)
        await logger.ainfo("worker_postgres_connected")
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("worker_postgres_unavailable", error=str(exc))

    # Graceful shutdown event
    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        asyncio.get_event_loop().call_soon_threadsafe(stop_event.set)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    await logger.ainfo("worker_ready", component="worker")

    # ── Main worker loop ─────────────────────────────────────────────────
    # Each task group will be populated in later implementation tasks.
    # For now we just idle until stop signal.
    try:
        await stop_event.wait()
    finally:
        await logger.ainfo("worker_shutting_down", component="worker")

        if redis is not None:
            await redis.aclose()
        if db_engine is not None:
            await db_engine.dispose()

        await logger.ainfo("worker_stopped", component="worker")


def main() -> None:
    """CLI entry point for ``data-service-worker`` script."""
    try:
        asyncio.run(_run_worker())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
