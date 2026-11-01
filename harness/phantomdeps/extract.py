"""Extract dependency declarations from model responses.

Only *executable* declarations are extracted: manifest entries
(``platformio.ini`` ``lib_deps``, ``idf_component.yml``, ``package.xml``,
``Cargo.toml``, ``requirements.txt``, ``package.json``,
``library.properties``) and install commands (``arduino-cli lib install``,
``pio pkg install``, ``idf.py add-dependency``, ``cargo add``, ``mip.install``,
``mpremote mip install``, ``pip install``, ``npm install``, ``apt install
ros-<distro>-*``).  Prose recommendations are ignored: precision is preferred
over recall, so the phantom rate is a lower bound for what a reader of the
answer would try to install.

The ecosystem of a declaration follows from its syntax, not from the task.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass, field

import yaml

from .registries import ESP_SLUG, norm_pypi

# --------------------------------------------------------------------------


@dataclass
class Decl:
    eco: str
    name: str
    kind: str = "registry"  # registry | url | path | legacy_id | special | unsupported
    owner: str | None = None
    version: str | None = None
    via: str = ""
    raw: str = ""
    flags: list = field(default_factory=list)

    def key(self) -> tuple:
        n = norm_pypi(self.name) if self.eco == "pypi" else self.name.lower()
        return (self.eco, (self.owner or "").lower(), n, self.kind)

    def to_dict(self) -> dict:
        return asdict(self)


FENCE = re.compile(r"```[ \t]*([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
URL_RE = re.compile(r"^(?:git\+)?(?:https?|git|ssh)://|^git@|\.git(?:#.*)?$|^github:|^gitlab:", re.I)


def code_blocks(text: str) -> list[tuple[str, str, int]]:
    """(language, body, start offset); an unterminated last fence is kept."""
    return [(m.group(1).strip().lower(), m.group(2), m.start()) for m in FENCE.finditer(text)]


def _is_url(s: str) -> bool:
    """A URL is one token: "pip install git+https://.../x.git" is a command, not a URL."""
    s = s.strip()
    return not re.search(r"\s", s) and bool(URL_RE.search(s))


def _is_path(s: str) -> bool:
    s = s.strip()
    return s.startswith(("file://", "symlink://", "./", "../", "/", "~/")) or s in (".", "..")


def _strip_comment(s: str) -> str:
    """Cut a shell comment: '#' at the start of a word, outside quotes ("SD#1.0" keeps its '#')."""
    quote = None
    for i, c in enumerate(s):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or s[i - 1].isspace()):
            return s[:i]
    return s


def _split_args(argstr: str) -> list[str]:
    argstr = _strip_comment(argstr)
    argstr = re.split(r"\s(?:&&|\|\||;|\|)\s", argstr)[0]
    try:
        return shlex.split(argstr, posix=True)
    except ValueError:
        return re.findall(r'"[^"]*"|\'[^\']*\'|\S+', argstr)


# --------------------------------------------------------------------------
# install commands: only in code context
#
# A command counts if it appears on a line of a fenced code block, inside an
# inline code span, or on a prose line that consists of the command alone
# (after a list marker or shell prompt).  Commands inside running prose
# ("... then arduino-cli lib install X (for the IMU), 2) Wire for I2C") are
# skipped, and in code context the arguments end at the first token that reads
# as prose, so that words of a sentence never become package names.

INLINE = re.compile(r"`([^`\n]+)`")
LINE_PREFIX = re.compile(r"^\s*(?:(?:[-*+\u2022]|\d+[.)])\s+)?(?:(?:\$|>|#|PS>)\s*)?")
PROSE_WORDS = {"and", "or", "for", "to", "then", "with", "the", "a", "an", "in", "on", "from", "of", "by",
               "library", "libraries", "package", "packages", "module", "modules", "if", "you", "your", "it",
               "this", "that", "which", "using", "via", "as", "is", "are", "be", "will", "can", "also", "etc"}
ARG_TOKEN = re.compile(r"^[A-Za-z0-9@_~^<>=!*/+:\[\],.-][A-Za-z0-9@_~^<>=!*/+:\[\],.#-]*$")


def _argish(t: str) -> bool:
    if " " in t:  # came from a quoted argument
        return True
    if t.startswith("-") or _is_url(t) or _is_path(t):
        return True
    if (t.lower() in PROSE_WORDS and not (len(t) > 1 and t.isupper())) or t.endswith((".", ",", ";", ":", ")")) \
            or "(" in t:
        return False
    return bool(ARG_TOKEN.match(t))


def _until_prose(toks: list[str]) -> list[str]:
    out = []
    for t in toks:
        if not _argish(t):
            break
        out.append(t)
    return out


COMMENTED = re.compile(r"(?:^|\s)(?:#|//)")     # a comment opens before a command head
CALL_LINE = re.compile(r"""\s*['"][^'"]+['"]\s*(?:,[^)]*)?\)\s*(?:#.*)?$""")


def command_hits(text: str, head: str, call: bool = False) -> list[tuple[str, str]]:
    """(argument string, context) for every occurrence of a command head in code context.
    ``call``: the head opens a function call (``mip.install(``); a prose line then counts only
    if it consists of that call."""
    hre = re.compile(head)
    hits, fenced = [], []
    for m in FENCE.finditer(text):
        fenced.append((m.start(), m.end()))
        for line in m.group(2).splitlines():
            for h in hre.finditer(line):
                if COMMENTED.search(line[:h.start()]):
                    continue          # in a comment (# or //): not executed
                hits.append((line[h.end():], "fence"))

    def in_fence(pos: int) -> bool:
        return any(a <= pos < b for a, b in fenced)

    for m in INLINE.finditer(text):
        if in_fence(m.start()):
            continue
        for h in hre.finditer(m.group(1)):
            hits.append((m.group(1)[h.end():], "inline"))
    pos = 0
    for line in text.splitlines(keepends=True):
        start, pos = pos, pos + len(line)
        if in_fence(start) or "`" in line:
            continue
        rest = line[LINE_PREFIX.match(line).end():]
        h = hre.match(rest)
        if h:
            args = rest[h.end():].strip()
            if call:
                if CALL_LINE.match(args):
                    hits.append((args, "line"))
                continue
            toks = _split_args(args)
            if toks and all(_argish(t) for t in toks):
                hits.append((args, "line"))
    return hits


def command_tokens(text: str, head: str) -> list[tuple[list[str], str]]:
    """Tokens of each accepted command, cut at the first prose-like token."""
    return [(_until_prose(_split_args(a)), a) for a, _ in command_hits(text, head)]


# --------------------------------------------------------------------------
# Arduino


def _arduino(text: str, arduino_names: set | None) -> list[Decl]:
    out = []
    for toks, raw in command_tokens(text, r"\barduino-cli\s+lib\s+install\s+"):
        quoted = '"' in raw or "'" in raw
        i = 0
        names = []
        while i < len(toks):
            t = toks[i]
            if t in ("--git-url", "--zip-path"):
                for u in toks[i + 1:]:
                    if u.startswith("-"):
                        break
                    out.append(Decl("arduino", u, "url" if t == "--git-url" else "path", via="arduino-cli", raw=raw))
                break
            if t.startswith("-"):
                i += 1
                continue
            names.append(t)
            i += 1
        # arduino-cli installs every unquoted word as a library of its own (`lib install Adafruit
        # BusIO` asks for "Adafruit" and "BusIO"), even when the words together name a registered
        # library: the installer would fetch what an outsider could register under either word
        flags = ["unquoted_multi"] if not quoted and len(names) > 1 else []
        for n in names:
            name, _, ver = n.partition("@")
            if name:
                out.append(Decl("arduino", name.strip(), via="arduino-cli", version=ver or None,
                                raw=raw.strip(), flags=list(flags)))
    for m in re.finditer(r"^\s*depends\s*=\s*(.+)$", text, re.M):
        for part in m.group(1).split(","):
            name = re.sub(r"\(.*?\)", "", part).strip()
            if name:
                out.append(Decl("arduino", name, via="library.properties", raw=part.strip()))
    return out


# --------------------------------------------------------------------------
# PlatformIO


def _pio_entry(e: str, via: str) -> Decl | None:
    e = e.strip().strip(",").strip()
    e = re.sub(r"^-\s+", "", e)
    e = e.strip("'\"")
    if not e or e.startswith((";", "#")) or e.startswith("${"):
        return None
    e = re.split(r"\s+;", e)[0].strip()
    if "=" in e and _is_url(e.split("=", 1)[1]):
        return Decl("platformio", e.split("=", 1)[0].strip(), "url", via=via, raw=e)
    if _is_url(e) or "github.com/" in e:
        return Decl("platformio", e, "url", via=via, raw=e)
    if _is_path(e):
        return Decl("platformio", e, "path", via=via, raw=e)
    # PlatformIO writes a version requirement after '@'; as in the official reading (validity.py), a
    # name followed by another requirement operator ('name = ^1.0', 'name=*', 'name>=1.0') is read
    # as that name
    m = re.match(r"^([^@=<>~^ ]+(?:\s+[^@=<>~^ ]+)*?)\s*(?:([@=<>~^])\s*(.*))?$", e)
    if not m:
        return None
    op, rest = m.group(2), (m.group(3) or "").strip()
    spec = m.group(1).strip()
    ver = ((rest if op in ("@", "=") else f"{op}{rest}") if op else "") or None
    if spec.isdigit():
        return Decl("platformio", spec, "legacy_id", via=via, raw=e, version=ver)
    owner = None
    if "/" in spec:
        owner, spec = spec.split("/", 1)
    return Decl("platformio", spec.strip(), owner=owner.strip() if owner else None,
                version=ver, via=via, raw=e)


def _platformio(text: str) -> list[Decl]:
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*lib_deps\s*[=:]\s*(.*)$", lines[i])
        if m:
            entries = [m.group(1)]
            j = i + 1
            # ini continuation lines are indented (or written as "- item"); a section header, a new
            # key or a fence ends the value.  PlatformIO reads the file with configparser, which keeps
            # blank lines inside a value: the value goes on when the next non-blank line is indented.
            while j < len(lines):
                ln = lines[j]
                if not ln.strip():
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines) and lines[k][:1].isspace() and "```" not in lines[k] \
                            and not re.match(r"^\s*\[", lines[k]):
                        j = k
                        continue
                    break
                if "```" in ln or re.match(r"^\s*\[", ln) or \
                        re.match(r"^\s*(?:monitor|upload|build|board|platform|framework|extra|lib)_\w*\s*=", ln):
                    break
                if not (ln[:1].isspace() or ln.lstrip().startswith("-")):
                    break
                entries.append(ln)
                j += 1
            for ent in entries:
                # comments as configparser reads them (PlatformIO: inline prefixes ';' and '#'): a
                # full-line comment is skipped, an inline one (after whitespace) is cut off, before
                # the line is split at commas; '#' without whitespace before it (a git ref) stays
                if ent.strip().startswith((";", "#")):
                    continue
                ent = re.split(r"\s[;#]", ent, maxsplit=1)[0]
                for part in re.split(r",(?![^<>]*>)", ent):
                    d = _pio_entry(part, "lib_deps")
                    if d:
                        out.append(d)
            i = j
            continue
        i += 1
    cmd = r"\b(?:pio|platformio)\s+(?:lib\s+(?:-g\s+)?install|pkg\s+install)\s+"
    for toks, _ in command_tokens(text, cmd):
        k = 0
        while k < len(toks):
            t = toks[k]
            if t in ("-l", "--library"):
                if k + 1 < len(toks):
                    d = _pio_entry(toks[k + 1], "pio-install")
                    if d:
                        out.append(d)
                k += 2
                continue
            if t in ("-p", "--platform", "-t", "--tool", "-e", "--environment", "-d", "--project-dir"):
                k += 2
                continue
            if t.startswith("-"):
                k += 1
                continue
            d = _pio_entry(t, "pio-install")
            if d:
                out.append(d)
            k += 1
    return out


# --------------------------------------------------------------------------
# ESP-IDF


def _espidf_key(k: str, v, via: str) -> Decl | None:
    k = str(k).strip().strip("'\"")
    if not k:
        return None
    if k == "idf":
        return Decl("espidf", "idf", "special", via=via, raw=k)
    if isinstance(v, dict) and (v.get("git") or v.get("path") or v.get("override_path")):
        kind = "url" if v.get("git") else "path"
        return Decl("espidf", k, kind, via=via, raw=k)
    ns, name = (k.split("/", 1) if "/" in k else ("espressif", k))
    ver = v.get("version") if isinstance(v, dict) else (str(v) if v is not None else None)
    flags = [] if "/" in k else ["implicit_namespace"]
    return Decl("espidf", name, owner=ns, version=ver, via=via, raw=k, flags=flags)


# a list item that carries its version ("led_strip@^2.0", "led_strip:^2.0" -- YAML reads
# "a:b" without a space as one string --, "led_strip >= 2.0" or "led_strip^2.0"); "a::b" and
# "source:name" are no versions and stay as written
ESP_VERSIONED = re.compile(r"^([^\s:@<>=~^!*]+)(?:\s*@\s*|:(?!:)\s*|\s+|(?=[\^~<>=!]))((?:[\^~<>=!*]|v?\d|latest\b).*)$",
                           re.I)


# the component manager's reading of an `add-dependency` argument (idf_component_tools.manifest.
# constants.WEB_DEPENDENCY_REGEX, version 3.1.2): the name is a slug (ESP_SLUG, registries.py),
# optionally namespace/slug, and the rest is the version requirement.  As everywhere, the
# declaration is judged by its name: the tool would refuse "modbus@1.0" for its requirement ("@1.0"
# is no SimpleSpec), not its name.
# a list item of a malformed manifest names its component by one of these fields
ESP_ITEM_NAME_KEYS = ("name", "component", "idf_component")
ESP_SOURCE_KEYS = ("uri", "url", "repo", "repository", "source_url")
# options of `idf.py add-dependency` that take a value (idf_component_manager/idf_extensions.py)
IDF_OPTS_WITH_VALUE = {"--path", "-p", "--component", "--git", "--git-path", "--git-ref", "--registry-url",
                       "--profile", "--service-profile"}
REPO_PATH = re.compile(r"^(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org)/", re.I)
# a further argument that is a version, not a name ("idf.py add-dependency x 1.0.0", "... x ^1.0")
VERSION_TOKEN = re.compile(r"^(?:[\^~<>=!*]|v?\d+(?:\.|$)|latest$)", re.I)
ESP_DEPENDENCY = re.compile(rf"^((?:{ESP_SLUG}/{ESP_SLUG})|(?:{ESP_SLUG}))(.*)$")


def _esp_deps(cand) -> tuple[dict | None, bool]:
    """The entries of a ``dependencies`` value: a mapping as the component manager requires, or a
    list (flagged malformed) of names (with or without a version), ``name: x`` items or one-key
    items."""
    if isinstance(cand, dict):
        return cand, False
    if not isinstance(cand, list):
        return None, False
    deps = {}
    for it in cand:
        if isinstance(it, str):
            m = ESP_VERSIONED.match(it.strip())
            if m:
                deps[m.group(1)] = m.group(2).strip()
            else:
                deps[it] = None
            continue
        if not isinstance(it, dict):
            continue
        key = next((k for k in ESP_ITEM_NAME_KEYS if isinstance(it.get(k), str)), None)
        if key:
            # "- name: x\n  version: ..." items (also "component:", "idf_component:"): that field names
            # the component; a repository given as uri, url, repo, repository or source_url makes it a
            # git source, as `git:` would
            spec = {k: v for k, v in it.items() if k != key}
            src = next((spec[k] for k in ESP_SOURCE_KEYS if isinstance(spec.get(k), str) and _is_url(spec[k])), None)
            if src and not spec.get("git"):
                spec["git"] = src
            deps[it[key]] = spec or None
        elif len(it) == 1:
            k, v = next(iter(it.items()))
            deps[str(k)] = v
    return deps, True


def _nested_dependencies(o):
    """Every value of a `dependencies` key below the top level (a manifest written under another
    key, e.g. as entries of a `components:` list)."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "dependencies" and isinstance(v, (dict, list)):
                yield v
            else:
                yield from _nested_dependencies(v)
    elif isinstance(o, list):
        for v in o:
            yield from _nested_dependencies(v)


def _espidf(text: str, eco_hint: str) -> list[Decl]:
    out = []
    for lang, body, start in code_blocks(text):
        if "dependencies" not in body:
            continue
        ctx = text[max(0, start - 200):start].lower()
        if not (eco_hint == "espidf" or "idf_component" in ctx or "idf_component" in body):
            continue
        if lang not in ("yaml", "yml", "", "text", "idf_component.yml"):
            continue
        deps, malformed = None, False
        try:
            doc = yaml.safe_load(body)
        except yaml.YAMLError:
            doc = None
        if isinstance(doc, dict):
            cand, wrapped = doc.get("dependencies"), False
            if cand is None:  # dependencies nested under a wrapper key (e.g. "idf_component:")
                for v in doc.values():
                    if isinstance(v, dict) and v.get("dependencies") is not None:
                        cand, wrapped = v["dependencies"], True
                        break
            if cand is None:  # ... or deeper (entries of a `components:` list)
                for sub in _nested_dependencies(doc):
                    got, _ = _esp_deps(sub)
                    if got:
                        deps = {**(deps or {}), **got}
                if deps:
                    malformed = True
            else:
                deps, malformed = _esp_deps(cand)
                malformed = malformed or wrapped
        m = re.search(r"^dependencies:[ \t]*\n((?:(?:[ \t]+.*|-(?:[ \t].*)?|[ \t]*)(?:\n|$))+)", body, re.M) \
            if deps is None else None
        if m:
            # a file YAML cannot read (cut off at the token limit, or broken further down): its
            # top-level `dependencies:` block alone (indented lines, or list items at the first
            # column, up to the next top-level key) is read with the same rules, cut back to the
            # last line at which it parses
            blk = m.group(1).rstrip().splitlines()
            while blk:
                try:
                    sub = yaml.safe_load("dependencies:\n" + "\n".join(blk))
                except yaml.YAMLError:
                    blk = blk[:-1]
                    continue
                deps, malformed = _esp_deps(sub.get("dependencies") if isinstance(sub, dict) else None)
                break
        if deps is None:
            continue
        for k, v in deps.items():
            d = _espidf_key(k, v, "idf_component.yml")
            if d:
                if malformed:
                    d.flags = list(d.flags) + ["malformed_manifest"]
                out.append(d)
    for toks, _ in command_tokens(text, r"\bidf\.py\s+add-dependency\s+"):
        git, pos, k = None, [], 0
        while k < len(toks):
            t = toks[k]
            if t.split("=", 1)[0] in IDF_OPTS_WITH_VALUE:
                if t.split("=", 1)[0] == "--git":
                    git = t.split("=", 1)[1] if "=" in t else (toks[k + 1] if k + 1 < len(toks) else "")
                k += 1 if "=" in t else 2
                continue
            if not t.startswith("-") and not VERSION_TOKEN.match(t):
                pos.append(t)
            k += 1
        if git is not None:
            for t in pos:
                out.append(Decl("espidf", t, "url", via="idf.py add-dependency", raw=git or t))
            continue
        for t in pos:
            if _is_url(t) or REPO_PATH.match(t):     # a repository written without its scheme
                out.append(Decl("espidf", t, "url", via="idf.py add-dependency", raw=t))
                continue
            m = ESP_DEPENDENCY.match(t.strip())
            name = m.group(1) if m else re.split(r"[\^~=<>!*]", t, maxsplit=1)[0].strip()
            if name:
                d = _espidf_key(name, None, "idf.py add-dependency")
                if d:
                    out.append(d)
    return out


# --------------------------------------------------------------------------
# ROS 2

ROS_TAGS = ("depend", "build_depend", "exec_depend", "build_export_depend", "buildtool_depend",
            "buildtool_export_depend", "test_depend", "run_depend", "doc_depend")


def _ros2(text: str) -> tuple[list[Decl], set]:
    out, local = [], set()
    xml_blocks = [b for _, b, _ in code_blocks(text) if "<package" in b or "<depend" in b or "_depend>" in b]
    for body in xml_blocks:
        # package.xml readers (catkin_pkg, rosdep, colcon) skip comments and read dependencies only
        # as children of <package>, not inside <export>; an unclosed comment runs to the end
        body = re.sub(r"<!--.*?(?:-->|\Z)", "", body, flags=re.S)
        body = re.sub(r"<export\b[^>]*(?<!/)>.*?(?:</export>|\Z)", "", body, flags=re.S)
        for m in re.finditer(r"<name>\s*([^<\s]+)\s*</name>", body):
            local.add(m.group(1).strip())
        for tag in ROS_TAGS:
            for m in re.finditer(rf"<{tag}(?:\s[^>]*)?>\s*([^<]+?)\s*</{tag}>", body):
                out.append(Decl("ros2", m.group(1).strip(), via=f"package.xml:{tag}", raw=m.group(0)))
    for toks, _ in command_tokens(text, r"(?:\bsudo\s+)?\bapt(?:-get)?\s+install\s+"):
        for t in toks:
            mm = re.match(r"^ros-(humble|jazzy|iron|rolling|foxy|galactic|noetic|kilted)-([a-z0-9-]+)$", t)
            if mm:
                # snapshots cover humble and jazzy; packages of other distributions cannot be
                # resolved here, so the gate gets no evidence for them (it asks for it)
                kind = "registry" if mm.group(1) in ("humble", "jazzy") else "unsupported"
                out.append(Decl("ros2", mm.group(2).replace("-", "_"), kind, via=f"apt:ros-{mm.group(1)}", raw=t,
                                flags=[f"distro:{mm.group(1)}"]))
    return out, local


# --------------------------------------------------------------------------
# Rust / Cargo


def _cargo_table(deps: dict, via: str) -> list[Decl]:
    out = []
    for k, v in deps.items():
        name = k
        if isinstance(v, dict):
            if v.get("workspace") is True:
                out.append(Decl("crates", k, "special", via=via, raw=k, flags=["workspace"]))
                continue
            if v.get("git"):
                out.append(Decl("crates", k, "url", via=via, raw=k))
                continue
            if v.get("path"):
                out.append(Decl("crates", k, "path", via=via, raw=k))
                continue
            if v.get("package"):
                name = v["package"]
            ver = v.get("version")
        else:
            ver = str(v)
        out.append(Decl("crates", str(name), via=via, version=ver, raw=k))
    return out


def _walk_deps(doc: dict, prefix="") -> list[tuple[str, dict]]:
    found = []
    for k, v in doc.items():
        if isinstance(v, dict):
            if k.endswith("dependencies"):
                found.append((prefix + k, v))
            else:
                found += _walk_deps(v, prefix + k + ".")
    return found


def _crates(text: str) -> tuple[list[Decl], set]:
    out, local, ws_defined = [], set(), set()
    for lang, body, _ in code_blocks(text):
        if not re.search(r"^\s*\[(?:package|dependencies|dev-dependencies|build-dependencies|target\.|workspace\.dependencies)",
                         body, re.M):
            continue
        try:
            doc = tomllib.loads(body)
            pkg = doc.get("package", {})
            if isinstance(pkg, dict) and pkg.get("name"):
                local.add(str(pkg["name"]))
            for section, deps in _walk_deps(doc):
                if section.split(".")[-1] in ("dependencies", "dev-dependencies", "build-dependencies"):
                    if section.startswith("workspace."):
                        ws_defined.update(str(k) for k in deps)
                    out += _cargo_table(deps, f"Cargo.toml:{section.split('.')[-1]}")
        except tomllib.TOMLDecodeError:
            sec = None
            for ln in body.splitlines():
                s = ln.strip()
                hm = re.match(r"^\[([^\]]+)\]$", s)
                if hm:
                    sec = hm.group(1)
                    continue
                if sec and sec.split(".")[-1].strip("'\"") in ("dependencies", "dev-dependencies", "build-dependencies"):
                    dm = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$", s)
                    if dm:
                        val = dm.group(2)
                        kind = ("url" if re.search(r"\bgit\s*=", val)
                                else "path" if re.search(r"\bpath\s*=", val) else "registry")
                        pm = re.search(r"package\s*=\s*\"([^\"]+)\"", val)
                        out.append(Decl("crates", pm.group(1) if pm else dm.group(1), kind,
                                        via=f"Cargo.toml:{sec}", raw=s, flags=["toml_fallback"]))
                elif sec == "package":
                    nm = re.match(r'^name\s*=\s*"([^"]+)"', s)
                    if nm:
                        local.add(nm.group(1))
    for toks, _ in command_tokens(text, r"\bcargo\s+add\s+"):
        k = 0
        while k < len(toks):
            t = toks[k]
            if t in ("-F", "--features", "--git", "--path", "--rename", "-p", "--package",
                     "--branch", "--tag", "--rev", "--registry", "--target"):
                k += 2
                continue
            if t.startswith("-"):
                k += 1
                continue
            name, _, ver = t.partition("@")
            if name:
                out.append(Decl("crates", name, via="cargo add", version=ver or None, raw=t))
            k += 1
    # `dep = { workspace = true }` refers to the workspace root: covered if the answer declares the
    # root entry, otherwise the source of the dependency is unknown and the gate gets no evidence
    for d in out:
        if "workspace" in d.flags and d.name not in ws_defined:
            d.kind = "unsupported"
    return out, local


# --------------------------------------------------------------------------
# MicroPython


CALL_ARG = re.compile(r"""\s*['"]([^'"]+)['"]""")


def _call_args(text: str, head: str) -> list[str]:
    """First string argument of a call such as mip.install("x") in code context."""
    out = []
    for args, _ in command_hits(text, head, call=True):
        m = CALL_ARG.match(args)
        if m:
            out.append(m.group(1).strip())
    return out


def _micropython(text: str) -> list[Decl]:
    out = []
    for n in _call_args(text, r"\bmip\.install\("):
        out.append(Decl("micropython", n, "url" if _is_url(n) or ":" in n or "/" in n else "registry",
                        via="mip.install", raw=n))
    for toks, _ in command_tokens(text, r"\bmpremote\s+(?:[\w:./-]+\s+)*?mip\s+install\s+"):
        for t in toks:
            if t.startswith("-"):
                continue
            out.append(Decl("micropython", t, "url" if _is_url(t) or ":" in t or "/" in t else "registry",
                            via="mpremote mip", raw=t))
    for n in _call_args(text, r"\bu?pip\.install\("):
        out.append(Decl("pypi", n, via="upip.install", raw=n, flags=["upip"]))
    # the Unix port's command line: `micropython -m mip install X` (micropython-lib) and
    # `micropython -m upip install X` (PyPI, as upip.install)
    for mod, eco in (("mip", "micropython"), ("upip", "pypi")):
        for toks, _ in command_tokens(text, rf"\bmicropython\s+-m\s+{mod}\s+install\s+"):
            k = 0
            while k < len(toks):
                t = toks[k]
                if t in ("-p", "-r", "-i", "--target", "--index"):
                    k += 2
                    continue
                if not t.startswith("-"):
                    kind = "url" if _is_url(t) or (eco == "micropython" and (":" in t or "/" in t)) else "registry"
                    out.append(Decl(eco, t, kind, via=f"micropython -m {mod}", raw=t,
                                    flags=["upip"] if mod == "upip" else []))
                k += 1
    return out


# --------------------------------------------------------------------------
# PyPI

REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([<>=!~][^;#]*)?\s*(;[^#]*)?\s*(#.*)?$")
STRICT_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*((?:===|==|>=|<=|!=|~=|>|<)\s*[^;#\s][^;#]*)?"
                        r"\s*(;[^#]*)?\s*(#.*)?$")


# a file name (a project tree: main.py, requirements.txt, README.md) is no requirement specifier
FILE_LIKE = re.compile(r"\.(?:py|txt|md|rst|json|ya?ml|toml|cfg|ini|html?|css|js|csv|xml|sh|lock|log|h|hpp|c|cpp|ino)$", re.I)


def spec_line(l: str) -> bool:
    """A line that reads as one requirement specifier and not as a file name."""
    m = STRICT_REQ.match(l)
    return bool(m) and not FILE_LIKE.search(m.group(1))


REQ_CTX = 160   # characters before a fence in which the word "requirements" introduces it
REQ_EXPLICIT = ("requirements", "requirements.txt", "pip", "python-requirements")
REQ_PLAIN = ("", "txt", "text", "plaintext", "markdown", "md")
REQ_MISLABELLED = ("python", "py", "python3", "bash", "sh", "shell", "console")


def requirements_block(lang: str, body: str, ctx: str, task_eco: str | None = None) -> list[str] | None:
    """The lines of a fenced block that is a pip requirements list, else None; one rule for the
    extractor and the parser check (validity.py).  ``ctx`` = the REQ_CTX characters before the fence.

    A fence labelled as requirements, or whose first line is a comment naming requirements, is a
    requirements list.  An unlabelled, plain-text or markdown fence, or one labelled as Python or
    shell, counts only if every line is a requirement specifier and the text right before it
    mentions requirements or the task is a PyPI task (which asks for requirements.txt), so that code
    or configuration is never read as requirements.  Other labels (ini, yaml, ...) never count."""
    first = next((l.strip() for l in body.splitlines() if l.strip()), "")
    headed = first.startswith("#") and "requirements" in first.lower()
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return None
    if lang in REQ_EXPLICIT or (headed and lang in REQ_PLAIN):
        return lines
    if lang in REQ_PLAIN + REQ_MISLABELLED and (headed or "requirements" in ctx.lower() or task_eco == "pypi") and \
            all(spec_line(l) or l.startswith(("-r ", "-e ", "--")) or _is_url(l) for l in lines):
        return lines
    return None


def _pip_args(argstr: str, via: str) -> list[Decl]:
    out = []
    toks = _split_args(argstr)
    k = 0
    while k < len(toks):
        t = toks[k]
        if t in ("-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url",
                 "-f", "--find-links", "--target", "-t", "--prefix", "--root", "--platform", "--python-version"):
            k += 2
            continue
        if t in ("-e", "--editable"):
            if k + 1 < len(toks):
                kind = "url" if _is_url(toks[k + 1]) else "path"
                out.append(Decl("pypi", toks[k + 1], kind, via=via, raw=toks[k + 1]))
            k += 2
            continue
        if t.startswith("-"):
            k += 1
            continue
        if _is_url(t):
            out.append(Decl("pypi", t, "url", via=via, raw=t))
        elif _is_path(t) or t.endswith((".whl", ".tar.gz", ".zip")):
            out.append(Decl("pypi", t, "path", via=via, raw=t))
        else:
            m = REQ_LINE.match(t)
            if m:
                out.append(Decl("pypi", m.group(1), via=via, raw=t, version=(m.group(3) or "").strip() or None))
        k += 1
    return out


REQ_HEADER = re.compile(r"^\s*(?:[#*_`>]+\s*)?requirements\.txt\s*[*_`]*\s*:?\s*$", re.I)
# a short line that names the file among a few words ("2. **Create a `requirements.txt`**:")
REQ_HEADER_WORDS = re.compile(r"^\s*(?:(?:[-*+\u2022]|\d+[.)])\s+)?[#*_>\s]*(?:[A-Za-z]+\s+){0,4}[*_`]*"
                              r"requirements\.txt[*_`]*\s*[*_]*\s*:?\s*$", re.I)
REQ_ITEM = re.compile(r"^(?:(?:[-*+\u2022]|\d+[.)])\s+)?`([^`]+)`\s*$")


def _req_item(line: str) -> str | None:
    """A specifier line of an unfenced list: plain, or a list item that is one code span."""
    s = line.strip()
    m = REQ_ITEM.match(s)
    if m:
        s = m.group(1).strip()
    elif s[:1] in "-*+\u2022":
        return None
    return s if spec_line(s) else None


def _unfenced_requirements(text: str) -> list[str]:
    """A requirements.txt written without a fence: a line that only names the file, then (after at
    most one blank line) lines that are all requirement specifiers, up to a blank line or other text."""
    spans = [(m.start(), m.end()) for m in FENCE.finditer(text)]
    lines, out, pos = text.splitlines(keepends=True), [], 0
    starts = []
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    fenced = [any(a <= st < b for a, b in spans) for st in starts]
    i = 0
    while i < len(lines):
        if not fenced[i] and (REQ_HEADER.match(lines[i]) or REQ_HEADER_WORDS.match(lines[i])):
            j = i + 1
            if j < len(lines) and not lines[j].strip():
                j += 1
            block = []
            while j < len(lines) and not fenced[j] and lines[j].strip() and _req_item(lines[j]):
                block.append(_req_item(lines[j]))
                j += 1
            out += block
            i = max(j, i + 1)
            continue
        i += 1
    return out


def _pypi(text: str, task_eco: str | None = None) -> list[Decl]:
    out = []
    pip_cmd = r"(?:\bsudo\s+(?:-H\s+)?)?(?:\bpython3?(?:\.\d+)?\s+-m\s+)?(?<![\w.])pip3?\s+install\s+"
    for toks, _ in command_tokens(text, pip_cmd):
        out += _pip_args(" ".join(shlex.quote(t) for t in toks), "pip install")
    blocks = [(requirements_block(lang, body, text[max(0, start - REQ_CTX):start], task_eco), "requirements.txt")
              for lang, body, start in code_blocks(text)]
    # the unfenced list keeps a via of its own: the parser check (validity.py) reads fenced files only
    blocks.append((_unfenced_requirements(text), "unfenced requirements.txt"))
    for lines, via in blocks:
        if not lines:
            continue
        for l in lines:
            if l.startswith("-"):
                if l.startswith(("-e ", "--editable")):
                    out += _pip_args(l, via)
                continue
            if _is_url(l) or " @ " in l:
                out.append(Decl("pypi", l.split(" @ ")[0].strip(), "url", via=via, raw=l))
                continue
            m = REQ_LINE.match(l)
            if m:
                out.append(Decl("pypi", m.group(1), via=via, raw=l,
                                version=(m.group(3) or "").strip() or None))
    return out


# --------------------------------------------------------------------------
# npm (Node-RED palette)


def _npm_name(t: str) -> tuple[str, str | None]:
    if t.startswith("@"):
        at = t.find("@", 1)
        return (t, None) if at == -1 else (t[:at], t[at + 1:] or None)
    name, _, ver = t.partition("@")
    return name, ver or None


def _npm(text: str) -> list[Decl]:
    out = []
    cmd = r"(?:\bsudo\s+)?\b(?:npm\s+(?:install|i|add|isntall)|yarn\s+add|pnpm\s+(?:add|install))\s+"
    for toks, _ in command_tokens(text, cmd):
        k = 0
        while k < len(toks):
            t = toks[k]
            if t in ("--prefix", "-C", "--registry", "--tag"):
                k += 2
                continue
            if t.startswith("-"):
                k += 1
                continue
            if _is_url(t) or t.startswith(("github:", "git:")):
                out.append(Decl("npm", t, "url", via="npm install", raw=t))
            elif _is_path(t) or t.endswith(".tgz"):
                out.append(Decl("npm", t, "path", via="npm install", raw=t))
            else:
                name, ver = _npm_name(t)
                if re.match(r"^(@[a-z0-9._~-]+/)?[a-z0-9._~-]+$", name, re.I):
                    out.append(Decl("npm", name, via="npm install", version=ver, raw=t))
            k += 1
    for lang, body, _ in code_blocks(text):
        if lang not in ("json", "") or '"dependencies"' not in body:
            continue
        try:
            doc = json.loads(body)
            deps = {}
            for sec in ("dependencies", "devDependencies"):
                if isinstance(doc.get(sec), dict):
                    deps.update(doc[sec])
        except (json.JSONDecodeError, AttributeError):
            m = re.search(r'"dependencies"\s*:\s*\{([^}]*)\}', body, re.S)
            deps = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', m.group(1))) if m else {}
        for k, v in deps.items():
            kind = "url" if _is_url(str(v)) or str(v).startswith(("github:", "git")) else \
                "path" if str(v).startswith(("file:", "link:")) else "registry"
            out.append(Decl("npm", k, kind, via="package.json", version=str(v), raw=k))
    return out


# --------------------------------------------------------------------------


_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S | re.I)


def strip_reasoning(text: str) -> tuple[str, bool]:
    """Drop reasoning blocks that some servers inline into the answer
    (``<think>...</think>``; an unterminated block runs to the end)."""
    out = _THINK.sub("", text)
    return out, out != text


def extract(text: str, task_eco: str, arduino_names: set | None = None) -> dict:
    """Return {'decls': [...], 'local': [...], 'n_blocks': int, 'reasoning_stripped': bool}."""
    text, stripped = strip_reasoning(text)
    decls: list[Decl] = []
    decls += _arduino(text, arduino_names)
    decls += _platformio(text)
    decls += _espidf(text, task_eco)
    ros, local_ros = _ros2(text)
    decls += ros
    cr, local_cr = _crates(text)
    decls += cr
    decls += _micropython(text)
    decls += _pypi(text, task_eco)
    decls += _npm(text)
    # Only ROS workspace packages are built from source next to each other; a Cargo registry
    # entry is fetched from crates.io even if another crate of the answer has that name
    # (local Cargo crates are declared with `path` or `workspace = true`).
    local = set(local_ros)
    seen, uniq = set(), []
    for d in decls:
        # a name without a letter or digit (a line-continuation backslash, an empty list `[]`, a `//`
        # comment after a command) is no package name in any of the registries
        # (a path may be the current directory: `pip install .`)
        if not d.name or len(d.name) > 214 or not (re.search(r"[A-Za-z0-9]", d.name)
                                                  or (d.kind == "path" and d.name.rstrip("/") in (".", ".."))):
            continue
        k = d.key()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(d)
    return {"decls": [d.to_dict() for d in uniq], "local": sorted(local), "n_blocks": len(code_blocks(text)),
            "reasoning_stripped": stripped}
