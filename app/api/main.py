"""FastAPI: health, задел под вебхук LavaTop (этап 5) и вебапп."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db.session import dispose_engine, get_engine
from app.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("api запускается", окружение=settings.env, порт=settings.api_port)
    yield
    await dispose_engine()
    log.info("api остановлен")


app = FastAPI(title="china_bot API", version="0.1.0", lifespan=lifespan)


async def _check_postgres() -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True, "ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:
        log.error("postgres недоступен", ошибка=repr(exc))
        return {"ok": False, "ошибка": repr(exc)}


async def _check_redis() -> dict[str, Any]:
    started = time.monotonic()
    client = aioredis.from_url(settings.redis_url)
    try:
        await client.ping()
        return {"ok": True, "ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:
        log.error("redis недоступен", ошибка=repr(exc))
        return {"ok": False, "ошибка": repr(exc)}
    finally:
        await client.aclose()


@app.get("/health")
async def health() -> JSONResponse:
    postgres = await _check_postgres()
    redis_state = await _check_redis()
    healthy = postgres["ok"] and redis_state["ok"]
    body = {
        "status": "ok" if healthy else "degraded",
        "env": settings.env,
        "postgres": postgres,
        "redis": redis_state,
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
