"""Manifest validity and an independent reading of the manifests (PROTOCOL.md, section 5.2).

For a response this module finds the manifest files in its fenced code blocks, independently of
``extract.py``, and parses each with the parser that the ecosystem's own tooling uses:

  Cargo.toml         tomllib (TOML 1.0, the grammar Cargo reads); dependency tables
  package.json       json (npm reads JSON); dependencies, devDependencies, peer-, optional-
  requirements.txt   packaging.requirements (pip's requirement grammar); option lines skipped
  platformio.ini     configparser (PlatformIO's ProjectConfig is a ConfigParser); lib_deps
  idf_component.yml  yaml and the component manager's top-level schema (a ``dependencies`` mapping)
  package.xml        catkin_pkg.package.parse_package_string (the parser of the ROS tooling)

A block that does not parse, or that violates the schema the tool requires, is *malformed*.  For
well-formed blocks the dependency names the official reading yields are compared with the names
that ``extract.py`` took from manifest files of the same response: the agreement measures how
faithfully the extractor reproduces the tools' own reading.  It does not replace the manual
validation of the protocol (which also judges what a reader would install from prose and
commands); install commands are not files and are only counted here.
"""

from __future__ import annotations

import configparser
import json
import re
import tomllib
import warnings

import yaml

from .extract import FENCE, REQ_CTX, STRICT_REQ, _is_path, _is_url, requirements_block, strip_reasoning
from .registries import norm_pypi

CARGO_SIG = re.compile(r"^\s*\[(?:package|dependencies|dev-dependencies|build-dependencies|target\.|workspace\.dependencies)",
                       re.M)
FILE_VIAS = ("Cargo.toml", "package.json", "lib_deps", "idf_component.yml", "package.xml", "requirements.txt")


def _blocks(text: str):
    for m in FENCE.finditer(text):
        yield m.group(1).strip().lower(), m.group(2), m.start(), m.group(0).rstrip().endswith("```")


def _norm(eco: str, name: str) -> str:
    name = name.strip()
    if eco == "pypi":
        return norm_pypi(name)
    if eco == "npm":
        return name
    if eco == "espidf":
        return name.lower() if "/" in name else f"espressif/{name.lower()}"
    return name.lower()


def _cargo(body: str):
    doc = tomllib.loads(body)
    names = set()

    def walk(d, path=""):
        for k, v in d.items():
            if isinstance(v, dict):
                if k in ("dependencies", "dev-dependencies", "build-dependencies") and not path.startswith("workspace"):
                    for dk, dv in v.items():
                        if isinstance(dv, dict) and (dv.get("git") or dv.get("path") or dv.get("workspace")):
                            continue
                        names.add(str(dv.get("package") if isinstance(dv, dict) and dv.get("package") else dk))
                else:
                    walk(v, f"{path}.{k}" if path else k)
    walk(doc)
    return "crates", names


def _npm(body: str):
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise ValueError("package.json is not an object")
    names = set()
    for sec in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        v = doc.get(sec)
        if v is None:
            continue
        if not isinstance(v, dict):
            raise ValueError(f"{sec} is not an object")
        for k, spec in v.items():
            s = str(spec)
            if _is_url(s) or s.startswith(("github:", "git", "file:", "link:")):
                continue
            names.add(k)
    return "npm", names


def _pio(body: str):
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None, strict=False)
    cp.read_string(body)
    names = set()
    for sec in cp.sections():
        if not cp.has_option(sec, "lib_deps"):
            continue
        for part in re.split(r"[\n,]", cp.get(sec, "lib_deps")):
            e = part.strip().strip("'\"")
            if not e or e.startswith("${") or _is_url(e) or "github.com/" in e or _is_path(e):
                continue
            spec = re.split(r"\s*@\s*|\s*[=<>~^]", e, maxsplit=1)[0].strip()
            if spec.isdigit() or not spec:
                continue
            # PlatformIO package specification: [owner/]name with a version requirement after '@'
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+ -]+)?", spec):
                raise ValueError(f"lib_deps entry is not a package specification: {e[:60]}")
            names.add(spec.split("/", 1)[1] if "/" in spec else spec)
    return "platformio", names


def _idf(body: str):
    doc = yaml.safe_load(body)
    if not isinstance(doc, dict) or not isinstance(doc.get("dependencies"), dict):
        raise ValueError("idf_component.yml needs a top-level 'dependencies' mapping")
    names = set()
    for k, v in doc["dependencies"].items():
        k = str(k)
        if k == "idf" or (isinstance(v, dict) and (v.get("git") or v.get("path") or v.get("override_path"))):
            continue
        names.add(k)
    return "espidf", names


def _ros(body: str):
    from catkin_pkg.package import parse_package_string
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pkg = parse_package_string(body)
    names = set()
    for attr in ("build_depends", "buildtool_depends", "build_export_depends", "buildtool_export_depends",
                 "exec_depends", "test_depends", "doc_depends", "run_depends"):
        for d in getattr(pkg, attr, None) or []:
            names.add(d.name)
    return "ros2", names


def _reqs(body: str):
    from packaging.requirements import InvalidRequirement, Requirement
    names = set()
    for raw in body.splitlines():
        ln = raw.split(" #", 1)[0].strip()
        if not ln or ln.startswith("#") or ln.startswith("-"):
            continue
        if _is_url(ln) or " @ " in ln:
            continue
        try:
            names.add(Requirement(ln).name)
        except InvalidRequirement as e:
            raise ValueError(str(e)) from e
    return "pypi", names


def _kind(lang: str, body: str, ctx: str, task_eco: str, ctx_req: str = "") -> str | None:
    if "<package" in body:
        return "package.xml"
    if CARGO_SIG.search(body):
        return "Cargo.toml"
    if lang in ("json", "") and '"dependencies"' in body:
        return "package.json"
    if re.search(r"^\s*lib_deps\s*[=:]", body, re.M):
        return "platformio.ini"
    if "dependencies" in body and lang in ("yaml", "yml", "", "text", "idf_component.yml") and \
            (task_eco == "espidf" or "idf_component" in ctx or "idf_component" in body):
        return "idf_component.yml"
    if requirements_block(lang, body, ctx_req, task_eco):      # the extractor's own rule (extract.py)
        return "requirements.txt"
    return None


PARSERS = {"Cargo.toml": _cargo, "package.json": _npm, "platformio.ini": _pio, "idf_component.yml": _idf,
           "package.xml": _ros, "requirements.txt": _reqs}


def assess(text: str, task_eco: str) -> dict:
    """Manifest files of one response, their validity and their official dependency names."""
    text, _ = strip_reasoning(text or "")
    files = []
    for lang, body, start, closed in _blocks(text):
        ctx = text[max(0, start - 200):start].lower()
        kind = _kind(lang, body, ctx, task_eco, text[max(0, start - REQ_CTX):start])
        if not kind:
            continue
        rec = {"kind": kind, "closed": closed}
        try:
            eco, names = PARSERS[kind](body)
            # a name longer than 214 characters (npm's limit, the longest of the registries) or
            # without a letter or digit is no declaration (PROTOCOL.md, section 5); the extractor
            # drops it, and so does the official reading
            names = {n for n in names if len(n) <= 214 and re.search(r"[A-Za-z0-9]", n)}
            rec.update({"valid": True, "eco": eco, "names": sorted(_norm(eco, n) for n in names)})
        except Exception as e:  # noqa: BLE001 - any parser failure makes the file malformed
            rec.update({"valid": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
        files.append(rec)
    return {"files": files, "n_files": len(files), "n_valid": sum(1 for f in files if f["valid"]),
            "n_malformed": sum(1 for f in files if not f["valid"])}


def extractor_names(deps: list[dict], any_via: bool = False) -> dict[str, set]:
    """Names our extractor took from manifest files (registry kind only), per ecosystem; with
    ``any_via`` also those it took from install commands (a name declared in a file and in a command
    is kept once, with the first place it was found)."""
    out: dict[str, set] = {}
    for d in deps:
        if not any_via and not any(d.get("via", "").startswith(v) for v in FILE_VIAS):
            continue
        if d.get("kind") in ("url", "path", "special", "legacy_id"):
            continue
        nm = d["name"]
        if d["eco"] == "espidf":
            nm = f"{d.get('owner') or 'espressif'}/{nm}"
        out.setdefault(d["eco"], set()).add(_norm(d["eco"], nm))
    return out
