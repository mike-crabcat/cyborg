"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cyborg_server import __version__
from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.exceptions import ServiceError
from cyborg_server.heartbeat import (
    CallCleanupTask,
    EmailPollingTask,
    EmailSyncTask,
    HeartbeatRunner,
    LLMCallStalenessTask,
    SessionIdleSummaryTask,
)
from cyborg_server.services.routine_scheduler import RoutineSchedulerTask
from cyborg_server.models import HealthResponse
from cyborg_server.routers import calendars, contacts, context, dashboard_api, dashboard_ws, email, session_routes, webhooks, whatsapp
from cyborg_server.services.event_bus import EventBus
from cyborg_server.structured_logging import configure_logging, CorrelationIdMiddleware

logger = logging.getLogger(__name__)

def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    resolved_settings = settings or Settings.from_env()

    # Configure structured logging
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings.ensure_directories()
        database = Database(
            db_path=resolved_settings.db_path,
            schema_dir=Path(__file__).parent / "schemas",
            pool_size=resolved_settings.pool_size,
        )
        await database.connect()
        await database.apply_migrations()
        database.settings = resolved_settings
        app.state.settings = resolved_settings
        app.state.db = database

        app_ctx = AppContext(db=database, settings=resolved_settings)

        # Clean up stale subagents from previous runs
        try:
            from cyborg_server.services.subagent_service import SubagentService
            await SubagentService(app_ctx).cleanup_stale()
        except Exception:
            logger.debug("Subagent cleanup skipped (table may not exist yet)")

        event_bus = EventBus()
        app_ctx.event_bus = event_bus
        app.state.event_bus = event_bus

        # Conditional voice engine preload
        if resolved_settings.voice.enabled:
            try:
                from cyborg_server.services.voice_engines import VoiceEngineManager

                voice_engines = VoiceEngineManager(resolved_settings.voice)
                await voice_engines.preload()
                app.state.voice_engines = voice_engines
                app_ctx.voice_engines = voice_engines
            except ImportError:
                logger.warning("Voice dependencies not installed — install with: pip install cyborg-server[voice]")
                resolved_settings.voice.enabled = False
            except Exception:
                logger.exception("Voice engine preload failed — disabling voice")
                resolved_settings.voice.enabled = False

        # Attach database log handler for structured logging
        from cyborg_server.structured_logging import attach_database_handler
        attach_database_handler(database)

        # Conditional WhatsApp bridge service
        wa_bridge_service = None
        if resolved_settings.whatsapp_bridge.enabled:
            try:
                from cyborg_server.services.whatsapp_bridge_service import WhatsAppBridgeService
                wa_bridge_service = WhatsAppBridgeService(app_ctx)
                await wa_bridge_service.start()
                app.state.whatsapp_bridge_service = wa_bridge_service
                app_ctx.whatsapp_bridge = wa_bridge_service
                logger.info("WhatsApp bridge service started")
            except Exception:
                logger.exception("WhatsApp bridge service failed to start")

        stop_event = asyncio.Event()
        runner = HeartbeatRunner(app_ctx, interval_seconds=resolved_settings.heartbeat_interval_seconds)
        runner.register(EmailPollingTask())
        runner.register(EmailSyncTask())
        runner.register(CallCleanupTask())
        runner.register(SessionIdleSummaryTask())
        runner.register(LLMCallStalenessTask())
        runner.register(RoutineSchedulerTask())
        heartbeat_worker = asyncio.create_task(runner.run_loop(stop_event))
        try:
            yield
        finally:
            stop_event.set()

            heartbeat_worker.cancel()
            try:
                await heartbeat_worker
            except asyncio.CancelledError:
                pass

            if wa_bridge_service is not None:
                await wa_bridge_service.stop()

            await database.close()

    # Create FastAPI app
    app = FastAPI(
        title="Cyborg Data Service",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Add correlation ID middleware
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error_handler(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health_check(request: Request) -> HealthResponse:
        database: Database = request.app.state.db
        healthy = await database.health_check()
        if not healthy:
            raise RuntimeError("Database health check failed")
        return HealthResponse(status="ok", database="ok")

    app.include_router(calendars.router)
    app.include_router(context.router)
    app.include_router(session_routes.router)
    app.include_router(webhooks.router, prefix="/api/v1/webhooks")
    app.include_router(contacts.router, prefix="/api/v1")
    app.include_router(email.router)

    # Dashboard API (HTTP) + WebSocket (live events)
    app.include_router(dashboard_api.router, prefix="/dashboard")
    app.include_router(dashboard_ws.router, prefix="/dashboard")

    # Dashboard SPA static files (must be last dashboard-related mount)
    dashboard_dist = Path(__file__).parent / "ui_dist"
    if dashboard_dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/dashboard", StaticFiles(directory=str(dashboard_dist), html=True), name="dashboard_spa")
        logger.info("Dashboard SPA mounted from %s", dashboard_dist)

    # Conditional voice chat router
    if resolved_settings.voice.enabled:
        from cyborg_server.routers import voice as voice_router
        app.include_router(voice_router.router, prefix="/voice")
        voice_router.mount_frontend(app, resolved_settings.voice.frontend_dir)

    # Conditional phone/telephony router (requires voice)
    if resolved_settings.phone.enabled:
        from cyborg_server.routers import phone as phone_router
        app.include_router(phone_router.router, prefix="/phone")

    # Conditional WhatsApp bridge router
    if resolved_settings.whatsapp_bridge.enabled:
        app.include_router(whatsapp.router)

    # Conditional OpenAI evaluation router
    if resolved_settings.openai.enabled:
        try:
            from cyborg_server.routers import openai_llm as openai_router
            app.include_router(openai_router.router)
        except ImportError:
            logger.warning("OpenAI SDK not installed — install with: pip install cyborg-server[openai]")

    return app

app = create_app()
