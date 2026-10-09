"""Answer questions from graph.json without going back to the source files."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import networkx as nx

from .detect import OUT_DIR
from .util import read_json


class Graph:
    def __init__(self, root: Path):
        data = read_json(root / OUT_DIR / "graph.json")
        if data is None:
            raise FileNotFoundError(f"no graph in {root / OUT_DIR}; run 'linkednodes build' first")
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.edges = data["edges"]
        self.communities = {c["id"]: c["label"] for c in data["communities"]}
        self.git = data.get("git") or {}
        self.around: dict[str, list[tuple[dict, str, bool]]] = defaultdict(list)
        self.nx = nx.Graph()
        for e in self.edges:
            self.around[e["source"]].append((e, e["target"], True))
            self.around[e["target"]].append((e, e["source"], False))
            self.nx.add_edge(e["source"], e["target"])

    def find(self, text: str, limit: int = 5) -> list[dict]:
        """Rank nodes by how well their label, id or note matches the text."""
        needle = text.strip().lower()
        words = set(re.findall(r"\w+", needle))
        scored = []
        for n in self.nodes.values():
            label, nid = n["label"].lower(), n["id"].lower()
            if needle in (label, nid):
                score = 100
            elif label.startswith(needle):
                score = 60
            elif needle in label or needle in nid:
                score = 40
            else:
                haystack = set(re.findall(r"\w+", f"{label} {nid} {n.get('note', '')} "
                                                   f"{n.get('summary', '')}".lower()))
                score = 10 * len(words & haystack) if words else 0
            if score:
                scored.append((score, n.get("degree", 0), n))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]["id"]))
        return [n for _, _, n in scored[:limit]]

    def describe(self, n: dict, limit: int = 12) -> str:
        lines = [f"{n['label']}  [{n['kind']}]  {n['id']}"]
        if n.get("source_file"):
            lines.append(f"  source: {n['source_file']}" + (f":{n['line']}" if n.get("line") else ""))
        for key in ("url", "summary", "note"):
            if n.get(key):
                lines.append(f"  {key}: {n[key]}")
        past = self.git.get("files", {}).get(n.get("source_file"))
        if past:
            lines.append(f"  git: last changed {past['last']} by {past['author']}, {past['commits']} commits"
                         + (f"; {self.git['nodes'][n['id']]} and not committed when the graph was built"
                            if n["id"] in self.git.get("nodes", {}) else ""))
        if n.get("doc"):  # the note a person attached to this node
            lines.append("  notes:")
            lines += [f"    {line}" for line in n["doc"].splitlines()[:40]]
        if n.get("community") is not None:
            lines.append(f"  community: {self.communities.get(n['community'], n['community'])}")
        groups: dict[str, list[str]] = defaultdict(list)
        for e, other, outgoing in self.around[n["id"]]:
            arrow = f"--{e['relation']}-->" if outgoing else f"<--{e['relation']}--"
            mark = "" if e["confidence"] == "EXTRACTED" else f" ({e['confidence'].lower()})"
            groups[arrow].append(f"{self.nodes[other]['label']} [{self.nodes[other]['kind']}]{mark}")
        for arrow, found in sorted(groups.items()):
            more = f" ... and {len(found) - limit} more" if len(found) > limit else ""
            lines.append(f"  {arrow} " + "; ".join(found[:limit]) + more)
        return "\n".join(lines)

    def path(self, start: dict, end: dict) -> str:
        try:
            steps = nx.shortest_path(self.nx, start["id"], end["id"])
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return f"No path between \"{start['label']}\" and \"{end['label']}\"."
        lines = [f"{self.nodes[steps[0]]['label']} [{self.nodes[steps[0]]['kind']}]"]
        for a, b in zip(steps, steps[1:]):
            e, _, outgoing = next(item for item in self.around[a] if item[1] == b)
            arrow = f"--{e['relation']}-->" if outgoing else f"<--{e['relation']}--"
            lines.append(f"  {arrow} {self.nodes[b]['label']} [{self.nodes[b]['kind']}]"
                         f" ({e['confidence'].lower()})")
        return "\n".join(lines)
