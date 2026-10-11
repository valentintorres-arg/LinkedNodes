"""Walk a folder and sort its files into the kinds LinkedNodes can read."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from pathlib import Path

from . import saved
from .util import QUIET, read_text

OUT_DIR = "linkednodes-out"
IGNORE_FILE = ".linkednodesignore"

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
             "dist", "build", ".next", ".nuxt", ".cache", "target", ".idea", ".vscode",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "site-packages",
             "coverage", ".turbo", ".claude", OUT_DIR}

CODE_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
            ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go",
            ".rs": "rust", ".java": "java", ".cs": "csharp", ".rb": "ruby", ".php": "php"}
TEXT_DOC_EXT = {".md", ".markdown", ".mdx", ".txt", ".rst", ".org"}
DOC_EXT = TEXT_DOC_EXT | {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_BYTES = {"code": 1_000_000, "doc": 30_000_000, "image": 20_000_000, "saved": 60_000_000}


def file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _ignore_patterns(root: Path) -> list[str]:
    path = root / IGNORE_FILE
    if not path.is_file():
        return []
    lines = (line.strip() for line in read_text(path).splitlines())
    return [line.rstrip("/") for line in lines if line and not line.startswith("#")]


def _git_files(root: Path) -> set[str] | None:
    """Files git tracks or would track, or None when the folder is not a repository.

    Asking git is the only way to honour every .gitignore rule, and what a project
    ignores is what it does not want read: build output, generated code, secrets.
    """
    command = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
               "--exclude-standard"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=120, **QUIET)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {path for path in result.stdout.decode("utf-8", "replace").split("\0") if path}


def _ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    return any(fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(part, p) for part in parts)
               for p in patterns)


def pulse(root: Path) -> str:
    """A short string that changes when a file in the folder is added, removed or written.

    It reads no file, only names, sizes and times, and leaves out the folders a scan
    leaves out, so it can be asked several times a second: it is how an open window
    notices at once that the folder under it has changed.
    """
    digest, waiting = hashlib.sha1(), [str(root)]
    while waiting:
        folder = waiting.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRS and not entry.name.startswith(".") and not entry.name.endswith(".egg-info"):
                        waiting.append(entry.path)
                else:
                    found = entry.stat(follow_symlinks=False)
                    digest.update(f"{entry.path}:{found.st_mtime_ns}:{found.st_size};".encode("utf-8", "replace"))
            except OSError:
                continue
    return digest.hexdigest()


def scan(root: Path, each=None) -> tuple[list[dict], list[saved.Item]]:
    """Return every readable file as a record, plus the saved items found in exports.

    each, when given, is told how many files have been taken so far, after every one.
    """
    patterns = _ignore_patterns(root)
    in_git = _git_files(root)
    files: list[dict] = []
    items: list[saved.Item] = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                         and not d.startswith(".") and not d.endswith(".egg-info"))
        for name in sorted(names):
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if _ignored(rel, patterns) or (in_git is not None and rel not in in_git):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            ext = path.suffix.lower()
            kind, lang = None, ""
            if saved.is_candidate(path) and size <= MAX_BYTES["saved"]:
                found = saved.parse(path, rel)
                if found:
                    kind = "saved"
                    items += found
            if kind is None:
                if ext in CODE_EXT:
                    kind, lang = "code", CODE_EXT[ext]
                elif ext in DOC_EXT:
                    kind = "doc"
                elif ext in IMAGE_EXT:
                    kind = "image"
            if kind is None or size == 0 or size > MAX_BYTES[kind]:
                continue
            files.append({"path": rel, "kind": kind, "lang": lang, "hash": file_hash(path),
                          "size": size})
            if each:
                each(len(files))
    return files, saved.dedupe(items)
