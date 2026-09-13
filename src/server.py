"""
DATA-SERVICE 2.0 — FastAPI application factory.

Entry point: ``uvicorn src.server:app --host 0.0.0.0 --port 8200``
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Deferred imports so settings are resolved at startup, not import time.
# Each subsystem's router / middleware is imported inside the lifespan to
# allow the settings module to be bootstrapped first.
# ---------------------------------------------------------------------------

_START_TIME_MS: float = time.monotonic() * 1000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — startup and graceful shutdown."""
    # ── Startup ──────────────────────────────────────────────────────────
    from src.observability.logging import configure_logging  # noqa: PLC0415
    from src.observability.tracing import configure_tracing  # noqa: PLC0415
    from src.core.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    configure_tracing(settings.otel_exporter)

    import structlog  # noqa: PLC0415

    logger = structlog.get_logger(__name__)
    await logger.ainfo("data_service_starting", version="2.0.0", port=settings.data_service_port)

    # Attach start time to app state so health endpoint can compute uptimeMs.
    app.state.start_time_ms = _START_TIME_MS
    app.state.settings = settings

    # Redis connection
    try:
        from src.cache.redis_client import create_redis_pool  # noqa: PLC0415

        app.state.redis = await create_redis_pool(settings.redis_url)
        await logger.ainfo("redis_connected", url=settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("redis_unavailable", error=str(exc), degraded=True)
        app.state.redis = None

    # PostgreSQL connection
    try:
        from src.db.engine import create_async_engine_from_settings  # noqa: PLC0415

        app.state.db_engine = await create_async_engine_from_settings(settings)
        await logger.ainfo("postgres_connected")
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("postgres_unavailable", error=str(exc), degraded=True)
        app.state.db_engine = None

    await logger.ainfo("data_service_ready", version="2.0.0")

    yield  # ── Application running ────────────────────────────────────────

    # ── Shutdown ─────────────────────────────────────────────────────────
    await logger.ainfo("data_service_shutting_down")

    # Flush and shut down OpenTelemetry spans
    from src.observability.tracing import shutdown_tracing  # noqa: PLC0415

    shutdown_tracing()

    # Close Redis pool
    if app.state.redis is not None:
        await app.state.redis.aclose()
        await logger.ainfo("redis_pool_closed")

    # Dispose PostgreSQL engine
    if app.state.db_engine is not None:
        await app.state.db_engine.dispose()
        await logger.ainfo("postgres_engine_disposed")

    await logger.ainfo("data_service_stopped")


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    from src.core.settings import get_settings  # noqa: PLC0415

    settings = get_settings()

    app = FastAPI(
        title="DATA-SERVICE 2.0",
        description=(
            "Standalone, production-grade Market Data Platform. "
            "Single market-data authority for AlphaForge and all future consumers."
        ),
        version="2.0.0",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )

    # ── CORS ─────────────────────────────────────────────────────────────
    # Wildcard CORS is prohibited on production endpoints (Req 19.6).
    # Origins are loaded from CORS_ALLOWED_ORIGINS env var.
    allowed_origins: list[str] = (
        [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
        if settings.cors_allowed_origins
        else []
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    # ── Credential stripping (Req 16.8, 19.4) ────────────────────────────
    # Must run after CORS so credential fields never appear in responses.
    from src.middleware.credential_stripper import CredentialStripperMiddleware  # noqa: PLC0415

    app.add_middleware(CredentialStripperMiddleware)

    # ── Routers (registered here; implemented in later tasks) ────────────
    _register_routers(app)

    return app


def _register_routers(app: FastAPI) -> None:
    """Register all API routers under the /v1/ prefix."""
    # Each router module is imported lazily inside this function.
    # If a router module doesn't exist yet (future tasks), we skip it
    # gracefully so the server still boots during incremental development.

    _try_include(app, "src.api.health", prefix="", tags=["Health"])
    _try_include(app, "src.api.metrics", prefix="", tags=["Metrics"])
    _try_include(app, "src.api.instruments", prefix="/v1", tags=["Instruments"])
    _try_include(app, "src.api.india", prefix="/v1", tags=["India Markets"])
    _try_include(app, "src.api.crypto", prefix="/v1", tags=["Crypto Markets"])
    _try_include(app, "src.api.deribit", prefix="/v1", tags=["Deribit"])
    _try_include(app, "src.api.quality", prefix="/v1", tags=["Quality"])
    _try_include(app, "src.api.providers", prefix="/v1", tags=["Providers"])
    _try_include(app, "src.api.lineage", prefix="/v1", tags=["Lineage"])
    _try_include(app, "src.api.provenance", prefix="/v1", tags=["Provenance"])
    _try_include(app, "src.api.streaming", prefix="/v1", tags=["Streaming"])
    _try_include(app, "src.api.internal", prefix="/v1", tags=["Internal"])
    _try_include(app, "src.api.analytics", prefix="/v1", tags=["Analytics"])
    _try_include(app, "src.auth.consumer_auth", prefix="/v1", tags=["Auth"])
    _try_include(app, "src.api.replay", prefix="/v1", tags=["Replay"])


def _try_include(app: FastAPI, module_path: str, *, prefix: str, tags: list[str]) -> None:
    """Attempt to import a router module and include it; skip if not yet implemented."""
    try:
        import importlib  # noqa: PLC0415

        module = importlib.import_module(module_path)
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router, prefix=prefix, tags=tags)
    except (ImportError, ModuleNotFoundError):
        pass  # Module not yet created — safe to skip during incremental build


# Module-level app instance consumed by Uvicorn.
app: FastAPI = create_app()


def main() -> None:
    """CLI entry point for ``data-service-api`` script."""
    import uvicorn  # noqa: PLC0415

    from src.core.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",  # noqa: S104
        port=settings.data_service_port,
        workers=settings.uvicorn_workers,
        loop="uvloop",
        log_config=None,  # structlog handles all logging
        access_log=False,
    )


if __name__ == "__main__":
    main()
