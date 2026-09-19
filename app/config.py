"""Environment configuration shared across the project.

Phase 0 used a private ``.env`` loader inside ``app.tools.pubmed``; Phase 2
needs the same behaviour for ``GLM_API_KEY`` / ``GLM_MODEL``, so the loader
moved here (single implementation, two consumers).

Semantics (unchanged since Phase 0):
- a project-root ``.env`` file is optional;
- real process environment variables always win (``setdefault``);
- an unreadable ``.env`` logs a warning and is skipped — credentials can
  still come from the real environment, so this must not be fatal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Project root (the directory that contains main.py / .env).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_env_file_loaded = False


def load_env_file(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from ``.env`` into ``os.environ`` (once).

    Deliberately a tiny stdlib replacement for python-dotenv: the project
    only needs simple ``KEY=VALUE`` lines and comments. Values already
    present in the real environment are never overridden.
    """
    global _env_file_loaded
    if _env_file_loaded:
        return
    _env_file_loaded = True
    env_path = path if path is not None else PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(
            f"[config warning] Could not read {env_path}: {exc} "
            "(continuing with the real environment only)",
            file=sys.stderr,
        )
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)
