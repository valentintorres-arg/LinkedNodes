"""Merge extracted nodes and edges into one graph, cluster it and find what stands out."""
from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx

RANK = {"EXTRACTED": 2, "INFERRED": 1, "AMBIGUOUS": 0}
FAMILY = {"file": "code", "function": "code", "class": "code", "method": "code", "type": "code",
          "package": "code", "doc": "notes", "image": "images", "saved": "saved",
          "folder": "saved"}
# Edges that only say where something lives or who committed it; they make containers
# and prolific authors look important.
CONTAINMENT = {"contains", "in_folder", "part_of", "authored"}
LIBRARY_WEIGHT = 0.15


def family(kind: str) -> str:
    return FAMILY.get(kind, "concepts")


def merge(node_lists: list[list[dict]], edge_lists: list[list[dict]]) -> tuple[dict, list[dict]]:
    nodes: dict[str, dict] = {}
    for group in node_lists:
        for n in group:
            found = nodes.setdefault(n["id"], dict(n))
            if found is not n:
                if found["kind"] == "tag" and n["kind"] != "tag":
                    found["kind"] = n["kind"]  # a tag that a note also discusses is a concept
                for key, value in n.items():
                    found.setdefault(key, value)
    edges: dict[tuple, dict] = {}
    for group in edge_lists:
        for e in group:
            if e["source"] == e["target"] or e["source"] not in nodes or e["target"] not in nodes:
                continue
            key = (e["source"], e["target"], e["relation"])
            old = edges.get(key)
            if not old or (RANK[e["confidence"]], e["score"]) > (RANK[old["confidence"]], old["score"]):
                edges[key] = e
    return nodes, sorted(edges.values(), key=lambda e: (e["source"], e["target"], e["relation"]))


def to_networkx(nodes: dict, edges: list[dict]) -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(sorted(nodes))
    for e in edges:
        score = e["score"]
        if "package" in (nodes[e["source"]]["kind"], nodes[e["target"]]["kind"]) or e["relation"] == "authored":
            # Half the project imports react, and one person may have written all of it;
            # neither says much about what belongs together.
            score *= LIBRARY_WEIGHT
        weight = graph.get_edge_data(e["source"], e["target"], {}).get("weight", 0)
        graph.add_edge(e["source"], e["target"], weight=max(weight, score))
    return graph


def cluster(graph: nx.Graph, nodes: dict, names: dict[str, str]) -> list[dict]:
    """Group nodes into communities. names maps a community's anchor node to a chosen label."""
    if graph.number_of_edges():
        groups = nx.community.louvain_communities(graph, weight="weight", seed=42)
    else:
        groups = []
    groups = sorted((g for g in groups if len(g) > 1), key=lambda g: (-len(g), min(g)))
    communities = []
    for index, members in enumerate(groups):
        ranked = sorted(members, key=lambda n: (-graph.degree(n), n))
        inner = graph.subgraph(members).number_of_edges()
        possible = len(members) * (len(members) - 1) / 2
        for member in members:
            nodes[member]["community"] = index
        communities.append({
            "id": index,
            "anchor": ranked[0],
            "label": names.get(ranked[0]) or " / ".join(nodes[n]["label"][:40] for n in ranked[:3]),
            "named": ranked[0] in names,
            "size": len(members),
            "cohesion": round(inner / possible, 3),
            "top": ranked[:8],
            "families": dict(Counter(family(nodes[n]["kind"]) for n in members).most_common()),
        })
    return communities


def analyze(graph: nx.Graph, nodes: dict, edges: list[dict], communities: list[dict]) -> dict:
    meaningful = Counter()
    for e in edges:
        if e["relation"] not in CONTAINMENT:
            meaningful[e["source"]] += 1
            meaningful[e["target"]] += 1
    for nid, n in nodes.items():
        n["degree"] = graph.degree(nid)

    def brief(nid: str, **extra) -> dict:
        n = nodes[nid]
        return {"id": nid, "label": n["label"], "kind": n["kind"], **extra}

    ranked = sorted(meaningful.items(), key=lambda kv: (-kv[1], kv[0]))
    gods = [brief(nid, links=count) for nid, count in ranked
            if nodes[nid]["kind"] != "package"][:10]
    packages = [brief(nid, links=count) for nid, count in ranked
                if nodes[nid]["kind"] == "package"][:8]

    # Connectors sit on many shortest paths: remove one and parts of the graph drift apart.
    # The paths are counted from a sample of nodes, a smaller one in a large graph, where
    # each costs more and this would otherwise be most of the time a build takes.
    size = len(graph) + graph.number_of_edges()
    sample = min(len(graph), 300, max(50, 500_000 // (size or 1))) or None
    between = nx.betweenness_centrality(graph, k=sample, seed=42) if graph.number_of_edges() else {}
    hubs = {g["id"] for g in gods[:5]}
    connectors = [brief(nid, score=round(score, 3)) for nid, score in
                  sorted(between.items(), key=lambda kv: (-kv[1], kv[0]))
                  if score > 0 and nid not in hubs][:5]

    # A bridge is surprising when it is one of few links between two communities,
    # and more so when it joins different kinds of things.
    crossing = Counter()
    for e in edges:
        a, b = nodes[e["source"]].get("community"), nodes[e["target"]].get("community")
        if a is not None and b is not None and a != b:
            crossing[frozenset((a, b))] += 1
    bridges = []
    for e in edges:
        source, target = nodes[e["source"]], nodes[e["target"]]
        a, b = source.get("community"), target.get("community")
        if a is None or b is None or a == b or e["relation"] in CONTAINMENT:
            continue
        mixed = family(source["kind"]) != family(target["kind"])
        surprise = 1 / crossing[frozenset((a, b))] + (0.5 if mixed else 0)
        bridges.append((surprise, e, a, b))
    bridges.sort(key=lambda item: (-item[0], item[1]["source"], item[1]["target"]))
    seen_pairs, top_bridges = set(), []
    for surprise, e, a, b in bridges:
        if frozenset((a, b)) in seen_pairs:
            continue
        seen_pairs.add(frozenset((a, b)))
        top_bridges.append({
            "source": brief(e["source"]), "target": brief(e["target"]),
            "relation": e["relation"], "confidence": e["confidence"],
            "communities": [communities[a]["label"], communities[b]["label"]],
        })
        if len(top_bridges) == 8:
            break

    saved_links = defaultdict(set)
    for e in edges:
        for item, other in ((e["source"], e["target"]), (e["target"], e["source"])):
            if nodes[item]["kind"] == "saved" and family(nodes[other]["kind"]) == "concepts":
                saved_links[other].add(item)
    interests = [brief(nid, saved=len(found)) for nid, found in
                 sorted(saved_links.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:12]]
    domains = Counter(n["domain"] for n in nodes.values() if n.get("domain")).most_common(10)

    # Files that are committed together. The pairs with no other link between them are
    # the interesting ones: a coupling that the code does not show.
    linked = {frozenset((e["source"], e["target"])) for e in edges if e["relation"] != "changes_with"}
    coupled = [{"source": brief(e["source"]), "target": brief(e["target"]), "together": e.get("together", 0),
                "hidden": frozenset((e["source"], e["target"])) not in linked}
               for e in sorted((e for e in edges if e["relation"] == "changes_with"),
                               key=lambda e: (-e.get("together", 0), e["source"], e["target"]))][:12]

    ambiguous = [{"source": brief(e["source"]), "target": brief(e["target"]),
                  "relation": e["relation"], "source_file": e["source_file"]}
                 for e in edges if e["confidence"] == "AMBIGUOUS"][:15]

    questions = []
    for bridge in top_bridges[:2]:
        questions.append(f"Why does \"{bridge['source']['label']}\" connect to "
                         f"\"{bridge['target']['label']}\"?")
    for god in gods[:2]:
        if family(god["kind"]) == "code":
            questions.append(f"What depends on \"{god['label']}\", and what breaks if it changes?")
        elif family(god["kind"]) == "concepts":
            questions.append(f"What do my notes and saved links say about \"{god['label']}\"?")
        else:
            questions.append(f"What does \"{god['label']}\" cover, and what does it leave out?")
    if len(communities) >= 2:
        questions.append(f"How do \"{communities[0]['label']}\" and "
                         f"\"{communities[1]['label']}\" relate?")
    if interests:
        questions.append(f"What have I saved about \"{interests[0]['label']}\", and what is missing?")

    return {
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "communities": len(communities),
            "isolated": sum(1 for n in nodes.values() if n["degree"] == 0),
            "by_family": dict(Counter(family(n["kind"]) for n in nodes.values()).most_common()),
            "by_confidence": dict(Counter(e["confidence"] for e in edges).most_common()),
        },
        "gods": gods,
        "packages": packages,
        "connectors": connectors,
        "bridges": top_bridges,
        "interests": interests,
        "domains": domains,
        "coupled": coupled,
        "ambiguous": ambiguous,
        "questions": questions,
    }
