"""End-to-end test: a small folder with every kind of content goes in, a graph comes out."""
import html
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from linkednodes import annotations, cli, code, desktop, detect, gitinfo, hooks, pipeline, saved, server
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

    def test_server_edits_and_refuses(self):
        self.answer_all()
        httpd = server.make_server(self.root)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port, token = httpd.server_address[1], httpd.app.token
        target = "code:app/models.py::Order"

        def call(method, path, body=None, token=token, host=None):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-LinkedNodes-Token": token, **({"Host": host} if host else {})})
            try:
                with urllib.request.urlopen(request) as reply:
                    raw = reply.read()
                    return reply.status, raw if path == "/" else json.loads(raw)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        try:
            status, page = call("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(token.encode(), page)
            # Without the token, or under another site's name, nothing is served.
            self.assertEqual(call("GET", f"/api/node?id={target}", token="wrong")[0], 403)
            self.assertEqual(call("GET", "/", host="evil.example")[0], 403)

            status, reply = call("PUT", "/api/note", {"id": target, "text": "Hecho a mano."})
            self.assertEqual((status, reply["doc"]), (200, "Hecho a mano."))
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
        finally:
            httpd.shutdown()
            httpd.server_close()

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
            self.assertTrue(address.endswith("/?lang=es"))
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

    def test_folder_without_git_has_no_git_data(self):
        self.answer_all()
        self.assertNotIn("git", json.loads((self.out / "graph.json").read_text(encoding="utf-8")))

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


if __name__ == "__main__":
    unittest.main()
