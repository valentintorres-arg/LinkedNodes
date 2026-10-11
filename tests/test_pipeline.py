"""End-to-end test: a small folder with every kind of content goes in, a graph comes out."""
import base64
import html
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from linkednodes import annotations, cli, code, desktop, detect, github, gitinfo, gitops, graph, hooks, pipeline, saved, server
from linkednodes.query import Graph

FILES = {
    "app/models.py": (
        "class Base:\n"
        "    def save(self):\n"
        "        return True\n\n"
        "class Order(Base):\n"
        "    def total_price(self):\n"
        "        return self.apply_discount()\n\n"
        "    def apply_discount(self):\n"
        "        return 1\n"
    ),
    "app/service.py": (
        "import requests\n"
        "import os\n"
        "from .models import Order\n\n"
        "def place_order():\n"
        "    order = Order()\n"
        "    return order.total_price()\n\n"
        "def main():\n"
        "    print(len([]))\n"
        "    return place_order()\n"
    ),
    "web/util.ts": (
        "export function formatPrice(value: number): string {\n"
        "  return value.toFixed(2);\n"
        "}\n"
    ),
    "web/api.ts": (
        "import axios from 'axios';\n"
        "import { formatPrice } from './util';\n\n"
        "export class Client extends BaseClient {\n"
        "  async fetchOrder(id: string) {\n"
        "    return formatPrice(1);\n"
        "  }\n"
        "}\n\n"
        "export const loadOrders = async () => {\n"
        "  return formatPrice(2);\n"
        "};\n"
    ),
    "notes/ideas.md": (
        "# Pricing ideas\n\n"
        "See [[other]] and the [service](../app/service.py). The `total_price` method\n"
        "is the core. Background: https://example.com/pricing?utm_source=x\n\n"
        "![diagram](../pic.png)\n"
    ),
    "notes/other.md": "Loose thoughts with no heading.\n",
    "notes/plain.txt": "Just a text note.\n",
    "bookmarks.html": (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<TITLE>Bookmarks</TITLE><H1>Bookmarks</H1>\n"
        "<DL><p>\n<DT><H3>Bookmarks bar</H3>\n<DL><p>\n<DT><H3>Dev</H3>\n<DL><p>\n"
        '<DT><A HREF="https://www.example.com/pricing/" ADD_DATE="1700000000" TAGS="pricing,saas">'
        "Pricing &amp; packaging</A>\n"
        '<DT><A HREF="https://tokio.rs/tutorial" ADD_DATE="1700000001">Tokio tutorial</A>\n'
        "</DL><p>\n</DL><p>\n</DL><p>\n"
    ),
    "likes.json": json.dumps([
        {"like": {"tweetId": "1", "fullText": "Async Rust is finally pleasant",
                  "expandedUrl": "https://twitter.com/i/web/status/1"}},
        {"like": {"tweetId": "2", "fullText": "Sourdough hydration chart",
                  "expandedUrl": "https://twitter.com/i/web/status/2"}},
        {"like": {"tweetId": "3", "fullText": "<script>alert(1)</script>",
                  "expandedUrl": "https://twitter.com/i/web/status/3"}},
    ]),
    "package.json": json.dumps({"name": "x", "homepage": "https://example.com"}),
}


class FakeGitHub(BaseHTTPRequestHandler):
    """Enough of github.com to sign in, list repositories and clone one over plain HTTP."""
    token = "token-123"
    files: Path  # bare repositories, served as the files they are
    polls = 0
    verdict: dict = {}  # what a sign-in is finally told
    carried: list = []  # the Authorization header of every request git made

    def log_message(self, *args):
        pass

    def answer(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        here = f"http://{self.headers['Host']}"
        if self.path == "/login/device/code":
            self.answer({"device_code": "device", "user_code": "ABCD-1234", "interval": 0, "expires_in": 60,
                         "verification_uri": here + "/login/device"})
        elif self.path == "/login/oauth/access_token":
            type(self).polls += 1  # the first time the code has not been typed yet
            self.answer({"error": "authorization_pending"} if self.polls < 2 else self.verdict)
        else:
            self.answer({"message": "Not Found"}, 404)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        here = f"http://{self.headers['Host']}"
        if path.startswith("/api/"):
            if self.headers.get("Authorization") != f"Bearer {self.token}":
                self.answer({"message": "Bad credentials"}, 401)
            elif path == "/api/user":
                self.answer({"login": "ana", "name": "Ana <Pérez>", "avatar_url": "https://example.com/ana.png"})
            elif path == "/api/user/repos":
                self.answer([
                    {"full_name": "ana/shop", "private": False, "description": "Prices & orders",
                     "pushed_at": "2026-10-01T10:00:00Z", "clone_url": here + "/ana/shop.git"},
                    {"full_name": "org/secret", "private": True, "description": None, "archived": True,
                     "pushed_at": "2026-09-01T10:00:00Z", "clone_url": "https://elsewhere.example/org/secret.git"}])
            else:
                self.answer({"message": "Not Found"}, 404)
            return
        self.carried.append(self.headers.get("Authorization"))
        file = (self.files / path.lstrip("/")).resolve()
        if self.files.resolve() not in file.parents or not file.is_file():
            return self.answer({"message": "Not Found"}, 404)
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel, text in FILES.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self.out = self.root / "linkednodes-out"

    def tearDown(self):
        self.tmp.cleanup()

    def answer(self, todo):
        """Write the result files a model would write for the queued chunks."""
        results = self.root / todo["results_dir"]
        for chunk in todo["chunks"]:
            if chunk["type"] == "saved":
                items = json.loads((self.root / chunk["items_file"]).read_text(encoding="utf-8"))
                topics = {"Pricing": ["Pricing strategy"], "Tokio": ["Async Rust"],
                          "Async": ["async rust"], "Sourdough": ["Sourdough baking"]}
                data = {"saved": {i["id"]: next((v for k, v in topics.items() if k in i["title"]), [])
                                  for i in items},
                        "links": [{"source": "Async Rust", "target": "Rust", "relation": "part of"}]}
            else:
                data = {"files": {}}
                for f in chunk["files"]:
                    entry = {"summary": f"About {f['path']}", "nodes": [], "edges": []}
                    if f["path"] == "notes/ideas.md":
                        entry["nodes"] = [{"label": "Pricing strategy", "kind": "concept"}]
                        entry["edges"] = [
                            {"source": "@file", "target": "Pricing strategy", "relation": "explains",
                             "confidence": "EXTRACTED"},
                            {"source": "Pricing strategy", "target": "app/models.py",
                             "relation": "implemented in", "confidence": "AMBIGUOUS"},
                            {"source": "", "target": "dropped"},
                        ]
                    data["files"][f["path"]] = entry
            (results / f"{chunk['id']}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_full_run(self):
        summary = pipeline.scan(self.root)
        self.assertEqual(summary["files"], {"code": 4, "saved": 2, "doc": 3, "image": 1})
        self.assertEqual(summary["saved_items"], 5)
        todo = json.loads((self.out / ".work" / "todo.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(c["type"] for c in todo["chunks"]), ["docs", "images", "saved"])

        self.answer(todo)
        result = pipeline.build(self.root)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["missing"], {"files": [], "items": 0})

        data = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in data["nodes"]}
        edges = {(e["source"], e["target"], e["relation"]): e for e in data["edges"]}

        def has(source, target, relation, confidence="EXTRACTED"):
            self.assertIn((source, target, relation), edges)
            self.assertEqual(edges[(source, target, relation)]["confidence"], confidence)

        # Python: exact edges from the syntax tree.
        has("code:app/service.py", "code:app/models.py", "imports")
        has("code:app/service.py", "pkg:requests", "imports")
        self.assertNotIn("pkg:os", nodes)
        has("code:app/models.py::Order", "code:app/models.py::Base", "inherits")
        has("code:app/service.py::place_order", "code:app/models.py::Order", "calls")
        has("code:app/models.py::Order.total_price", "code:app/models.py::Order.apply_discount", "calls")
        has("code:app/service.py::main", "code:app/service.py::place_order", "calls")
        has("code:app/service.py::place_order", "code:app/models.py::Order.total_price", "calls", "INFERRED")
        # TypeScript: regex-based, so calls are inferred.
        has("code:web/api.ts", "code:web/util.ts", "imports")
        has("code:web/api.ts", "pkg:axios", "imports")
        has("code:web/api.ts::Client.fetchOrder", "code:web/util.ts::formatPrice", "calls", "INFERRED")
        has("code:web/api.ts::loadOrders", "code:web/util.ts::formatPrice", "calls", "INFERRED")
        # Notes: explicit links, a URL that matches a bookmark, a quoted code name.
        self.assertEqual(nodes["doc:notes/ideas.md"]["label"], "Pricing ideas")
        has("doc:notes/ideas.md", "doc:notes/other.md", "links_to")
        has("doc:notes/ideas.md", "code:app/service.py", "links_to")
        has("doc:notes/ideas.md", "img:pic.png", "embeds")
        has("doc:notes/ideas.md", "code:app/models.py::Order.total_price", "mentions", "INFERRED")
        pricing = next(n for n in nodes.values() if n.get("url", "").endswith("/pricing/"))
        has("doc:notes/ideas.md", pricing["id"], "references")
        # Saved content: folders without the browser root, tags, topics from the model.
        self.assertEqual(pricing["label"], "Pricing & packaging")
        self.assertEqual(pricing["added"], "2023-11-14")
        has(pricing["id"], "folder:dev", "in_folder")
        self.assertNotIn("folder:bookmarks-bar", nodes)
        has(pricing["id"], "concept:saas", "tagged")
        # The bookmark and the note meet at one concept.
        has(pricing["id"], "concept:pricing-strategy", "about", "INFERRED")
        has("doc:notes/ideas.md", "concept:pricing-strategy", "explains")
        has("concept:pricing-strategy", "code:app/models.py", "implemented_in", "AMBIGUOUS")
        # Two spellings of a topic are one node, and the hierarchy link is kept.
        self.assertEqual(sum(1 for e in edges if e[1] == "concept:async-rust" and e[2] == "about"), 2)
        has("concept:async-rust", "concept:rust", "part_of", "INFERRED")
        self.assertEqual(nodes["doc:notes/other.md"]["summary"], "About notes/other.md")

        html = (self.out / "graph.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("__DATA__", html)
        if os.name == "nt":  # the launcher for the editable view, with the mark PowerShell needs
            self.assertTrue((self.out / "open-graph.ps1").read_bytes().startswith(b"\xef\xbb\xbf# Opens"))
        report = (self.out / "GRAPH_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("## Communities", report)
        self.assertIn("Uncertain edges to review", report)

        graph = Graph(self.root)
        self.assertEqual(graph.find("total_price")[0]["id"], "code:app/models.py::Order.total_price")
        path = graph.path(graph.find("Tokio tutorial")[0], graph.find("Rust", 1)[0])
        self.assertIn("--part_of--> Rust", path)

    def test_second_run_reads_only_what_changed(self):
        self.answer_all()
        self.assertEqual(pipeline.scan(self.root)["chunks"], {})
        (self.root / "notes" / "other.md").write_text("Rewritten.\n", encoding="utf-8")
        summary = pipeline.scan(self.root)
        self.assertEqual(summary["chunks"], {"docs": 1})
        todo = json.loads((self.out / ".work" / "todo.json").read_text(encoding="utf-8"))
        self.assertEqual([f["path"] for f in todo["chunks"][0]["files"]], ["notes/other.md"])
        result = pipeline.build(self.root)
        self.assertEqual(result["missing"]["files"], ["notes/other.md"])

    def test_bad_results_are_reported_not_fatal(self):
        pipeline.scan(self.root)
        results = self.out / ".work" / "results"
        (results / "c001.json").write_text("{not json", encoding="utf-8")
        (results / "c002.json").write_text(json.dumps({"files": {"nope.md": {}}}), encoding="utf-8")
        problems = pipeline.build(self.root)["problems"]
        self.assertEqual(len(problems), 2)
        self.assertTrue((results / "c001.json").exists())

    def test_files_ignored_by_git_are_skipped(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("app/generated/\n", encoding="utf-8")
        generated = self.root / "app" / "generated" / "client.py"
        generated.parent.mkdir()
        generated.write_text("def generated_helper():\n    return 1\n", encoding="utf-8")
        files, _ = detect.scan(self.root)
        paths = [f["path"] for f in files]
        self.assertNotIn("app/generated/client.py", paths)
        self.assertIn("app/models.py", paths)  # untracked but not ignored

    def test_notes_and_corrections_survive_a_rebuild(self):
        self.answer_all()
        target = "code:app/models.py::Order"
        annotations.write_note(self.out, target, "# Order\n\nThe **total** lives here.\n")
        annotations.write_note(self.out, "code:gone.py", "About a file that was deleted.")
        annotations.write_props(self.out, target, {"label": "Pedido", "kind": "", "summary": "Un pedido."})
        pipeline.scan(self.root)
        result = pipeline.build(self.root)
        self.assertEqual(result["orphan_notes"], ["code:gone.py"])
        graph = Graph(self.root)
        node = graph.nodes[target]
        self.assertEqual((node["label"], node["kind"], node["summary"]), ("Pedido", "class", "Un pedido."))
        self.assertEqual(node["doc"], "# Order\n\nThe **total** lives here.")
        self.assertIn("    The **total** lives here.", graph.describe(node))
        self.assertIn("The **total** lives here.", (self.out / "graph.html").read_text(encoding="utf-8"))
        annotations.write_note(self.out, target, "  ")
        annotations.write_props(self.out, target, {})
        pipeline.build(self.root)
        node = Graph(self.root).nodes[target]
        self.assertEqual(node["label"], "Order")
        self.assertNotIn("doc", node)

    def serve(self):
        """Start the server on the folder; returns its token and a function that calls it."""
        httpd = server.make_server(self.root)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        port, token = httpd.server_address[1], httpd.app.token

        def call(method, path, body=None, token=token, host=None):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-LinkedNodes-Token": token, **({"Host": host} if host else {})})
            try:
                with urllib.request.urlopen(request) as reply:
                    raw = reply.read()
                    return reply.status, raw if path.split("?")[0] in ("/", "/loading") else json.loads(raw)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        return token, call

    def test_server_edits_and_refuses(self):
        self.answer_all()
        token, call = self.serve()
        target = "code:app/models.py::Order"

        status, page = call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(token.encode(), page)
        # Without the token, or under another site's name, nothing is served.
        self.assertEqual(call("GET", f"/api/node?id={target}", token="wrong")[0], 403)
        self.assertEqual(call("GET", "/", host="evil.example")[0], 403)

        status, reply = call("PUT", "/api/note", {"id": target, "text": "Hecho a mano."})
        self.assertEqual((status, reply["doc"]), (200, "Hecho a mano."))
        # The note is in the graph on disk at once, without the graph being built again.
        self.assertEqual(Graph(self.root).nodes[target]["doc"], "Hecho a mano.")
        self.assertIn("Hecho a mano.", (self.out / "graph.html").read_text(encoding="utf-8"))
        status, reply = call("PUT", "/api/props", {"id": target, "label": "Pedido"})
        self.assertEqual((reply["node"]["label"], reply["node"]["family"]), ("Pedido", "code"))

        source = call("GET", f"/api/node?id={urllib.parse.quote(target)}")[1]["source"]
        self.assertEqual((source["path"], source["line"], source["editable"]), ("app/models.py", 5, True))
        edited = source["text"].replace("return 1", "return 2")
        status, saved = call("PUT", "/api/source", {"id": target, "text": edited, "hash": source["hash"]})
        self.assertEqual(status, 200)
        self.assertIn("return 2", (self.root / "app" / "models.py").read_text(encoding="utf-8"))
        # A second save from the stale copy must not overwrite the first.
        status, reply = call("PUT", "/api/source", {"id": target, "text": "x", "hash": source["hash"]})
        self.assertEqual(status, 409)
        # Images, concepts and files outside the project have no editable source.
        self.assertIsNone(call("GET", "/api/node?id=img:pic.png")[1]["source"])
        self.assertEqual(call("PUT", "/api/source", {"id": "img:pic.png", "text": "x", "hash": ""})[0], 403)
        self.assertEqual(call("GET", "/api/node?id=nope")[0], 404)
        self.assertEqual(call("POST", "/api/rebuild", {})[1]["pending"], 0)
        # Outside a repository there is no git to show, to follow or to drive.
        self.assertEqual(call("GET", "/api/git")[1], {"git": None, "repo": None})
        quiet = call("GET", "/api/watch")[1]
        self.assertEqual((quiet["stamp"], quiet["built"], len(quiet["beat"])), (None, 0, 40))
        self.assertEqual(call("POST", "/api/repo", {"do": "stage"})[0], 409)
        # A rebuild asked for in the page is done on the side, and watched from a page of its own.
        self.assertEqual(call("GET", "/api/progress")[1]["state"]["step"], "done")  # none yet: back to the graph
        self.assertEqual(call("POST", "/api/rebuild", {"watch": True})[1], {"watching": True})
        for _ in range(200):
            state = call("GET", "/api/progress")[1]["state"]
            if state["step"] == "done":
                break
            time.sleep(.05)
        counts = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))["analysis"]["counts"]
        self.assertEqual((state["step"], state["name"], state["nodes"], state["edges"]),
                         ("done", self.root.name, counts["nodes"], counts["edges"]))
        status, page = call("GET", "/loading?lang=es&app=1")
        self.assertEqual(status, 200)
        self.assertIn(token.encode(), page)
        self.assertIn("Reconstruyendo el grafo".encode(), page)
        self.assertIn(b'"back": "/?lang=es&app=1"', page)
        self.assertIn(b'"back": "/"', call("GET", "/loading?lang=xx")[1])  # in a browser, and in no language it knows
        self.assertEqual(call("GET", "/api/progress", token="wrong")[0], 403)
        # An emptied note leaves the graph as well.
        self.assertEqual(call("PUT", "/api/note", {"id": target, "text": "  "})[1], {"doc": ""})
        self.assertNotIn("doc", Graph(self.root).nodes[target])

    def test_source_keeps_line_endings(self):
        self.answer_all()
        path = self.root / "app" / "models.py"
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        pipeline.scan(self.root)
        pipeline.build(self.root)
        app = server.App(self.root, 1)
        node = app.node("code:app/models.py")
        source = app.read_source(node)
        self.assertNotIn("\r", source["text"])
        app.write_source(node, source["text"] + "# fin\n", source["hash"])
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"# fin\r\n"))
        self.assertNotIn(b"\r\r", raw)
        self.assertEqual(list(path.parent.glob(".*tmp")), [])

    def test_git_history_and_changes(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")

        def git(*args, who="Ana"):
            subprocess.run(["git", "-C", str(self.root), "-c", f"user.name={who}",
                            "-c", f"user.email={who.lower()}@example.com",
                            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "first version")
        models, service = self.root / "app" / "models.py", self.root / "app" / "service.py"
        for path in (models, service):  # a second person changes these two together
            path.write_text(path.read_text(encoding="utf-8") + "# revisado\n", encoding="utf-8")
        git("commit", "-q", "-a", "-m", "pair change", who="Beto")
        models.write_text(models.read_text(encoding="utf-8").replace("return 1", "return 2"), encoding="utf-8")
        (self.root / "app" / "extra.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")
        self.answer_all()

        data = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))
        state = data["git"]
        self.assertEqual(state["changed"], {"app/models.py": "modified", "app/extra.py": "untracked"})
        past = state["files"]["app/models.py"]
        self.assertEqual((past["author"], past["commits"], past["subject"]), ("Beto", 2, "pair change"))
        self.assertNotIn("app/extra.py", state["files"])
        # Committed together twice, so linked; committed together once, so not.
        links = {(e["source"], e["target"]): e for e in data["edges"] if e["relation"] == "changes_with"}
        self.assertEqual(list(links), [("code:app/models.py", "code:app/service.py")])
        self.assertEqual(links[("code:app/models.py", "code:app/service.py")]["together"], 2)
        self.assertIn("## Change together", (self.out / "GRAPH_REPORT.md").read_text(encoding="utf-8"))
        # Two authors, so each becomes a node tied to what they committed.
        ids = {n["id"]: n for n in data["nodes"]}
        self.assertEqual((ids["person:ana"]["kind"], ids["person:beto"]["label"]), ("person", "Beto"))
        wrote = {(e["source"], e["target"]) for e in data["edges"] if e["relation"] == "authored"}
        self.assertIn(("person:beto", "code:app/service.py"), wrote)
        self.assertNotIn(("person:beto", "code:web/api.ts"), wrote)
        self.assertNotIn("person:beto", [g["id"] for g in data["analysis"]["gods"]])
        # The editor gets the file's own state: what it is, which lines, and the patch.
        own = gitinfo.file_diff(self.root, "app/models.py")
        self.assertEqual((own["status"], own["ranges"]), ("modified", [(10, 10)]))
        self.assertIn("+        return 2", own["patch"])
        self.assertEqual(gitinfo.file_diff(self.root, "app/extra.py")["status"], "untracked")
        self.assertIsNone(gitinfo.file_diff(self.root, "web/api.ts")["status"])
        opened = server.App(self.root, 1).read_source(ids["code:app/models.py"])
        self.assertEqual(opened["git"]["status"], "modified")
        # Only the definitions whose lines changed are marked, plus the file and the class around them.
        touched = state["nodes"]
        self.assertEqual({k for k in touched if k.startswith("code:app/models.py")},
                         {"code:app/models.py", "code:app/models.py::Order", "code:app/models.py::Order.apply_discount"})
        self.assertEqual(touched["code:app/extra.py::brand_new"], "untracked")
        reached = gitinfo.affected(set(touched), data["edges"])
        self.assertIn("code:app/models.py::Order.total_price", reached)  # it calls what changed
        self.assertIn("code:app/service.py::place_order", reached)
        self.assertNotIn("code:app/models.py::Base", reached)

        graph = Graph(self.root)
        self.assertIn("git: last changed", graph.describe(graph.nodes["code:app/models.py::Order.apply_discount"]))
        self.assertIn("orange ring", (self.out / "graph.html").read_text(encoding="utf-8").lower())
        # A commit makes the tree clean again; a reference that is not one is refused.
        git("add", "app")
        git("commit", "-q", "-m", "second")
        self.assertEqual(gitinfo.snapshot(self.root, data["nodes"])["nodes"], {})
        self.assertIn("code:app/extra.py::brand_new", gitinfo.snapshot(self.root, data["nodes"], "HEAD~1")["nodes"])
        for bad in ("--output=x", "no such ref", "nope"):
            with self.assertRaises(ValueError):
                gitinfo.snapshot(self.root, data["nodes"], bad)
        self.assertEqual(cli.main(["changed", "--root", str(self.root), "--base", "HEAD~1"]), 0)
        self.assertEqual(cli.main(["refresh", str(self.root)]), 0)

        # Hooks: ours can be installed twice and removed; somebody else's is left alone.
        written = hooks.install(self.root)
        self.assertEqual([p.name for p in hooks.install(self.root)], ["post-commit", "post-merge"])
        script = written[0].read_text(encoding="utf-8")
        self.assertIn("-m linkednodes refresh .", script)
        self.assertNotIn("\r", written[0].read_bytes().decode())
        self.assertEqual(hooks.remove(self.root), written)
        written[0].write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            hooks.install(self.root)
        self.assertFalse(written[1].exists())
        self.assertEqual(hooks.remove(self.root), [])
        self.assertIn("echo mine", written[0].read_text(encoding="utf-8"))

    def test_git_work_from_the_viewer(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        remote, other = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(lambda: (remote.cleanup(), other.cleanup()))

        def git(*args, at=self.root):
            subprocess.run(["git", "-C", str(at), "-c", "user.name=Ana", "-c", "user.email=ana@example.com",
                            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

        self.answer_all()
        git("init", "-q", "--bare", "-b", "main", at=remote.name)
        git("init", "-q", "-b", "main")
        for key, value in (("user.name", "Ana"), ("user.email", "ana@example.com"), ("commit.gpgsign", "false")):
            git("config", key, value)
        git("remote", "add", "origin", remote.name)
        git("add", "-A")
        git("commit", "-q", "-m", "first version")
        models = self.root / "app" / "models.py"
        models.write_text(models.read_text(encoding="utf-8").replace("return 1", "return 2"), encoding="utf-8")
        (self.root / "app" / "extra.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")
        token, call = self.serve()

        repo = call("GET", "/api/git")[1]["repo"]
        self.assertEqual((repo["branch"], repo["upstream"], repo["staged"]), ("main", "", {}))
        self.assertEqual(repo["unstaged"], {"app/extra.py": "untracked", "app/models.py": "modified"})
        self.assertEqual([(c["subject"], c["pushed"]) for c in repo["log"]], [("first version", False)])
        # A file git does not track yet is shown whole; one that is not staged has no staged diff.
        self.assertIn("+def brand_new():", call("GET", "/api/diff?path=app/extra.py")[1]["patch"])
        self.assertIn("+        return 2", call("GET", "/api/diff?path=app/models.py")[1]["patch"])
        self.assertEqual(call("GET", "/api/diff?path=app/models.py&staged=1")[1]["patch"], "")

        # Staging one file and committing it leaves the other where it was.
        repo = call("POST", "/api/repo", {"do": "stage", "paths": ["app/models.py"]})[1]["repo"]
        self.assertEqual((repo["staged"], repo["unstaged"]), ({"app/models.py": "modified"}, {"app/extra.py": "untracked"}))
        self.assertIn("+        return 2", call("GET", "/api/diff?path=app/models.py&staged=1")[1]["patch"])
        self.assertEqual(call("POST", "/api/repo", {"do": "commit", "message": " "})[0], 409)
        repo = call("POST", "/api/repo", {"do": "commit", "message": "raise the price"})[1]["repo"]
        self.assertEqual((repo["staged"], repo["counts"], repo["log"][0]["subject"]),
                         ({}, {"staged": 0, "unstaged": 1}, "raise the price"))
        self.assertIn("raise the price", call("GET", f"/api/diff?commit={repo['log'][0]['hash']}")[1]["patch"])
        repo = call("POST", "/api/repo", {"do": "stage"})[1]["repo"]  # no paths: everything
        self.assertEqual(repo["staged"], {"app/extra.py": "added"})
        self.assertEqual(call("POST", "/api/repo", {"do": "unstage"})[1]["repo"]["staged"], {})

        # The first push publishes the branch. After it nothing is ahead and every commit is on the remote.
        status, reply = call("POST", "/api/repo", {"do": "push"})
        self.assertEqual((status, reply["repo"]["upstream"], reply["repo"]["ahead"]), (200, "origin/main", 0))
        self.assertTrue(all(c["pushed"] for c in reply["repo"]["log"]))

        # A new branch, and back. What git refuses, and what is not git's to take, comes back as an error.
        repo = call("POST", "/api/repo", {"do": "branch", "name": "feature/x"})[1]["repo"]
        self.assertEqual((repo["branch"], repo["branches"]), ("feature/x", ["feature/x", "main"]))
        self.assertEqual(call("POST", "/api/repo", {"do": "switch", "name": "main"})[1]["repo"]["branch"], "main")
        for bad in ({"do": "switch", "name": "--orphan"}, {"do": "switch", "name": "nope"},
                    {"do": "stage", "paths": ["../outside"]}, {"do": "stage", "paths": "app"}, {"do": "rm"}):
            self.assertEqual(call("POST", "/api/repo", bad)[0], 409, bad)
        self.assertEqual(call("GET", "/api/diff?commit=--output%3Dx")[0], 409)
        self.assertEqual(call("POST", "/api/repo", {"do": "push"}, token="wrong")[0], 403)

        # Somebody else pushes; a fetch shows it and a pull fast-forwards to it.
        git("clone", "-q", remote.name, other.name, at=remote.name)
        (Path(other.name) / "elsewhere.txt").write_text("from another desk\n", encoding="utf-8")
        git("add", "-A", at=other.name)
        git("commit", "-q", "-m", "from elsewhere", at=other.name)
        git("push", "-q", at=other.name)
        self.assertEqual(call("POST", "/api/repo", {"do": "fetch"})[1]["repo"]["behind"], 1)
        repo = call("POST", "/api/repo", {"do": "pull"})[1]["repo"]
        self.assertEqual((repo["behind"], repo["log"][0]["subject"]), (0, "from elsewhere"))
        self.assertTrue((self.root / "elsewhere.txt").exists())

        # The page follows the folder by asking what changed since the state it last saw.
        stamp = call("GET", "/api/watch")[1]["stamp"]
        self.assertEqual(gitops.fingerprint(self.root), stamp)
        threading.Timer(0.3, lambda: models.write_text("# otra vez\n", encoding="utf-8")).start()
        self.assertNotEqual(call("GET", f"/api/watch?seen={stamp}")[1]["stamp"], stamp)
        # A graph built by another program is told to the page, which then loads it; one that
        # this server built itself, for a note or a rebuild asked of it, is not.
        seen = call("GET", "/api/watch")[1]
        self.assertEqual(seen["built"], 0)
        call("PUT", "/api/note", {"id": "code:app/models.py::Order", "text": "Una nota."})
        self.assertEqual(call("GET", "/api/watch")[1]["built"], 0)
        time.sleep(.05)
        pipeline.build(self.root)  # as a hook or an assistant would, outside this server
        after = call("GET", f"/api/watch?seen={urllib.parse.quote(seen['stamp'])}&built=0")[1]
        self.assertEqual(after["built"], 1)
        # The page builds the graph again when the project's files change, not when a note beside it does:
        # the answer it goes by reads no file under linkednodes-out. It is told at once, and takes the
        # graph that a rebuild hands back, or asks for it.
        self.assertEqual(after["beat"], seen["beat"])  # only a note was written since
        (self.root / "app" / "nuevo.py").write_text("def nuevo():\n    return 1\n", encoding="utf-8")
        changed = call("GET", f"/api/watch?seen={urllib.parse.quote(after['stamp'])}&built=1&beat={seen['beat']}")[1]
        self.assertNotEqual(changed["beat"], seen["beat"])
        grown = call("POST", "/api/rebuild", {"graph": True})[1]["graph"]
        self.assertIn("nuevo", [node["label"] for node in grown["nodes"]])
        self.assertEqual(call("GET", "/api/graph")[1]["graph"]["nodes"], grown["nodes"])
        self.assertEqual(call("GET", "/api/watch")[1]["built"], 1)  # told once, not at every look

    def test_desktop_window_serves_a_folder(self):
        # The window itself needs a screen; what feeds it does not.
        other, home = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        os.environ["LINKEDNODES_HOME"] = home.name  # keep the test out of the real settings
        self.addCleanup(lambda: (os.environ.pop("LINKEDNODES_HOME", None), home.cleanup()))
        self.assertEqual((desktop.recent(), desktop.settings()), ([], {}))
        app = desktop.Desktop()
        app.lang = "es"
        self.assertIn("pywebview.api.choose()", app.welcome())
        self.assertNotIn("Recientes", app.welcome())
        try:
            address = app.serve(self.root)  # no graph yet, so it builds one first
            self.assertTrue((self.out / "graph.json").exists())
            self.assertTrue(address.endswith("/?lang=es&app=1"))
            with urllib.request.urlopen(address) as reply:
                page = reply.read().decode("utf-8")
            self.assertIn(app.httpd.app.token, page)
            self.assertIn("get('lang')", page)
            (Path(other.name) / "nota.md").write_text("# Otra carpeta\n", encoding="utf-8")
            first = app.httpd
            app.serve(Path(other.name))
            self.assertEqual(app.root, Path(other.name).resolve())
            self.assertIsNot(app.httpd, first)
            # Both folders are remembered, newest first, and offered on the start page by position.
            self.assertEqual(desktop.recent(), [Path(other.name).resolve(), self.root.resolve()])
            desktop.remember(lang="en")
            self.assertEqual((desktop.language(), len(desktop.recent())), ("en", 2))
            start = app.welcome()
            self.assertIn("pywebview.api.again(1)", start)
            self.assertIn(html.escape(str(self.root.resolve())), start)
            # A port that is taken is never shared: the next server moves to another one.
            taken = server.bind(self.root, app.httpd.server_address[1])
            self.assertNotEqual(taken.server_address[1], app.httpd.server_address[1])
            taken.server_close()
        finally:
            app.close()
            other.cleanup()

    def test_reading_a_folder_is_reported_as_it_goes(self):
        seen = []
        pipeline.scan(self.root, lambda step, **facts: seen.append((step, facts)))
        result = pipeline.build(self.root, lambda step, **facts: seen.append((step, facts)))
        steps = [step for index, (step, _) in enumerate(seen) if not index or seen[index - 1][0] != step]
        self.assertEqual(steps, ["files", "code", "notes", "history", "groups", "writing", "done"])
        found = {}
        for _, facts in seen:
            found.update(facts)
        counts = result["counts"]
        self.assertEqual((found["nodes"], found["edges"], found["communities"]),
                         (counts["nodes"], counts["edges"], counts["communities"]))
        self.assertEqual((found["parsed"], found["total"], found["languages"]["python"]), (4, 4, 2))
        self.assertNotIn("commits", found)  # the folder is not a repository

    def test_desktop_window_shows_a_folder_being_read(self):
        class Window:  # stands in for the real one: it keeps what it is shown and what the page is told
            def __init__(self):
                self.pages, self.told, self.address = [], [], ""

            def load_html(self, page):
                self.pages.append(page)

            def load_url(self, address):
                self.address = address

            def evaluate_js(self, script):
                self.told.append(json.loads(script.split("window.loading(", 1)[1][:-1]))

        home = tempfile.TemporaryDirectory()
        os.environ["LINKEDNODES_HOME"] = home.name
        self.addCleanup(lambda: (os.environ.pop("LINKEDNODES_HOME", None), home.cleanup()))
        app = desktop.Desktop()
        app.lang, app.linger, app.window = "es", 0, Window()
        self.addCleanup(app.close)
        # A folder's name that would end the script it is written into is escaped.
        page = app.loading({"step": "files", "name": '</script><p id="x">'})
        self.assertNotIn('</script><p id="x">', page)
        self.assertNotIn("__ANIME__", page)
        self.assertIn("Anime.js", page)  # the animation library travels inside the page: there is no network to count on

        app.show(self.root)  # no graph yet: the folder is read with the window following along
        self.assertIn("Leyendo la carpeta", app.window.pages[0])
        self.assertTrue(app.window.address.endswith("/?lang=es&app=1"))
        last = app.window.told[-1]
        counts = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))["analysis"]["counts"]
        self.assertEqual((last["step"], last["name"], last["nodes"], last["edges"]),
                         ("done", self.root.name, counts["nodes"], counts["edges"]))
        self.assertFalse(app.reading)

        # An answer for a page that has left is dropped; a script of ours that fails still says so.
        def gone(script):
            raise ValueError("no such function in the page")

        answer = 'window.pywebview._returnValuesCallbacks["zoomed"]["1"]({value: "false"})'
        self.assertIsNone(desktop.forgiving(gone)(answer))
        self.assertRaises(ValueError, desktop.forgiving(gone), "window.loading({})")
        self.assertEqual(desktop.forgiving(lambda script: 7)(answer), 7)

        app.show(self.root)  # with its graph on disk there is nothing to read, so no such page
        self.assertIn("Abriendo el grafo", app.window.pages[-1])
        told = len(app.window.told)
        app.rebuild()  # asked for from the menu, it is read again, and shown as that
        self.assertIn("Reconstruyendo el grafo", app.window.pages[-1])
        self.assertEqual(app.window.told[-1]["step"], "done")
        self.assertGreater(len(app.window.told), told)

    def test_github_sign_in_and_repositories(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        home, hub = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        state = Path(home.name)
        os.environ["LINKEDNODES_HOME"] = home.name  # the token goes under a name of its own, away from a real one
        before = (github.SITE, github.API, github.CLIENT_ID)

        def restore():
            github.forget(state)
            github.SITE, github.API, github.CLIENT_ID = before
            os.environ.pop("LINKEDNODES_HOME", None)
            home.cleanup()
            hub.cleanup()

        self.addCleanup(restore)
        github.CLIENT_ID = ""  # with no registration there is nothing to offer
        self.assertNotIn("GitHub", desktop.Desktop().welcome())

        def git(*args, at=self.root):
            subprocess.run(["git", "-C", str(at), "-c", "user.name=Ana", "-c", "user.email=ana@example.com",
                            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-q", "-m", "first version")
        bare = Path(hub.name) / "ana" / "shop.git"
        git("clone", "-q", "--bare", str(self.root), str(bare))
        git("update-server-info", at=bare)
        FakeGitHub.files, FakeGitHub.polls, FakeGitHub.carried = Path(hub.name), 0, []
        FakeGitHub.verdict = {"access_token": FakeGitHub.token, "scope": "repo"}
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        site = f"http://127.0.0.1:{httpd.server_address[1]}"
        github.SITE, github.API, github.CLIENT_ID = site, site + "/api", "client-id"
        desktop.remember(workspace=str(state / "repos"), lang="en")

        app, opened = desktop.Desktop(), []
        app.browse = opened.append
        self.assertFalse(app.asking)
        self.assertIn("pywebview.api.signin()", app.welcome())
        app.signin()  # the code is approved by the second time GitHub is asked
        self.assertEqual((opened, FakeGitHub.polls), ([site + "/login/device"], 2))
        self.assertEqual(github.load(state), FakeGitHub.token)
        self.assertFalse((state / "settings.json").read_text(encoding="utf-8").count(FakeGitHub.token))
        self.assertEqual((app.user["login"], [r["name"] for r in app.repos]), ("ana", ["ana/shop", "org/secret"]))
        page = app.welcome()
        self.assertIn("Ana &lt;Pérez&gt; (@ana)", page)
        self.assertIn("pywebview.api.repo(1)", page)
        self.assertIn("<i>private</i><i>archived</i>", page)
        self.assertNotIn("on this computer", page)
        self.assertNotIn(FakeGitHub.token, page)

        # Opening a repository clones it into the workspace, once, with the token sent but not kept.
        folder = app.fetch(0)
        self.assertEqual(folder, state / "repos" / "ana" / "shop")
        self.assertTrue((folder / "app" / "models.py").exists())
        self.assertIn("Basic " + base64.b64encode(f"x-access-token:{FakeGitHub.token}".encode()).decode(), FakeGitHub.carried)
        self.assertNotIn("extraheader", (folder / ".git" / "config").read_text(encoding="utf-8").lower())
        asked = len(FakeGitHub.carried)
        self.assertEqual((app.fetch(0), len(FakeGitHub.carried)), (folder, asked))
        self.assertIn("<i>on this computer</i>", app.welcome())
        for bad in (1, 2, "0", None):  # an address that is not GitHub's, and positions that name nothing
            with self.assertRaises(ValueError):
                app.fetch(bad)

        # The next start finds the token, and with it the account and what is already cloned.
        later = desktop.Desktop()
        self.assertTrue(later.asking)
        self.assertIn("Asking GitHub", later.welcome())
        later.account()
        self.assertEqual((later.asking, later.user["login"], later.local), (False, "ana", {"ana/shop": folder}))
        # A token revoked on github.com signs the account out here and is forgotten.
        FakeGitHub.token = "another"
        self.addCleanup(lambda: setattr(FakeGitHub, "token", "token-123"))
        later.account()
        self.assertEqual((later.user, later.trouble, github.load(state)), (None, "", ""))
        self.assertIn("pywebview.api.signin()", later.welcome())

        # A sign-in that GitHub refuses says why; one that is given up says nothing; signing out forgets.
        FakeGitHub.polls, FakeGitHub.verdict = 0, {"error": "access_denied", "error_description": "The user said no."}
        app.signout()
        app.signin()
        self.assertEqual((app.user, app.code, app.trouble, github.load(state)), (None, None, "The user said no.", ""))
        FakeGitHub.polls, FakeGitHub.verdict = -10 ** 6, {}
        waiting = threading.Thread(target=app.signin)
        waiting.start()
        for _ in range(500):  # until the code is on show
            if app.code:
                break
            time.sleep(.01)
        self.assertIn("ABCD-1234", app.welcome())
        app.cancel()
        waiting.join(10)
        self.assertEqual((waiting.is_alive(), app.code, app.trouble), (False, None, ""))

    def test_folder_without_git_has_no_git_data(self):
        self.answer_all()
        self.assertNotIn("git", json.loads((self.out / "graph.json").read_text(encoding="utf-8")))

    def test_slow_answers_are_kept_for_the_same_graph(self):
        import networkx as nx
        asked = []
        work = lambda: asked.append(1) or len(asked)
        first = nx.Graph()
        first.add_edge("a", "b", weight=1.0)
        same = nx.Graph()
        same.add_edge("a", "b", weight=1.0)
        other = nx.Graph()
        other.add_edge("a", "b", weight=.5)
        answers = [graph._recall("test", g, work) for g in (first, same, other, other, first)]
        self.assertEqual(answers, [1, 1, 2, 2, 3])  # worked out again only when the graph is another one
        # A build that changes no structure gives the same graph as the one before it.
        self.answer_all()
        before = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))
        annotations.write_props(self.out, "code:app/models.py::Order", {"summary": "Un pedido."})
        pipeline.build(self.root)
        after = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))
        self.assertEqual([c["top"] for c in before["communities"]], [c["top"] for c in after["communities"]])
        self.assertEqual(before["analysis"]["connectors"], after["analysis"]["connectors"])

    def test_labels_name_communities(self):
        self.answer_all()
        first = pipeline.build(self.root)["communities"][0]
        (self.out / "labels.json").write_text(json.dumps({first["anchor"]: "My name"}), encoding="utf-8")
        again = pipeline.build(self.root)["communities"][0]
        self.assertEqual((again["label"], again["named"]), ("My name", True))

    def answer_all(self):
        pipeline.scan(self.root)
        self.answer(json.loads((self.out / ".work" / "todo.json").read_text(encoding="utf-8")))
        pipeline.build(self.root)


class ParserTest(unittest.TestCase):
    def test_url_normalisation(self):
        self.assertEqual(saved.normalize_url("https://WWW.Example.com/a/?utm_source=x&id=2#top"),
                         "https://example.com/a?id=2")

    def test_csv_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pocket.csv"
            path.write_text("title,url,time_added,tags,status\n"
                            "A post,https://a.com/x,1700000000,rust|web,unread\n", encoding="utf-8")
            item = saved.parse(path, "pocket.csv")[0]
            self.assertEqual((item.title, item.tags, item.added), ("A post", ["rust", "web"], "2023-11-14"))
            path.write_text("Video ID,Playlist Video Creation Timestamp\nabc123,2024-01-02T10:00:00\n",
                            encoding="utf-8")
            item = saved.parse(path, "pocket.csv")[0]
            self.assertEqual((item.url, item.added), ("https://www.youtube.com/watch?v=abc123", "2024-01-02"))

    def test_ordinary_files_are_not_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in {"page.html": "<html><a href='https://a.com'>a</a></html>",
                               "data.json": '{"url": "https://a.com", "items": [1, 2]}',
                               "notes.txt": "A note.\nhttps://a.com\nMore prose.\nAnd more.\n"}.items():
                path = Path(tmp) / name
                path.write_text(text, encoding="utf-8")
                self.assertEqual(saved.parse(path, name), [], name)

    def languages(self, lang, rel, text):
        info = code._parse_generic(rel, lang, text)
        return {d.qual: d.kind for d in info.defs}, info

    def test_other_languages(self):
        defs, info = self.languages("go", "m.go", (
            'package main\n\nimport (\n\t"fmt"\n\t"github.com/acme/kit/log"\n)\n\n'
            "type Server struct {\n\tport int\n}\n\n"
            "func (s *Server) Start() error {\n\treturn listen(s.port)\n}\n\n"
            "func listen(port int) error {\n\treturn nil\n}\n"))
        self.assertEqual(defs, {"Server": "class", "Server.Start": "method", "listen": "function"})
        self.assertIn("github.com/acme/kit/log", [i.module for i in info.imports])
        self.assertIn(("Server.Start", "listen"), [(c.caller, c.name) for c in info.calls])

        defs, info = self.languages("rust", "m.rs", (
            "use serde::Serialize;\nuse std::fmt;\n\npub struct Cart {\n    items: Vec<u32>,\n}\n\n"
            "impl fmt::Display for Cart {\n    fn fmt(&self) -> String {\n        self.total()\n    }\n}\n\n"
            "impl Cart {\n    pub fn total(&self) -> u32 {\n        sum(&self.items)\n    }\n}\n\n"
            "pub async fn sum(v: &[u32]) -> u32 {\n    0\n}\n"))
        self.assertEqual(defs, {"Cart": "class", "Cart.fmt": "method", "Cart.total": "method",
                                "sum": "function"})
        self.assertEqual(info.bases, [("Cart", "fmt::Display", "implements")])
        self.assertEqual([i.module for i in info.imports], ["serde", "std"])

        defs, info = self.languages("java", "A.java", (
            "import com.acme.Base;\n\npublic class Account extends Base {\n"
            "    private int balance = compute(1);\n\n"
            "    public Account(int start) {\n        this.balance = start;\n    }\n\n"
            "    public static int compute(int x) {\n        return helper(x);\n    }\n}\n"))
        self.assertEqual(defs, {"Account": "class", "Account.compute": "method"})
        self.assertEqual(info.bases, [("Account", "Base", "inherits")])

        defs, _ = self.languages("csharp", "A.cs", (
            "namespace Shop\n{\n    public class Basket : IBasket\n    {\n"
            "        public async Task<int> CountAsync(int x)\n        {\n            return x;\n        }\n"
            "    }\n}\n"))
        self.assertEqual(defs, {"Basket": "class", "Basket.CountAsync": "method"})

        defs, info = self.languages("ruby", "a.rb", (
            "require_relative 'base'\nrequire 'json'\n\nclass Invoice < Base\n"
            "  def total\n    compute_tax(1)\n  end\n\n  def self.build\n    new\n  end\nend\n"))
        self.assertEqual(defs, {"Invoice": "class", "Invoice.total": "method", "Invoice.build": "method"})
        self.assertEqual([(i.module, i.relative) for i in info.imports], [("base", True), ("json", False)])

        defs, info = self.languages("php", "a.php", (
            "<?php\nuse App\\Models\\User;\n\nclass Mailer extends Transport\n{\n"
            "    public function send($to)\n    {\n        return $this->render($to);\n    }\n"
            "    private function render($to)\n    {\n        return '';\n    }\n}\n"))
        self.assertEqual(defs, {"Mailer": "class", "Mailer.send": "method", "Mailer.render": "method"})
        self.assertIn(("Mailer.send", "render", "this"), [(c.caller, c.name, c.base) for c in info.calls])

    def test_typescript_from_a_real_project(self):
        # Shapes that a NestJS and Next.js monorepo uses and the first version missed.
        defs, info = self.languages("typescript", "c.ts", (
            "@Controller('parejas')\nexport class ParejasController {\n"
            "  constructor(private readonly parejas: ParejasService) {}\n\n"
            "  @Post()\n"
            "  crear(@Body() dto: CrearDto, @Sesion() sesion: SesionActual) {\n"
            "    return this.parejas.crearPorQr(dto);\n  }\n\n"
            "  @Get(':id')\n"
            "  async buscar(\n    @Param('id') id: string,\n  ): Promise<Pareja> {\n"
            "    return this.parejas.buscarUna(id);\n  }\n}\n"))
        self.assertEqual(defs, {"ParejasController": "class", "ParejasController.crear": "method",
                                "ParejasController.buscar": "method"})
        callers = {c.name: c.caller for c in info.calls}
        self.assertEqual(callers["crearPorQr"], "ParejasController.crear")
        self.assertEqual(callers["buscarUna"], "ParejasController.buscar")

        files = [{"path": p, "kind": "code", "lang": "typescript"} for p in
                 ("apps/web/src/lib/formato.ts", "apps/web/src/app/(app)/panel/page.tsx")]
        with tempfile.TemporaryDirectory() as tmp:
            for record, text in zip(files, (
                    "export function nombreCompleto(j: Jugador) {\n  return j.nombre;\n}\n",
                    "import { nombreCompleto } from '@/lib/formato';\n\n"
                    "export default function Panel() {\n  return nombreCompleto(yo);\n}\n")):
                path = Path(tmp) / record["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            _, edges = code.extract(Path(tmp), files)
        found = {(e["source"], e["target"], e["relation"]): e["score"] for e in edges}
        page = "code:apps/web/src/app/(app)/panel/page.tsx"
        self.assertIn((page, "code:apps/web/src/lib/formato.ts", "imports"), found)
        self.assertEqual(found[(page + "::Panel", "code:apps/web/src/lib/formato.ts::nombreCompleto",
                                "calls")], 0.8)

    def test_javascript_details(self):
        defs, info = self.languages("javascript", "a.jsx", (
            "import React, { useState as useS } from 'react';\n"
            "import * as api from './api';\n"
            "const x = require('./legacy');\n\n"
            "export default function Page() {\n"
            "  const onClick = () => {\n    api.load();\n  };\n"
            "  if (x) {\n    return null;\n  }\n"
            "  return render(onClick);\n}\n"))
        self.assertEqual(defs, {"Page": "function"})
        names = {i.module: i.names for i in info.imports}
        self.assertEqual(names["react"], {"useS": "useState", "React": "default"})
        self.assertEqual(names["./api"], {"api": None})
        self.assertIn("./legacy", names)
        self.assertIn(("Page", "load", "api"), [(c.caller, c.name, c.base) for c in info.calls])


def browser() -> str | None:
    """A Chromium on this machine, which can run the viewer without showing a window."""
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    places = [Path(os.environ[key]) for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA") if os.environ.get(key)]
    found = [place / tail for place in places
             for tail in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe")]
    found.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    return next((str(path) for path in found if path.is_file()), None)


class ViewerTest(unittest.TestCase):
    # Once the page has had time to lay the graph out, it counts what its canvas shows and says so.
    COUNT = """<script>
    setTimeout(() => {
      const canvas = document.getElementById('canvas');
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let painted = 0;
      for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) painted++;
      fetch('/painted?count=' + painted);
    }, 1500);
    </script>"""

    def painted(self, data: dict) -> int:
        """How much of the canvas the viewer has drawn on for a graph, in pixels.

        The page is served to a browser that shows no window, and reports back to the same
        server. That asks nothing of the browser but to load a page, which every one of
        them does the same way; having it print the page instead hangs Chrome on macOS.
        """
        program = browser()
        if not program:
            # Where the tests are run for everyone, a missing browser must not pass for a passing test.
            self.assertFalse(os.environ.get("LINKEDNODES_NEED_BROWSER"), "no Chromium browser was found")
            self.skipTest("no Chromium browser is installed")
        page, told, heard = (pipeline._viewer("test", data) + self.COUNT).encode("utf-8"), {}, threading.Event()

        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = page
                if self.path.startswith("/painted?count="):
                    told["painted"], body = int(self.path.split("=", 1)[1]), b"noted"
                    heard.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):  # the browser is stopped mid-request at the end
                pass

        httpd = Quiet(("127.0.0.1", 0), Page)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            # What the browser prints goes to a file: its helper processes outlive it for a moment
            # and would keep a pipe open.
            with open(Path(folder) / "browser.log", "wb") as log:
                shown = subprocess.Popen(
                    [program, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                     "--no-default-browser-check", "--use-mock-keychain", "--disable-extensions",
                     f"--user-data-dir={Path(folder) / 'profile'}", "--window-size=1200,800",
                     f"http://127.0.0.1:{httpd.server_address[1]}/"], stdout=log, stderr=log)
                try:
                    arrived = heard.wait(90)
                finally:
                    shown.kill()
                    shown.wait(30)
                    httpd.shutdown()
                    httpd.server_close()
            said = (Path(folder) / "browser.log").read_bytes().decode("utf-8", "replace")
        self.assertTrue(arrived, f"the page never said what it drew; the browser said: {said[-400:]}")
        return told["painted"]

    def test_a_node_with_hundreds_of_links_is_still_drawn(self):
        # Its springs once threw the layout so far apart that nothing was left on the canvas.
        nodes = [{"id": "hub", "label": "hub", "kind": "file", "source_file": "hub.py", "community": 0, "degree": 200}]
        edges = []
        for index in range(200):
            nodes.append({"id": f"leaf{index}", "label": f"leaf {index}", "kind": "function", "source_file": "hub.py",
                          "community": index % 3, "degree": 1})
            edges.append({"source": "hub", "target": f"leaf{index}", "relation": "contains", "confidence": "EXTRACTED",
                          "score": 1.0, "source_file": "hub.py"})
        communities = [{"id": index, "label": f"group {index}", "size": 67} for index in range(3)]
        self.assertGreater(self.painted({"nodes": nodes, "edges": edges, "communities": communities}), 5000)

    def test_a_large_graph_is_drawn(self):
        # Past a thousand nodes the layout takes the far ones a cell at a time; it must still end on screen.
        nodes = [{"id": f"n{index}", "label": f"node {index}", "kind": "function", "source_file": f"f{index % 40}.py",
                  "community": index % 12, "degree": 2} for index in range(1600)]
        edges = [{"source": f"n{index}", "target": f"n{(index * 7 + 12) % 1600}", "relation": "calls",
                  "confidence": "INFERRED", "score": .6, "source_file": ""} for index in range(1600)]
        communities = [{"id": index, "label": f"group {index}", "size": 133} for index in range(12)]
        self.assertGreater(self.painted({"nodes": nodes, "edges": edges, "communities": communities}), 5000)


if __name__ == "__main__":
    unittest.main()
