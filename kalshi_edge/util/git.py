"""
Git helpers used by the CLI for run metadata.

These helpers are intentionally best-effort: failures return None rather than
raising, so trading/evaluation is not blocked by missing git metadata.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional, Sequence


def find_repo_root(start: str, *, max_depth: int = 30) -> Optional[str]:
    cur = os.path.abspath(str(start))
    for _ in range(int(max_depth)):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def best_effort_git_commit(*, start_paths: Sequence[str]) -> Optional[str]:
    """
    Return the current git commit hash (HEAD) for the first repo root found.

    Args:
      start_paths: candidate paths to search upward for a `.git` directory.
    """
    for p in list(start_paths):
        if not p:
            continue
        repo_root = find_repo_root(str(p))
        if not repo_root:
            continue
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            s = (out or "").strip()
            return s if s else None
        except Exception:
            return None
    return None

