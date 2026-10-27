"""Standalone live EDD gate — real LLM, real search/scrape, no Langfuse.

Runs the eval dataset through the real graph exactly like a production
job, scores it against ``EDD_THRESHOLDS``, and writes a self-contained
run file under ``evaluation/results/live/`` (per-case metrics, citation
diagnoses, evidence texts, aggregates + Wilson intervals, spend).

Evidence snapshots (``--snapshot record|replay``) freeze the search and
scrape layer per case so prompt/model iteration replays the *same*
evidence while still calling the real LLM. Blocked cases cost nothing —
the input guardrail stops them before any LLM call.

Requires ``--live`` to spend money; without it the run uses the
deterministic stub + fixtures (free — a harness self-test). ``--cap-usd``
is always enforced; the runner aborts before starting a case that would
project past the cap.

Usage (project root):

    uv run python scripts/live_gate.py --live --snapshot record
    uv run python scripts/live_gate.py --live --snapshot replay --pin writer=4
    uv run python scripts/live_gate.py --live --heldout --runs 3 --cap-usd 0.60
    uv run python scripts/live_gate.py --diagnose evaluation/results/live/<file>.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.core.thresholds import EDD_THRESHOLDS  # noqa: E402
from app.graph.citations import explain_quote  # noqa: E402
from app.graph.claim_support import judge_claim_support  # noqa: E402
from app.graph.nodes.reviewer import verify_citations  # noqa: E402
from app.services.llm_client import (  # noqa: E402
    OpenAIStructuredLLM,
    StubLLM,
    build_judge_llm,
    build_llm,
)
from app.services.prompt_eval import (  # noqa: E402
    aggregate_metrics,
    brief_for_case,
    run_eval_case,
)
from app.services.prompt_registry import PromptRegistry  # noqa: E402
from app.services.scraper_client import ScrapedPage, ScrapeError  # noqa: E402
from app.services.search_client import SearchResult, build_search_client  # noqa: E402
from evaluation.dataset import DATASET, EvalCase  # noqa: E402

RESULTS_DIR = Path("evaluation/results/live")
SNAPSHOT_DIR = Path("evaluation/evidence_snapshots")

# Per-case projection for the cap check — ~4x the observed ~$0.012 average
# (gpt-4o-mini pipeline + gpt-4o judge). Deliberately tight: a fat
# projection would refuse small probe caps, a thin one risks overshoot.
_PROJECTED_PER_CASE_USD = 0.05


# ---------------------------------------------------------------------------
# Evidence snapshots
# ---------------------------------------------------------------------------


def _snapshot_path(case_id: str) -> Path:
    return SNAPSHOT_DIR / f"{case_id}.json"


class _RecordingSearch:
    """Wrap a real SearchClient; every result lands in ``store``."""

    def __init__(self, real: Any, store: dict[str, Any]) -> None:
        self._real = real
        self._store = store

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        results = await self._real.search(
            query, max_results=max_results, workspace_id=workspace_id
        )
        self._store["searches"][query] = [
            r.model_dump(mode="json") for r in results
        ]
        return results


class _RecordingScraper:
    """Wrap a real scraper; successful pages are stored, errors recorded."""

    def __init__(self, real: Any, store: dict[str, Any]) -> None:
        self._real = real
        self._store = store

    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
        try:
            page = await self._real.scrape(url, workspace_id=workspace_id)
        except Exception as exc:  # record the failure too — replay must be identical
            self._store["scrapes"][url] = {"error": str(exc)}
            raise
        self._store["scrapes"][url] = {
            "url": str(page.url),
            "status_code": page.status_code,
            "content_type": page.content_type,
            "body_b64": base64.b64encode(page.body).decode("ascii"),
        }
        return page


class _SnapshotSearch:
    """Replay: the recorded result pool, regardless of query wording.

    The Researcher's planner is a live LLM — it may phrase the same need
    differently each run. Freezing evidence therefore can't key on query
    text; it serves the union of recorded hits (deduped by URL, rank order
    preserved) so the same URL pool reaches the scraper every time.
    """

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self.misses: list[str] = []
        pool: dict[str, dict[str, Any]] = {}
        for hits in self._store.get("searches", {}).values():
            for r in hits:
                pool.setdefault(r["url"], r)
        self._pool = list(pool.values())

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        if not self._pool:
            self.misses.append(f"search:{query}")
            return []
        # All recorded results, not pool[:max_results] — the record run saw
        # a different top-N per query; returning only the head would starve
        # the scrape stage to the same two URLs (and if those were recorded
        # failures, the case gets zero evidence and fails).
        return [SearchResult.model_validate(r) for r in self._pool]


class _SnapshotScraper:
    """Serve recorded pages; unknown URL or recorded error → ScrapeError."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self.misses: list[str] = []

    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
        rec = self._store.get("scrapes", {}).get(url)
        if rec is None:
            self.misses.append(f"scrape:{url}")
            raise ScrapeError(f"url not in evidence snapshot: {url}")
        if "error" in rec:
            raise ScrapeError(f"recorded scrape failure: {rec['error']}")
        return ScrapedPage(
            url=rec["url"],
            status_code=rec["status_code"],
            content_type=rec["content_type"],
            body=base64.b64decode(rec["body_b64"]),
        )


def _load_snapshot(case_id: str) -> dict[str, Any]:
    path = _snapshot_path(case_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _save_snapshot(case_id: str, store: dict[str, Any]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot_path(case_id).write_text(
        json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Statistics / gating
# ---------------------------------------------------------------------------


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return round((centre - margin) / denom, 4), round((centre + margin) / denom, 4)


def _gate_report(
    agg: dict[str, float], thresholds: dict[str, float] | None = None
) -> dict[str, dict[str, Any]]:
    th = EDD_THRESHOLDS if thresholds is None else thresholds
    return {
        metric: {
            "value": round(agg.get(metric, 0.0), 4),
            "threshold": threshold,
            "pass": agg.get(metric, 0.0) >= threshold,
        }
        for metric, threshold in th.items()
    }


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Per-case citation diagnosis (stored in the run file)
# ---------------------------------------------------------------------------


def _diagnose_case(state: Any) -> list[dict[str, Any]]:
    """Per-citation verdict + failure stage against the case's evidence."""
    drafts = state.get("drafts", []) if isinstance(state, dict) else []
    if not drafts:
        return []
    draft = drafts[-1]
    evidence = state.get("evidence", [])
    kept, _dropped, _h, _u = verify_citations(draft, evidence)
    kept_ids = {id(c) for c in kept}
    by_url = {e.url: e for e in evidence}
    out: list[dict[str, Any]] = []
    for c in draft.citations:
        item = by_url.get(c.source_url)
        if item is None:
            stage, ratio = "unknown_url", 0.0
        else:
            diag = explain_quote(c.quote, item.text)
            stage, ratio = diag["stage"], diag["best_ratio"]
        out.append(
            {
                "claim": c.claim[:200],
                "ref": c.ref or None,
                "quote": c.quote[:400],
                "source_url": c.source_url,
                "kept": id(c) in kept_ids,
                "stage": stage,
                "best_ratio": round(float(ratio), 4),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def _run_once(
    *,
    cases: list[EvalCase],
    run_index: int,
    args: argparse.Namespace,
    settings: Settings,
    registry: PromptRegistry,
    spent: list[float],
) -> dict[str, Any]:
    """One pass over the case list; returns the serialisable run record."""
    llm = judge_llm = search = scraper = overrides = None
    if args.live:
        llm = build_llm(settings)
        judge_llm = build_judge_llm(settings)
        overrides = None
        if settings.reviewer_model:
            overrides = {
                "reviewer": OpenAIStructuredLLM(
                    settings, model=settings.reviewer_model
                )
            }
        search = build_search_client(settings)
        from app.services.scraper_client import ScraperClient

        scraper = ScraperClient(settings)
    else:
        # Stub mode must stay fully offline — never let ``run_eval_case``
        # fall back to ``build_judge_llm(settings)``, which would make a
        # real call whenever .env happens to hold a key.
        judge_llm = StubLLM()

    results: list[dict[str, Any]] = []
    case_records: list[dict[str, Any]] = []
    snapshot_misses: list[str] = []
    aborted = False
    for case in cases:
        projected = spent[0] + _PROJECTED_PER_CASE_USD
        if projected > args.cap_usd and args.live:
            print(
                f"  cap stop: spent ${spent[0]:.3f} + projected "
                f"${_PROJECTED_PER_CASE_USD:.2f} > ${args.cap_usd:.2f} "
                f"— skipping {case.case_id} and the rest",
                file=sys.stderr,
            )
            aborted = True
            break
        store: dict[str, Any] | None = None
        use_search, use_scraper = search, scraper
        snap_clients: list[Any] = []
        if args.live and args.snapshot != "none":
            if args.snapshot == "record":
                store = {"case_id": case.case_id, "searches": {}, "scrapes": {}}
                use_search = _RecordingSearch(search, store)
                use_scraper = _RecordingScraper(scraper, store)
            else:  # replay
                snap = _load_snapshot(case.case_id)
                use_search = _SnapshotSearch(snap)
                use_scraper = _SnapshotScraper(snap)
                snap_clients = [use_search, use_scraper]

        try:
            metrics, state = await run_eval_case(
                case_id=case.case_id,
                brief=brief_for_case(case),
                expect_blocked=case.expect_blocked,
                settings=settings,
                registry=registry,
                llm=llm,
                judge_llm=judge_llm,
                search=use_search,
                scraper=use_scraper,
                llm_overrides=overrides,
                auto_review=True,
            )
        except Exception as exc:  # one bad case must not kill the run
            print(
                f"  {case.case_id}: CASE ERROR {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            metrics = {
                "case_id": case.case_id,
                "status": "error",
                "citation_support": 0.0,
                "rubric_score": 0.0,
                "judge_score": 0.0,
                "converged": False,
                "blocked": False,
                "expected_blocked": case.expect_blocked,
                "hitl_resumes": 0,
                "debate_rounds": 0,
                "duration_seconds": 0.0,
                "cost_usd": 0.0,
                "judge_cost_usd": 0.0,
                "llm_calls": 0,
                "claim_support_rate": 0.0,
                "claim_verdicts": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
            results.append(metrics)
            case_records.append(
                {
                    **metrics,
                    "verdict": None,
                    "sub_questions": [],
                    "headings": [],
                    "final_markdown": "",
                    "citations": [],
                    "evidence": [],
                }
            )
            continue
        # Integrity metric (D3): does each surviving quote actually back
        # its claim? One batched judge call per case — eval-time only.
        citations_final = (
            state.get("drafts", [])[-1].citations
            if isinstance(state, dict) and state.get("drafts")
            else []
        )
        verdicts, cs_rate = await judge_claim_support(
            list(citations_final), judge_llm=judge_llm
        )
        metrics["claim_support_rate"] = cs_rate
        metrics["claim_verdicts"] = verdicts
        case_spend = float(metrics["cost_usd"]) + float(metrics["judge_cost_usd"])
        spent[0] += case_spend
        results.append(metrics)
        per_cit = _diagnose_case(state)
        for cit, v in zip(per_cit, metrics["claim_verdicts"], strict=False):
            cit["claim_verdict"] = v
        if store is not None:
            _save_snapshot(case.case_id, store)
        for c in snap_clients:
            snapshot_misses.extend(getattr(c, "misses", []))
        latest = state.get("latest_verdict") if isinstance(state, dict) else None
        final_draft = (
            state.get("drafts", [])[-1]
            if isinstance(state, dict) and state.get("drafts")
            else None
        )
        headings = (
            [ln.strip() for ln in final_draft.markdown.splitlines() if ln.startswith("#")]
            if final_draft is not None
            else []
        )
        case_records.append(
            {
                **metrics,
                # Failure forensics: a case that dies before its first LLM
                # call (status failed, $0) is undiagnosable from metrics
                # alone — keep the state errors and transcript tail.
                "errors": list(state.get("errors", []))
                if isinstance(state, dict)
                else [],
                "transcript_tail": [
                    getattr(m, "content", str(m))
                    for m in (state.get("transcript", []) if isinstance(state, dict) else [])[-4:]
                ],
                "verdict": (
                    {
                        "verdict": latest.verdict,
                        "score": latest.score,
                        "rubric": latest.rubric,
                        "feedback": latest.feedback,
                        "unsupported": latest.unsupported_citations,
                    }
                    if latest is not None
                    else None
                ),
                "sub_questions": list(state.get("sub_questions", []))
                if isinstance(state, dict)
                else [],
                "headings": headings,
                "final_markdown": final_draft.markdown if final_draft else "",
                "citations": per_cit,
                "evidence": [
                    {
                        "url": e.url,
                        "sha256": e.sha256,
                        "text": e.text,
                    }
                    for e in (state.get("evidence", []) if isinstance(state, dict) else [])
                ],
            }
        )
        print(
            f"  {case.case_id}: status={metrics['status']} "
            f"support={metrics['citation_support']:.2f} "
            f"rubric={metrics['rubric_score']} conv={metrics['converged']} "
            f"${case_spend:.4f} (total ${spent[0]:.3f})"
        )

    agg = aggregate_metrics(results)
    normal_rates = [
        float(r["claim_support_rate"])
        for r in results
        if not r["expected_blocked"] and r.get("claim_verdicts")
    ]
    if normal_rates:
        agg["claim_support_rate_avg"] = sum(normal_rates) / len(normal_rates)
    normal = [r for r in results if not r["expected_blocked"]]
    blocked = [r for r in results if r["expected_blocked"]]
    # The claim-support gate is meaningful only where a judge adjudicated —
    # live runs. Stub runs produce empty verdict sets, so the metric is
    # dropped from the gate set rather than scored 0.
    gate_thresholds = dict(EDD_THRESHOLDS)
    if not (args.live and normal_rates):
        gate_thresholds.pop("claim_support_rate_avg", None)
    gates = _gate_report(agg, gate_thresholds)
    run = {
        "run_index": run_index,
        "dataset": "both" if args.both else "heldout" if args.heldout else "dev",
        "git_sha": _git_sha(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "live": bool(args.live),
        "snapshot": args.snapshot,
        "pins": args.pins,
        "models": {
            "chat": settings.chat_model_name,
            "reviewer": settings.reviewer_model,
            "judge": settings.judge_model,
        },
        "aborted_on_cap": aborted,
        "snapshot_misses": snapshot_misses,
        "cases": case_records,
        "aggregates": agg,
        "wilson95": {
            "debate_convergence": _wilson(
                sum(1 for r in normal if r["converged"]), len(normal)
            ),
            "blocked": _wilson(
                sum(1 for r in blocked if r["blocked"]), len(blocked)
            ),
            "citation_support": _wilson(
                sum(1 for r in normal if r["citation_support"] >= 0.90),
                len(normal),
            ),
        },
        "gates": gates,
        "spend_usd": round(spent[0], 4),
        "passed": all(g["pass"] for g in gates.values()) and not aborted,
    }
    return run


def _write_run(run: dict[str, Any], label: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = run["ts"].replace(":", "").replace("-", "")
    name = f"{stamp}-{run['dataset']}-{label}-r{run['run_index']}.json"
    path = RESULTS_DIR / name
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _print_gates(run: dict[str, Any]) -> None:
    print(f"  --- aggregates (run {run['run_index']}) ---")
    for metric, g in run["gates"].items():
        mark = "PASS" if g["pass"] else "FAIL"
        print(f"  {mark} {metric}: {g['value']:.3f} (>= {g['threshold']})")
    for k, v in run["wilson95"].items():
        print(f"  wilson95 {k}: {v}")


# ---------------------------------------------------------------------------
# Diagnose an existing run file
# ---------------------------------------------------------------------------


def _diagnose_file(path: Path) -> int:
    run = json.loads(path.read_text(encoding="utf-8"))
    stages: dict[str, int] = {}
    total = dropped = 0
    for case in run.get("cases", []):
        for cit in case.get("citations", []):
            total += 1
            if not cit["kept"]:
                dropped += 1
                stages[cit["stage"]] = stages.get(cit["stage"], 0) + 1
    print(f"{path.name}: {dropped}/{total} citations dropped")
    for stage, n in sorted(stages.items(), key=lambda kv: -kv[1]):
        print(f"  {stage}: {n}")
    worst = sorted(
        (
            c
            for case in run.get("cases", [])
            for c in case.get("citations", [])
            if not c["kept"]
        ),
        key=lambda c: -float(c["best_ratio"]),
    )[:8]
    for c in worst:
        print(
            f"  [{c['stage']} r={c['best_ratio']:.2f}] {c['source_url'][:60]}\n"
            f"      {c['quote'][:120]!r}"
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="real LLM/search/scrape — spends money")
    parser.add_argument("--heldout", action="store_true",
                        help="sealed held-out set instead of the dev set")
    parser.add_argument("--both", action="store_true",
                        help="dev + held-out in one pass — the final proof "
                             "configuration (14 quality + blocked cases)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0,
                        help="first N cases only (smoke)")
    parser.add_argument("--only", default="",
                        help="comma-separated case_ids — targeted probes")
    parser.add_argument("--pin", action="append", default=[],
                        metavar="NAME=VERSION", dest="pins")
    parser.add_argument("--snapshot", choices=["none", "record", "replay"],
                        default="none",
                        help="record: save search/scrape per case; "
                             "replay: reuse them (LLM stays live)")
    parser.add_argument("--cap-usd", type=float, default=2.0,
                        help="hard spend ceiling for this invocation")
    parser.add_argument("--writer-model", default=None)
    parser.add_argument("--reviewer-model", default=None)
    parser.add_argument("--label", default="run")
    parser.add_argument("--diagnose", metavar="RUNFILE", default=None)
    args = parser.parse_args()

    if args.diagnose:
        return _diagnose_file(Path(args.diagnose))

    from app.core.logging import harden_console_streams

    harden_console_streams()

    if args.both:
        from evaluation.dataset_heldout import HELDOUT

        cases: list[EvalCase] = [*DATASET, *HELDOUT]
    elif args.heldout:
        from evaluation.dataset_heldout import HELDOUT

        cases = list(HELDOUT)
    else:
        cases = list(DATASET)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.case_id in wanted]
        if not cases:
            print(f"--only matched nothing in {wanted}", file=sys.stderr)
            return 1
    if args.limit:
        cases = cases[: args.limit]

    settings = Settings()
    if args.writer_model:
        settings = settings.model_copy(update={"chat_model_name": args.writer_model})
    if args.reviewer_model:
        settings = settings.model_copy(
            update={"reviewer_model": args.reviewer_model}
        )

    if args.live and settings.openai_api_key is None:
        print("--live requires OPENAI_API_KEY", file=sys.stderr)
        return 1
    if args.live and not args.cap_usd:
        print("--live requires --cap-usd", file=sys.stderr)
        return 1
    if args.snapshot == "replay":
        missing = [c.case_id for c in cases if not _snapshot_path(c.case_id).exists()]
        if missing:
            print(f"no snapshot for: {missing} — run --snapshot record first",
                  file=sys.stderr)
            return 1

    pins: dict[str, int] = {}
    for spec in args.pins:
        name, sep, version = spec.partition("=")
        if not sep or not name:
            parser.error(f"bad --pin {spec!r}")
        pins[name] = int(version)

    registry = PromptRegistry.load(db_path=settings.database_path)
    if pins:
        registry = registry.with_overrides(pins)

    spent = [0.0]
    all_passed = True
    paths: list[Path] = []
    for i in range(args.runs):
        run = asyncio.run(
            _run_once(
                cases=cases, run_index=i + 1, args=args,
                settings=settings, registry=registry, spent=spent,
            )
        )
        _print_gates(run)
        paths.append(_write_run(run, args.label))
        all_passed = all_passed and run["passed"]
        if run["aborted_on_cap"]:
            break

    print(f"runs written: {[p.name for p in paths]}")
    print(f"total spend: ${spent[0]:.4f} / cap ${args.cap_usd:.2f}")
    print(f"RESULT: {'ALL RUNS PASSED' if all_passed else 'GATE FAILED'}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
