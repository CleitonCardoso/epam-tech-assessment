"""FastAPI application factory.

Two processes share this code: the API (`uvicorn app.main:app`) and the worker
(`python -m app.worker`). They share the settings, models and storage adapters, and nothing
else — the API never imports the worker's processing loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.documents import AppContext, router as documents_router
from app.config import Settings, get_settings
from app.models import HealthResponse
from app.registry import ProviderRegistry
from app.storage.jobs import JobRepository
from app.storage.objects import S3ObjectStore

logger = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 5.0


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("botocore", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    _configure_logging(settings.log_level)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    ctx = AppContext(
        settings=settings,
        redis=redis,
        repo=JobRepository(redis, settings),
        store=S3ObjectStore(settings),
        registry=ProviderRegistry(settings),
    )
    app.state.ctx = ctx

    # Startup configuration, deliberately without secrets.
    logger.info(
        "api_started ai_mode=%s gemini_model=%s s3_bucket=%s s3_endpoint=%s redis_host=%s",
        settings.ai_mode,
        settings.gemini_model,
        settings.s3_bucket,
        settings.s3_endpoint_url,
        settings.redis_url.rsplit("@", 1)[-1],
    )
    if settings.ai_mode == "fake":
        logger.warning(
            "ai_mode=fake: markdown extraction and summaries are deterministic stand-ins. "
            "No model is called."
        )
    for info in ctx.registry.describe():
        logger.info(
            "parser parser=%s enabled=%s note=%s", info.id.value, info.enabled, info.note
        )

    try:
        yield
    finally:
        try:
            await redis.aclose()
        except AttributeError:  # pragma: no cover - older redis-py
            await redis.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Async PDF processing",
        version="1.0.0",
        description=(
            "Upload one or more PDFs, choose a parser, and read per-page content plus one "
            "document-level summary once the worker finishes. Processing runs in a separate "
            "process fed by a Redis Stream."
        ),
        lifespan=lifespan,
    )
    app.include_router(documents_router)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def health_ready(request: Request) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        settings = ctx.settings
        checks: dict[str, str] = {}
        healthy = True

        try:
            await asyncio.wait_for(ctx.redis.ping(), timeout=READY_TIMEOUT_SECONDS)
            checks["redis"] = "ok"
        except (RedisError, asyncio.TimeoutError, OSError) as exc:
            checks["redis"] = f"unavailable: {type(exc).__name__}"
            healthy = False

        try:
            s3_ok, s3_detail = await asyncio.wait_for(
                ctx.store.healthy(), timeout=READY_TIMEOUT_SECONDS
            )
            checks["s3"] = s3_detail
            healthy = healthy and s3_ok
        except (asyncio.TimeoutError, OSError) as exc:
            checks["s3"] = f"unavailable: {type(exc).__name__}"
            healthy = False

        summarizer_ok, summarizer_detail = ctx.registry.summarizer_ready()
        checks["summarizer"] = summarizer_detail
        checks["ai_mode"] = settings.ai_mode
        # In fake mode a deterministic summarizer is expected and counts as ready. In live
        # mode a missing key means uploads must not be accepted, so readiness fails here.
        if settings.ai_mode == "live" and not summarizer_ok:
            healthy = False

        payload = HealthResponse(
            status="ok" if healthy else "degraded",
            checks=checks,
            detail=None if healthy else "One or more dependencies are unavailable; see checks.",
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content=payload.model_dump(mode="json"),
        )

    @app.exception_handler(RedisError)
    async def redis_error_handler(_: Request, exc: RedisError) -> JSONResponse:
        logger.warning("redis_error_unhandled type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The job store is unavailable; try again shortly.",
                "code": "QUEUE_UNAVAILABLE",
            },
        )

    return app


app = create_app()
