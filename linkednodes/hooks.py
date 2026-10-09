"""Git hooks that rebuild the graph after a commit or a merge.

This is the one place that writes inside .git, and only into its hooks folder. A hook
that LinkedNodes did not write is never touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MARK = "# linkednodes: rebuild the graph after this repository changes"
NAMES = ("post-commit", "post-merge")
SCRIPT = """#!/bin/sh
{mark}
# Runs in the background so that committing is not slowed down. Remove it with:
#   python -m linkednodes hook remove
(cd "$(git rev-parse --show-toplevel)/{folder}" && [ -d linkednodes-out ] \\
  && "{python}" -m linkednodes refresh . > linkednodes-out/.work/hook.log 2>&1) &
"""


def _ask(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
    if result.returncode != 0:
        raise FileNotFoundError(f"{root} is not inside a git repository")
    return result.stdout.decode("utf-8", "replace").strip()


def _folder(root: Path) -> tuple[Path, str]:
    """Where the hooks live, and the graphed folder as a path from the top of the repository."""
    top = Path(_ask(root, "rev-parse", "--show-toplevel"))
    hooks = (root / _ask(root, "rev-parse", "--git-path", "hooks")).resolve()
    return hooks, root.resolve().relative_to(top.resolve()).as_posix()


def install(root: Path) -> list[Path]:
    hooks, folder = _folder(root)
    targets = [hooks / name for name in NAMES]
    for target in targets:  # check them all before writing any
        if target.exists() and MARK not in target.read_text(encoding="utf-8", errors="replace"):
            raise FileExistsError(
                f"{target} already exists and was not written by LinkedNodes. Add this line to it "
                f"yourself instead:\n  python -m linkednodes refresh .")
    hooks.mkdir(parents=True, exist_ok=True)
    script = SCRIPT.format(mark=MARK, folder=folder, python=Path(sys.executable).as_posix())
    for target in targets:
        target.write_text(script, encoding="utf-8", newline="\n")
        os.chmod(target, 0o755)
    return targets


def remove(root: Path) -> list[Path]:
    hooks, _ = _folder(root)
    removed = []
    for target in (hooks / name for name in NAMES):
        if target.exists() and MARK in target.read_text(encoding="utf-8", errors="replace"):
            target.unlink()
            removed.append(target)
    return removed
