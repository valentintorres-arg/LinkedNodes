"""A local server that makes the graph editable: notes, node properties and source files.

A page opened from disk cannot write files, so editing goes through this server. It
listens on 127.0.0.1 only. Because it can write to the project, every API call must
carry a token that is handed out with the page, and the Host header must name this
server; together they stop another website from driving it through the browser.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import annotations, gitinfo, graph, pipeline
from .detect import OUT_DIR
from .util import read_json

MAX_SOURCE = 2_000_000
MAX_BODY = 6_000_000
BOM = b"\xef\xbb\xbf"


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, root: Path, port: int):
        self.root = root.resolve()
        self.out = self.root / OUT_DIR
        self.token = secrets.token_urlsafe(24)
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.lock = threading.Lock()  # one write or rebuild at a time

    def node(self, node_id) -> dict:
        data = read_json(self.out / "graph.json") or {"nodes": []}
        for node in data["nodes"]:
            if node["id"] == node_id:
                return node
        raise Refused(404, "no such node")

    def source_path(self, node: dict) -> Path | None:
        """The text file behind a node, if it is one this project lets us edit."""
        rel = node.get("source_file") or ""
        manifest = read_json(self.out / ".work" / "manifest.json") or {"files": []}
        record = next((f for f in manifest["files"] if f["path"] == rel), None)
        if (not record or record["kind"] not in ("code", "doc") or rel.lower().endswith(".pdf")
                or graph.family(node["kind"]) not in ("code", "notes")):
            return None
        path = (self.root / rel).resolve()
        return path if path.is_file() and self.root in path.parents else None

    def read_source(self, node: dict) -> dict | None:
        path = self.source_path(node)
        if path is None:
            return None
        rel = node["source_file"]
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE:
            return {"path": rel, "editable": False, "reason": "This file is too large to edit here."}
        try:
            text = raw.removeprefix(BOM).decode("utf-8")
        except UnicodeDecodeError:
            return {"path": rel, "editable": False, "reason": "This file is not UTF-8 text."}
        return {"path": rel, "editable": True, "text": text.replace("\r\n", "\n"),
                "hash": hashlib.sha1(raw).hexdigest(), "line": node.get("line", 1),
                "eol": "CRLF" if b"\r\n" in raw else "LF", "git": gitinfo.file_diff(self.root, rel)}

    def write_source(self, node: dict, text: str, expected: str) -> dict:
        path = self.source_path(node)
        if path is None:
            raise Refused(403, "this node has no editable source file")
        raw = path.read_bytes()
        if hashlib.sha1(raw).hexdigest() != expected:
            raise Refused(409, "The file changed on disk after it was opened. Discard your "
                               "changes to load the new version, then edit again.")
        text = text.replace("\r\n", "\n")
        if b"\r\n" in raw:  # keep the file's own line endings, so the diff shows only the edit
            text = text.replace("\n", "\r\n")
        data = (BOM if raw.startswith(BOM) else b"") + text.encode("utf-8")
        temporary = path.with_name(f".{path.name}.linkednodes-tmp")
        temporary.write_bytes(data)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
        return {"hash": hashlib.sha1(data).hexdigest(), "git": gitinfo.file_diff(self.root, node["source_file"])}


class _Handler(BaseHTTPRequestHandler):
    server_version = "LinkedNodes"

    def log_message(self, *args):  # requests are not worth a line each in the terminal
        pass

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise Refused(413, "request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise Refused(400, "the request body is not JSON")
        if not isinstance(data, dict):
            raise Refused(400, "the request body must be a JSON object")
        return data

    def _handle(self, method: str) -> None:
        app: App = self.server.app
        url = urlsplit(self.path)
        try:
            if self.headers.get("Host", "") not in app.hosts:
                raise Refused(403, "unexpected Host header")
            if method == "GET" and url.path in ("/", "/index.html"):
                page = pipeline.render(app.root, {"token": app.token})
                return self._reply(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                raise Refused(404, "not found")
            if not secrets.compare_digest(self.headers.get("X-LinkedNodes-Token", ""), app.token):
                raise Refused(403, "missing or wrong token; reload the page")
            self._json(200, self._api(app, method, url))
        except Refused as error:
            self._json(error.status, {"error": str(error)})
        except FileNotFoundError as error:
            self._json(404, {"error": str(error)})
        except Exception as error:  # a broken request must not take the server down
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    def _api(self, app: App, method: str, url) -> dict:
        route = (method, url.path)
        if route == ("GET", "/api/git"):  # the working tree as it is now, not as it was at build time
            data = read_json(app.out / "graph.json") or {"nodes": []}
            try:
                state = gitinfo.snapshot(app.root, data["nodes"], parse_qs(url.query).get("base", ["HEAD"])[0])
            except ValueError as error:
                raise Refused(400, str(error))
            return {"git": state}
        if route == ("GET", "/api/node"):
            node = app.node(parse_qs(url.query).get("id", [""])[0])
            return {"note": annotations.read_notes(app.out).get(node["id"], ""),
                    "source": app.read_source(node)}
        body = self._body()
        with app.lock:
            if route == ("POST", "/api/rebuild"):
                scanned = pipeline.scan(app.root)
                built = pipeline.build(app.root)
                return {"nodes": built["counts"]["nodes"], "pending": sum(scanned["chunks"].values())}
            node = app.node(body.get("id"))
            if route == ("PUT", "/api/note"):
                annotations.write_note(app.out, node["id"], str(body.get("text") or ""))
                pipeline.build(app.root)
                return {"doc": app.node(node["id"]).get("doc", "")}
            if route == ("PUT", "/api/props"):
                annotations.write_props(app.out, node["id"], body)
                pipeline.build(app.root)
                fresh = app.node(node["id"])
                return {"node": {**fresh, "family": graph.family(fresh["kind"])}}
            if route == ("PUT", "/api/source"):
                return app.write_source(node, str(body.get("text") or ""), str(body.get("hash") or ""))
        raise Refused(404, "not found")

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_POST(self):
        self._handle("POST")


def make_server(root: Path, port: int = 0) -> ThreadingHTTPServer:
    """Bind to 127.0.0.1 on the given port, or on any free port when it is 0."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.app = App(root, server.server_address[1])
    return server


def serve(root: Path, port: int, open_browser: bool) -> None:
    pipeline.render(root)  # fail now, with a clear message, if there is no graph yet
    server = None
    for candidate in range(port, port + 20):
        try:
            server = make_server(root, candidate)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"error: no free port between {port} and {port + 19}")
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Serving the graph of {root} at {address}")
    print("Notes and node edits are saved in linkednodes-out. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()
