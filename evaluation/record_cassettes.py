"""Record live OpenAI/search/scrape interactions as VCR cassettes.

Requires real credentials in ``.env`` (never committed). The recorded
cassettes let CI replay 2–3 real model responses fully offline —
``tests/test_live_replay.py`` asserts the pipeline completes against
them without network or keys.

Replay is deterministic because matching is *normalized*: provider
session tokens (DuckDuckGo's rotating ``vqd`` and friends) are stripped
before comparison, and request bodies compare semantically (JSON bodies
as parsed objects, form bodies as sorted params). Secrets are scrubbed
at record time (auth headers, org headers, cookies, and ``api_key``-style
query params never reach the cassette), and
``tests/test_cassettes_scrubbed.py`` fails CI if a secret ever lands in
one.

Usage:
    uv run python -m evaluation.record_cassettes
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import vcr
import yaml

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.services.llm_client import build_llm
from app.services.prompt_eval import brief_for_case
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import build_search_client
from evaluation.dataset import DATASET, EvalCase

CASSETTE_DIR = Path(__file__).parent.parent / "tests" / "cassettes"

# Cases recorded for offline replay — two quality cases plus the
# adversarial brief (it must still block before any external call).
CASES_TO_RECORD = ["rag-saas", "eu-ai-act", "blocked-injection"]

_FILTERED_HEADERS = [
    "authorization",
    "x-api-key",
    "api-key",
    "openai-organization",
    "openai-organization-id",
    "cookie",
    "set-cookie",
]
_FILTERED_HEADER_SET = {h.lower() for h in _FILTERED_HEADERS}

_SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "key", "token", "access_token"}

# Provider session/noise params — rotate per session; never part of a
# request's identity for replay purposes.
_NOISE_QUERY_KEYS = {
    "vqd", "v", "kl", "df", "bing_market", "setlang", "sse_token",
    "s", "o", "pq", "r", "ct", "ss_mkt", "cvid",
}


def _normalized_url(url: str) -> str:
    """Scheme+host+path plus sorted query minus secrets/noise."""
    parts = urlsplit(url)
    kept = sorted(
        (k.lower(), v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _SENSITIVE_QUERY_KEYS | _NOISE_QUERY_KEYS
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(kept),
            "",
        )
    )


def _normalized_body(body: Any) -> Any:
    """JSON bodies compare as objects; form bodies as sorted params."""
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            return sorted(
                (k.lower(), v) for k, v in pairs
                if k.lower() not in _SENSITIVE_QUERY_KEYS | _NOISE_QUERY_KEYS
            )
        return text


def _pipeline_matcher(r1: Any, r2: Any) -> bool:
    """Method + normalized URL + normalized body — deterministic replay."""
    if (r1.method or "").upper() != (r2.method or "").upper():
        return False
    if _normalized_url(r1.uri) != _normalized_url(r2.uri):
        return False
    return _normalized_body(r1.body) == _normalized_body(r2.body)


MATCH_ON = ["pipeline"]

_VCR = vcr.VCR(match_on=MATCH_ON)
_VCR.register_matcher("pipeline", _pipeline_matcher)


def before_record_request(request: Any) -> Any:
    """Store the normalized URI — symmetric with matcher normalization."""
    request.uri = _normalized_url(request.uri)
    return request


def before_record_response(response: Any) -> Any:
    """Drop auth/org/cookie headers echoed back in responses."""
    headers = response.get("headers")
    if isinstance(headers, dict):
        for name in list(headers):
            if name.lower() in _FILTERED_HEADER_SET:
                del headers[name]
    return response


def cassette_for(case_id: str, *, record_mode: str = "once") -> Any:
    return _VCR.use_cassette(
        str(CASSETTE_DIR / f"edd_{case_id}.yaml"),
        record_mode=record_mode,
        filter_headers=_FILTERED_HEADERS,
        before_record_request=before_record_request,
        before_record_response=before_record_response,
        decode_compressed_response=True,
    )


def search_sidecar_path(case_id: str) -> Path:
    """Sidecar JSON with recorded ``query -> results`` for a case.

    ``ddgs`` talks over ``curl_cffi`` — outside vcrpy's interception —
    so search responses are captured as data, not HTTP.
    """
    return CASSETTE_DIR / f"edd_{case_id}_search.json"


class _RecordingSearch:
    """Wrap the real search client; dump every query's results.

    Outcomes are recorded per *call*, in order — a query that fails in
    round 1 but succeeds in round 3 keeps both outcomes, so a replay
    reproduces the exact run instead of seeing the later success early.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.captures: dict[str, list[list[dict[str, Any]]]] = {}

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[Any]:
        outcomes = self.captures.setdefault(query, [])
        try:
            results = await self._inner.search(
                query, max_results=max_results, workspace_id=workspace_id
            )
        except Exception:
            # A transient failure is itself an outcome: an empty set here
            # reproduces the skipped query exactly (the researcher treats
            # a raised search and zero hits identically).
            outcomes.append([])
            raise
        outcomes.append(
            [
                r.model_dump() if hasattr(r, "model_dump") else dict(r)
                for r in results
            ]
        )
        return results


async def record_case(case: EvalCase, settings: Settings) -> str:
    registry = PromptRegistry.load()
    search = _RecordingSearch(build_search_client(settings))
    graph = build_graph(
        settings,
        llm=build_llm(settings),
        search=search,
        scraper=ScraperClient(settings),
        prompts=registry,
    )
    state = initial_state(
        job_id=f"cassette-{case.case_id}",
        workspace_id="eval",
        brief=brief_for_case(case),
        max_debate_rounds=settings.max_debate_rounds,
    )
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    cassette_path = CASSETTE_DIR / f"edd_{case.case_id}.yaml"
    cassette_path.unlink(missing_ok=True)  # record fresh, not "once"
    with cassette_for(case.case_id, record_mode="new_episodes"):
        out = await graph.ainvoke(state)
    if not cassette_path.exists():
        # The input guardrail blocked before any HTTP call — the empty
        # cassette is itself the proof that nothing was fetched.
        cassette_path.write_text(
            yaml.safe_dump(
                {"interactions": [], "recorded_with": "record_cassettes"},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    search_sidecar_path(case.case_id).write_text(
        json.dumps({"queries": search.captures}, indent=2), encoding="utf-8"
    )
    return str(out.get("status"))


async def _main() -> int:
    settings = Settings()  # real .env — credentials required
    if settings.openai_api_key is None:
        print("OPENAI_API_KEY required to record cassettes", file=sys.stderr)
        return 2
    cases = [c for c in DATASET if c.case_id in CASES_TO_RECORD]
    for case in cases:
        print(f"recording {case.case_id} ...")
        status = await record_case(case, settings)
        print(f"  -> {status}")
    print(f"Cassettes written under {CASSETTE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
