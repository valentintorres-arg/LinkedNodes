"""What people add to the graph by hand: a note per node and corrections to a node.

Notes are Markdown files in linkednodes-out/notes, one per node, so they can be read,
edited and committed like any other documentation. Corrections to a node's label,
kind or description are kept in linkednodes-out/nodes.json. Both are applied on
every build, so they survive the graph being regenerated.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .util import read_json, read_text, slug, write_json

NOTES_DIR = "notes"
PROPS_FILE = "nodes.json"
MARK = re.compile(r"\A<!-- linkednodes: (.+?) -->\r?\n?")
LIMITS = {"label": 120, "kind": 40, "summary": 2000}
# The output folder is generated, except for what a person wrote in it.
GITIGNORE = "*\n!.gitignore\n!notes/\n!notes/*.md\n!nodes.json\n!labels.json\n"


def note_path(out: Path, node_id: str) -> Path:
    name = slug(node_id.split(":", 1)[-1])[-60:].strip("-") or "node"
    digest = hashlib.sha1(node_id.encode()).hexdigest()[:8]
    return out / NOTES_DIR / f"{name}-{digest}.md"


def read_notes(out: Path) -> dict[str, str]:
    notes = {}
    for path in sorted((out / NOTES_DIR).glob("*.md")):
        text = read_text(path)
        match = MARK.match(text)
        if match and text[match.end():].strip():
            notes[match.group(1)] = text[match.end():].strip("\r\n")
    return notes


def write_note(out: Path, node_id: str, text: str) -> None:
    """Save a node's note; an empty note removes the file."""
    path = note_path(out, node_id)
    if not text.strip():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\r\n", "\n").rstrip() + "\n"
    path.write_text(f"<!-- linkednodes: {node_id} -->\n{body}", encoding="utf-8", newline="\n")


def read_props(out: Path) -> dict[str, dict]:
    data = read_json(out / PROPS_FILE, {})
    return data if isinstance(data, dict) else {}


def write_props(out: Path, node_id: str, props: dict) -> None:
    """Save corrections to a node; fields left empty go back to what was extracted."""
    clean = {}
    for key, limit in LIMITS.items():
        value = str(props.get(key) or "").strip()[:limit]
        if key == "kind":
            value = slug(value)
        if value:
            clean[key] = value
    data = read_props(out)
    if clean:
        data[node_id] = clean
    else:
        data.pop(node_id, None)
    write_json(out / PROPS_FILE, data)


def apply(nodes: dict[str, dict], out: Path) -> list[str]:
    """Attach notes and corrections to the nodes. Returns notes whose node is gone."""
    orphans = []
    for node_id, text in read_notes(out).items():
        if node_id in nodes:
            nodes[node_id]["doc"] = text
        else:
            orphans.append(node_id)
    for node_id, props in read_props(out).items():
        if node_id in nodes and isinstance(props, dict):
            nodes[node_id].update({k: v for k, v in props.items() if k in LIMITS})
            nodes[node_id]["edited"] = sorted(k for k in props if k in LIMITS)
    return orphans
