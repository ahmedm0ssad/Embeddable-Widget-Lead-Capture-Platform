"""Score the /books/classify endpoint against the labelled cases in cases.json.

Usage (both work):
    python evals/run_eval.py [--base-url http://127.0.0.1:8000]
    python -m evals.run_eval [--base-url ...]

The base URL comes from ``--base-url``, the ``EVAL_BASE_URL`` / ``SUBMIT_BASE_URL``
env vars, or ``http://127.0.0.1:8000`` by default. If the server is not
reachable, the script falls back to FastAPI's in-process TestClient (identical
router code) and says ``mode: in-process``. No network is touched at import time.

Grading (three separate numbers — the product is the number):
- CATEGORY: expected category matched exactly.
- FLAGS (expected present): every EXPECTED quality flag present; extras are
  reported but do not fail (the model's flag ordering/set may legitimately
  differ beyond the contract).
- WHEN-UNSURE (bounded cases): for cases carrying confidence_min/max (the
  ambiguous case), the confidence must sit within the stated bounds
  (inclusive — the bound is the edge of the allowed band).

Writes ``evals/results.json`` (committed evidence) and exits 0 regardless of
the score; a low score is a result, not an error. Exit 2 only for
catastrophic failures (cases file missing/unreadable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
CASES_PATH = EVALS_DIR / "cases.json"
RESULTS_PATH = EVALS_DIR / "results.json"

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ensure_repo_importable() -> None:
    """Importable ``app`` for script-style runs (``python evals/run_eval.py``).

    Module form (``python -m evals.run_eval``) already has the repo root on
    ``sys.path``; script form does not, so add it lazily — this file only,
    never a global side effect at import of the package.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_HTTP_TIMEOUT_SECONDS = 150.0  # worst case: a 30s-timeout chain per repair


def _read_env(name: str) -> str:
    """Read LLM_* vars, loading .env first if python-dotenv is available."""
    value = os.getenv(name)
    if value:
        return value
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    return os.getenv(name, "")


class CatastrophicEvalError(RuntimeError):
    """The eval cannot run at all (missing/unreadable cases file)."""


def _load_cases() -> list[dict[str, Any]]:
    try:
        raw = CASES_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatastrophicEvalError(f"cannot load {CASES_PATH}: {exc}") from exc
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CatastrophicEvalError(f"{CASES_PATH} has no cases list")
    return cases


class Endpoint:
    """Submits payloads to POST /books/classify, HTTP first, TestClient next."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.mode = "http"
        self._test_client: Any = None
        if not self._server_reachable():
            # No live server: run the identical router code in-process.
            self.mode = "in-process"

    def _server_reachable(self) -> bool:
        import httpx

        try:
            response = httpx.get(
                f"{self.base_url}/health", timeout=3.0, follow_redirects=False
            )
        except Exception:  # noqa: BLE001 — reachability probe, any failure counts
            return False
        return response.status_code == 200 and response.json().get("status") == "ok"

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST and return the parsed body; raises on endpoint/transport errors."""
        if self.mode == "http":
            import httpx

            response = httpx.post(
                f"{self.base_url}/books/classify",
                json=payload,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        else:
            self._test_client = self._test_client or self._build_test_client()
            response = self._test_client.post("/books/classify", json=payload)
        body: Any = response.json()
        return {"status": response.status_code, "body": body}

    @staticmethod
    def _build_test_client() -> Any:
        """Offline in-process mode: DB/Redis disabled like ``conftest.py``.

        ``load_dotenv()`` only fills variables that are ABSENT from the
        environment, so pre-seeding empty values keeps ``.env``'s
        ``DATABASE_URL``/``REDIS_URL`` from making app startup require a live
        Postgres/Redis that a bare eval run does not have — the identical
        pattern the offline test suite uses to run anywhere.
        """
        for var in ("DATABASE_URL", "REDIS_URL"):
            if var not in os.environ:
                os.environ[var] = ""
        _ensure_repo_importable()
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app)


def _grade(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Grade one case against its expected block; never raises."""
    expected = case.get("expected", {})
    body = response.get("body")
    if response["status"] != 200 or not isinstance(body, dict):
        return {
            "id": case["id"],
            "status": "FAIL",
            "reason": f"endpoint returned HTTP {response['status']}",
            "expected_category": expected.get("category"),
            "got_category": body.get("category") if isinstance(body, dict) else None,
            "missing_flags": sorted(expected.get("quality_flags", [])),
            "extra_flags": [],
            "confidence": None,
            "bounded": False,
            "bounds_ok": None,
        }

    got_category = body.get("category")
    category_ok = got_category == expected.get("category")

    expected_flags = set(expected.get("quality_flags", []) or [])
    got_flags = set(body.get("quality_flags", []) or [])
    missing_flags = sorted(expected_flags - got_flags)
    extra_flags = sorted(got_flags - expected_flags)
    flags_ok = not missing_flags

    conf = body.get("confidence")
    conf_min = expected.get("confidence_min")
    conf_max = expected.get("confidence_max")
    bounded = conf_min is not None or conf_max is not None
    bounds_ok: bool | None
    if not bounded:
        bounds_ok = None
    else:
        bounds_ok = (conf_min is None or (conf is not None and conf >= conf_min)) and (
            conf_max is None or (conf is not None and conf <= conf_max)
        )

    passed = category_ok and flags_ok and (bounds_ok is not False)
    reasons: list[str] = []
    if not category_ok:
        reasons.append(
            f"category {got_category!r} != expected {expected.get('category')!r}"
        )
    if missing_flags:
        reasons.append(f"missing expected flag(s): {', '.join(missing_flags)}")
    if extra_flags:
        reasons.append(f"extra flag(s), noted only: {', '.join(extra_flags)}")
    if bounds_ok is False:
        reasons.append(f"confidence {conf} outside [{conf_min}, {conf_max}]")

    return {
        "id": case["id"],
        "status": "PASS" if passed else "FAIL",
        "reason": "; ".join(reasons),
        "expected_category": expected.get("category"),
        "got_category": got_category,
        "missing_flags": missing_flags,
        "extra_flags": extra_flags,
        "confidence": conf,
        "bounded": bounded,
        "bounds_ok": bounds_ok,
    }


def _prompt_version() -> str:
    _ensure_repo_importable()
    from app.services.llm.prompt_loader import BOOK_CLASSIFY_PROMPT, load_prompt

    try:
        version, _ = load_prompt(BOOK_CLASSIFY_PROMPT)
        return version
    except Exception:  # noqa: BLE001 — the run still works without this field
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    """Run one eval pass; print the table + scores; save evals/results.json."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url",
        default=(
            os.getenv("EVAL_BASE_URL")
            or os.getenv("SUBMIT_BASE_URL")
            or DEFAULT_BASE_URL
        ),
        help="base URL of the running API (default: EVAL_BASE_URL or "
        "http://127.0.0.1:8000)",
    )
    args = parser.parse_args(argv)

    try:
        cases = _load_cases()
    except CatastrophicEvalError as exc:
        print(f"CATASTROPHIC: {exc}", file=sys.stderr)
        return 2

    started = time.monotonic()
    endpoint = Endpoint(args.base_url)
    print(f"mode: {endpoint.mode}")
    print(f"base_url: {endpoint.base_url}")
    print(f"cases: {len(cases)}")
    print()

    results: list[dict[str, Any]] = []
    for case in cases:
        raw_input = case.get("input", {})
        try:
            response = endpoint.submit(raw_input)
        except Exception as exc:  # noqa: BLE001 — a case failure is a result
            response = {"status": 0, "body": None, "transport_error": str(exc)}
        graded = _grade(case, response)
        graded["input"] = raw_input
        graded["response"] = response.get("body")
        graded["endpoint_status"] = response.get("status")
        results.append(graded)

        flags_delta = f"{len(graded['missing_flags'])} missing / {len(graded['extra_flags'])} extra"
        conf_cell = (
            f"{graded['confidence']} ({'in' if graded['bounds_ok'] else 'OUT of'} bounds)"
            if graded["bounded"]
            else "n/a"
        )
        print(
            f"{graded['id']:<10} {graded['status']:<4} "
            f"{graded['expected_category']:>16} -> {graded['got_category']!s:>16} "
            f"  flags[{flags_delta}]  confidence[{conf_cell}]"
        )
        if graded["status"] == "FAIL":
            print(f"           ^ {graded['reason']}")

    total = len(results)
    category_score = sum(
        1 for r in results if r["got_category"] == r["expected_category"]
    )
    flags_score = sum(1 for r in results if not r["missing_flags"])
    unbounded_total = sum(1 for r in results if r["bounded"])
    unsure_score = sum(1 for r in results if r["bounded"] and (r["bounds_ok"] or False))
    failures = [r for r in results if r["status"] != "PASS"]

    print()
    print(f"CATEGORY: {category_score}/{total}")
    print(f"FLAGS (expected present): {flags_score}/{total}")
    print(f"WHEN-UNSURE (bounded cases): {unsure_score}/{unbounded_total}")
    if failures:
        print("FAILED cases:")
        for r in failures:
            print(f"  - {r['id']}: {r['reason']}")
    else:
        print("No failed cases.")

    model = _read_env("LLM_MODEL") or "unknown"
    wall_clock_seconds = round(time.monotonic() - started, 1)
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": endpoint.mode,
        "base_url": endpoint.base_url,
        "model": model,
        "prompt_version": _prompt_version(),
        "wall_clock_seconds": wall_clock_seconds,
        "category_score": f"{category_score}/{total}",
        "flags_score": f"{flags_score}/{total}",
        "when_unsure_score": f"{unsure_score}/{unbounded_total}",
    }
    RESULTS_PATH.write_text(
        json.dumps({**summary, "cases": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"wall-clock total: {wall_clock_seconds}s")
    print(f"saved: {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
