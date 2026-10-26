"""SSE streaming route + streaming pipeline tests (A17 stretch: Streaming).

Offline like the sync classify suite: stub mode in conftest stays pinned,
real-mode tests monkeypatch the shared provider seam
(``app.services.llm.pipeline._get_provider``) with scripted fakes, and the
quarantine log points at a temp file.
"""

import json

import pytest
from fastapi.testclient import TestClient

SCHEMA_FIELDS = {"category", "summary", "quality_flags", "confidence", "reason"}

_VALID_CONTENT = json.dumps(
    {
        "category": "fiction",
        "summary": "A summary.",
        "quality_flags": [],
        "confidence": 0.9,
        "reason": "A reason.",
    }
)
_BAD_CONTENT = json.dumps(
    {
        "category": "cooking",
        "summary": "A summary.",
        "quality_flags": [],
        "confidence": 0.9,
        "reason": "A reason.",
    }
)


@pytest.fixture(autouse=True)
def stub_mode(monkeypatch):
    """Every test runs with LLM_STUB=1 unless it explicitly removes the var."""
    monkeypatch.setenv("LLM_STUB", "1")
    yield


@pytest.fixture(autouse=True)
def clean_cache():
    from app.services.llm import cache

    cache.clear()
    yield
    cache.clear()


def _parse_sse(text: str) -> list[tuple[str, str]]:
    """Parse a raw SSE body into ``(event, data)`` pairs (order preserved).

    Multiple ``data:`` lines inside one frame are joined with ``\\n`` (SSE
    reassembly), so multi-line deltas stay lossless.
    """
    events: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        kind: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[len("event: ") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data: ") :])
        if kind is None and not data_lines:
            continue
        events.append((kind or "message", "\n".join(data_lines)))
    return events


def _stream_post(client: TestClient, payload) -> object:
    return client.post("/books/classify/stream", json=payload)


class _StreamScriptedProvider:
    """Fake provider: scripted ``complete_stream`` chunks + ``complete`` replies.

    Records both call counts so tests can assert ONE provider attempt for the
    initial stream, exactly one repair, and zero calls for stub/kill-switch.
    """

    def __init__(self, stream_chunks=None, complete_contents=None):
        self.stream_chunks = list(stream_chunks or [])
        self.complete_contents = list(complete_contents or [])
        self.stream_calls = 0
        self.complete_calls = 0

    def complete_stream(self, **kwargs):
        self.stream_calls += 1
        yield from self.stream_chunks

    def complete(self, **kwargs):
        from app.services.llm.provider import LLMResponse

        self.complete_calls += 1
        return LLMResponse(content=self.complete_contents.pop(0), model="test-model")


class TestHappyStream:
    def test_tokens_then_validated_result_event(self, client, monkeypatch):
        from app.services.llm import pipeline

        provider = _StreamScriptedProvider(
            stream_chunks=[
                '{"category": "fic',
                'tion", "summary": "A summary.",',
                ' "quality_flags": [], "confidence": 0.9, "reason": "R."}',
            ],
            complete_contents=[],
        )
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        response = _stream_post(client, {"title": "Dune", "description": "A desc."})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _parse_sse(response.text)
        kinds = [kind for kind, _ in events]
        assert kinds.count("token") >= 1
        # exactly ONE terminal event, and it is LAST
        assert kinds[-1] == "result"
        assert kinds.count("result") == 1
        assert kinds.count("error") == 0

        result = json.loads(events[-1][1])
        assert set(result) == SCHEMA_FIELDS  # no extra keys, no "raw"
        assert result["category"] == "fiction"

        # the streamed deltas reassemble into the provider's raw text
        tokens = "".join(data for kind, data in events if kind == "token")
        assert '{"category": "fiction"' in tokens

    def test_result_frame_is_the_only_terminal_frame(self, client, monkeypatch):
        from app.services.llm import pipeline

        provider = _StreamScriptedProvider(
            stream_chunks=[
                json.dumps(
                    {
                        "category": "other",
                        "summary": "S",
                        "quality_flags": [],
                        "confidence": 0.31,
                        "reason": "why",
                    }
                )
            ],
            complete_contents=[],
        )
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        response = _stream_post(client, {"title": "T"})

        events = _parse_sse(response.text)
        terminal = [
            (kind, data) for kind, data in events if kind in ("result", "error")
        ]
        assert len(terminal) == 1
        assert terminal[0][0] == "result"
        assert set(json.loads(terminal[0][1])) == SCHEMA_FIELDS


class TestStubStreaming:
    def test_stub_streams_result_without_provider(self, client, monkeypatch):
        from app.services.llm import pipeline

        def _explode(*args, **kwargs):
            raise AssertionError("stub mode must not consult the provider")

        monkeypatch.setattr(pipeline, "_get_provider", _explode)

        response = _stream_post(
            client, {"title": "The Midnight Library", "description": None}
        )

        assert response.status_code == 200
        events = _parse_sse(response.text)
        # validated answer already exists by construction: no model text to
        # stream, so the stream is the bare terminal result event
        assert len(events) == 1
        assert events[0][0] == "result"
        result = json.loads(events[0][1])
        assert set(result) == SCHEMA_FIELDS
        assert result["quality_flags"] == ["missing_description"]

    def test_stub_beats_kill_switch(self, client, monkeypatch):
        # Both flags set: stub mode is checked FIRST and must win, exactly
        # like the sync route's precedence.
        monkeypatch.setenv("LLM_STUB", "1")
        monkeypatch.setenv("LLM_ENABLED", "false")
        response = _stream_post(client, {"title": "Dune"})

        events = _parse_sse(response.text)
        assert len(events) == 1
        assert events[0][0] == "result"
        assert json.loads(events[0][1])["category"] == "fiction"
        assert "kill switch" not in json.loads(events[0][1])["summary"]


class TestKillSwitchStreaming:
    def test_kill_switch_result_with_zero_provider_calls(self, client, monkeypatch):
        from app.services.llm import pipeline

        def _explode(*args, **kwargs):
            raise AssertionError("kill switch must not touch the provider")

        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "false")
        monkeypatch.setattr(pipeline, "_get_provider", _explode)

        response = _stream_post(client, {"title": "Dune", "description": "A desc."})

        assert response.status_code == 200
        events = _parse_sse(response.text)
        assert len(events) == 1
        assert events[0][0] == "result"
        result = json.loads(events[0][1])
        assert set(result) == SCHEMA_FIELDS
        assert result["confidence"] == 0.0
        assert "kill switch" in result["summary"]

    def test_not_configured_is_an_error_event(self, client, monkeypatch):
        from app.services.llm import pipeline

        def _explode(*args, **kwargs):
            raise AssertionError("unconfigured must not touch the provider")

        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.delenv("LLM_ENABLED", raising=False)
        monkeypatch.setattr(pipeline, "_get_provider", _explode)

        response = _stream_post(client, {"title": "Dune"})

        assert response.status_code == 200  # HTTP stays 200; error is in-stream
        events = _parse_sse(response.text)
        assert len(events) == 1
        assert events[0][0] == "error"
        assert "LLM integration not configured" in json.loads(events[0][1])["detail"]


class TestBrokenStreamRepair:
    def test_broken_stream_repaired_once_yields_result(
        self, client, monkeypatch, tmp_path
    ):
        from app.services.llm import pipeline

        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(pipeline, "QUARANTINE_LOG_PATH", tmp_path / "q.jsonl")
        provider = _StreamScriptedProvider(
            stream_chunks=[_BAD_CONTENT],  # fails schema validation
            complete_contents=[_VALID_CONTENT],  # the ONE (non-streamed) repair
        )
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        response = _stream_post(client, {"title": "Dune", "description": "A desc."})

        events = _parse_sse(response.text)
        terminal = [(k, d) for k, d in events if k in ("result", "error")]
        assert len(terminal) == 1
        assert terminal[0][0] == "result"
        assert set(json.loads(terminal[0][1])) == SCHEMA_FIELDS
        # one streamed attempt + exactly one repair
        assert provider.stream_calls == 1
        assert provider.complete_calls == 1
        assert not (tmp_path / "q.jsonl").exists()  # recovered → no quarantine

    def test_unrepairable_stream_error_event_plus_one_quarantine_line(
        self, client, monkeypatch, tmp_path
    ):
        from app.services.llm import pipeline

        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        quarantine_path = tmp_path / "q.jsonl"
        monkeypatch.setattr(pipeline, "QUARANTINE_LOG_PATH", quarantine_path)
        provider = _StreamScriptedProvider(
            stream_chunks=[_BAD_CONTENT],
            complete_contents=[_BAD_CONTENT],
        )
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        response = _stream_post(client, {"title": "Dune", "description": "A desc."})

        events = _parse_sse(response.text)
        terminal = [(k, d) for k, d in events if k in ("result", "error")]
        assert len(terminal) == 1
        assert terminal[0][0] == "error"
        detail = json.loads(terminal[0][1])["detail"]
        assert "failed schema validation" in detail
        # exactly ONE provider attempt of each kind — never a second repair
        assert provider.stream_calls == 1
        assert provider.complete_calls == 1
        lines = [
            json.loads(line)
            for line in quarantine_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        assert lines[0]["attempt"] == "repair"
        assert lines[0]["input"] == {"title": "Dune", "description": "A desc."}
        assert lines[0]["model"] == "test-model"


class TestValidation400BeforeStream:
    def test_missing_title_is_plain_http_400(self, client):
        response = _stream_post(client, {"description": "A desc."})

        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/json")
        assert "title" in response.json()["detail"]["fields"]

    def test_empty_body_is_plain_http_400(self, client):
        response = client.post("/books/classify/stream")

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "validation_failed"

    def test_invalid_json_is_plain_http_400(self, client):
        response = client.post(
            "/books/classify/stream",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "invalid_json"

    def test_non_object_body_is_plain_http_400(self, client):
        response = client.post("/books/classify/stream", json=["a", "b"])

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "validation_failed"


class TestNoMidStreamRetry:
    def test_connection_error_after_first_delta_is_one_attempt(
        self, client, monkeypatch, tmp_path
    ):
        from app.services.llm import pipeline
        from app.services.llm.provider import LLMConnectionError

        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(pipeline, "QUARANTINE_LOG_PATH", tmp_path / "q.jsonl")

        class _BrokenMidStreamProvider:
            def __init__(self):
                self.stream_calls = 0
                self.complete_calls = 0

            def complete_stream(self, **kwargs):
                self.stream_calls += 1
                yield '{"category": "fic'  # first delta flows
                raise LLMConnectionError("stream broke after 200 OK")

            def complete(self, **kwargs):
                self.complete_calls += 1
                raise AssertionError("mid-stream break must NOT be retried")

        provider = _BrokenMidStreamProvider()
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        response = _stream_post(client, {"title": "Dune", "description": "A desc."})

        events = _parse_sse(response.text)
        terminal = [(k, d) for k, d in events if k in ("result", "error")]
        assert len(terminal) == 1
        assert terminal[0][0] == "error"
        assert "unreachable" in json.loads(terminal[0][1])["detail"]
        assert any(kind == "token" for kind, _ in events)  # the first delta emitted
        # ONE provider attempt — no mid-stream retry, and NOT repaired either
        # (a transport failure is not a schema failure)
        assert provider.stream_calls == 1
        assert provider.complete_calls == 0
        # transport failures never write a quarantine line
        assert not (tmp_path / "q.jsonl").exists()


class TestCacheFirst:
    def test_cache_hit_emits_result_without_tokens_or_provider(
        self, client, monkeypatch
    ):
        from app.services.llm import cache, pipeline

        prompt_version, _ = pipeline._system_prompt_with_sentinel(
            str(pipeline.resolve_book_classify_prompt_path())
        )
        model = "test-model"
        payload = {"title": "Dune", "description": "A desc."}
        key = cache.cache_key(prompt_version, model, payload)
        cache.put(key, _VALID_CONTENT)

        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", model)

        def _explode(*args, **kwargs):
            raise AssertionError("cache hit must not consult the provider")

        monkeypatch.setattr(pipeline, "_get_provider", _explode)

        response = _stream_post(client, payload)

        events = _parse_sse(response.text)
        assert [(kind, data) for kind, data in events if kind == "token"] == []
        terminal = [(k, d) for k, d in events if k in ("result", "error")]
        assert len(terminal) == 1
        assert terminal[0][0] == "result"
        assert set(json.loads(terminal[0][1])) == SCHEMA_FIELDS


class TestStubProviderStream:
    def test_stub_complete_stream_yields_canned_json_in_chunks(self):
        from app.services.llm.provider import StubProvider

        provider = StubProvider()
        deltas = list(
            provider.complete_stream(
                model="m", system_prompt="s", user_content="u", temperature=0.0
            )
        )
        assert len(deltas) > 1
        assert "".join(deltas) == provider.content

    def test_fake_streaming_fallback_to_complete_once(self, client, monkeypatch):
        """A provider WITHOUT complete_stream still yields a validated result."""
        from app.services.llm import pipeline
        from app.services.llm.provider import LLMResponse

        class _SyncOnlyProvider:
            def __init__(self):
                self.calls = 0

            def complete(self, **kwargs):
                self.calls += 1
                return LLMResponse(content=_VALID_CONTENT, model="test-model")

        provider = _SyncOnlyProvider()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        response = _stream_post(client, {"title": "Dune", "description": "A desc."})

        terminal = [
            (k, d) for k, d in _parse_sse(response.text) if k in ("result", "error")
        ]
        assert len(terminal) == 1
        assert terminal[0][0] == "result"
        assert set(json.loads(terminal[0][1])) == SCHEMA_FIELDS
        assert provider.calls == 1
