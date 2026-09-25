"""Worker registry — maps names to workers and renders the routing catalog."""

from collections.abc import Iterator

from app.framework.worker import Worker

# Sentinel the Supervisor returns when no worker should run next.
FINISH = "FINISH"


class WorkerRegistry:
    """Registration and lookup for a team of workers.

    One registry per domain/team; the Supervisor routes only among the
    registered names (plus the ``FINISH`` sentinel).
    """

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, worker: Worker) -> Worker:
        """Register a worker; returns it for decorator-style use."""
        if worker.name in self._workers:
            msg = f"worker {worker.name!r} is already registered"
            raise ValueError(msg)
        if worker.name == FINISH:
            msg = f"{FINISH!r} is a reserved name"
            raise ValueError(msg)
        self._workers[worker.name] = worker
        return worker

    def get(self, name: str) -> Worker:
        try:
            return self._workers[name]
        except KeyError:
            msg = f"unknown worker {name!r}; registered: {sorted(self._workers)}"
            raise KeyError(msg) from None

    def names(self) -> list[str]:
        return list(self._workers)

    def view(self, names: list[str]) -> "WorkerRegistry":
        """Return a registry limited to ``names`` — the job's active team.

        Unknown names are skipped so a stale ``team`` in state can never
        resurrect an unregistered worker.
        """
        v = WorkerRegistry()
        for n in names:
            if n in self._workers:
                v._workers[n] = self._workers[n]
        return v

    def catalog(self) -> str:
        """Render the worker list for the Supervisor's routing prompt."""
        return "\n".join(
            f"- {w.name}: {w.description}" for w in self._workers.values()
        )

    def __contains__(self, name: str) -> bool:
        return name in self._workers

    def __iter__(self) -> Iterator[Worker]:
        return iter(self._workers.values())

    def __len__(self) -> int:
        return len(self._workers)
