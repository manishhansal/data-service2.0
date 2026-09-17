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

    # ── Angel One adapter (live quotes, option chain, broker analytics) ──────
    # Credentials from env vars — never logged.
    # Redis is injected so workers share the JWT token (prevents TOTP conflict).
    app.state.angel_one_adapter = None
    if (
        settings.angel_one_api_key
        and settings.angel_one_client_id
        and settings.angel_one_totp_secret
    ):
        try:
            from src.providers.adapters.angel_one import AngelOneAdapter  # noqa: PLC0415
            adapter = AngelOneAdapter(
                api_key=settings.angel_one_api_key,
                client_id=settings.angel_one_client_id,
                totp_secret=settings.angel_one_totp_secret,
                mpin=settings.angel_one_mpin,
                redis_client=app.state.redis,  # enables cross-worker JWT sharing
            )
            await adapter.ensure_authenticated()
            app.state.angel_one_adapter = adapter
            await logger.ainfo("angel_one_adapter_authenticated", provider="angel_one")
        except Exception as exc:  # noqa: BLE001
            await logger.awarning(
                "angel_one_adapter_auth_failed",
                error=str(exc),
                note="live quotes and option chain will return null values",
            )
    else:
        await logger.awarning(
            "angel_one_credentials_not_configured",
            note="Set ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_ID, ANGEL_ONE_TOTP_SECRET",
            degraded=True,
        )

    # ── Upstox adapter (historical OHLCV, live quotes, index intraday) ──────────
    # Uses the pre-obtained OAuth access token from UPSTOX_ACCESS_TOKEN env var.
    # When present the adapter is ready immediately — no OAuth round-trip needed.
    app.state.upstox_adapter = None
    if settings.upstox_access_token and settings.upstox_api_key:
        try:
            from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
            upstox_adapter = UpstoxAdapter(
                api_key=settings.upstox_api_key,
                api_secret=settings.upstox_api_secret or "",
                redirect_uri=settings.upstox_redirect_uri or "http://localhost:8200/v1/auth/upstox/callback",
            )
            await upstox_adapter.set_access_token(settings.upstox_access_token)
            app.state.upstox_adapter = upstox_adapter
            await logger.ainfo("upstox_adapter_ready", provider="upstox")
        except Exception as exc:  # noqa: BLE001
            await logger.awarning(
                "upstox_adapter_init_failed",
                error=str(exc),
                note="Upstox data will be unavailable",
            )
    else:
        await logger.awarning(
            "upstox_credentials_not_configured",
            note="Set UPSTOX_ACCESS_TOKEN and UPSTOX_API_KEY for Upstox data",
            degraded=True,
        )

    # ── Market engine (uses real Angel One adapter when available) ────────────
    try:
        from src.engines.market_engine import MarketEngine  # noqa: PLC0415
        market_engine = MarketEngine(angel_one_adapter=app.state.angel_one_adapter)
        app.state.market_engine = market_engine
        await logger.ainfo(
            "market_engine_ready",
            real_provider=app.state.angel_one_adapter is not None,
        )
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("market_engine_init_failed", error=str(exc))

    # ── InstrumentMasterService (loads instrument_master table into memory) ──
    app.state.instrument_master = None
    if app.state.db_engine is not None:
        try:
            from src.engines.instrument_master import InstrumentMasterService  # noqa: PLC0415
            im_service = InstrumentMasterService()
            await im_service.load_from_db(app.state.db_engine)
            app.state.instrument_master = im_service
            await logger.ainfo(
                "instrument_master_loaded",
                instrument_count=len(im_service._instruments),
            )
            # ── Inject into MarketEngine so live quote token resolution works ──
            if hasattr(app.state, "market_engine") and app.state.market_engine is not None:
                app.state.market_engine.set_instrument_master(im_service)
                await logger.ainfo("market_engine_instrument_master_injected")
            # Inject DB engine for market_quote persistence
            if hasattr(app.state, "market_engine") and app.state.market_engine is not None:
                app.state.market_engine.set_db_engine(app.state.db_engine)
                await logger.ainfo("market_engine_db_engine_injected")
        except Exception as exc:  # noqa: BLE001
            await logger.awarning(
                "instrument_master_load_failed",
                error=str(exc),
                note="Instrument lookups will return empty results",
            )

    # ── HistoricalEngine — inject shared authenticated adapters ──────────────
    # Pre-create the HistoricalEngine and inject the shared adapter instances
    # so backfill jobs reuse the active JWT session (no new TOTP logins needed).
    try:
        from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415
        hist_engine = HistoricalEngine()
        if app.state.angel_one_adapter is not None:
            hist_engine._angel_one_adapter = app.state.angel_one_adapter
        if app.state.upstox_adapter is not None:
            hist_engine._upstox_adapter = app.state.upstox_adapter
        if app.state.db_engine is not None:
            hist_engine._db_engine = app.state.db_engine
        app.state.historical_engine = hist_engine
        await logger.ainfo("historical_engine_ready")
    except Exception as exc:  # noqa: BLE001
        await logger.awarning("historical_engine_init_failed", error=str(exc))

    # ── DualProviderEngine — inject db_engine if available ────────────────
    if app.state.db_engine is not None:
        try:
            from src.engines.dual_provider_engine import DualProviderEngine  # noqa: PLC0415
            dual_engine = DualProviderEngine(
                angel_one_adapter=app.state.angel_one_adapter,
                upstox_adapter=app.state.upstox_adapter,
            )
            dual_engine.set_db_engine(app.state.db_engine)
            app.state.dual_provider_engine = dual_engine
            await logger.ainfo("dual_provider_engine_ready")
        except Exception as exc:  # noqa: BLE001
            await logger.awarning("dual_provider_engine_init_failed", error=str(exc))

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

    # ── Unauthenticated routes (health, metrics, auth token exchange) ────
    _try_include(app, "src.api.health", prefix="", tags=["Health"])
    _try_include(app, "src.api.metrics", prefix="", tags=["Metrics"])
    _try_include(app, "src.auth.consumer_auth", prefix="/v1", tags=["Auth"])
    # ── AlphaForge ScraplingProvider compatibility routes (unauthenticated) ──
    # These /scraping/* endpoints translate AlphaForge's ScraplingProvider
    # calls into data-service2.0 backend logic.  They do NOT require API key
    # authentication because they are called from AlphaForge's server-side
    # only (not exposed to the browser) and are on the same internal network.
    # If external exposure is required, add auth via CONSUMER_API_KEYS.
    # Also exposes /data/gate for backward compatibility with the gate-client.ts
    _try_include(app, "src.api.compat", prefix="", tags=["Compat"])

    # ── Authenticated routes — require API key or JWT bearer ─────────────
    # DS2-RCA-016 fix: all data routes require consumer authentication.
    # ConsumerAuthDependency is applied as a router-level dependency so every
    # endpoint in these modules is protected without modifying each handler.
    try:
        from src.auth.consumer_auth import ConsumerAuthDependency  # noqa: PLC0415
        _auth_dep = ConsumerAuthDependency()
    except Exception:  # noqa: BLE001
        _auth_dep = None  # Degraded: auth not available — log and continue

    def _include_protected(module_path: str, *, prefix: str, tags: list[str]) -> None:
        """Include a router with authentication dependency applied."""
        try:
            import importlib  # noqa: PLC0415
            module = importlib.import_module(module_path)
            router = getattr(module, "router", None)
            if router is None:
                return
            if _auth_dep is not None:
                from fastapi import Depends  # noqa: PLC0415
                import fastapi  # noqa: PLC0415
                # Apply auth as a router-level dependency via include_router
                app.include_router(
                    router,
                    prefix=prefix,
                    tags=tags,
                    dependencies=[Depends(_auth_dep)],
                )
            else:
                app.include_router(router, prefix=prefix, tags=tags)
        except (ImportError, ModuleNotFoundError):
            pass

    _include_protected("src.api.instruments", prefix="/v1", tags=["Instruments"])
    _include_protected("src.api.india", prefix="/v1", tags=["India Markets"])
    _include_protected("src.api.broker_analytics", prefix="/v1", tags=["Broker Analytics"])
    _include_protected("src.api.crypto", prefix="/v1", tags=["Crypto Markets"])
    _include_protected("src.api.deribit", prefix="/v1", tags=["Deribit"])
    _include_protected("src.api.quality", prefix="/v1", tags=["Quality"])
    _include_protected("src.api.providers", prefix="/v1", tags=["Providers"])
    _include_protected("src.api.lineage", prefix="/v1", tags=["Lineage"])
    _include_protected("src.api.provenance", prefix="/v1", tags=["Provenance"])
    # src.api.streaming is intentionally NOT wrapped with _include_protected.
    # FastAPI's APIKeyHeader dependency expects an HTTP Request object, but
    # WebSocket connections inject a WebSocket — causing a TypeError at
    # connection time.  The streaming module handles auth inline instead.
    _try_include(app, "src.api.streaming", prefix="/v1", tags=["Streaming"])
    _include_protected("src.api.internal", prefix="/v1", tags=["Internal"])
    _include_protected("src.api.analytics", prefix="/v1", tags=["Analytics"])
    _include_protected("src.api.replay", prefix="/v1", tags=["Replay"])


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
