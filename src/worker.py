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

    # ── Initialise provider adapters for OHLCV catch-up ─────────────────
    # Pre-authenticate so the catch-up task reuses a single shared session.
    hist_engine = None
    try:
        from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415

        hist_engine = HistoricalEngine()

        if (settings.angel_one_api_key and settings.angel_one_client_id
                and settings.angel_one_totp_secret):
            try:
                from src.providers.adapters.angel_one import AngelOneAdapter  # noqa: PLC0415
                angel = AngelOneAdapter(
                    api_key=settings.angel_one_api_key,
                    client_id=settings.angel_one_client_id,
                    totp_secret=settings.angel_one_totp_secret,
                    mpin=settings.angel_one_mpin,
                    redis_client=redis,
                )
                await angel.ensure_authenticated()
                hist_engine._angel_one_adapter = angel
                await logger.ainfo("worker_angel_one_authenticated")
            except Exception as exc:  # noqa: BLE001
                await logger.awarning("worker_angel_one_auth_failed", error=str(exc))

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
                hist_engine._upstox_adapter = upstox
                await logger.ainfo("worker_upstox_adapter_ready")
            except Exception as exc:  # noqa: BLE001
                await logger.awarning("worker_upstox_init_failed", error=str(exc))

        if db_engine is not None:
            hist_engine._db_engine = db_engine

        await logger.ainfo("worker_historical_engine_ready")
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("worker_historical_engine_init_failed", error=str(exc))

    # ── Main worker loop — OHLCV catch-up + graceful shutdown ────────────
    tasks = []

    if hist_engine is not None and db_engine is not None and redis is not None:
        from src.worker_tasks.ohlcv_catchup import run_ohlcv_catchup  # noqa: PLC0415

        tasks.append(
            asyncio.create_task(
                run_ohlcv_catchup(
                    db_engine=db_engine,
                    redis_client=redis,
                    hist_engine=hist_engine,
                    stop_event=stop_event,
                ),
                name="ohlcv_catchup",
            )
        )
        await logger.ainfo("worker_ohlcv_catchup_task_started")
    else:
        await logger.awarning(
            "worker_ohlcv_catchup_skipped",
            note="Missing db_engine, redis, or historical_engine — catch-up disabled",
        )

    # Wait for stop signal
    await stop_event.wait()

    # Cancel all running tasks gracefully
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

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
