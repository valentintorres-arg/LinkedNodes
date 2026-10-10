"""Signing in to GitHub, and what the account reaches.

The sign-in is GitHub's device flow: the program shows a code, the user approves it on
github.com, and the program is handed a token. No secret ships with the program, only
the public id of its registration. The token is kept by the system's credential store
where there is one, and never in the settings.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE = "https://github.com"
API = "https://api.github.com"
# The id of the OAuth App registered for LinkedNodes. It is public: it names the program
# to GitHub and opens nothing by itself. Another registration can be named in its place.
CLIENT_ID = os.environ.get("LINKEDNODES_GITHUB_CLIENT") or "Ov23liZMKfDbDpEXVUeK"
# Private repositories and pull requests need "repo"; "read:org" shows what comes through
# an organization. GitHub has nothing narrower for an OAuth App.
SCOPE = "repo read:org"
PAGES = 3  # of a hundred repositories each
STORED = "github-token"
REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


class GitHubError(Exception):
    """GitHub refused, or could not be reached. status is 401 when the token is no longer good."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _ask(url: str, fields: dict | None = None, token: str = "") -> object:
    """One request: a form when there are fields to send, a question otherwise."""
    headers = {"Accept": "application/json", "User-Agent": "LinkedNodes"}
    if token:
        headers.update({"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as reply:
            return json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            said = json.loads(error.read()).get("message", "")
        except (ValueError, AttributeError):
            said = ""
        raise GitHubError(said or f"GitHub answered {error.code}", error.code)
    except (OSError, ValueError) as error:
        raise GitHubError(f"GitHub could not be reached: {error}")


def begin() -> dict:
    """Start a sign-in: the code to show, where to type it, and how often to ask about it."""
    if not CLIENT_ID:
        raise GitHubError("this copy of LinkedNodes has no GitHub registration to sign in with")
    reply = _ask(SITE + "/login/device/code", {"client_id": CLIENT_ID, "scope": SCOPE})
    if not isinstance(reply, dict) or not reply.get("device_code"):
        raise GitHubError((isinstance(reply, dict) and (reply.get("error_description") or reply.get("error")))
                          or "GitHub did not start the sign-in")
    return reply


def wait(started: dict, stopped: threading.Event) -> str:
    """Ask until the code is approved, and return the token. Setting stopped gives up."""
    interval = int(started.get("interval", 5))
    deadline = time.monotonic() + int(started.get("expires_in", 900))
    while time.monotonic() < deadline:
        if stopped.wait(interval):
            raise GitHubError("the sign-in was cancelled")
        reply = _ask(SITE + "/login/oauth/access_token", {
            "client_id": CLIENT_ID, "device_code": started["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        reply = reply if isinstance(reply, dict) else {}
        if reply.get("access_token"):
            return reply["access_token"]
        if reply.get("error") == "slow_down":
            interval = int(reply.get("interval") or interval + 5)
        elif reply.get("error") != "authorization_pending":
            raise GitHubError(reply.get("error_description") or reply.get("error") or "GitHub refused the sign-in")
    raise GitHubError("the code expired before it was approved")


def user(token: str) -> dict:
    found = _ask(API + "/user", token=token)
    return {"login": found.get("login", ""), "name": found.get("name") or "", "avatar": found.get("avatar_url") or ""}


def repos(token: str) -> list[dict]:
    """What the account owns, collaborates on or reaches through an organization, latest push first."""
    found: list[dict] = []
    for page in range(1, PAGES + 1):
        batch = _ask(f"{API}/user/repos?affiliation=owner,collaborator,organization_member"
                     f"&sort=pushed&per_page=100&page={page}", token=token)
        for item in batch if isinstance(batch, list) else []:
            found.append({"name": item.get("full_name", ""), "private": bool(item.get("private")),
                          "about": (item.get("description") or "")[:200], "pushed": (item.get("pushed_at") or "")[:10],
                          "clone": item.get("clone_url") or "", "fork": bool(item.get("fork")),
                          "archived": bool(item.get("archived"))})
        if not isinstance(batch, list) or len(batch) < 100:
            break
    return found


def repo_name(remote: str) -> str:
    """'owner/name', in lower case, from a remote's address; '' when it is not on GitHub."""
    match = REMOTE.search(remote.strip())
    return f"{match[1]}/{match[2]}".lower() if match else ""


# ---- where the token is kept. Windows has a store for exactly this; elsewhere it is a
# file that only its owner can read, which is what git's own "store" helper does.
_TYPE, _PERSIST, _MISSING = 1, 2, 1168  # a generic credential, kept for this user on this machine


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p), ("LastWritten", ctypes.c_uint32 * 2), ("BlobSize", ctypes.c_uint32),
                ("Blob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_uint32),
                ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]


def _vault():
    vault = ctypes.WinDLL("advapi32", use_last_error=True)
    vault.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.POINTER(ctypes.POINTER(_Credential))]
    vault.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_uint32]
    vault.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    vault.CredFree.argtypes = [ctypes.c_void_p]
    return vault


def _target(home: Path) -> str:
    """The name the token goes by. A copy that keeps its state apart keeps its token apart too."""
    return "LinkedNodes:github" + (f":{home}" if os.environ.get("LINKEDNODES_HOME") else "")


def save(home: Path, token: str) -> None:
    if os.name != "nt":
        home.mkdir(parents=True, exist_ok=True)
        path = home / STORED
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(token, encoding="utf-8")
        return
    blob = token.encode("utf-8")
    entry = _Credential(Type=_TYPE, TargetName=_target(home), BlobSize=len(blob), Persist=_PERSIST,
                        Blob=ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_ubyte)),
                        UserName="LinkedNodes")
    if not _vault().CredWriteW(ctypes.byref(entry), 0):
        raise GitHubError(f"the token could not be stored: {ctypes.WinError(ctypes.get_last_error())}")


def load(home: Path) -> str:
    """The stored token, or '' when nobody is signed in."""
    if os.name != "nt":
        try:
            return (home / STORED).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    vault, found = _vault(), ctypes.POINTER(_Credential)()
    if not vault.CredReadW(_target(home), _TYPE, 0, ctypes.byref(found)):
        return ""
    try:
        return ctypes.string_at(found.contents.Blob, found.contents.BlobSize).decode("utf-8", "replace")
    finally:
        vault.CredFree(found)


def forget(home: Path) -> None:
    """Remove the token from this computer. GitHub still knows it until it is revoked there."""
    if os.name != "nt":
        (home / STORED).unlink(missing_ok=True)
    elif not _vault().CredDeleteW(_target(home), _TYPE, 0) and ctypes.get_last_error() != _MISSING:
        raise GitHubError(f"the token could not be removed: {ctypes.WinError(ctypes.get_last_error())}")
