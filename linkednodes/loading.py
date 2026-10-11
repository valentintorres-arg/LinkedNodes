"""The page on show while a folder is read, and the account it is given of the reading.

The desktop window shows it the first time a folder is opened, pushing what is found
into the page. The local server shows it too, when a graph is built again from the
viewer: there the page asks the server how far things have got, and goes back to the
graph at the end.
"""
from __future__ import annotations

import json
import threading
from importlib import resources
from pathlib import Path

from . import github, gitops


class Progress:
    """What has been found so far in a folder that is being read.

    It is what pipeline.scan and pipeline.build are given to report to: called with the
    step under way and the counts it has reached, from the thread that does the reading,
    while another asks for the whole account whenever it wants to show it.
    """

    def __init__(self, root: Path):
        self.lock = threading.Lock()
        self.state = {"step": "files", "name": root.name, "path": str(root)}

        def repository() -> None:  # which repository this is; asked on the side, git being slow to start
            name, on = github.repo_name(gitops.origin(root)), gitops.branch(root)
            with self.lock:
                self.state.update(repo=name, branch=on)

        threading.Thread(target=repository, daemon=True).start()

    def __call__(self, step: str, **facts) -> None:
        with self.lock:
            self.state.update(facts, step=step)

    def fail(self, message: str) -> None:
        with self.lock:
            self.state["error"] = message

    def account(self) -> dict:
        with self.lock:
            return dict(self.state)

    @property
    def over(self) -> bool:
        with self.lock:
            return self.state["step"] == "done" or "error" in self.state


def page(state: dict, lang: str, again: bool = False, bar: int = 0, follow: dict | None = None) -> str:
    """The page itself, starting from an account of what is known so far.

    bar is the room a window's own title bar takes at the top. follow, when the page is
    served rather than pushed to, holds the token to ask the server with and the address
    to go back to once the reading is over.
    """
    def say(english, castellano):
        return castellano if lang == "es" else english

    text = {
        "locale": lang,
        "doing": say("Building the graph again", "Reconstruyendo el grafo") if again
                 else say("Reading the folder", "Leyendo la carpeta"),
        "ready": say("Ready", "Listo"),
        "failed": say("The folder could not be read", "No se pudo leer la carpeta"),
        "back": say("Back to the graph", "Volver al grafo"),
        "files": say("Files", "Archivos"), "nodes": say("Nodes", "Nodos"),
        "edges": say("Relations", "Relaciones"), "communities": say("Groups", "Grupos"),
        "steps": {"files": say("Finding the files", "Buscar los archivos"),
                  "code": say("Reading the code", "Leer el código"),
                  "notes": say("Notes, images and saved links", "Notas, imágenes y enlaces guardados"),
                  "history": say("Git history", "Historia de git"),
                  "groups": say("Grouping what belongs together", "Agrupar lo que va junto"),
                  "writing": say("Drawing the graph", "Dibujar el grafo")},
        "of": say("of", "de"), "since": say("since", "desde"),
        "noHistory": say("not a repository", "no es un repositorio"),
        "commits": ["commit", "commits"], "authors": say(["author", "authors"], ["autor", "autores"]),
        "fileWords": say(["file", "files"], ["archivo", "archivos"]),
        "noteWords": say(["note", "notes"], ["nota", "notas"]),
        "imageWords": say(["image", "images"], ["imagen", "imágenes"]),
        "savedWords": say(["saved link", "saved links"], ["enlace", "enlaces"]),
        "groupWords": say(["group", "groups"], ["grupo", "grupos"]),
        "follow": follow,
    }

    def blob(data) -> str:  # escaping "<" keeps a folder's name from closing the script tag it is written into
        return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")

    package = resources.files("linkednodes")
    found = package.joinpath("loading.html").read_text(encoding="utf-8")
    library = package.joinpath("vendor").joinpath("anime.umd.min.js").read_text(encoding="utf-8")
    for mark, value in (("__LANG__", lang), ("__BAR__", str(bar)), ("__TEXT__", blob(text)),
                        ("__STATE__", blob(state)), ("__ANIME__", library)):
        found = found.replace(mark, value)
    return found
