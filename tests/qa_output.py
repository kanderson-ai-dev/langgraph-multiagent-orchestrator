"""Where generated QA artifacts (screenshots, run logs) land.

A plain ``pytest`` run must leave ``git status`` clean, so the default
output root is a per-session temp dir. To refresh the committed artifacts
under ``docs/`` (screenshots + RUNS.md), run the dedicated command instead
of bare pytest:

    uv run python scripts/refresh_qa_artifacts.py

which sets ``QA_OUTPUT_DIR=<repo root>`` for the browser-QA test files.
"""

import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_default_root: Path | None = None


def qa_output_dir() -> Path:
    """Root for generated QA artifacts — ``$QA_OUTPUT_DIR`` or a temp dir."""
    global _default_root
    override = os.environ.get("QA_OUTPUT_DIR", "").strip()
    if override:
        return Path(override).resolve()
    if _default_root is None:
        _default_root = Path(tempfile.mkdtemp(prefix="orx-uiqa-"))
    return _default_root
