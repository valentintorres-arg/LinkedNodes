"""The two steps of a run: scan (everything that needs no model) and build (the graph)."""
from __future__ import annotations

import json
import os
from collections import Counter
from importlib import resources
from pathlib import Path

from . import annotations, code, detect, docs, gitinfo, graph, report, saved, semantic
from .util import read_json, write_json

VIEWER_LIMIT = 4000  # nodes the browser view can lay out comfortably

# Written next to graph.html on Windows, so the editable view is one double click away.
# It is saved with a byte order mark: Windows PowerShell reads a script without one as ANSI.
LAUNCHER = "open-graph.ps1"
LAUNCHER_SCRIPT = r"""# Opens this graph with editing: notes, node properties and the code behind each node.
# It opens in a window of its own when pywebview is installed, and in the browser otherwise.
# Run it with right click > "Run with PowerShell", or from a terminal:
#   powershell -ExecutionPolicy Bypass -File open-graph.ps1
param([int]$Port = 8765, [switch]$Browser)

$folder = Split-Path -Parent $PSScriptRoot
$spanish = (Get-Culture).Name -like 'es*'
function Say($english, $castellano) { if ($spanish) { Write-Host $castellano } else { Write-Host $english } }
function Leave($code) {
    if ($code -ne 0) { if ($spanish) { Read-Host 'Enter para cerrar' } else { Read-Host 'Press Enter to close' } }
    exit $code
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) {
    Say 'Python was not found. Install it from python.org and try again.' 'No se encontró Python. Instalalo desde python.org y probá de nuevo.'
    Leave 1
}
& $python.Source -m linkednodes --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say 'LinkedNodes is not installed for this Python. In the LinkedNodes folder, run: pip install -e .' 'LinkedNodes no está instalado para este Python. En la carpeta de LinkedNodes, ejecutá: pip install -e .'
    Leave 1
}

& $python.Source -c "import webview" 2>$null
if ($LASTEXITCODE -eq 0 -and -not $Browser) {
    # pythonw runs without a console, so this window can close and only the graph's stays.
    $windowed = Join-Path (Split-Path (& $python.Source -c "import sys; print(sys.executable)")) 'pythonw.exe'
    if (-not (Test-Path $windowed)) { $windowed = $python.Source }
    Start-Process $windowed -ArgumentList @('-m', 'linkednodes', 'app', ('"{0}"' -f $folder))
    exit 0
}

Say "Opening the graph of $folder" "Abriendo el grafo de $folder"
Say 'The browser opens by itself. Keep this window open while you edit; close it or press Ctrl+C to stop.' 'El navegador se abre solo. Dejá esta ventana abierta mientras editás; cerrala o pulsá Ctrl+C para terminar.'
& $python.Source -m linkednodes serve $folder --port $Port
Leave $LASTEXITCODE
"""


def _unwatched(step: str, **facts) -> None:
    """Where progress goes when nobody asked to follow it."""


def scan(root: Path, progress=_unwatched) -> dict:
    """Detect files, extract everything structural and queue the semantic work.

    progress is told which step is under way and what it has found so far: 'files',
    then 'code', then 'notes'. The desktop window shows it while a folder is read.
    """
    out = root / detect.OUT_DIR
    progress("files")
    files, items = detect.scan(root, lambda count: progress("files", files=count))
    kinds = dict(Counter(f["kind"] for f in files))
    languages = dict(Counter(f["lang"] for f in files if f["lang"]).most_common())
    progress("files", files=len(files), kinds=kinds, languages=languages, saved=len(items))
    # Keep the generated output out of the project's own repository, but not the notes.
    ignore = out / ".gitignore"
    if not ignore.exists() or ignore.read_text(encoding="utf-8") == "*\n":
        out.mkdir(parents=True, exist_ok=True)
        ignore.write_text(annotations.GITIGNORE, encoding="utf-8")
    problems = semantic.ingest(out)  # keep results from an interrupted run

    progress("code", parsed=0, total=kinds.get("code", 0))
    code_nodes, code_edges = code.extract(root, files, lambda read, found: progress("code", parsed=read, nodes=found))
    progress("code", parsed=kinds.get("code", 0), nodes=len(code_nodes), edges=len(code_edges))
    progress("notes")
    saved_nodes, saved_edges = saved.to_graph(items)
    doc_nodes, doc_edges = docs.extract(root, files, code_nodes + saved_nodes)
    progress("notes", nodes=len(code_nodes) + len(saved_nodes) + len(doc_nodes),
             edges=len(code_edges) + len(saved_edges) + len(doc_edges))
    write_json(out / ".work" / "manifest.json",
               {"files": files, "items": [item.to_dict() for item in items]})
    write_json(out / ".work" / "structural.json",
               {"nodes": code_nodes + saved_nodes + doc_nodes,
                "edges": code_edges + saved_edges + doc_edges})
    todo = semantic.plan(out, files, items)
    return {
        "files": kinds,
        "languages": languages,
        "saved_items": len(items),
        "nodes": len(code_nodes) + len(saved_nodes) + len(doc_nodes),
        "edges": len(code_edges) + len(saved_edges) + len(doc_edges),
        "chunks": dict(Counter(c["type"] for c in todo["chunks"])),
        "problems": problems,
    }


def render(root: Path, edit: dict | None = None) -> str:
    """The viewer page for the graph on disk. edit carries what a live server allows."""
    data = read_json(root / detect.OUT_DIR / "graph.json")
    if data is None:
        raise FileNotFoundError(f"no graph in {root / detect.OUT_DIR}; run 'linkednodes build' first")
    return _viewer(root.name, data, edit)


def _viewer(title: str, data: dict, edit: dict | None = None) -> str:
    nodes = [{**n, "family": graph.family(n["kind"])}
             for n in sorted(data["nodes"], key=lambda n: -n["degree"])[:VIEWER_LIMIT]]
    kept = {n["id"] for n in nodes}
    payload = {
        "title": title,
        "edit": edit,
        "git": data.get("git"),
        "total": len(data["nodes"]),
        "nodes": nodes,
        "edges": [e for e in data["edges"] if e["source"] in kept and e["target"] in kept],
        "communities": [{k: c[k] for k in ("id", "label", "size")} for c in data["communities"]],
    }
    # Escaping "<" keeps file contents from closing the script tag or opening a new one.
    blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    template = resources.files("linkednodes").joinpath("viewer.html").read_text(encoding="utf-8")
    return template.replace("__TITLE__", title.replace("<", "")).replace("__DATA__", blob)


def renote(root: Path, node_id: str) -> str:
    """Bring one node's note up to date in the graph on disk, and return the note.

    A note changes nothing else about the graph, so nothing else is worked out again:
    saving one is a matter of a moment, however large the graph.
    """
    out = root / detect.OUT_DIR
    data = read_json(out / "graph.json")
    if data is None:
        raise FileNotFoundError(f"no graph in {out}; run 'linkednodes build' first")
    text = annotations.read_notes(out).get(node_id, "")
    for node in data["nodes"]:
        if node["id"] == node_id:
            node.pop("doc", None)
            if text:
                node["doc"] = text
    write_json(out / "graph.json", data)
    (out / "graph.html").write_text(_viewer(root.name, data), encoding="utf-8")
    return text


def build(root: Path, progress=_unwatched) -> dict:
    """Assemble the graph from the structural pass and every cached semantic result.

    progress follows on from scan's: 'history', 'groups', 'writing', and 'done' at the end.
    """
    out = root / detect.OUT_DIR
    work = out / ".work"
    if not (work / "manifest.json").exists():
        scan(root, progress)
    manifest = read_json(work / "manifest.json")
    structural = read_json(work / "structural.json")
    files = manifest["files"]
    items = [saved.Item(**{k: v for k, v in item.items() if k != "id"})
             for item in manifest["items"]]
    problems = semantic.ingest(out)

    file_ids = {f["path"]: docs.node_id(f) for f in files if f["kind"] != "saved"}
    meaning = semantic.load(out, files, items, file_ids, {n["id"] for n in structural["nodes"]})
    # From the history: files that are committed together, and who wrote what.
    progress("history")
    log = gitinfo.commits(root) if gitinfo.is_repo(root) else []
    people, wrote = gitinfo.authors(log, file_ids)
    if log:
        progress("history", commits=len(log), authors=len({entry["email"] or entry["author"] for entry, _ in log}),
                 since=log[-1][0]["date"])
    nodes, edges = graph.merge([structural["nodes"], meaning["nodes"], people],
                               [structural["edges"], meaning["edges"], gitinfo.cochanges(log, file_ids), wrote])
    progress("groups", nodes=len(nodes), edges=len(edges))
    for nid, summary in meaning["summaries"].items():
        nodes[nid]["summary"] = summary
    orphan_notes = annotations.apply(nodes, out)

    names = read_json(out / "labels.json", {})
    nxg = graph.to_networkx(nodes, edges)
    communities = graph.cluster(nxg, nodes, names if isinstance(names, dict) else {})
    progress("groups", communities=len(communities))
    analysis = graph.analyze(nxg, nodes, edges, communities)
    progress("writing")
    missing = {"files": meaning["missing_files"], "items": meaning["missing_items"]}

    data = {"root": root.name, "nodes": list(nodes.values()), "edges": edges,
            "communities": communities, "analysis": analysis}
    git = gitinfo.snapshot(root, data["nodes"])
    if git:  # history for the files that are in the graph, and what differs from the last commit
        sources = {n.get("source_file") for n in data["nodes"]}
        git["files"] = {path: entry for path, entry in gitinfo.history(log).items() if path in sources}
        data["git"] = git
    write_json(out / "graph.json", data)
    (out / "GRAPH_REPORT.md").write_text(
        report.render(root.name, nodes, communities, analysis, missing), encoding="utf-8")
    (out / "graph.html").write_text(_viewer(root.name, data), encoding="utf-8")
    if os.name == "nt":
        (out / LAUNCHER).write_text(LAUNCHER_SCRIPT, encoding="utf-8-sig")
    progress("done", nodes=len(nodes), edges=len(edges), communities=len(communities))
    return {"counts": analysis["counts"], "communities": communities, "missing": missing,
            "problems": problems, "orphan_notes": orphan_notes, "out": out}
