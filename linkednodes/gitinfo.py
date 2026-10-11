"""What git knows about the folder: who last touched each file, how often, and what changed.

Everything here only reads the repository; what writes to it is in gitops. History is taken per file, which is cheap.
What changed is narrowed down to functions and methods by matching the changed lines
against where each definition starts.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path

from .detect import OUT_DIR
from .graph import CONTAINMENT, family
from .util import QUIET, edge, node, slug

REF = re.compile(r"^[\w./@^~{}-]+$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
STATUS = {"M": "modified", "A": "added", "D": "deleted", "T": "modified"}
DEFINITIONS = ("function", "class", "method", "type")
MAX_PATCH = 400_000


def _git(root: Path, *args: str) -> str | None:
    command = ["git", "-C", str(root), "-c", "core.quotepath=false", *args]
    try:
        result = subprocess.run(command, capture_output=True, timeout=120, **QUIET)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def _gits(root: Path, *asks: tuple[str, ...]) -> list[str | None]:
    """Several questions for git, asked side by side: it is slow to start, on Windows above
    all, and a build that asks them one after another spends most of its time waiting."""
    with ThreadPoolExecutor(len(asks)) as pool:
        return list(pool.map(lambda args: _git(root, *args), asks))


def is_repo(root: Path) -> bool:
    return (_git(root, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def commits(root: Path, limit: int = 5000) -> list[tuple[dict, list[str]]]:
    """The log, newest first: (hash, author, email, date and subject, paths under the folder)."""
    out = _git(root, "log", f"-n{limit}", "--no-merges", "--relative", "--name-only", "--date=short",
               "--format=%x01%h%x02%aN%x02%aE%x02%ad%x02%s")
    log: list[tuple[dict, list[str]]] = []
    for line in (out or "").splitlines():
        if line.startswith("\x01"):
            fields = (line[1:].split("\x02", 4) + [""] * 4)[:5]
            log.append((dict(zip(("hash", "author", "email", "date", "subject"), fields)), []))
        elif line.strip() and log:
            log[-1][1].append(line)
    return log


_read: dict = {}  # the last log read, and the folder and commit it was read at


def log(root: Path) -> list[tuple[dict, list[str]]]:
    """commits(), or nothing outside a repository, read again only once HEAD has moved.

    git is slow to start, so a program that builds the same folder again and again, as
    the server does after every edit, asks it one short question instead of two long ones.
    """
    head = (_git(root, "rev-parse", "--verify", "-q", "HEAD") or "").strip()
    if not head:  # no commits yet, or no repository at all
        return commits(root) if is_repo(root) else []
    if _read.get("at") != (str(root), head):
        _read.update(at=(str(root), head), log=commits(root))
    return _read["log"]


def history(log: list) -> dict[str, dict]:
    """Per file: number of commits, and the date, author and subject of the latest one."""
    files: dict[str, dict] = {}
    for commit, paths in log:
        for path in paths:  # the log runs newest first, so the first sighting is the latest
            entry = files.setdefault(path, {"commits": 0, "hash": commit["hash"], "author": commit["author"],
                                            "last": commit["date"], "subject": commit["subject"][:120]})
            entry["commits"] += 1
    return files


def cochanges(log: list, file_ids: dict[str, str], largest: int = 25) -> list[dict]:
    """Edges between files that keep being committed together.

    Two files are linked when they shared at least two commits and at least half the
    commits of the quieter one. Sweeping commits say nothing about coupling, so a
    commit that touches more than `largest` files is not counted as a pairing.
    """
    seen: Counter = Counter()
    together: Counter = Counter()
    for _, paths in log:
        present = sorted({path for path in paths if path in file_ids})
        seen.update(present)
        if 2 <= len(present) <= largest:
            together.update(combinations(present, 2))
    edges = []
    for (first, second), count in together.most_common():
        share = count / min(seen[first], seen[second])
        if count >= 2 and share >= 0.5:
            link = edge(file_ids[first], file_ids[second], "changes_with", "INFERRED",
                        round(min(0.9, 0.3 + 0.6 * share), 2))
            edges.append({**link, "together": count})
    return edges[:300]


def authors(log: list, file_ids: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """People as nodes, linked to the files they committed to.

    With a single author this would be one node tied to everything, which shows
    nothing. So it is only done when at least two people each made a tenth of the
    commits. One person committing under two names with one email counts once.
    """
    people: dict[str, dict] = defaultdict(lambda: {"names": Counter(), "files": Counter(), "commits": 0})
    for commit, paths in log:
        key = commit["email"].lower() or commit["author"]
        if not slug(commit["author"]):
            continue
        people[key]["names"][commit["author"]] += 1
        people[key]["commits"] += 1
        people[key]["files"].update(path for path in paths if path in file_ids)
    total = sum(person["commits"] for person in people.values())
    if sum(1 for person in people.values() if person["files"] and person["commits"] >= total / 10) < 2:
        return [], []
    nodes, edges = [], []
    for _, person in sorted(people.items()):
        if not person["files"]:
            continue
        name = person["names"].most_common(1)[0][0]
        pid, files = "person:" + slug(name), person["files"]
        nodes.append(node(pid, name, "person", note=f"{person['commits']} commits, {len(files)} files"))
        edges += [{**edge(pid, file_ids[path], "authored", source_file=path), "together": count}
                  for path, count in sorted(files.items())]
    return nodes, edges


def diff(root: Path, base: str = "HEAD", path: str | None = None
         ) -> tuple[dict[str, str], dict[str, list[tuple[int, int]]]]:
    """Files that differ from a commit, and for each the line ranges that changed.

    The comparison is against the working tree, so uncommitted edits count, and files
    git does not track yet are reported as untracked.
    """
    if not REF.match(base) or base.startswith("-"):
        raise ValueError(f"'{base}' is not a valid git reference")
    only = ["--", path] if path else []
    changes: dict[str, str] = {}
    hunks: dict[str, list[tuple[int, int]]] = {}
    known, names, patch, others = _gits(
        root, ("rev-parse", "--verify", "--quiet", base + "^{commit}"),
        ("diff", "--name-status", "--relative", "--no-renames", base, *only),
        ("diff", "-U0", "--relative", "--no-renames", "--no-color", "--no-ext-diff", base, *only),
        ("ls-files", "--others", "--exclude-standard", *only))
    if known is not None:
        for line in (names or "").splitlines():
            status, _, name = line.partition("\t")
            if name:
                changes[name] = STATUS.get(status[:1], "modified")
        current = None
        patch = patch or ""
        for line in patch.splitlines():
            if line.startswith("+++ "):
                current = line[6:].rstrip("\t") if line.startswith("+++ b/") else None
                continue
            match = HUNK.match(line) if current else None
            if match:
                start, count = int(match[1]), int(match[2]) if match[2] is not None else 1
                # Lines that were only removed leave no new lines; the spot where they were counts.
                hunks.setdefault(current, []).append((start, start + count - 1) if count else (start, start + 1))
    elif base != "HEAD":  # a repository without commits has no HEAD yet, which is not an error
        raise ValueError(f"git does not know '{base}'")
    for name in (others or "").splitlines():
        changes[name] = "untracked"
    return changes, hunks


def file_diff(root: Path, path: str, base: str = "HEAD") -> dict | None:
    """One file against a commit: its status, the lines that changed, and the patch."""
    if not is_repo(root):
        return None
    changes, hunks = diff(root, base, path)
    status = changes.get(path)
    patch = ""
    if status in ("modified", "added", "deleted"):
        patch = _git(root, "diff", "--relative", "--no-renames", "--no-color", "--no-ext-diff", base, "--", path) or ""
    return {"status": status, "base": base, "ranges": hunks.get(path, []), "patch": patch[:MAX_PATCH]}


def touched(nodes: list[dict], changes: dict[str, str], hunks: dict) -> dict[str, str]:
    """Which nodes a set of file changes reaches: node id -> status of its file."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for item in nodes:
        if item.get("source_file") in changes and family(item["kind"]) in ("code", "notes", "images"):
            by_file[item["source_file"]].append(item)
    result: dict[str, str] = {}
    for path, group in by_file.items():
        status, ranges = changes[path], hunks.get(path)
        definitions = sorted((n for n in group if n["kind"] in DEFINITIONS and n.get("line")),
                             key=lambda n: n["line"])
        for item in group:  # the file itself, and every definition when the lines are unknown
            if ranges is None or item not in definitions:
                result[item["id"]] = status
        for index, item in enumerate(definitions):
            if ranges is None:
                break
            # A definition runs until the next one that is not its own member.
            end = next((later["line"] - 1 for later in definitions[index + 1:]
                        if not later["id"].startswith(item["id"] + ".")), 10 ** 9)
            if any(first <= end and last >= item["line"] for first, last in ranges):
                result[item["id"]] = status
    return result


def snapshot(root: Path, nodes: list[dict], base: str = "HEAD") -> dict | None:
    """The state of the working tree against a commit, or None outside a repository."""
    with ThreadPoolExecutor(1) as pool:
        facts = pool.submit(_gits, root, ("rev-parse", "--is-inside-work-tree"), ("rev-parse", "--abbrev-ref", "HEAD"),
                            ("rev-parse", "--short", "HEAD"))
        try:
            changes, hunks = diff(root, base)
        finally:
            inside, branch, head = facts.result()
    if (inside or "").strip() != "true":
        return None
    changes = {path: status for path, status in changes.items() if not path.startswith(OUT_DIR + "/")}
    return {
        "branch": (branch or "").strip(),
        "head": (head or "").strip(),
        "base": base,
        "changed": changes,
        "nodes": touched(nodes, changes, hunks),
    }


def affected(changed: set[str], edges: list[dict]) -> dict[str, list[dict]]:
    """Nodes that use something that changed: node id -> the edges that lead to the change."""
    reached: dict[str, list[dict]] = defaultdict(list)
    for link in edges:
        if (link["target"] in changed and link["source"] not in changed
                and link["relation"] not in CONTAINMENT and link["relation"] != "changes_with"):
            reached[link["source"]].append(link)
    return reached
