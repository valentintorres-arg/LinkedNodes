"""Code extraction without an LLM.

Python is parsed with the ast module, so its edges are exact. The other languages
are read with regexes and indentation, which finds definitions and imports
reliably but can only guess at calls, so those edges are marked INFERRED.
"""
from __future__ import annotations

import ast
import posixpath
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .util import edge, node, read_text


@dataclass
class Def:
    qual: str  # "function" or "Class.method"
    name: str
    kind: str  # function, class, method, type
    line: int


@dataclass
class Imp:
    module: str
    # Local name -> name inside the module; None means the name is the module itself.
    names: dict[str, str | None] = field(default_factory=dict)
    level: int = 0  # leading dots of a Python relative import
    relative: bool = False  # Ruby require_relative


@dataclass
class Call:
    caller: str | None  # qualified name of the enclosing definition, None at file level
    name: str
    base: str | None = None  # "obj" in obj.name(); "?" when the object is an expression


@dataclass
class Info:
    rel: str
    lang: str
    defs: list[Def] = field(default_factory=list)
    imports: list[Imp] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    bases: list[tuple[str, str, str]] = field(default_factory=list)  # class, base, relation


def file_id(rel: str) -> str:
    return "code:" + rel


def file_label(rel: str) -> str:
    """File name, with its folder when the name alone says nothing (page.tsx, index.ts)."""
    parts = rel.split("/")
    if parts[-1].split(".")[0] not in {"page", "layout", "route", "index", "main", "mod", "__init__"}:
        return parts[-1]
    start = len(parts) - 1
    while start > 0:
        start -= 1
        if not parts[start].startswith(("[", "(")):  # a route parameter or group names nothing
            break
    return "/".join(parts[start:])


def is_specific(name: str) -> bool:
    """True for multi-word names such as total_price or fetchOrder.

    A one-word name (resolve, target, load) is as likely to be a library method or
    an ordinary word as a reference to the definition that happens to share it.
    """
    return "_" in name.strip("_") or bool(re.search(r"[a-z0-9][A-Z]", name))


# ---------------------------------------------------------------- Python

def _py_calls(scope: ast.AST, caller: str | None, info: Info) -> None:
    for item in ast.walk(scope):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Name):
            info.calls.append(Call(caller, func.id))
        elif isinstance(func, ast.Attribute):
            base = func.value.id if isinstance(func.value, ast.Name) else "?"
            info.calls.append(Call(caller, func.attr, base))


def _base_name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Subscript):  # Generic[T]
        return _base_name(expr.value)
    return ""


def _parse_python(rel: str, text: str) -> Info:
    info = Info(rel, "python")
    tree = ast.parse(text)
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                local = alias.asname or alias.name.split(".")[0]
                info.imports.append(Imp(alias.name, {local: None}))
        elif isinstance(item, ast.ImportFrom):
            names = {a.asname or a.name: a.name for a in item.names}
            info.imports.append(Imp(item.module or "", names, item.level))
    for item in tree.body:
        if isinstance(item, functions):
            info.defs.append(Def(item.name, item.name, "function", item.lineno))
            _py_calls(item, item.name, info)
        elif isinstance(item, ast.ClassDef):
            info.defs.append(Def(item.name, item.name, "class", item.lineno))
            for base in item.bases:
                if _base_name(base):
                    info.bases.append((item.name, _base_name(base), "inherits"))
            for member in item.body:
                if isinstance(member, functions):
                    qual = f"{item.name}.{member.name}"
                    info.defs.append(Def(qual, member.name, "method", member.lineno))
                    _py_calls(member, qual, info)
        else:
            _py_calls(item, None, info)
    return info


# ---------------------------------------------------------------- other languages

_KEYWORDS = {"if", "else", "for", "while", "do", "switch", "case", "catch", "try", "finally",
             "return", "throw", "new", "delete", "typeof", "instanceof", "await", "yield",
             "function", "fn", "func", "def", "class", "struct", "enum", "interface", "trait",
             "impl", "type", "import", "export", "from", "require", "use", "using", "package",
             "namespace", "super", "this", "self", "constructor", "match", "loop", "unless",
             "until", "when", "with", "in", "of", "as", "is", "not", "and", "or", "sizeof",
             "print", "println", "printf", "echo", "lock", "foreach", "elif", "elsif", "go",
             "defer", "select", "let", "const", "var", "pub", "mut", "where", "assert"}
_MODS_JS = r"(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
_MODS_JAVA = (r"(?:(?:public|private|protected|internal|static|final|abstract|virtual|override|"
              r"async|sealed|partial|synchronized|native|default|extern|new)\s+)")
_JS = {
    "defs": [
        ("class", r"\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)"
                  r"(?:\s+extends\s+(?P<base>[\w.]+))?"),
        ("function", r"\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
                     r"(?P<name>\w+)\s*[(<]"),
        ("function", r"\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*"
                     r"(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+?)?=>|\w+\s*=>)"),
        ("type", r"\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+(?P<name>\w+)"),
        # The parameters may hold parentheses of their own (@Body() dto) or continue on
        # the next lines, so the line either closes the signature or ends at "(".
        ("method", r"\s+" + _MODS_JS + r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\("
                   r"(?:.*\)\s*(?::\s*[^{;]+)?\{)?\s*$"),
    ],
    "imports": [
        r"""(?:import|export)\s+(?P<names>[^'";=()]*?)\s*from\s*['"](?P<mod>[^'"]+)['"]""",
        r"""import\s*['"](?P<mod>[^'"]+)['"]""",
        r"""(?:require|import)\(\s*['"](?P<mod>[^'"]+)['"]\s*\)""",
    ],
}
_LANGS = {
    "javascript": _JS,
    "typescript": _JS,
    "go": {
        "defs": [
            ("method", r"func\s*\([^)]*?\*?(?P<parent>\w+)\)\s*(?P<name>\w+)\s*[(\[]"),
            ("function", r"func\s+(?P<name>\w+)\s*[(\[]"),
            ("class", r"type\s+(?P<name>\w+)\s+(?:struct|interface)\b"),
        ],
        "imports": [r'^\s*import\s+(?:[\w.]+\s+)?"(?P<mod>[^"]+)"',
                    r'^\s+(?:[\w.]+\s+)?"(?P<mod>[\w.\-]+/[^"\s]+)"\s*$'],
    },
    "rust": {
        "defs": [
            ("function", r"\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe)\s+)*fn\s+"
                         r"(?P<name>\w+)"),
            ("class", r"\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>\w+)"),
            ("impl", r"\s*impl(?:<[^>]*>)?\s+(?:(?P<base>[\w:]+)(?:<[^>]*>)?\s+for\s+)?"
                     r"(?P<name>\w+)"),
        ],
        "imports": [r"^\s*(?:pub\s+)?use\s+(?P<mod>\w+)::"],
    },
    "java": {
        "defs": [
            ("class", r"\s*" + _MODS_JAVA + r"*(?:class|interface|enum|record)\s+(?P<name>\w+)"
                      r"(?:[^{]*?\bextends\s+(?P<base>[\w.]+))?"),
            ("method", r"\s+" + _MODS_JAVA + r"+(?:<[^>]+>\s+)?[\w<>\[\],.? ]+?\s+"
                       r"(?P<name>\w+)\s*\([^;]*$"),
        ],
        "imports": [r"^\s*import\s+(?:static\s+)?(?P<mod>[\w.]+)\s*;"],
    },
    "csharp": {
        "defs": [
            ("class", r"\s*" + _MODS_JAVA + r"*(?:class|interface|struct|record|enum)\s+"
                      r"(?P<name>\w+)(?:\s*(?:<[^>]*>)?\s*:\s*(?P<base>[\w.]+))?"),
            ("method", r"\s+" + _MODS_JAVA + r"+[\w<>\[\],.? ]+?\s+(?P<name>\w+)\s*\([^;]*$"),
        ],
        "imports": [],
    },
    "ruby": {
        "defs": [
            ("class", r"\s*(?:class|module)\s+(?P<name>\w+)(?:\s*<\s*(?P<base>[\w:]+))?"),
            ("function", r"\s*def\s+(?:self\.)?(?P<name>\w+[?!]?)"),
        ],
        "imports": [r"""^\s*require_relative\s+['"](?P<rel>[^'"]+)['"]""",
                    r"""^\s*require\s+['"](?P<mod>[^'"]+)['"]"""],
    },
    "php": {
        "defs": [
            ("class", r"\s*(?:(?:abstract|final)\s+)*(?:class|interface|trait)\s+(?P<name>\w+)"
                      r"(?:\s+extends\s+(?P<base>[\w\\]+))?"),
            ("function", r"\s*(?:(?:public|private|protected|static|abstract|final)\s+)*"
                         r"function\s+&?(?P<name>\w+)\s*\("),
        ],
        "imports": [r"^\s*use\s+(?P<mod>[\w\\]+)"],
    },
}
for _spec in _LANGS.values():
    if isinstance(_spec["defs"][0][1], str):
        _spec["defs"] = [(kind, re.compile(rx)) for kind, rx in _spec["defs"]]
        _spec["imports"] = [re.compile(rx, re.MULTILINE) for rx in _spec["imports"]]

_CALL = re.compile(r"(?:(?P<base>[A-Za-z_$][\w$]*)\s*(?:\.|::|->)\s*)?(?P<name>[A-Za-z_$][\w$]*)\s*\(")
_STRING = re.compile(r"""(['"`])(?:\\.|(?!\1).)*\1""")
_COMMENT = ("//", "#", "*", "/*", "--")


def _js_names(clause: str) -> dict[str, str | None]:
    """Turn 'React, { a, b as c }' or '* as ns' into local name -> original name."""
    names: dict[str, str | None] = {}
    clause = re.sub(r"\btype\s+", "", clause)
    braces = re.search(r"\{([^}]*)\}", clause)
    if braces:
        for part in braces.group(1).split(","):
            bits = part.split()
            if bits:
                names[bits[-1]] = bits[0]
    outside = re.sub(r"\{[^}]*\}", "", clause)
    star = re.search(r"\*\s*as\s+(\w+)", outside)
    if star:
        names[star.group(1)] = None
    for part in re.sub(r"\*\s*as\s+\w+", "", outside).split(","):
        if part.strip().isidentifier():
            names[part.strip()] = "default"
    return names


def _parse_generic(rel: str, lang: str, text: str) -> Info:
    spec = _LANGS[lang]
    info = Info(rel, lang)
    for rx in spec["imports"]:
        for match in rx.finditer(text):
            groups = match.groupdict()
            if groups.get("rel"):
                info.imports.append(Imp(groups["rel"], relative=True))
            else:
                info.imports.append(Imp(groups["mod"], _js_names(groups.get("names") or "")))

    scopes: list[tuple[int, str, str]] = []  # indent, qualified name, kind
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(_COMMENT):
            continue
        indent = len(raw) - len(raw.lstrip())
        # A brace on its own line, or the tail of a wrapped signature, opens the body of
        # the definition above it rather than ending it.
        opens_body = stripped[0] in "{)"
        while scopes and scopes[-1][0] >= indent and not opens_body:
            scopes.pop()
        owner = next((s for s in reversed(scopes) if s[2] in ("class", "impl")), None)
        inside_function = bool(scopes) and scopes[-1][2] in ("function", "method")

        defined = False
        for kind, rx in spec["defs"]:
            match = rx.match(raw)
            if not match or match.group("name") in _KEYWORDS:
                continue
            name, groups = match.group("name"), match.groupdict()
            if kind == "impl":
                if groups.get("base"):
                    info.bases.append((name, groups["base"], "implements"))
                scopes.append((indent, name, "impl"))
            elif kind in ("class", "type"):
                info.defs.append(Def(name, name, kind, number))
                if groups.get("base"):
                    info.bases.append((name, groups["base"], "inherits"))
                scopes.append((indent, name, kind))
            elif inside_function:
                break  # a helper nested in a function stays part of that function
            elif groups.get("parent"):  # Go receiver
                qual = f"{groups['parent']}.{name}"
                info.defs.append(Def(qual, name, "method", number))
                scopes.append((indent, qual, "method"))
            elif owner:
                qual = f"{owner[1]}.{name}"
                info.defs.append(Def(qual, name, "method", number))
                scopes.append((indent, qual, "method"))
            elif kind == "function":
                info.defs.append(Def(name, name, "function", number))
                scopes.append((indent, name, "function"))
            else:
                continue  # a method pattern outside any class is not a definition
            defined = True
            break
        if defined:
            continue

        caller = next((s[1] for s in reversed(scopes) if s[2] in ("function", "method")), None)
        for match in _CALL.finditer(_STRING.sub("''", raw)):
            if match.group("name") not in _KEYWORDS:
                base = (match.group("base") or "").lstrip("$") or None  # PHP writes $this
                info.calls.append(Call(caller, match.group("name"), base))
    return info


# ---------------------------------------------------------------- resolution

_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_NODE_BUILTINS = {"fs", "path", "os", "http", "https", "url", "util", "crypto", "events",
                  "stream", "child_process", "assert", "buffer", "zlib", "net", "readline",
                  "process", "worker_threads", "querystring", "timers", "tty", "dns"}
_RUST_BUILTINS = {"crate", "self", "super", "std", "core", "alloc"}
# Names too common to link on the name alone: obj.get() rarely means your get().
_COMMON = {"append", "extend", "update", "get", "set", "items", "keys", "values", "format",
           "join", "split", "strip", "replace", "read", "write", "close", "open", "add",
           "remove", "pop", "push", "sort", "index", "count", "copy", "clear", "find", "map",
           "filter", "reduce", "forEach", "then", "slice", "includes", "indexOf", "toString",
           "length", "log", "error", "warn", "info", "debug", "send", "json", "text", "render",
           "save", "delete", "load", "dump", "loads", "dumps", "run", "start", "stop", "exists",
           "match", "search", "group", "insert", "execute", "commit", "connect", "query",
           "parse", "main", "init", "test", "setup", "create", "build", "call", "apply",
           "next", "reset", "validate", "handle", "process", "name", "size", "equals"}


class _Resolver:
    def __init__(self, infos: list[Info]):
        self.infos = {info.rel: info for info in infos}
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.defs: dict[tuple[str, str], str] = {}  # (file, qualified name) -> node id
        self.kinds: dict[str, str] = {}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.py_full: dict[str, str] = {}
        self.py_suffix: dict[str, list[str]] = defaultdict(list)
        for info in infos:
            if info.lang == "python":
                parts = info.rel[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                self.py_full[".".join(parts)] = info.rel
                for start in range(len(parts)):
                    self.py_suffix[".".join(parts[start:])].append(info.rel)

    def link(self, source, target, relation, confidence, score, rel):
        if source == target:
            return
        key = (source, target, relation)
        if key not in self.edges or self.edges[key]["score"] < score:
            self.edges[key] = edge(source, target, relation, confidence, score, rel)

    def declare(self) -> None:
        for info in self.infos.values():
            fid = file_id(info.rel)
            self.nodes[fid] = node(fid, file_label(info.rel), "file", info.rel, lang=info.lang)
            for d in info.defs:
                if (info.rel, d.qual) in self.defs:
                    continue
                did = f"{fid}::{d.qual}"
                self.defs[(info.rel, d.qual)] = did
                self.kinds[did] = d.kind
                self.by_name[d.name].append(did)
                label = d.qual if d.kind == "method" else d.name
                self.nodes[did] = node(did, label, d.kind, info.rel, line=d.line)
            for d in info.defs:
                owner = self.defs.get((info.rel, d.qual.rsplit(".", 1)[0])) if "." in d.qual else None
                self.link(owner or fid, self.defs[(info.rel, d.qual)], "contains", "EXTRACTED",
                          1.0, info.rel)

    def package(self, name: str, label: str | None = None) -> str:
        pid = "pkg:" + name
        self.nodes.setdefault(pid, node(pid, label or name, "package"))
        return pid

    def py_module(self, dotted: str) -> str | None:
        if dotted in self.py_full:
            return self.py_full[dotted]
        found = self.py_suffix.get(dotted, [])
        return found[0] if len(found) == 1 else None

    def js_file(self, rel: str, spec: str) -> str | None:
        if spec.startswith("."):
            bases = [posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))]
        elif spec.startswith(("@/", "~/")):
            # The usual alias for a package's source root. In a monorepo that root is some
            # folder above the file, so try each one, nearest first.
            parts = posixpath.dirname(rel).split("/")
            bases = [posixpath.join("/".join(parts[:depth]), middle, spec[2:])
                     for depth in range(len(parts), -1, -1) for middle in ("", "src")]
        else:
            return None
        for base in bases:
            stem = re.sub(r"\.(js|jsx|mjs|cjs)$", "", base)
            for cand in [base, *(stem + e for e in _JS_EXT), *(f"{base}/index{e}" for e in _JS_EXT)]:
                if cand in self.infos:
                    return cand
        return None

    def class_file(self, name: str) -> str | None:
        """File that defines the only class with this name, for Java and PHP imports."""
        found = [d for d in self.by_name.get(name, []) if self.kinds[d] == "class"]
        return self.nodes[found[0]]["source_file"] if len(found) == 1 else None

    def imports(self, info: Info) -> dict[str, tuple[str, str | None]]:
        """Add import edges; return local name -> (file it comes from, name in that file)."""
        bound: dict[str, tuple[str, str | None]] = {}
        fid = file_id(info.rel)

        def internal(target: str | None) -> bool:
            if target:
                self.link(fid, file_id(target), "imports", "EXTRACTED", 1.0, info.rel)
            return bool(target)

        for imp in info.imports:
            if info.lang == "python":
                module = imp.module
                if imp.level:
                    here = info.rel.split("/")[:-1]
                    up = here[:len(here) - (imp.level - 1)] if imp.level > 1 else here
                    module = ".".join([*up, *filter(None, [imp.module])])
                    target = self.py_full.get(module)
                else:
                    target = self.py_module(module)
                found = internal(target)
                for local, original in imp.names.items():
                    lookup = self.py_full.get if imp.level else self.py_module
                    submodule = lookup(f"{module}.{original}") if original else None
                    if submodule:
                        found = internal(submodule)
                        bound[local] = (submodule, None)
                    elif target:
                        bound[local] = (target, original)
                top = imp.module.split(".")[0]
                if not found and not imp.level and top and top not in sys.stdlib_module_names:
                    self.link(fid, self.package(top), "imports", "EXTRACTED", 1.0, info.rel)
            elif info.lang in ("javascript", "typescript"):
                target = self.js_file(info.rel, imp.module)
                if internal(target):
                    bound.update({local: (target, original) for local, original in imp.names.items()})
                elif not imp.module.startswith((".", "@/", "~/", "node:")):
                    parts = imp.module.split("/")
                    name = "/".join(parts[:2]) if imp.module.startswith("@") else parts[0]
                    if name not in _NODE_BUILTINS:
                        self.link(fid, self.package(name), "imports", "EXTRACTED", 1.0, info.rel)
            elif info.lang == "go":
                if "." in imp.module.split("/")[0]:  # the standard library has no domain
                    label = "/".join(imp.module.split("/")[-2:])
                    self.link(fid, self.package(imp.module, label), "imports", "EXTRACTED", 1.0,
                              info.rel)
            elif info.lang == "rust":
                if imp.module not in _RUST_BUILTINS:
                    self.link(fid, self.package(imp.module), "imports", "EXTRACTED", 1.0, info.rel)
            elif info.lang == "ruby":
                if imp.relative:
                    base = posixpath.normpath(posixpath.join(posixpath.dirname(info.rel), imp.module))
                    internal(base + ".rb" if base + ".rb" in self.infos else None)
                else:
                    self.link(fid, self.package(imp.module), "imports", "EXTRACTED", 1.0, info.rel)
            else:  # Java, PHP: the last segment is a class name
                name = re.split(r"[.\\]", imp.module)[-1]
                target = self.class_file(name)
                if target != info.rel and internal(target):
                    bound[name] = (target, name)
        return bound

    def unique(self, name: str, kinds: tuple[str, ...] | None = None,
               specific: bool = True) -> str | None:
        if len(name) < 4 or name in _COMMON or name.startswith("__"):
            return None
        if specific and not is_specific(name):
            return None
        found = [d for d in self.by_name.get(name, []) if not kinds or self.kinds[d] in kinds]
        return found[0] if len(found) == 1 else None

    def target(self, info: Info, bound: dict, call: Call) -> tuple[str | None, bool]:
        """Find the definition a call refers to; the flag says whether the match is firm."""
        rel, name, base = info.rel, call.name, call.base
        if base in ("self", "cls", "this", "Self") and call.caller and "." in call.caller:
            owner = call.caller.rsplit(".", 1)[0]
            return self.defs.get((rel, f"{owner}.{name}")), True
        if base is None:
            if (rel, name) in self.defs:
                return self.defs[(rel, name)], True
            if name in bound:
                source, original = bound[name]
                found = self.defs.get((source, original or name)) or self.defs.get((source, name))
                if found:
                    return found, True
            if info.lang == "python":
                return None, False  # a bare Python name that is not in scope is a builtin
        elif base in bound:
            source, original = bound[base]
            found = self.defs.get((source, name if original is None else f"{original}.{name}"))
            if found:
                return found, True
        elif (rel, f"{base}.{name}") in self.defs:
            return self.defs[(rel, f"{base}.{name}")], True
        # In Python, obj.name() on an object of unknown type can only reach a method;
        # path.read_text() is never the module-level read_text().
        kinds = ("method",) if info.lang == "python" else None
        return self.unique(name, kinds), False

    def resolve(self) -> tuple[list[dict], list[dict]]:
        self.declare()
        for info in self.infos.values():
            bound = self.imports(info)
            exact = info.lang == "python"
            for call in info.calls:
                found, firm = self.target(info, bound, call)
                if not found:
                    continue
                source = self.defs.get((info.rel, call.caller or ""), file_id(info.rel))
                if firm:
                    confidence, score = ("EXTRACTED", 1.0) if exact else ("INFERRED", 0.8)
                else:
                    confidence, score = "INFERRED", 0.5
                self.link(source, found, "calls", confidence, score, info.rel)
            for cls, base, relation in info.bases:
                source = self.defs.get((info.rel, cls))
                name = re.split(r"[.:\\]", base)[-1]
                found, firm = self.defs.get((info.rel, name)), True
                if not found and name in bound:
                    origin, original = bound[name]
                    found = self.defs.get((origin, original or name))
                if not found:
                    found, firm = self.unique(name, ("class", "type"), specific=False), False
                if source and found:
                    confidence, score = ("EXTRACTED", 1.0) if exact and firm else ("INFERRED", 0.8 if firm else 0.6)
                    self.link(source, found, relation, confidence, score, info.rel)
        return list(self.nodes.values()), list(self.edges.values())


def extract(root: Path, files: list[dict]) -> tuple[list[dict], list[dict]]:
    infos = []
    for record in files:
        if record["kind"] != "code":
            continue
        rel, lang = record["path"], record["lang"]
        try:
            text = read_text(root / rel)
            infos.append(_parse_python(rel, text) if lang == "python" else _parse_generic(rel, lang, text))
        except (OSError, SyntaxError, ValueError, RecursionError):
            infos.append(Info(rel, lang))  # unreadable code still gets its file node
    return _Resolver(infos).resolve()
