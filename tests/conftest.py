import os

os.environ["DATABASE_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_KEY"] = "test-anon-key"
# A17 Stage 2: app/main.py load_dotenv() would otherwise import real LLM_* values
# and the classify endpoint's non-stub path would hit the network. Force the
# fully-offline mode (stub on, real model path disabled); individual tests may
# still override — load_dotenv() does not touch variables set here.
os.environ["LLM_STUB"] = "1"
os.environ["LLM_ENABLED"] = ""


import pytest
from fastapi.testclient import TestClient

from app.main import app


class _FakeRedis:
    def __init__(self):
        self._data: dict[str, dict[str, str]] = {}
        self._strings: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    def hset(self, key, mapping=None):
        if key not in self._data:
            self._data[key] = {}
        self._data[key].update(mapping or {})

    def hgetall(self, key):
        return self._data.get(key, {})

    def hincrby(self, key, field, amount=1):
        self._data.setdefault(key, {})
        current = int(self._data[key].get(field, 0))
        self._data[key][field] = str(current + amount)
        return current + amount

    def expire(self, key, ttl):
        self._ttls[key] = ttl

    def get(self, key):
        return self._strings.get(key)

    def setex(self, key, ttl, value):
        self._strings[key] = value
        self._ttls[key] = ttl

    def scan(self, cursor=0, match=None, count=10):
        keys = [
            k
            for k in self._data
            if match is None or k.startswith(match.replace("*", ""))
        ]
        return 0, keys

    def delete(self, key):
        self._strings.pop(key, None)
        self._data.pop(key, None)
        self._ttls.pop(key, None)

    def close(self):
        pass

    def ping(self):
        return True

    def pipeline(self):
        return _FakePipeline(self._strings)

    def incr(self, key):
        val = int(self._strings.get(key, 0)) + 1
        self._strings[key] = str(val)
        return val

    def ttl(self, key):
        return self._ttls.get(key, -1)


class _FakePipeline:
    def __init__(self, strings):
        self._strings = strings
        self._commands = []

    def incr(self, key):
        self._commands.append(key)
        return self

    def execute(self):
        results = []
        for key in self._commands:
            val = int(self._strings.get(key, 0)) + 1
            self._strings[key] = str(val)
            results.append(val)
        return results


class _FakeQueue:
    def __init__(self):
        self.enqueued_jobs: list[dict] = []

    def enqueue(self, func, *args, **kwargs):
        self.enqueued_jobs.append({"func": func, "args": args, "kwargs": kwargs})
        return type("Job", (), {"id": kwargs.get("job_id", "fake-id")})()


_fake_redis = _FakeRedis()
_fake_queue = _FakeQueue()


@pytest.fixture(autouse=True)
def _reset_queue(monkeypatch):
    _fake_redis._data.clear()
    _fake_redis._strings.clear()
    _fake_redis._ttls.clear()
    _fake_queue.enqueued_jobs.clear()
    monkeypatch.setattr("app.core.queue.get_connection", lambda: _fake_redis)
    monkeypatch.setattr("app.core.queue.get_queue", lambda: _fake_queue)
    monkeypatch.setattr("app.core.queue.get_report_queue", lambda: _fake_queue)
    monkeypatch.setattr("app.core.queue.get_enrichment_queue", lambda: _fake_queue)


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch):
    monkeypatch.setattr("app.main.is_postgres_enabled", lambda: False)
    monkeypatch.setattr("app.core.database.is_postgres_enabled", lambda: False)
    monkeypatch.setattr("app.services.task_service.is_postgres_enabled", lambda: False)
    monkeypatch.setattr(
        "app.services.scraped_book_service.is_postgres_enabled", lambda: False
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_book_data() -> dict:
    return {
        "url": "http://books.toscrape.com/catalogue/test_1000/index.html",
        "title": "Test Book",
        "price": 51.77,
        "availability": "In Stock",
        "rating": 3,
        "description": "A test book description",
        "category": "Fiction",
        "upc": "abc123def456",
        "image_url": "http://books.toscrape.com/media/cache/test.jpg",
    }


@pytest.fixture
def sample_task_data() -> dict:
    return {"title": "Test Task", "done": False}


@pytest.fixture
def sample_lead_data() -> dict:
    return {
        "widget_id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "00000000-0000-0000-0000-000000000002",
        "form_data": {
            "name": "John Doe",
            "email": "john@example.com",
            "phone": "+1234567890",
        },
        "ip_address": "192.168.1.1",
        "fingerprint": "abc123def456",
        "user_agent": "test-agent",
        "referer": "https://myshop.com/contact",
    }


class _FakeAsyncRedis:
    def __init__(self, sync_redis):
        self._sync = sync_redis

    async def get(self, key):
        return self._sync.get(key)

    async def setex(self, key, ttl, value):
        self._sync.setex(key, ttl, value)

    async def delete(self, key):
        self._sync._strings.pop(key, None)
        self._sync._data.pop(key, None)

    async def ping(self):
        return True

    def pipeline(self):
        return (
            self._sync.pipeline()
            if hasattr(self._sync, "pipeline")
            else _FakePipeline(self._sync._strings)
        )

    async def expire(self, key, ttl):
        self._sync.expire(key, ttl)

    async def incr(self, key):
        val = int(self._sync._strings.get(key, 0)) + 1
        self._sync._strings[key] = str(val)
        return val


@pytest.fixture
def fake_redis():
    return _fake_redis


@pytest.fixture
def fake_async_redis(fake_redis):
    return _FakeAsyncRedis(fake_redis)


@pytest.fixture
def mock_redis(monkeypatch, fake_async_redis):
    monkeypatch.setattr("app.main.get_redis", lambda: fake_async_redis)
    yield fake_async_redis


@pytest.fixture
def mock_lead_queue(monkeypatch):
    from tests.conftest import _fake_queue

    monkeypatch.setattr("app.core.queue.get_enrichment_queue", lambda: _fake_queue)
    return _fake_queue
