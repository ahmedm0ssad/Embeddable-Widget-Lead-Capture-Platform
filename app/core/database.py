import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str | None = os.getenv("DATABASE_URL")
_pool: asyncpg.Pool | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool, _pool_loop
    loop = asyncio.get_running_loop()
    if _pool is not None and _pool_loop is not loop:
        # The cached pool was created on a different event loop (e.g. an RQ
        # worker's per-call asyncio.run() loop that has since closed). asyncpg
        # pools are bound to the loop they were created on, so reusing one from
        # a new loop raises InterfaceError / 'Event loop is closed'. Drop it
        # best-effort and recreate below.
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None
        _pool_loop = None
    if _pool is None:
        last_error = None
        for attempt in range(1, 6):
            try:
                _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
                _pool_loop = loop
                break
            except asyncpg.PostgresError as e:
                last_error = e
                if attempt < 5:
                    await asyncio.sleep(2)
        else:
            raise last_error
    return _pool


async def close_pool():
    global _pool, _pool_loop
    if _pool:
        await _pool.close()
        _pool = None
        _pool_loop = None


def is_postgres_enabled() -> bool:
    return DATABASE_URL is not None and DATABASE_URL != ""
