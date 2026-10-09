"""The part of the graph that needs a model: what notes, images and saved links are about.

plan() writes a queue of chunks for the model to read. The model writes one result
file per chunk. ingest() validates those results and files them in a cache keyed by
file hash and item id, so unchanged content is never read twice. load() turns the
cache into nodes and edges.
"""
from __future__ import annotations

import json
from pathlib import Path

from .saved import Item
from .util import CONFIDENCE, edge, node, read_json, read_text, slug, write_json

DOC_BUDGET = 60_000  # bytes of text per chunk
DOC_LIMIT = 10
IMAGE_LIMIT = 6
SAVED_LIMIT = 60


def _dirs(out: Path) -> tuple[Path, Path, Path, Path]:
    cache = out / "cache"
    return out / ".work", cache / "files", cache / "saved.json", cache / "links.json"


def plan(out: Path, files: list[dict], items: list[Item]) -> dict:
    """Queue everything that has no cached extraction yet and return the queue."""
    work, file_cache, saved_path, _ = _dirs(out)
    saved_cache = read_json(saved_path, {})
    pending = [f for f in files if f["kind"] in ("doc", "image")
               and not (file_cache / f"{f['hash']}.json").exists()]
    chunks: list[dict] = []

    def add(kind: str, **content) -> dict:
        chunks.append({"id": f"c{len(chunks) + 1:03d}", "type": kind, **content})
        return chunks[-1]

    batch, used = [], 0
    for record in (f for f in pending if f["kind"] == "doc"):
        entry = {"path": record["path"], "hash": record["hash"], "bytes": record["size"]}
        # A PDF costs far more to read than its size on disk suggests.
        weight = max(record["size"], 20_000) if record["path"].lower().endswith(".pdf") else record["size"]
        if batch and (used + weight > DOC_BUDGET or len(batch) >= DOC_LIMIT):
            add("docs", files=batch)
            batch, used = [], 0
        batch.append(entry)
        used += weight
    if batch:
        add("docs", files=batch)

    images = [{"path": f["path"], "hash": f["hash"], "bytes": f["size"]}
              for f in pending if f["kind"] == "image"]
    for start in range(0, len(images), IMAGE_LIMIT):
        add("images", files=images[start:start + IMAGE_LIMIT])

    new_items = [item for item in items if item.id not in saved_cache]
    for start in range(0, len(new_items), SAVED_LIMIT):
        group = new_items[start:start + SAVED_LIMIT]
        chunk = add("saved", item_ids=[item.id for item in group], count=len(group))
        chunk["items_file"] = f"{out.name}/.work/saved_{chunk['id']}.json"
        write_json(work / f"saved_{chunk['id']}.json", [
            {"id": i.id, "title": i.title, "url": i.url, "folder": i.folder, "tags": i.tags,
             "note": i.note[:300]} for i in group])

    vocabulary = sorted({t for topics in saved_cache.values() for t in topics}
                        | {n["label"] for path in file_cache.glob("*.json")
                           for n in read_json(path, {}).get("nodes", [])})
    write_json(work / "vocabulary.json", vocabulary[:500])
    todo = {"results_dir": f"{out.name}/.work/results",
            "vocabulary_file": f"{out.name}/.work/vocabulary.json", "chunks": chunks}
    write_json(work / "todo.json", todo)
    (work / "results").mkdir(parents=True, exist_ok=True)
    return todo


def _clean_edge(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    source, target = str(raw.get("source") or "").strip(), str(raw.get("target") or "").strip()
    if not source or not target:
        return None
    confidence = str(raw.get("confidence") or "INFERRED").upper()
    if confidence not in CONFIDENCE:
        confidence = "INFERRED"
    try:
        score = min(1.0, max(0.0, float(raw.get("score"))))
    except (TypeError, ValueError):
        score = CONFIDENCE[confidence]
    relation = slug(str(raw.get("relation") or "")).replace("-", "_") or "related_to"
    return {"source": source, "target": target, "relation": relation,
            "confidence": confidence, "score": score}


def _clean_file(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    nodes = [{"label": str(n["label"]).strip()[:120],
              "kind": slug(str(n.get("kind") or "concept")) or "concept",
              "note": str(n.get("note") or "")[:300]}
             for n in raw.get("nodes") or []
             if isinstance(n, dict) and str(n.get("label") or "").strip()]
    edges = [e for e in map(_clean_edge, raw.get("edges") or []) if e]
    return {"summary": str(raw.get("summary") or "")[:400], "nodes": nodes, "edges": edges}


def ingest(out: Path) -> list[str]:
    """Move finished result files into the cache. Returns problems worth reporting."""
    work, file_cache, saved_path, links_path = _dirs(out)
    todo = read_json(work / "todo.json")
    if not todo:
        return []
    hashes = {f["path"]: f["hash"] for c in todo["chunks"] for f in c.get("files", [])}
    item_ids = {i for c in todo["chunks"] for i in c.get("item_ids", [])}
    saved_cache = read_json(saved_path, {})
    links = read_json(links_path, [])
    seen_links = {(l["source"], l["target"], l["relation"]) for l in links}
    problems = []
    for result in sorted((work / "results").glob("*.json")):
        try:
            data = json.loads(read_text(result))
        except ValueError as error:
            problems.append(f"{result.name} is not valid JSON ({error}); fix it and build again")
            continue
        if not isinstance(data, dict):
            problems.append(f"{result.name} must contain a JSON object")
            continue
        for path, raw in (data.get("files") or {}).items():
            key = path.replace("\\", "/").removeprefix("./")
            entry = _clean_file(raw)
            if key not in hashes or entry is None:
                problems.append(f"{result.name}: skipped '{path}', which is not a queued file")
                continue
            write_json(file_cache / f"{hashes[key]}.json", entry)
        for item_id, topics in (data.get("saved") or {}).items():
            if item_id not in item_ids or not isinstance(topics, list):
                problems.append(f"{result.name}: skipped '{item_id}', which is not a queued item")
                continue
            saved_cache[item_id] = [str(t).strip()[:80] for t in topics if str(t).strip()]
        for link in filter(None, map(_clean_edge, data.get("links") or [])):
            key = (link["source"], link["target"], link["relation"])
            if key not in seen_links:
                seen_links.add(key)
                links.append(link)
        result.unlink()
    write_json(saved_path, saved_cache)
    write_json(links_path, links)
    return problems


def load(out: Path, files: list[dict], items: list[Item], file_ids: dict[str, str],
         known: set[str]) -> dict:
    """Build nodes and edges from the cache for the files and items that still exist."""
    _, file_cache, saved_path, links_path = _dirs(out)
    concepts: dict[str, dict] = {}
    edges: list[dict] = []
    summaries: dict[str, str] = {}
    missing_files: list[str] = []

    def concept(label: str, kind: str = "concept", note: str = "", source: str = "") -> str | None:
        key = slug(label)
        if not key:
            return None
        cid = "concept:" + key
        found = concepts.setdefault(cid, node(cid, label.strip()[:120], kind, source))
        if kind != "concept" and found["kind"] == "concept":
            found["kind"] = kind
        if note and not found.get("note"):
            found["note"] = note
        return cid

    def resolve(ref: str, me: str | None, source: str, create: bool = True) -> str | None:
        if ref == "@file":
            return me
        if ref in known:
            return ref
        path = ref.replace("\\", "/").removeprefix("./")
        if path in file_ids:
            return file_ids[path]
        if create or "concept:" + slug(ref) in concepts:
            return concept(ref, source=source)
        return None

    def connect(raw: dict, source: str | None, target: str | None, rel: str) -> None:
        if source and target and source != target:
            edges.append(edge(source, target, raw["relation"], raw["confidence"], raw["score"], rel))

    for record in files:
        if record["kind"] not in ("doc", "image"):
            continue
        entry = read_json(file_cache / f"{record['hash']}.json")
        rel, me = record["path"], file_ids[record["path"]]
        if entry is None:
            missing_files.append(rel)
            continue
        if entry["summary"]:
            summaries[me] = entry["summary"]
        for n in entry["nodes"]:
            concept(n["label"], n["kind"], n["note"], rel)
        for raw in entry["edges"]:
            connect(raw, resolve(raw["source"], me, rel), resolve(raw["target"], me, rel), rel)

    saved_cache = read_json(saved_path, {})
    missing_items = 0
    for item in items:
        topics = saved_cache.get(item.id)
        if topics is None:
            missing_items += 1
            continue
        for topic in topics:
            target = concept(topic, source=item.source_file)
            if target:
                # A topic is read off a title and URL, never stated by the source itself.
                edges.append(edge(item.id, target, "about", "INFERRED", 0.8, item.source_file))

    for raw in read_json(links_path, []):
        # A link is kept only while one end is still in use, so deleted content takes
        # its topic hierarchy with it.
        source = resolve(raw["source"], None, "", create=False)
        target = resolve(raw["target"], None, "", create=False)
        if source or target:
            connect(raw, source or resolve(raw["source"], None, ""),
                    target or resolve(raw["target"], None, ""), "")

    return {"nodes": list(concepts.values()), "edges": edges, "summaries": summaries,
            "missing_files": missing_files, "missing_items": missing_items}
