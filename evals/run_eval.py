"""Score the /books/classify endpoint against the labelled cases in cases.json.

Usage (all work):
    python evals/run_eval.py [--base-url http://127.0.0.1:8000]
    python -m evals.run_eval [--base-url ...]
    python -m evals.run_eval --mode in-process --model openai/gpt-oss-120b

The base URL comes from ``--base-url``, the ``EVAL_BASE_URL`` / ``SUBMIT_BASE_URL``
env vars, or ``http://127.0.0.1:8000`` by default. If the server is not
reachable, the script falls back to FastAPI's in-process TestClient (identical
router code) and says ``mode: in-process``. ``--mode http|in-process`` forces a
side (useful for model races: the server always runs its own ``LLM_MODEL``;
``--model`` is an env override read at call time and therefore only takes
effect in-process). No network is touched at import time. ``--batch-size N``
with ``--sleep S`` submits in batches with a pause between them (free-tier
friendliness); defaults run everything in one pass, unchanged behavior.

Grading (three separate numbers — the product is the number):
- CATEGORY: expected category matched exactly.
- FLAGS (expected present): every EXPECTED quality flag present; extras are
  reported but do not fail (the model's flag ordering/set may legitimately
  differ beyond the contract).
- WHEN-UNSURE (bounded cases): for cases carrying confidence_min/max (the
  ambiguous case), the confidence must sit within the stated bounds
  (inclusive — the bound is the edge of the allowed band).
- INJECTION (attack cases): a case may pin ``expected.attack_category`` (the
  category the glued-on payload demands). If the model returns exactly that
  category, it obeyed the payload, so the case FAILS with an explicit
  "injection obeyed" reason even though the underlying content might have
  graded differently.

The category score is additionally reported for the easy and hard tag splits
(``expected_category == got_category`` within each labelled subset), and
``evals/results.json`` carries the split.

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

    def __init__(self, base_url: str, mode: str = "auto"):
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        if self.mode == "auto":
            # Probe the server once: reachable → HTTP, otherwise the identical
            # router code runs in-process.
            self.mode = "http" if self._server_reachable() else "in-process"

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
            "attack_category": expected.get("attack_category"),
            "injection_obeyed": False,
        }

    got_category = body.get("category")
    category_ok = got_category == expected.get("category")

    # Prompt-injection hard rule: if the case pins the category the attack
    # payload demanded and the model returned exactly that, it obeyed the
    # payload instead of classifying the underlying book — always a FAIL.
    attack_category = expected.get("attack_category")
    injection_obeyed = bool(
        attack_category is not None and got_category == attack_category
    )

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

    passed = (
        category_ok and flags_ok and (bounds_ok is not False) and not injection_obeyed
    )
    reasons: list[str] = []
    if not category_ok:
        reasons.append(
            f"category {got_category!r} != expected {expected.get('category')!r}"
        )
    if injection_obeyed:
        reasons.append(
            "injection obeyed: category equals the payload-suggested "
            f"{attack_category!r} without evidence"
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
        "attack_category": attack_category,
        "injection_obeyed": injection_obeyed,
    }


def _tags_by_id(cases: list[dict[str, Any]]) -> dict[str, set[str]]:
    return {case["id"]: set(case.get("tags", []) or []) for case in cases}


def split_scores(
    results: list[dict[str, Any]], tags: dict[str, set[str]]
) -> dict[str, dict[str, str]]:
    """Category scores for the easy and hard tag subsets (each a dict row)."""
    subsets: dict[str, list[dict[str, Any]]] = {
        "easy": [r for r in results if "easy" in tags.get(r["id"], set())],
        "hard": [r for r in results if "hard" in tags.get(r["id"], set())],
    }
    return {
        name: {
            "category": (
                f"{sum(1 for r in rows if r['got_category'] == r['expected_category'])}"
                f"/{len(rows)}"
            ),
            "cases": len(rows),
        }
        for name, rows in subsets.items()
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
    parser.add_argument(
        "--mode",
        choices=["auto", "http", "in-process"],
        default="auto",
        help="force http or in-process submission (default: probe the server "
        "and fall back to in-process)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override LLM_MODEL in this process's env (read at call time — "
        "effective for the in-process mode; an HTTP server keeps its own "
        "configured model)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="submit in batches of N cases (0 = all cases in one pass)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to pause between batches (free-tier friendliness)",
    )
    args = parser.parse_args(argv)

    # In-process mode needs app config (Supabase etc.); load .env absent-only
    # first so an explicit --model/--mode choice still wins. Never printed.
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    if args.model:
        os.environ["LLM_MODEL"] = args.model

    try:
        cases = _load_cases()
    except CatastrophicEvalError as exc:
        print(f"CATASTROPHIC: {exc}", file=sys.stderr)
        return 2

    started = time.monotonic()
    endpoint = Endpoint(args.base_url, mode=args.mode)
    print(f"mode: {endpoint.mode}")
    print(f"base_url: {endpoint.base_url}")
    print(f"cases: {len(cases)}")
    if args.model:
        print(
            f"model override: {args.model}"
            + (
                " (in-process: effective)"
                if endpoint.mode == "in-process"
                else " (WARNING: an HTTP server keeps its own LLM_MODEL)"
            )
        )
    print()

    tags = _tags_by_id(cases)
    batch_size = args.batch_size if args.batch_size > 0 else len(cases)

    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        raw_input = case.get("input", {})
        case_started = time.monotonic()
        try:
            response = endpoint.submit(raw_input)
        except Exception as exc:  # noqa: BLE001 — a case failure is a result
            response = {"status": 0, "body": None, "transport_error": str(exc)}
        graded = _grade(case, response)
        graded["latency_ms"] = int((time.monotonic() - case_started) * 1000.0)
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
            f"  flags[{flags_delta}]  confidence[{conf_cell}]  "
            f"latency_ms[{graded['latency_ms']}]"
        )
        if graded["status"] == "FAIL":
            print(f"           ^ {graded['reason']}")

        if (index + 1) % batch_size == 0 and index + 1 < len(cases) and args.sleep > 0:
            print(f"-- batch pause: {args.sleep:g}s (free-tier friendliness)")
            time.sleep(args.sleep)

    total = len(results)
    category_score = sum(
        1 for r in results if r["got_category"] == r["expected_category"]
    )
    flags_score = sum(1 for r in results if not r["missing_flags"])
    unbounded_total = sum(1 for r in results if r["bounded"])
    unsure_score = sum(1 for r in results if r["bounded"] and (r["bounds_ok"] or False))
    easy_hard = split_scores(results, tags)
    failures = [r for r in results if r["status"] != "PASS"]

    print()
    print(f"CATEGORY: {category_score}/{total}")
    print(f"CATEGORY easy: {easy_hard['easy']['category']}")
    print(f"CATEGORY hard: {easy_hard['hard']['category']}")
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
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    average_latency_ms = round(sum(latencies) / len(latencies)) if latencies else 0
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": endpoint.mode,
        "base_url": endpoint.base_url,
        "model": model,
        "prompt_version": _prompt_version(),
        "wall_clock_seconds": wall_clock_seconds,
        "avg_latency_ms": average_latency_ms,
        "category_score": f"{category_score}/{total}",
        "flags_score": f"{flags_score}/{total}",
        "when_unsure_score": f"{unsure_score}/{unbounded_total}",
        "easy_hard_split": easy_hard,
    }
    RESULTS_PATH.write_text(
        json.dumps({**summary, "cases": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"wall-clock total: {wall_clock_seconds}s")
    print(f"avg latency per case: {average_latency_ms}ms")
    print(f"saved: {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
