"""In-process latency tracking — exact p50/p95 for the dashboard.

Prometheus histograms answer "how many in each bucket"; the operator
dashboard needs real quantiles, so node/job durations are also kept in a
bounded rolling window per series. Asyncio makes a plain deque safe
enough — no threads, no locks.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field

_WINDOW = 256  # samples kept per series — recent-representative, bounded


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(len(ordered) * pct)))
    return ordered[idx]


@dataclass
class LatencyTracker:
    """Rolling per-series duration tracker (node names, ``job``, …)."""

    _series: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=_WINDOW))
    )

    def observe(self, series: str, seconds: float) -> None:
        self._series[series].append(seconds)

    def quantiles(self) -> dict[str, dict[str, float | int]]:
        """``{series: {count, p50, p95}}`` — empty when nothing observed."""
        return {
            name: {
                "count": len(samples),
                "p50": _percentile(list(samples), 0.50),
                "p95": _percentile(list(samples), 0.95),
            }
            for name, samples in self._series.items()
            if samples
        }


LATENCY = LatencyTracker()
