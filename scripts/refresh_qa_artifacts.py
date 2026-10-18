"""Regenerate the committed UI-QA artifacts (screenshots + RUNS.md).

The browser QA tests write their outputs under the ``QA_OUTPUT_DIR`` root
— a temp dir by default so plain ``pytest`` never dirties the repo. This
script points that root at the repository and runs only the artifact-
producing test files, so ``docs/ui-qa/`` and ``docs/functional-qa/RUNS.md``
are refreshed deterministically on demand.

Usage::

    uv run python scripts/refresh_qa_artifacts.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    env = {**os.environ, "QA_OUTPUT_DIR": str(ROOT)}
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_frontend_qa_matrix.py",
            "tests/test_frontend_e2e.py",
        ],
        cwd=ROOT,
        env=env,
    )


if __name__ == "__main__":
    sys.exit(main())
