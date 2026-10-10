"""Command line entry point: linkednodes scan | build | app | serve | query | changed | install."""
from __future__ import annotations

import argparse
import shutil
import sys
from importlib import resources
from pathlib import Path

from . import __version__, gitinfo, hooks, pipeline
from .detect import OUT_DIR
from .query import Graph


def _root(value: str) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise SystemExit(f"error: {root} is not a folder")
    return root


def _scan(args) -> int:
    root = _root(args.path)
    result = pipeline.scan(root)
    print(f"Scanned {root}")
    for kind in ("code", "doc", "image", "saved"):
        count = result["files"].get(kind, 0)
        extra = ""
        if kind == "code" and result["languages"]:
            extra = " (" + ", ".join(f"{lang} {n}" for lang, n in result["languages"].items()) + ")"
        if kind == "saved":
            extra = f" holding {result['saved_items']} saved items"
        print(f"  {kind:<6}{count:>6} files{extra}")
    print(f"Structural pass: {result['nodes']} nodes, {result['edges']} edges")
    for problem in result["problems"]:
        print(f"warning: {problem}")
    total = sum(result["chunks"].values())
    if total:
        parts = ", ".join(f"{n} {kind}" for kind, n in result["chunks"].items())
        print(f"Semantic work: {total} chunk{'s' if total > 1 else ''} ({parts}) queued in "
              f"{OUT_DIR}/.work/todo.json")
    else:
        print("Semantic work: nothing new to read; run 'linkednodes build'")
    return 0


def _build(args) -> int:
    result = pipeline.build(_root(args.path))
    counts = result["counts"]
    print(f"Graph: {counts['nodes']} nodes, {counts['edges']} edges, "
          f"{counts['communities']} communities")
    for community in result["communities"][:12]:
        mark = "" if community["named"] else "  (unnamed)"
        print(f"  {community['id']:>3}. {community['label']}  [{community['size']} nodes, "
              f"anchor {community['anchor']}]{mark}")
    for problem in result["problems"]:
        print(f"warning: {problem}")
    missing = result["missing"]
    if missing["files"] or missing["items"]:
        print(f"warning: {len(missing['files'])} files and {missing['items']} saved items have "
              f"no semantic extraction yet, so the graph is partial")
        for path in missing["files"][:10]:
            print(f"  missing: {path}")
    for node_id in result["orphan_notes"]:
        print(f"warning: a note in {OUT_DIR}/notes belongs to '{node_id}', which is no longer "
              f"in the graph")
    print(f"Wrote graph.html, graph.json and GRAPH_REPORT.md to {result['out']}")
    return 0


def _pick(graph: Graph, text: str) -> dict:
    found = graph.find(text, 1)
    if not found:
        raise SystemExit(f"error: nothing in the graph matches '{text}'")
    return found[0]


def _query(args) -> int:
    graph = Graph(_root(args.root))
    found = graph.find(args.text, args.limit)
    if not found:
        print(f"Nothing in the graph matches '{args.text}'.")
        return 1
    print("\n\n".join(graph.describe(n) for n in found))
    return 0


def _path(args) -> int:
    graph = Graph(_root(args.root))
    print(graph.path(_pick(graph, args.start), _pick(graph, args.end)))
    return 0


def _node(args) -> int:
    graph = Graph(_root(args.root))
    print(graph.describe(_pick(graph, args.text), limit=200))
    return 0


def _changed(args) -> int:
    root = _root(args.root)
    graph = Graph(root)
    try:
        state = gitinfo.snapshot(root, list(graph.nodes.values()), args.base)
    except ValueError as error:
        raise SystemExit(f"error: {error}")
    if state is None:
        raise SystemExit(f"error: {root} is not inside a git repository")
    print(f"Branch {state['branch'] or '(none)'} at {state['head'] or '(no commits)'}, compared with "
          f"{state['base']}: {len(state['changed'])} files differ, {len(state['nodes'])} nodes touched")
    by_file: dict[str, list[dict]] = {}
    for node_id in state["nodes"]:
        node = graph.nodes[node_id]
        by_file.setdefault(node["source_file"], []).append(node)
    for path, status in sorted(state["changed"].items()):
        print(f"{status:<10} {path}" + ("" if path in by_file else "  (not in the graph)"))
        for node in sorted(by_file.get(path, []), key=lambda n: n.get("line", 0)):
            if node["kind"] not in ("file", "doc", "image"):
                print(f"             {node['label']} [{node['kind']}] line {node.get('line', '?')}")
    reached = gitinfo.affected(set(state["nodes"]), graph.edges)
    if reached:
        print(f"\nUses something that changed ({len(reached)} nodes):")
    for node_id, found in sorted(reached.items(), key=lambda item: -len(item[1]))[:args.limit]:
        node = graph.nodes[node_id]
        targets = ", ".join(f"{e['relation']} {graph.nodes[e['target']]['label']}" for e in found[:4])
        more = f", and {len(found) - 4} more" if len(found) > 4 else ""
        print(f"  {node['label']} [{node['kind']}] {node.get('source_file', '')}: {targets}{more}")
    return 0


def _refresh(args) -> int:
    root = _root(args.path)
    scanned = pipeline.scan(root)
    counts = pipeline.build(root)["counts"]
    waiting = sum(scanned["chunks"].values())
    print(f"Graph: {counts['nodes']} nodes, {counts['edges']} edges"
          + (f"; {waiting} chunks of new notes or images are waiting to be read" if waiting else ""))
    return 0


def _hook(args) -> int:
    root = _root(args.path)
    try:
        if args.action == "install":
            for path in hooks.install(root):
                print(f"Installed {path}")
            print("The graph is rebuilt in the background after every commit and merge.")
        else:
            removed = hooks.remove(root)
            for path in removed:
                print(f"Removed {path}")
            if not removed:
                print("No LinkedNodes hooks were installed here.")
    except FileExistsError as error:
        raise SystemExit(f"error: {error}")
    return 0


def _serve(args) -> int:
    from .server import serve
    serve(_root(args.path), args.port, not args.no_open)
    return 0


def _app(args) -> int:
    from . import desktop
    windowed = sys.stdout is None  # started with pythonw, so there is no console to print to
    try:
        desktop.run(_root(args.path) if args.path else None)
    except (SystemExit, Exception) as error:
        if not windowed:
            raise
        desktop.report(error)
        return 1
    return 0


def _install(args) -> int:
    base = Path.cwd() / ".claude" if args.project else Path.home() / ".claude"
    target = base / "skills" / "linkednodes" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(resources.files("linkednodes").joinpath("skill/SKILL.md")) as source:
        shutil.copyfile(source, target)
    print(f"Installed the skill at {target}")
    print("Start a new Claude Code session and run /linkednodes <folder>.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy codepage
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="linkednodes", description=__doc__)
    parser.add_argument("--version", action="version", version=f"linkednodes {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="detect files and extract everything structural")
    scan.add_argument("path", nargs="?", default=".")
    scan.set_defaults(run=_scan)

    build = commands.add_parser("build", help="assemble the graph and write the outputs")
    build.add_argument("path", nargs="?", default=".")
    build.set_defaults(run=_build)

    query = commands.add_parser("query", help="find nodes and show what they connect to")
    query.add_argument("text")
    query.add_argument("--root", default=".")
    query.add_argument("--limit", type=int, default=5)
    query.set_defaults(run=_query)

    path = commands.add_parser("path", help="shortest path between two nodes")
    path.add_argument("start")
    path.add_argument("end")
    path.add_argument("--root", default=".")
    path.set_defaults(run=_path)

    one = commands.add_parser("node", help="everything known about one node")
    one.add_argument("text")
    one.add_argument("--root", default=".")
    one.set_defaults(run=_node)

    changed = commands.add_parser("changed", help="what differs from a commit, and what uses it")
    changed.add_argument("--base", default="HEAD", help="commit, branch or tag to compare with")
    changed.add_argument("--root", default=".")
    changed.add_argument("--limit", type=int, default=40)
    changed.set_defaults(run=_changed)

    refresh = commands.add_parser("refresh", help="scan and build in one step, without the reading")
    refresh.add_argument("path", nargs="?", default=".")
    refresh.set_defaults(run=_refresh)

    hook = commands.add_parser("hook", help="rebuild the graph automatically after commits and merges")
    hook.add_argument("action", choices=["install", "remove"])
    hook.add_argument("path", nargs="?", default=".")
    hook.set_defaults(run=_hook)

    serve = commands.add_parser("serve", help="open the graph with editing: notes, nodes, source")
    serve.add_argument("path", nargs="?", default=".")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="do not open the browser")
    serve.set_defaults(run=_serve)

    app = commands.add_parser("app", help="open the graph in a desktop window (needs pywebview)")
    app.add_argument("path", nargs="?", help="folder to open; without it, the last one used")
    app.set_defaults(run=_app)

    install = commands.add_parser("install", help="install the Claude Code skill")
    install.add_argument("--project", action="store_true",
                         help="install into ./.claude instead of your home folder")
    install.set_defaults(run=_install)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except FileNotFoundError as error:
        raise SystemExit(f"error: {error}")
