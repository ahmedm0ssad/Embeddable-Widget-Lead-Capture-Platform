import logging
import os
from contextlib import asynccontextmanager

import redis
import redis.asyncio as redis_ai
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import close_pool, get_pool, is_postgres_enabled
from app.core.supabase import get_client_credentials
from app.middleware.body_limit import BodyLimitMiddleware
from app.routers import scrape, tasks
from app.routers.ai import router as ai_router
from app.routers.auth import auth_router, protected_router
from app.routers.books_classify import router as books_classify_router
from app.routers.embed import router as embed_router
from app.routers.leads import cross_router as leads_cross_router
from app.routers.leads import dashboard_router as leads_dashboard_router
from app.routers.leads import router as leads_router
from app.routers.reports import router as reports_router
from app.routers.widgets import router as widgets_router

load_dotenv()

# One-line production logging wiring (README-noted gap): without it, app-level
# INFO lines — including the structured ``llm_call`` cost lines — never reach
# stderr/container logs because the root logger has no handler.
logging.basicConfig(level=logging.INFO)

_redis_client: redis_ai.Redis | None = None


def get_redis() -> redis_ai.Redis | None:
    return _redis_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_client

    supa_url, supa_key = get_client_credentials()
    if supa_url and supa_key:
        print("Supabase client configured")
    else:
        print("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        raise RuntimeError("Missing Supabase credentials")

    port = os.getenv("PORT", "8000")
    print(f"Server starting on port {port}")

    if is_postgres_enabled():
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        print("Postgres connected")

    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        _redis_client = redis_ai.from_url(redis_url, decode_responses=True)
        try:
            pong = await _redis_client.ping()
            print(f"Redis ping: {pong}")
        except redis.RedisError as e:
            print(f"Redis unavailable: {e}")
            _redis_client = None
    else:
        print("Redis not configured")

    yield

    if _redis_client:
        await _redis_client.close()
    await close_pool()


app = FastAPI(
    title="FlyRank API",
    version="0.3.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)

app.add_middleware(BodyLimitMiddleware, max_bytes=50_000)

app.include_router(tasks.router)
app.include_router(tasks.stats_router)
app.include_router(scrape.router)
app.include_router(ai_router)
app.include_router(reports_router)
app.include_router(auth_router)
app.include_router(protected_router)
app.include_router(widgets_router)
app.include_router(embed_router)
app.include_router(leads_router)
app.include_router(leads_dashboard_router)
app.include_router(leads_cross_router)
app.include_router(books_classify_router)


@app.get("/")
async def home():
    info = {
        "name": "FlyRank API",
        "version": "0.3.0",
        "endpoints": [
            "/tasks",
            "/scrape",
            "/ai",
            "/widgets",
            "/leads",
            "/books/classify",
        ],
    }
    if get_redis():
        info["redis"] = "connected"
    return info


@app.get("/health")
async def health():
    status = {"status": "ok"}
    if get_redis():
        status["redis"] = "connected"
    if is_postgres_enabled():
        from app.core.database import get_pool

        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            status["postgres"] = "connected"
        except Exception:
            status["postgres"] = "unavailable"
            status["status"] = "degraded"
    else:
        status["database"] = "sqlite"
    return status


@app.get("/public/info")
async def public_info():
    return {"message": "Welcome stranger! This info is public."}
