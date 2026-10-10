"""The part of git that writes: staging, commits, branches, and talking to the remote.

gitinfo only reads. What changes the repository goes through here, and every path,
branch and reference that arrives from the page is checked before git sees it.
Conflicts are left to the terminal: a pull only fast-forwards, and when git refuses
something its own words are passed on.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from .gitinfo import MAX_PATCH, REF, STATUS
from .util import QUIET

LISTED = 400  # files shown per group, and branches per kind
LOG = 20
REMOTE = 300  # seconds a fetch, pull or push may take
CLONE = 1800


class GitError(Exception):
    """git refused, or could not be asked. The message is the one to show."""


def _run(root: Path | None, *args: str, ok: tuple[int, ...] = (0,), timeout: int = 120, chatter: bool = False,
         env: dict | None = None) -> str:
    """What git printed. With chatter, also what it said on the side, where push and pull report."""
    # Optional locks are left alone so that the watcher's status never gets in the way of
    # a git command in a terminal. Nothing may wait for a terminal either: a push that
    # needs a password has to fail here, where nobody could type one.
    command = ["git", *(["-C", str(root)] if root else []), "--no-optional-locks", "--literal-pathspecs",
               "-c", "core.quotepath=false", *args]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}, **QUIET)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took more than {timeout} seconds and was stopped")
    except OSError as error:
        raise GitError(f"git could not be run: {error}")
    out, err = (text.decode("utf-8", "replace") for text in (result.stdout, result.stderr))
    if result.returncode not in ok:
        raise GitError((err or out).strip() or f"git {args[0]} failed")
    return (out + err).strip() if chatter else out


@lru_cache(maxsize=32)
def _prefix(root: Path) -> str:
    """Where the folder sits inside its repository: '' at the top, 'docs/' one level down."""
    return _run(root, "rev-parse", "--show-prefix").strip()


def _status(root: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """git status for the folder: the branch headers, what is staged, and what is not."""
    prefix = _prefix(root)
    listing = _run(root, "status", "--porcelain=v2", "--branch", "-z", "--no-renames",
                   "--untracked-files=all", "--", ".")
    head: dict[str, str] = {}
    staged: dict[str, str] = {}
    unstaged: dict[str, str] = {}
    records = iter(listing.split("\0"))
    for record in records:
        kind = record[:1]
        if kind == "#":
            key, _, value = record[2:].partition(" ")
            head[key] = value
        elif kind == "?":
            unstaged[record[2:].removeprefix(prefix)] = "untracked"
        elif kind in ("1", "2", "u"):
            fields = record.split(" ", {"1": 8, "2": 9, "u": 10}[kind])
            path, index, tree = fields[-1].removeprefix(prefix), fields[1][0], fields[1][1]
            if kind == "2":
                next(records, None)  # a rename is followed by the name it had before
            if kind == "u":
                unstaged[path] = "conflict"
                continue
            if index != ".":
                staged[path] = STATUS.get(index, "modified")
            if tree != ".":
                unstaged[path] = STATUS.get(tree, "modified")
    return head, staged, unstaged


def fingerprint(root: Path) -> str | None:
    """A short string that changes when the working tree, the index or the branch does.

    The listing alone would not notice a second edit to a file that already differs,
    so the size and the time of every listed file go in as well.
    """
    try:
        head, staged, unstaged = _status(root)
    except GitError:
        return None
    digest = hashlib.sha1(repr((sorted(head.items()), sorted(staged.items()), sorted(unstaged.items()))).encode())
    for path in sorted({*staged, *unstaged}):
        try:
            found = os.stat(root / path)
            digest.update(f"{found.st_mtime_ns}:{found.st_size};".encode())
        except OSError:
            digest.update(b"gone;")
    return digest.hexdigest()


def _lines(root: Path, *args: str) -> list[str]:
    """The lines of a listing, or none when git has nothing to list or refuses."""
    try:
        return [line for line in _run(root, *args).splitlines() if line]
    except GitError:
        return []


def _remote(root: Path) -> str:
    remotes = _lines(root, "remote")
    return "origin" if "origin" in remotes else next(iter(remotes), "")


def origin(root: Path) -> str:
    """The address of the remote called origin, or '' when the folder has none."""
    return next(iter(_lines(root, "config", "--get", "remote.origin.url")), "")


def clone(address: str, folder: Path, token: str = "", site: str = "https://github.com") -> None:
    """Clone a repository into a folder that is not there yet.

    A token is handed to git for this one command, through its environment: it is not on
    the command line, and nothing of it is written into the clone. What the clone fetches
    and pushes later goes with the credentials git itself has.
    """
    if not address.startswith(("https://", "http://")) or folder.exists():
        raise GitError(f"cannot clone {address} into {folder}")
    env = {}
    if token:
        count = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {"GIT_CONFIG_COUNT": str(count + 1), f"GIT_CONFIG_KEY_{count}": f"http.{site}/.extraheader",
               f"GIT_CONFIG_VALUE_{count}": f"Authorization: Basic {basic}"}
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(None, "clone", "--quiet", "--", address, str(folder), timeout=CLONE, env=env)
    except GitError:
        shutil.rmtree(folder, ignore_errors=True)  # a clone that was stopped halfway is not left behind
        raise


def state(root: Path) -> dict | None:
    """Where the repository stands, or None outside one.

    The branch and how far it is from its upstream, what is staged and what is not,
    the branches there are to switch to, and the latest commits.
    """
    asks = [
        ("for-each-ref", f"--count={LISTED}", "--format=%(refname:short)", "refs/heads"),
        ("for-each-ref", f"--count={LISTED}", "--format=%(refname:lstrip=3)", "refs/remotes"),
        ("remote",),
        ("rev-list", "-n", "500", "HEAD", "--not", "--remotes"),
        ("log", f"-n{LOG}", "--date=short", "--format=%H%x02%h%x02%aN%x02%ad%x02%s"),
    ]
    # git is slow to start, on Windows above all, so its answers are asked for side by side.
    with ThreadPoolExecutor(len(asks) + 1) as pool:
        status = pool.submit(_status, root)
        local, elsewhere, remotes, unpushed, commits = pool.map(lambda args: _lines(root, *args), asks)
    try:
        head, staged, unstaged = status.result()
    except GitError:
        return None
    ahead, behind = (int(part[1:]) for part in (head.get("branch.ab") or "+0 -0").split())
    branch = head.get("branch.head", "")
    remote, unpushed = "origin" if "origin" in remotes else next(iter(remotes), ""), set(unpushed)
    log = []
    for line in commits:
        full, short, author, date, subject = (line.split("\x02", 4) + [""] * 4)[:5]
        log.append({"hash": short, "author": author, "date": date, "subject": subject[:160],
                    "pushed": bool(remote) and full not in unpushed})
    return {
        "branch": "" if branch == "(detached)" else branch,
        "head": "" if head.get("branch.oid") == "(initial)" else head.get("branch.oid", "")[:7],
        "upstream": head.get("branch.upstream", ""),
        "remote": remote,
        "ahead": ahead,
        "behind": behind,
        "staged": dict(sorted(staged.items())[:LISTED]),
        "unstaged": dict(sorted(unstaged.items())[:LISTED]),
        "counts": {"staged": len(staged), "unstaged": len(unstaged)},
        "branches": local,
        # A branch that only the remote has can be switched to by its plain name.
        "remote_branches": sorted({*elsewhere} - {*local, "HEAD"}),
        "log": log,
    }


def _inside(paths) -> list[str]:
    """Paths as the page sent them, refused unless each one stays inside the folder."""
    if paths is None:
        return []
    if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
        raise GitError("paths must be a list of file names")
    for path in paths:
        if Path(path).is_absolute() or Path(path).drive or ".." in Path(path).parts:
            raise GitError(f"'{path}' is not a file of this folder")
    return paths


def patch(root: Path, path: str | None = None, staged: bool = False, commit: str | None = None) -> str:
    """The diff to show: one commit, or one file as staged or as it is in the working tree."""
    look = ("--no-color", "--no-ext-diff", "--relative")
    if commit:
        if not REF.match(commit) or commit.startswith("-"):
            raise GitError(f"'{commit}' is not a valid git reference")
        text = _run(root, "show", *look, "--stat", "--patch", "--date=short",
                    "--format=%h  %aN  %ad%n%n%w(0,4,4)%B", commit, "--")
    else:
        (path,) = _inside([path or ""])
        text = _run(root, "diff", *look, "--no-renames", *(["--cached"] if staged else []), "--", path)
        if not text and not staged and not _lines(root, "ls-files", "--", path) and (root / path).is_file():
            # git has no diff for a file it does not track yet, so it is compared with nothing.
            text = _run(root, "diff", "--no-index", "--no-color", "--no-ext-diff", "--", "/dev/null", path, ok=(0, 1))
    return text[:MAX_PATCH]


def act(root: Path, request: dict) -> str:
    """Carry out one request from the page, and return what git said about it."""
    do = request.get("do")
    if do in ("stage", "unstage"):
        paths = _inside(request.get("paths")) or ["."]
        return _run(root, *(("add", "-A") if do == "stage" else ("reset", "-q")), "--", *paths)
    if do == "commit":
        message = str(request.get("message") or "").strip()
        if not message:
            raise GitError("a commit needs a message")
        return _run(root, "commit", "-q", "-m", message, chatter=True)
    if do in ("switch", "branch"):
        name = str(request.get("name") or "").strip()
        _run(root, "check-ref-format", "--branch", name)
        return _run(root, "switch", *(["-c"] if do == "branch" else []), name, chatter=True)
    if do == "fetch":
        return _run(root, "fetch", chatter=True, timeout=REMOTE)
    if do == "pull":  # only ever a fast-forward; anything else is a merge, and belongs in a terminal
        return _run(root, "pull", "--ff-only", chatter=True, timeout=REMOTE)
    if do == "push":
        head = _status(root)[0]
        if head.get("branch.upstream"):
            return _run(root, "push", chatter=True, timeout=REMOTE)
        remote, branch = _remote(root), head.get("branch.head", "")
        if not remote:
            raise GitError("this repository has no remote to push to")
        if branch in ("", "(detached)"):
            raise GitError("there is no branch to push: HEAD is detached")
        return _run(root, "push", "-u", remote, branch, chatter=True, timeout=REMOTE)
    raise GitError(f"unknown request '{do}'")
