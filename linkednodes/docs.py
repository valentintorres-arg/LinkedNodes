"""Structural pass over notes and images: the links an author wrote down explicitly.

What a note means is left to the semantic pass. This pass only records what can be
read off the page: links to other files, wikilinks, URLs that match a saved item,
and code names quoted in backticks.
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote

from .code import is_specific
from .detect import TEXT_DOC_EXT
from .saved import normalize_url
from .util import edge, node, read_text

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK = re.compile(r"(!?)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
URL = re.compile(r"https?://[^\s<>\"')\]]+")
CODESPAN = re.compile(r"`([^`\n]{3,80})`")
HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def node_id(record: dict) -> str:
    prefix = {"code": "code:", "doc": "doc:", "image": "img:"}[record["kind"]]
    return prefix + record["path"]


def extract(root: Path, files: list[dict], known: list[dict]) -> tuple[list[dict], list[dict]]:
    """known holds the code and saved nodes already extracted, used as link targets."""
    paths = {f["path"]: node_id(f) for f in files if f["kind"] != "saved"}
    names: dict[str, list[str]] = defaultdict(list)  # file name or stem -> node ids
    for rel, nid in paths.items():
        base = posixpath.basename(rel).lower()
        names[base].append(nid)
        names[posixpath.splitext(base)[0]].append(nid)
    urls = {normalize_url(n["url"]): n["id"] for n in known if n["kind"] == "saved"}
    symbols: dict[str, list[str]] = defaultdict(list)
    for n in known:
        if n["kind"] in ("function", "class", "method", "type"):
            symbols[n["label"].rsplit(".", 1)[-1]].append(n["id"])

    nodes, edges, seen = [], [], set()

    def link(source, target, relation, rel, confidence="EXTRACTED", score=None):
        if target and source != target and (source, target, relation) not in seen:
            seen.add((source, target, relation))
            edges.append(edge(source, target, relation, confidence, score, rel))

    for record in files:
        rel, kind = record["path"], record["kind"]
        if kind == "image":
            nodes.append(node(paths[rel], posixpath.basename(rel), "image", rel))
            continue
        if kind != "doc":
            continue
        me = paths[rel]
        title = posixpath.splitext(posixpath.basename(rel))[0]
        if posixpath.splitext(rel)[1].lower() not in TEXT_DOC_EXT:
            nodes.append(node(me, title, "doc", rel))  # PDFs have no structure to read here
            continue
        try:
            text = read_text(root / rel)
        except OSError:
            text = ""
        heading = HEADING.search(text)
        nodes.append(node(me, heading.group(1) if heading else title, "doc", rel))

        for match in WIKILINK.finditer(text):
            found = names.get(match.group(1).strip().lower(), [])
            if len(found) == 1:
                link(me, found[0], "links_to", rel)
        for match in MDLINK.finditer(text):
            target = unquote(match.group(2).split("#")[0].split("?")[0])
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            link(me, paths.get(resolved), "embeds" if match.group(1) else "links_to", rel)
        for match in URL.finditer(text):
            link(me, urls.get(normalize_url(match.group(0).rstrip(".,;:"))), "references", rel)
        for match in CODESPAN.finditer(text):
            span = match.group(1).strip().removesuffix("()")
            by_file = names.get(span.lower(), []) if "." in span else []  # `cli.py`, not `cli`
            if len(by_file) == 1:
                link(me, by_file[0], "mentions", rel, "INFERRED", 0.8)
                continue
            name = re.split(r"[.:]+", span)[-1]
            # `target` alone is a word; `total_price`, `target()` and `Graph.target` are code.
            if not (is_specific(name) or name != match.group(1).strip()):
                continue
            if len(name) >= 4 and len(symbols.get(name, [])) == 1:
                link(me, symbols[name][0], "mentions", rel, "INFERRED", 0.7)
    return nodes, edges
