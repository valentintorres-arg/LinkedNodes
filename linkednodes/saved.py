"""Parse exports of liked and saved content: bookmarks, likes, read-later lists.

Every parser returns a list of Item. A file only counts as saved content when a
parser actually finds items in it, so ordinary JSON, CSV and HTML are left alone.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .util import edge, node, read_text, slug

CANDIDATE_EXT = {".html", ".htm", ".json", ".csv", ".url", ".txt", ".js"}
# JSON that is configuration, never an export.
SKIP_NAMES = {"package.json", "package-lock.json", "composer.json", "composer.lock",
              "tsconfig.json", "manifest.json", "launch.json", "settings.json"}
# Top-level browser folders that say nothing about the content.
ROOT_FOLDERS = {"bookmarks", "bookmarks bar", "bookmarks toolbar", "bookmarks menu",
                "other bookmarks", "mobile bookmarks", "unsorted bookmarks", "favorites bar",
                "favourites bar", "toolbar", "menu", "unfiled", "marcadores",
                "barra de marcadores", "otros marcadores", "marcadores del movil",
                "marcadores de dispositivos moviles", "barra de favoritos"}
TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|igshid|mc_cid|mc_eid|ref|ref_src|si|feature)$")
URL_RE = re.compile(r"^https?://\S+$")

URL_KEYS = ("url", "uri", "link", "href", "expandedUrl", "expanded_url", "titleUrl", "permalink")
TITLE_KEYS = ("title", "name", "fullText", "full_text", "text", "description", "excerpt")
DATE_KEYS = ("created_at", "createdAt", "created", "date", "time", "dateAdded", "date_added",
             "addDate", "add_date", "time_added", "timestamp")
NOTE_KEYS = ("note", "notes", "excerpt", "description", "comment")
FOLDER_KEYS = ("folder", "collection", "category", "playlist")


@dataclass
class Item:
    url: str
    title: str = ""
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    added: str = ""
    note: str = ""
    source_file: str = ""

    @property
    def id(self) -> str:
        return "saved:" + hashlib.sha1(normalize_url(self.url).encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}


def normalize_url(url: str) -> str:
    """Reduce a URL to a form that is equal for the same page saved twice."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING.match(k)])
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))


def domain(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _date(value) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        match = re.match(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else text[:40]
    while number > 4e10:  # milliseconds or microseconds
        number /= 1000
    if number < 1e8:
        return ""
    return datetime.fromtimestamp(number, timezone.utc).date().isoformat()


def _tags(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;|]", value)
    if not isinstance(value, list):
        return []
    return [t.strip() for t in value if isinstance(t, str) and t.strip()]


def _first(record: dict, keys) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _url(record: dict) -> str:
    for key in URL_KEYS:
        value = record.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if URL_RE.match(value):
            return value
        if value.startswith("/r/"):  # Reddit exports store a path only
            return "https://www.reddit.com" + value
    return ""


class _Bookmarks(HTMLParser):
    """Netscape bookmark format, the export format of every major browser."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[Item] = []
        self.folders: list[str | None] = []
        self.pending: str | None = None
        self.capture: str | None = None
        self.text = ""
        self.link: dict = {}

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.capture, self.text = "h3", ""
        elif tag == "a":
            self.capture, self.text, self.link = "a", "", dict(attrs)
        elif tag == "dl":
            self.folders.append(self.pending)
            self.pending = None

    def handle_data(self, data):
        if self.capture:
            self.text += data

    def handle_endtag(self, tag):
        if tag == "h3" and self.capture == "h3":
            self.pending, self.capture = self.text.strip(), None
        elif tag == "dl" and self.folders:
            self.folders.pop()
        elif tag == "a" and self.capture == "a":
            self.capture = None
            href = (self.link.get("href") or "").strip()
            if URL_RE.match(href):
                self.items.append(Item(
                    url=href,
                    title=self.text.strip(),
                    folder="/".join(f for f in self.folders if f),
                    tags=_tags(self.link.get("tags") or ""),
                    added=_date(self.link.get("add_date") or self.link.get("time_added")),
                ))


def _parse_html(text: str) -> list[Item]:
    head = text[:3000].lower()
    if "netscape-bookmark-file" not in head and "pocket export" not in head:
        return []
    parser = _Bookmarks()
    parser.feed(text)
    return parser.items


def _walk(obj, folder: str, out: list[Item]) -> None:
    if isinstance(obj, list):
        for value in obj:
            _walk(value, folder, out)
        return
    if not isinstance(obj, dict):
        return
    url = _url(obj)
    children = obj.get("children")
    if url:
        text = _first(obj, TITLE_KEYS)
        out.append(Item(
            url=url,
            title=text[:140],
            folder=_first(obj, FOLDER_KEYS) or folder,
            tags=_tags(obj.get("tags")),
            added=_date(_first(obj, DATE_KEYS)),
            note=_first(obj, NOTE_KEYS) or (text if len(text) > 140 else ""),
        ))
        _walk(children, folder, out)
        return
    if isinstance(children, list):
        name = _first(obj, ("title", "name"))
        folder = f"{folder}/{name}" if folder and name else (name or folder)
    for value in obj.values():
        _walk(value, folder, out)


def _parse_json(text: str) -> list[Item]:
    # Twitter/X archives wrap the JSON in an assignment: window.YTD.like.part0 = [...]
    text = re.sub(r"^\s*window\.[\w.]+\s*=\s*", "", text)
    items: list[Item] = []
    _walk(json.loads(text), "", items)
    return items if len(items) >= 3 else []


def _parse_csv(text: str) -> list[Item]:
    csv.field_size_limit(10_000_000)
    items = []
    for row in csv.DictReader(io.StringIO(text)):
        record = {(k or "").strip().lower(): v for k, v in row.items() if isinstance(v, str)}
        record.setdefault("url", record.get("video url", ""))
        if not _url(record) and record.get("video id"):  # YouTube Takeout playlists
            record["url"] = "https://www.youtube.com/watch?v=" + record["video id"].strip()
        url = _url(record)
        if not url:
            continue
        items.append(Item(
            url=url,
            title=_first(record, TITLE_KEYS)[:140],
            folder=_first(record, FOLDER_KEYS),
            tags=_tags(record.get("tags", "")),
            added=_date(_first(record, DATE_KEYS)
                        or record.get("playlist video creation timestamp", "")),
            note=_first(record, NOTE_KEYS),
        ))
    return items


def _parse_shortcut(path: Path, text: str) -> list[Item]:
    match = re.search(r"^URL=(\S+)", text, re.MULTILINE)
    if not match or not URL_RE.match(match.group(1)):
        return []
    return [Item(url=match.group(1), title=path.stem, folder=path.parent.name)]


def _parse_url_list(text: str) -> list[Item]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    urls = [line for line in lines if URL_RE.match(line)]
    if len(urls) < 2 or len(urls) < 0.6 * len(lines):
        return []
    return [Item(url=u) for u in urls]


def is_candidate(path: Path) -> bool:
    name, ext = path.name.lower(), path.suffix.lower()
    if ext not in CANDIDATE_EXT or name in SKIP_NAMES:
        return False
    if ext == ".js":
        return name in {"like.js", "bookmark.js", "bookmarks.js"}
    return True


def parse(path: Path, rel: str) -> list[Item]:
    """Return the saved items in a file, or an empty list when it is not an export."""
    ext = path.suffix.lower()
    try:
        text = read_text(path)
        if ext in (".html", ".htm"):
            items = _parse_html(text)
        elif ext in (".json", ".js"):
            items = _parse_json(text)
        elif ext == ".csv":
            items = _parse_csv(text)
        elif ext == ".url":
            items = _parse_shortcut(path, text)
        else:
            items = _parse_url_list(text)
    except (OSError, ValueError, csv.Error, RecursionError):
        return []
    for item in items:
        item.source_file = rel
    return items


def dedupe(items: list[Item]) -> list[Item]:
    """Merge the same page saved in several places, keeping every tag."""
    merged: dict[str, Item] = {}
    for item in items:
        seen = merged.setdefault(item.id, item)
        if seen is item:
            continue
        seen.tags += [t for t in item.tags if t not in seen.tags]
        seen.title = seen.title or item.title
        seen.folder = seen.folder or item.folder
        seen.note = seen.note or item.note
        seen.added = seen.added or item.added
    return list(merged.values())


def _folder_path(folder: str) -> list[str]:
    parts = [p.strip() for p in folder.split("/") if p.strip()]
    while parts and slug(parts[0]).replace("-", " ") in ROOT_FOLDERS:
        parts.pop(0)
    return parts


def to_graph(items: list[Item]) -> tuple[list[dict], list[dict]]:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for item in items:
        site = domain(item.url)
        label = item.title or (site + urlsplit(item.url).path)[:120]
        nodes[item.id] = node(item.id, label, "saved", item.source_file, url=item.url,
                              domain=site, added=item.added, note=item.note[:300])
        parent = None
        path = _folder_path(item.folder)
        for depth in range(1, len(path) + 1):
            folder_id = "folder:" + slug("/".join(path[:depth]))
            nodes.setdefault(folder_id, node(folder_id, path[depth - 1], "folder", item.source_file))
            if parent:
                edges.append(edge(folder_id, parent, "part_of", source_file=item.source_file))
            parent = folder_id
        if parent:
            edges.append(edge(item.id, parent, "in_folder", source_file=item.source_file))
        for tag in item.tags:
            # Tags share the concept namespace so they meet the topics found in notes.
            tag_id = "concept:" + slug(tag)
            nodes.setdefault(tag_id, node(tag_id, tag, "tag", item.source_file))
            edges.append(edge(item.id, tag_id, "tagged", source_file=item.source_file))
    return list(nodes.values()), edges
