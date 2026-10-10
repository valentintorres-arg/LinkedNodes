"""The graph in a window of its own: no browser tab and no terminal left open.

The window is drawn by the web view that the system already has (WebView2 on Windows),
through pywebview, so nothing as large as a browser is shipped. Inside, it is the same
viewer talking to the same local server, started and stopped with the window.
"""
from __future__ import annotations

import html
import locale
import os
import threading
import traceback
from pathlib import Path

from . import pipeline
from .detect import OUT_DIR
from .server import bind
from .util import read_json, write_json

PORT = 8765  # kept stable when possible: the viewer's settings are stored per address
RECENT = 8
PAGE = """<!doctype html><meta charset="utf-8">
<style>
  body {{ margin: 0; height: 100vh; display: grid; place-items: center; background: #0b0c0e; color: #fff;
         font: 14px/1.4 "Helvetica Neue", Helvetica, Inter, Arial, sans-serif; }}
  main {{ width: min(560px, 86vw); }}
  h1 {{ font-size: 34px; font-weight: 400; letter-spacing: -.02em; margin: 0 0 6px; }}
  p {{ color: #b4b7bc; margin: 0 0 22px; }}
  h2 {{ font-size: 10px; font-weight: 500; text-transform: uppercase; letter-spacing: .11em;
        color: #7d8187; margin: 30px 0 8px; }}
  button {{ font: inherit; color: #fff; background: none; border: 1px solid rgba(255, 255, 255, .32);
            height: 36px; padding: 0 16px; cursor: pointer; }}
  button.main {{ background: #fff; color: #000; border-color: #fff; }}
  button.row {{ display: block; width: 100%; height: auto; padding: 10px 6px; border: 0; text-align: left;
                border-top: 1px solid rgba(255, 255, 255, .12); }}
  button.row:hover {{ background: rgba(255, 255, 255, .08); }}
  button.row small {{ display: block; color: #7d8187; overflow-wrap: anywhere; }}
  button:focus-visible {{ outline: 2px solid #b9a8ff; outline-offset: 2px; }}
</style>
<main>{body}</main>"""


def data_folder() -> Path:
    """Where the window keeps its own state: its settings, the web view's storage, a log."""
    elsewhere = os.environ.get("LINKEDNODES_HOME")  # lets tests and portable copies keep it apart
    return Path(elsewhere) if elsewhere else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LinkedNodes"


def settings() -> dict:
    found = read_json(data_folder() / "settings.json")
    return found if isinstance(found, dict) else {}


def remember(**changes) -> None:
    write_json(data_folder() / "settings.json", {**settings(), **changes})


def language() -> str:
    """The language chosen in the window's menu, or else the system's."""
    chosen = settings().get("lang")
    if chosen in ("es", "en"):
        return chosen
    name = (locale.getlocale()[0] or os.environ.get("LANG") or "").lower()
    return "es" if name.startswith(("es", "spanish")) else "en"


def recent() -> list[Path]:
    """Folders opened before that are still there, newest first."""
    found = [Path(p) for p in settings().get("recent", []) if isinstance(p, str)]
    return [p for p in found if p.is_dir()]


class Desktop:
    def __init__(self):
        self.window = None
        self.httpd = None
        self.root: Path | None = None
        self.lang = language()

    def say(self, english: str, castellano: str) -> str:
        return castellano if self.lang == "es" else english

    @property
    def address(self) -> str:
        # The web view reports the system's language, so the viewer is told which one to use.
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/?lang={self.lang}"

    def serve(self, root: Path) -> str:
        """Start a server for a folder, building its graph first if it has none."""
        root = root.resolve()
        if not (root / OUT_DIR / "graph.json").exists():
            pipeline.scan(root)
            pipeline.build(root)
        httpd = bind(root, PORT)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        old, self.httpd, self.root = self.httpd, httpd, root
        if old:
            old.shutdown()
            old.server_close()
        remember(recent=[str(root), *(str(p) for p in recent() if p != root)][:RECENT])
        return self.address

    def message(self, text: str) -> str:
        return PAGE.format(body=f"<p>{html.escape(text)}</p>")

    def welcome(self) -> str:
        """The page shown when there is no folder to open: pick one, or go back to a recent one."""
        rows = "".join(
            f"<button class='row' onclick='pywebview.api.again({index})'>{html.escape(path.name)}"
            f"<small>{html.escape(str(path))}</small></button>"
            for index, path in enumerate(recent()))
        return PAGE.format(body=(
            "<h1>LinkedNodes</h1>"
            f"<p>{self.say('Open a folder to see its code, notes, saved links and images as a graph.', 'Abrí una carpeta para ver su código, notas, enlaces guardados e imágenes como un grafo.')}</p>"
            f"<button class='main' onclick='pywebview.api.choose()'>{self.say('Open folder...', 'Abrir carpeta...')}</button>"
            + (f"<h2>{self.say('Recent', 'Recientes')}</h2>{rows}" if rows else "")))

    def show(self, root: Path) -> None:
        """Point the window at a folder; runs off the main thread, so it may take its time."""
        self.window.load_html(self.message(self.say(
            "Reading the folder. The first time takes longer.", "Leyendo la carpeta. La primera vez tarda más.")))
        try:
            address = self.serve(root)
        except Exception as error:  # the window must say what went wrong rather than stay blank
            self.window.load_html(self.message(f"{type(error).__name__}: {error}"))
            return
        self.window.set_title(f"{self.root.name} - LinkedNodes")
        self.window.load_url(address)

    def home(self) -> None:
        self.window.set_title("LinkedNodes")
        self.window.load_html(self.welcome())

    def choose(self) -> None:
        import webview
        picked = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        if picked:
            self.show(Path(picked[0]))

    def again(self, index) -> None:
        """Reopen a recent folder. The page names it by position, never by a path of its own."""
        folders = recent()
        if isinstance(index, int) and 0 <= index < len(folders):
            self.show(folders[index])

    def rebuild(self) -> None:
        if self.root:
            pipeline.scan(self.root)
            pipeline.build(self.root)
            self.window.load_url(self.address)

    def speak(self, lang: str) -> None:
        """Switch the viewer's language now; the menu itself follows on the next start."""
        self.lang = lang
        remember(lang=lang)
        if self.httpd:
            self.window.load_url(self.address)
        else:
            self.home()

    def close(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class _Api:
    """What a page may ask of the program: nothing but picking a folder to open."""

    def __init__(self, app: Desktop):
        self._app = app

    def choose(self) -> None:
        self._app.choose()

    def again(self, index) -> None:
        self._app.again(index)


def run(root: Path | None = None) -> None:
    """Open the window on a folder; without one, on the last folder used or the start page."""
    try:
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator
    except ImportError:
        raise SystemExit("error: the desktop window needs pywebview. Install it with: pip install pywebview")
    app = Desktop()
    start = root or next(iter(recent()), None)
    look = dict(width=1440, height=900, min_size=(900, 600), background_color="#0b0c0e", text_select=True,
                js_api=_Api(app))
    if start:
        address = app.serve(start)
        app.window = webview.create_window(f"{app.root.name} - LinkedNodes", address, **look)
    else:
        app.window = webview.create_window("LinkedNodes", html=app.welcome(), **look)
    menu = [
        Menu(app.say("File", "Archivo"), [
            MenuAction(app.say("Open folder...", "Abrir carpeta..."), app.choose),
            MenuAction(app.say("Recent folders", "Carpetas recientes"), app.home),
            MenuAction(app.say("Rebuild the graph", "Reconstruir el grafo"), app.rebuild),
            MenuSeparator(),
            MenuAction(app.say("Exit", "Salir"), lambda: app.window.destroy()),
        ]),
        Menu(app.say("Language", "Idioma"), [
            MenuAction("Español", lambda: app.speak("es")),
            MenuAction("English", lambda: app.speak("en")),
        ]),
    ]
    storage = data_folder() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        # Not private, and with a folder of its own, so the viewer's settings survive.
        webview.start(menu=menu, private_mode=False, storage_path=str(storage))
    finally:
        app.close()


def report(error: BaseException) -> None:
    """Without a console there is nowhere to print, so log the failure and show a dialog."""
    folder = data_folder()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "app.log").write_text("".join(traceback.format_exception(error)), encoding="utf-8")
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{error}\n\n{folder / 'app.log'}", "LinkedNodes", 0x10)
