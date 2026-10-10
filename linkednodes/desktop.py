"""The graph in a window of its own: no browser tab and no terminal left open.

The window is drawn by the web view that the system already has (WebView2 on Windows),
through pywebview, so nothing as large as a browser is shipped. Inside, it is the same
viewer talking to the same local server, started and stopped with the window.

Its start page is also where a GitHub account comes in: sign in, and the repositories
the account reaches are listed there to open, cloned first when they are not on this
computer yet.
"""
from __future__ import annotations

import html
import locale
import os
import re
import threading
import traceback
import webbrowser
from pathlib import Path

from . import github, gitops, pipeline
from .detect import OUT_DIR
from .server import bind
from .util import read_json, write_json

PORT = 8765  # kept stable when possible: the viewer's settings are stored per address
RECENT = 8
REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
PAGE = """<!doctype html><meta charset="utf-8">
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0b0c0e; color: #fff;
         font: 14px/1.4 "Helvetica Neue", Helvetica, Inter, Arial, sans-serif; }}
  main {{ width: min(560px, 86vw); padding: 48px 0; }}
  h1 {{ font-size: 34px; font-weight: 400; letter-spacing: -.02em; margin: 0 0 6px; }}
  p {{ color: #b4b7bc; margin: 0 0 22px; }}
  p.bad {{ color: #ff9c9c; }}
  p.code {{ font: 30px/1 "Cascadia Code", Consolas, Menlo, monospace; letter-spacing: .12em; color: #fff;
            user-select: all; }}
  p.who {{ display: flex; align-items: center; gap: 10px; color: #fff; margin-bottom: 14px; }}
  p.who img {{ width: 28px; height: 28px; border-radius: 50%; }}
  p.who span {{ flex: 1; }}
  h2 {{ font-size: 10px; font-weight: 500; text-transform: uppercase; letter-spacing: .11em;
        color: #7d8187; margin: 30px 0 8px; }}
  button, a.button, input {{ font: inherit; color: #fff; background: none; border: 1px solid rgba(255, 255, 255, .32);
                            height: 36px; padding: 0 16px; }}
  button, a.button {{ cursor: pointer; }}
  a.button {{ display: inline-block; line-height: 34px; text-decoration: none; }}
  button.main, a.button {{ background: #fff; color: #000; border-color: #fff; }}
  button.quiet {{ height: 28px; padding: 0 10px; color: #b4b7bc; }}
  input {{ display: block; width: 100%; box-sizing: border-box; padding: 0 12px; margin-bottom: 8px; }}
  input:focus {{ outline: none; border-color: #fff; }}
  button.row {{ display: block; width: 100%; height: auto; padding: 10px 6px; border: 0; text-align: left;
                border-top: 1px solid rgba(255, 255, 255, .12); }}
  button.row[hidden] {{ display: none; }}
  button.row:hover {{ background: rgba(255, 255, 255, .08); }}
  button.row small {{ display: block; color: #7d8187; overflow-wrap: anywhere; }}
  button.row i {{ font-size: 10px; font-weight: 500; font-style: normal; text-transform: uppercase; letter-spacing: .11em;
                  color: #b4b7bc; border: 1px solid rgba(255, 255, 255, .12); padding: 3px 5px; margin-left: 8px; }}
  button:focus-visible, a:focus-visible, input:focus-visible {{ outline: 2px solid #b9a8ff; outline-offset: 2px; }}
</style>
<main>{body}</main>"""
FILTER = ("const asked = this.value.toLowerCase();"
          "for (const row of document.querySelectorAll('.repo')) row.hidden = !row.dataset.name.includes(asked)")


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


def workspace() -> Path:
    """Where repositories from GitHub are cloned, as owner/name: the folder in the settings, or one at home."""
    chosen = settings().get("workspace")
    return Path(chosen) if isinstance(chosen, str) and chosen else Path.home() / "LinkedNodes"


class Desktop:
    def __init__(self):
        self.window = None
        self.httpd = None
        self.root: Path | None = None
        self.lang = language()
        self.at_home = False  # the window shows the start page, so news about the account redraws it
        self.browse = webbrowser.open
        # GitHub: who is signed in, what they reach, and which of it is already on this computer.
        self.user: dict | None = None
        self.repos: list[dict] = []
        self.local: dict[str, Path] = {}
        self.code: dict | None = None  # a sign-in under way: the code to type, and where
        self.asking = bool(github.CLIENT_ID and github.load(data_folder()))  # GitHub has yet to say who this is
        self.trouble = ""
        self.given_up = threading.Event()

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

    def message(self, text: str, back: bool = False) -> str:
        home = f"<button onclick='pywebview.api.home()'>{self.say('Back', 'Volver')}</button>" if back else ""
        return PAGE.format(body=f"<p>{html.escape(text)}</p>{home}")

    def account_section(self) -> str:
        """The part of the start page about GitHub: the way in, the code to type, or the repositories."""
        if not github.CLIENT_ID:
            return ""
        say = self.say
        head = "<h2>GitHub</h2>" + (f"<p class='bad'>{html.escape(self.trouble)}</p>" if self.trouble else "")
        if self.code:
            return head + (
                f"<p>{say('Type this code on GitHub to sign in. Its page should already be open in your browser.', 'Escribí este código en GitHub para iniciar sesión. La página ya debería estar abierta en tu navegador.')}</p>"
                f"<p class='code'>{html.escape(self.code['code'])}</p>"
                f"<a class='button' target='_blank' href='{html.escape(self.code['where'], quote=True)}'>{say('Open GitHub', 'Abrir GitHub')}</a> "
                f"<button onclick='pywebview.api.cancel()'>{say('Cancel', 'Cancelar')}</button>")
        if self.asking:
            return head + f"<p>{say('Asking GitHub for your repositories...', 'Consultando tus repositorios en GitHub...')}</p>"
        if not self.user:
            return head + f"<button onclick='pywebview.api.signin()'>{say('Sign in with GitHub', 'Iniciar sesión con GitHub')}</button>"
        face = self.user["avatar"]
        who = (f"<p class='who'>" + (f"<img alt='' src='{html.escape(face, quote=True)}'>" if face.startswith("https://") else "")
               + f"<span>{html.escape(self.user['name'] or self.user['login'])} (@{html.escape(self.user['login'])})</span>"
               f"<button class='quiet' onclick='pywebview.api.refresh()'>{say('Refresh', 'Actualizar')}</button>"
               f"<button class='quiet' onclick='pywebview.api.signout()'>{say('Sign out', 'Cerrar sesión')}</button></p>")
        rows = ""
        for index, item in enumerate(self.repos):
            marks = "".join(f"<i>{mark}</i>" for mark, on in (
                (say("private", "privado"), item["private"]), (say("archived", "archivado"), item["archived"]),
                (say("on this computer", "en este equipo"), item["name"].lower() in self.local)) if on)
            about = " · ".join(part for part in (item["about"], item["pushed"]) if part)
            rows += (f"<button class='row repo' data-name='{html.escape(item['name'].lower(), quote=True)}' "
                     f"onclick='pywebview.api.repo({index})'>{html.escape(item['name'])}{marks}"
                     f"<small>{html.escape(about)}</small></button>")
        if not rows:
            return head + who + f"<p>{say('This account reaches no repositories yet.', 'Esta cuenta todavía no tiene repositorios.')}</p>"
        find = say("Filter repositories", "Filtrar repositorios")
        return head + who + f"<input type='search' placeholder='{find}' aria-label='{find}' oninput=\"{FILTER}\">" + rows

    def welcome(self) -> str:
        """The start page: pick a folder, go back to a recent one, or open a repository from GitHub."""
        rows = "".join(
            f"<button class='row' onclick='pywebview.api.again({index})'>{html.escape(path.name)}"
            f"<small>{html.escape(str(path))}</small></button>"
            for index, path in enumerate(recent()))
        return PAGE.format(body=(
            "<h1>LinkedNodes</h1>"
            f"<p>{self.say('Open a folder to see its code, notes, saved links and images as a graph.', 'Abrí una carpeta para ver su código, notas, enlaces guardados e imágenes como un grafo.')}</p>"
            f"<button class='main' onclick='pywebview.api.choose()'>{self.say('Open folder...', 'Abrir carpeta...')}</button>"
            + (f"<h2>{self.say('Recent', 'Recientes')}</h2>{rows}" if rows else "")
            + self.account_section()))

    def show(self, root: Path) -> None:
        """Point the window at a folder; runs off the main thread, so it may take its time."""
        self.at_home = False
        self.window.load_html(self.message(self.say(
            "Reading the folder. The first time takes longer.", "Leyendo la carpeta. La primera vez tarda más.")))
        try:
            address = self.serve(root)
        except Exception as error:  # the window must say what went wrong rather than stay blank
            self.window.load_html(self.message(f"{type(error).__name__}: {error}", back=True))
            return
        self.window.load_url(address)

    def home(self) -> None:
        self.at_home = True
        self.window.load_html(self.welcome())

    def redraw(self) -> None:
        """Draw the start page again, if that is what the window is showing."""
        if self.window and self.at_home:
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
        if self.httpd and not self.at_home:
            self.window.load_url(self.address)
        else:
            self.home()

    # ---- GitHub. Each of these may wait on the network, so none runs on the main thread.
    def clones(self) -> dict[str, Path]:
        """Repositories already on this computer, by 'owner/name': cloned here, or opened before."""
        found = {f"{folder.parent.name}/{folder.name}".lower(): folder
                 for folder in workspace().glob("*/*") if (folder / ".git").exists()}
        for folder in recent():
            name = github.repo_name(gitops.origin(folder))
            if name:
                found.setdefault(name, folder)
        return found

    def account(self) -> None:
        """Ask GitHub who the stored token belongs to, and what it reaches."""
        try:
            token = github.load(data_folder())
            self.user, self.repos = (github.user(token), github.repos(token)) if token else (None, [])
            self.local = self.clones() if token else {}
            self.trouble = ""
        except github.GitHubError as error:
            if error.status == 401:  # the token was revoked on github.com, so nobody is signed in
                github.forget(data_folder())
                self.user, self.repos, self.trouble = None, [], ""
            else:
                self.trouble = str(error)
        self.asking = False
        self.redraw()

    def signin(self) -> None:
        """Show a code, wait for it to be approved on github.com, and keep the token that comes back."""
        if self.code:
            return
        try:
            started = github.begin()
        except github.GitHubError as error:
            self.trouble = str(error)
            return self.redraw()
        where = str(started.get("verification_uri") or "")
        stopped = self.given_up = threading.Event()
        self.trouble = ""
        self.code = {"code": str(started.get("user_code") or ""),
                     "where": where if where.startswith(github.SITE + "/") else github.SITE + "/login/device"}
        self.redraw()
        self.browse(self.code["where"])
        try:
            github.save(data_folder(), github.wait(started, stopped))
        except github.GitHubError as error:
            self.code, self.trouble = None, "" if stopped.is_set() else str(error)
            return self.redraw()
        self.code, self.asking = None, True
        self.redraw()
        self.account()

    def cancel(self) -> None:
        self.given_up.set()

    def signout(self) -> None:
        """Forget the token here. It stays valid on GitHub until it is revoked there."""
        github.forget(data_folder())
        self.user, self.repos, self.local, self.trouble = None, [], {}, ""
        self.redraw()

    def refresh(self) -> None:
        if not self.asking and not self.code:
            self.asking = True
            self.redraw()
            self.account()

    def fetch(self, index) -> Path:
        """The folder of a listed repository, cloned first when it is not on this computer."""
        if not (isinstance(index, int) and 0 <= index < len(self.repos)):
            raise ValueError("no such repository")
        item = self.repos[index]
        name, address = item["name"], item["clone"]
        known = self.local.get(name.lower())
        if known and known.is_dir():
            return known
        if not REPO.match(name) or ".." in name or not address.startswith(github.SITE + "/"):
            raise ValueError(f"'{name}' is not a repository this can clone")
        folder = workspace().joinpath(*name.split("/"))
        if not folder.exists():
            gitops.clone(address, folder, github.load(data_folder()), github.SITE)
        self.local[name.lower()] = folder
        return folder

    def repo(self, index) -> None:
        """Open one of the listed repositories. The page names it by position, as with recent folders."""
        if not (isinstance(index, int) and 0 <= index < len(self.repos)):
            return
        self.at_home = False
        self.window.load_html(self.message(self.say(
            f"Getting {self.repos[index]['name']} from GitHub.", f"Trayendo {self.repos[index]['name']} de GitHub.")))
        try:
            folder = self.fetch(index)
        except (ValueError, gitops.GitError) as error:
            self.window.load_html(self.message(str(error), back=True))
            return
        self.show(folder)

    def close(self) -> None:
        self.given_up.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class _Api:
    """What a page may ask of the program: to pick a folder, to sign in to GitHub, and to
    open what the start page lists. It names things by position; no path, address or
    token ever comes from a page or goes to one."""

    def __init__(self, app: Desktop):
        self._app = app

    def choose(self) -> None:
        self._app.choose()

    def again(self, index) -> None:
        self._app.again(index)

    def home(self) -> None:
        self._app.home()

    def signin(self) -> None:
        self._app.signin()

    def cancel(self) -> None:
        self._app.cancel()

    def signout(self) -> None:
        self._app.signout()

    def refresh(self) -> None:
        self._app.refresh()

    def repo(self, index) -> None:
        self._app.repo(index)


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
        app.window = webview.create_window("LinkedNodes", address, **look)  # the folder's name is in the page
    else:
        app.at_home = True
        app.window = webview.create_window("LinkedNodes", html=app.welcome(), **look)
    menu = [
        Menu(app.say("File", "Archivo"), [
            MenuAction(app.say("Open folder...", "Abrir carpeta..."), app.choose),
            MenuAction(app.say("Recent folders", "Carpetas recientes"), app.home),
            *([MenuAction(app.say("GitHub repositories", "Repositorios de GitHub"), app.home)] if github.CLIENT_ID else []),
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
        # Not private, and with a folder of its own, so the viewer's settings survive. Once the
        # window is up, GitHub is asked who the stored token belongs to, if there is one.
        webview.start(app.account if app.asking else None, menu=menu, private_mode=False, storage_path=str(storage))
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
