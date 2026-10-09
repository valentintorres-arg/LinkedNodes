"""Write GRAPH_REPORT.md, the one-page summary a reader or an assistant starts from."""
from __future__ import annotations


def _name(item: dict) -> str:
    return f"**{item['label']}** ({item['kind']})"


def render(title: str, nodes: dict, communities: list[dict], analysis: dict,
           missing: dict) -> str:
    counts = analysis["counts"]
    lines = [f"# Graph report: {title}", ""]
    lines.append(f"{counts['nodes']} nodes, {counts['edges']} edges, "
                 f"{counts['communities']} communities, {counts['isolated']} unconnected nodes.")
    lines.append("")
    lines.append("- Nodes: " + ", ".join(f"{n} {k}" for k, n in counts["by_family"].items()))
    lines.append("- Edges: " + (", ".join(f"{n} {k.lower()}" for k, n in counts["by_confidence"].items())
                                or "none"))
    if missing["files"] or missing["items"]:
        lines.append(f"- Not yet read for meaning: {len(missing['files'])} files, "
                     f"{missing['items']} saved items. The graph is partial until they are.")
    lines.append("")

    lines += ["## Most connected", ""]
    for god in analysis["gods"]:
        lines.append(f"- {_name(god)}: {god['links']} links")
    if analysis["packages"]:
        lines += ["", "## Most used packages", ""]
        lines += [f"- **{p['label']}**: imported by {p['links']} files" for p in analysis["packages"]]
    if analysis["connectors"]:
        lines += ["", "## Connectors", "",
                  "Nodes that hold separate parts of the graph together.", ""]
        lines += [f"- {_name(c)}" for c in analysis["connectors"]]

    if analysis["bridges"]:
        lines += ["", "## Surprising connections", ""]
        for bridge in analysis["bridges"]:
            lines.append(f"- {_name(bridge['source'])} --{bridge['relation']}--> "
                         f"{_name(bridge['target'])} [{bridge['confidence']}]: joins "
                         f"\"{bridge['communities'][0]}\" and \"{bridge['communities'][1]}\"")

    if analysis["interests"]:
        lines += ["", "## What you keep saving", ""]
        lines += [f"- **{i['label']}**: {i['saved']} saved items" for i in analysis["interests"]]
        if analysis["domains"]:
            lines += ["", "Most saved sites: "
                      + ", ".join(f"{site} ({n})" for site, n in analysis["domains"])]

    if analysis["coupled"]:
        lines += ["", "## Change together", "",
                  "Files that git shows being committed together, most often first.", ""]
        for pair in analysis["coupled"]:
            lines.append(f"- **{pair['source']['label']}** and **{pair['target']['label']}**: "
                         f"{pair['together']} commits"
                         + (", with no other link between them" if pair["hidden"] else ""))

    lines += ["", "## Communities", ""]
    for community in communities:
        mix = ", ".join(f"{n} {k}" for k, n in community["families"].items())
        lines.append(f"### {community['id']}. {community['label']}")
        lines.append("")
        lines.append(f"{community['size']} nodes ({mix}), cohesion {community['cohesion']}, "
                     f"anchor `{community['anchor']}`")
        lines.append("")
        lines.append("Key nodes: " + ", ".join(nodes[n]["label"] for n in community["top"]))
        lines.append("")

    if analysis["ambiguous"]:
        lines += ["## Uncertain edges to review", ""]
        for item in analysis["ambiguous"]:
            lines.append(f"- {_name(item['source'])} --{item['relation']}--> {_name(item['target'])}"
                         f" (from {item['source_file'] or 'topic links'})")
        lines.append("")

    if analysis["questions"]:
        lines += ["## Questions this graph can answer", ""]
        lines += [f"- {q}" for q in analysis["questions"]]
        lines.append("")
    return "\n".join(lines)
