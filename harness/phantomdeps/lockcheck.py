"""Lock-file check: what the ecosystems' own lock tools write for the generated manifests.

The pin item of the gate assumed that where the standard tooling locks artifact digests
(crates.io/Cargo, npm, PyPI) the pipeline pins every resolved dependency by digest.  This
module replaces the assumption with a measurement.

Measured.  Unit: one response (run_id, model_id, prompt_id, sample_idx; sample_idx == 0 only)
and one ecosystem in {crates, npm, pypi}.  Its declarations of that ecosystem with
``cls == 'exists'`` go into a minimal manifest (Cargo.toml: probe [package], [workspace], each
crate in the section it was declared in; package.json: "dependencies"; requirements.in: one
requirement per line) with the declared version constraint.  No constraint, or one the
ecosystem's grammar rejects (Cargo requirement, npm range or dist-tag, PEP 440 specifier), is
written as any version (``*``; a bare name for PyPI); a rejected one is noted in
``constraint_note``.  The real tool resolves the manifest in lock-only mode, one manifest at a
time, under a time limit and ``nice``, with configuration and caches made for this run only
(one per tool, deleted afterwards): ``cargo generate-lockfile`` -> Cargo.lock version and
checksum; ``npm install --package-lock-only --ignore-scripts --no-audit --no-fund`` ->
package-lock.json version and integrity; ``uv pip compile --generate-hashes --no-build`` for
one stated Python version and platform -> requirements.txt pin and --hash entries.  Kept per
manifest: manifest, command, exit code, wall time, stderr tail; per declared dependency: the
locked version, whether the lock records a digest for it (algorithm), the resolver's error.
A manifest that does not lock pins none of its dependencies; each of its declarations is then
resolved alone (``isolated``) to find what breaks the lock: ``version_not_found``,
``not_found`` (for crates with the index's yanked count), ``needs_build`` (PyPI: only a source
distribution would do; not attempted), ``target_incompatible`` (PyPI: no artifact or
Requires-Python fits the target), ``dependency_unresolvable`` (a dependency of it fails),
``blocked`` (it locks alone) or ``conflict`` (all lock alone, not together).  Outcomes are
read from the tools' messages (``_failure``), which are kept.  ``lock_totals`` counts every
package of a written lock, transitive ones included, those with a digest, and (npm) bundled
ones, which have none of their own: the tarball that bundles them carries it.
Provenance, presence only, of the locked versions of declared dependencies:
npm ``dist.attestations`` of the version document (predicateType values); PyPI files of the
version with a PEP 740 ``provenance`` URL in the simple JSON index, and for one such file the
attestation count, publisher kind and statement predicateType; crates.io has no attestation
API (null).  HTTP answers: about 5 requests/s, cached with retrieval time in
``data/cache/lockcheck/``.

Not measured.  No package code runs and no artifact is downloaded: Cargo reads the sparse
index only, npm installs nothing and runs no scripts, uv builds no sdist and reads index pages
and wheel metadata only.  Digests are those the lock files record (registry metadata), not
recomputed from artifacts.  Attestations are not verified: no signature, certificate,
transparency log or publisher identity is checked; transitive packages are not checked.
PyPI outcomes hold for the stated target only; all outcomes for the registries at
``generated_at``.  Names a tool reports missing that are not declarations of the manifest are
masked in stored messages (``util.mask_key``; PROTOCOL.md, section 9).

Usage: ``cd harness && python3 -m phantomdeps.lockcheck RUN_ID [RUN_ID ...]`` writes
``data/evidence/lockcheck.json``; ``pins()`` reads it into the ``pins`` argument of
``gate.evidence_for``.
"""

from __future__ import annotations

import argparse
import base64
import collections
import gzip
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
from pathlib import Path

from .registries import crates_path, norm_pypi
from .util import (CACHE, EVIDENCE, MASK_KEY, ROOT, RUNS, USER_AGENT, http_get, mask_key, quote,
                   read_jsonl, sha256_file, utcnow, write_json)

try:  # PEP 440 grammar and version equality; a regex fallback keeps the module usable without it
    import packaging
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover
    packaging = SpecifierSet = Version = None

ECOS = ("crates", "npm", "pypi")
OUT = EVIDENCE / "lockcheck.json"   # read by the analysis, not by classification
HTTP_CACHE = CACHE / "lockcheck"
TIMEOUT_S = 180
TAIL = 1000            # characters of stderr kept per manifest
ISO_TAIL = 600         # ... per isolated run that failed
NICE = 10              # the tools share 2 vCPUs with a long generation job
NODE_HEAP_MB = 1024    # keeps npm from pushing the host into the OOM killer
RATE_S = 0.2           # at most about 5 HTTP requests per second
PROBE = "phantomdeps-lockcheck-probe"
CARGO_EDITION = "2021"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_INDEX = "https://pypi.org/simple"
PEP691 = "application/vnd.pypi.simple.v1+json"
PY_VERSION = "3.13"
PY_PLATFORM = "x86_64-unknown-linux-gnu"   # uv: manylinux_2_28 x86_64
ANY = {"crates": "*", "npm": "*", "pypi": ""}
LOCK_FILE = {"crates": "Cargo.lock", "npm": "package-lock.json", "pypi": "requirements.txt"}

# --------------------------------------------------------------------------
# declared constraints

_NUM = r"(?:0|[1-9]\d*)"
_PRE = r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
_CPART = rf"(?:{_NUM}|[*xX])"
_CCOMP = rf"(?:=|>=|<=|>|<|~|\^)?\s*(?:[*xX]|{_NUM}(?:\.{_CPART}(?:\.{_CPART}{_PRE})?)?)"
CARGO_REQ = re.compile(rf"^\s*{_CCOMP}(?:\s*,\s*{_CCOMP})*\s*$")
_XR = rf"(?:[xX*]|{_NUM})"
_PARTIAL = rf"v?{_XR}(?:\.{_XR}(?:\.{_XR}{_PRE})?)?"
_SIMPLE = rf"(?:<=|>=|<|>|=|~>?|\^)?\s*{_PARTIAL}"
_RANGE = rf"(?:{_PARTIAL}\s+-\s+{_PARTIAL}|{_SIMPLE}(?:\s+{_SIMPLE})*)"
NPM_RANGE = re.compile(rf"^\s*{_RANGE}(?:\s*\|\|\s*{_RANGE})*\s*$")
NPM_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
PEP440_CLAUSE = re.compile(r"^\s*(?:~=|===|==|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*$")
GRAMMAR = {"crates": "Cargo version requirement", "npm": "npm semver range or dist-tag",
           "pypi": "PEP 440 version specifier"}


def _pep440_ok(v: str) -> bool:
    if SpecifierSet is not None:
        try:
            SpecifierSet(v)
            return True
        except InvalidSpecifier:
            return False
    return all(PEP440_CLAUSE.match(c) for c in v.split(","))


def normalise_constraint(eco: str, declared) -> tuple[str, str | None]:
    """(constraint written into the manifest, note if the declared one was replaced)."""
    if declared is None or not str(declared).strip():
        return ANY[eco], None
    v = str(declared).strip()
    ok = (bool(CARGO_REQ.match(v)) if eco == "crates" else
          bool(NPM_RANGE.match(v) or NPM_TAG.match(v)) if eco == "npm" else _pep440_ok(v))
    if ok:
        return v, None
    return ANY[eco], f"declared constraint {v!r} is not a {GRAMMAR[eco]}; written as any version"


def _nkey(eco: str, name: str) -> str:
    """Name as the tool matches it: PEP 503 for PyPI, exact for Cargo and npm."""
    return norm_pypi(name) if eco == "pypi" else name


SELECTION_FIELDS = ("run_id", "model_id", "prompt_id", "sample_idx", "eco", "name", "owner", "kind", "via",
                    "version", "canonical", "tau_lo", "tau_hi")


def selection_digest(rows) -> str:
    """SHA-256 over what the check reads from the selected declarations (not over the whole file,
    which also carries classification timings that change with every run of classify)."""
    import hashlib
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps([r.get(k) for k in SELECTION_FIELDS], ensure_ascii=False).encode() + b"\n")
    return h.hexdigest()


def load_declarations(run_ids: list[str], masked: bool = False) -> tuple[dict, dict]:
    """{(run_id, model_id, prompt_id, sample_idx, eco): [deps.jsonl rows]} and input hashes.
    ``masked`` reads the pseudonymised copies (the review artifact has no raw declarations): the rows
    the check selects, existing packages, are the same there, so the selection digest is too."""
    groups: dict = {}
    inputs = {}
    for rid in run_ids:
        p = RUNS / rid / ("deps_masked.jsonl" if masked else "deps.jsonl")
        if not p.exists():
            raise FileNotFoundError(f"{p}: run {rid!r} has no classified declarations (run classify first)")
        sel = []
        for r in read_jsonl(p):
            if r.get("sample_idx") != 0 or r.get("eco") not in ECOS or r.get("cls") != "exists":
                continue
            key = (r["run_id"], r["model_id"], r["prompt_id"], r["sample_idx"], r["eco"])
            groups.setdefault(key, []).append(r)
            sel.append(r)
        inputs[rid] = {"path": str(p.relative_to(ROOT)), "sha256": sha256_file(p), "rows_selected": len(sel),
                       "selection_sha256": selection_digest(sel)}
    return groups, inputs


def _declared(eco: str, rows: list[dict]) -> tuple[list[dict], list[str]]:
    deps, seen, dropped = [], set(), []
    for r in sorted(rows, key=lambda r: (r["name"].lower(), r["name"])):
        k = _nkey(eco, r["name"])
        if k in seen:
            dropped.append(r["name"])
            continue
        seen.add(k)
        constraint, note = normalise_constraint(eco, r.get("version"))
        normalised = note is not None
        if r.get("version") is None and "toml_fallback" in (r.get("flags") or []):
            note = "the extractor read this Cargo.toml with its line fallback and kept no version; written as any version"
        d = {"name": r["name"], "declared_version": r.get("version"), "constraint": constraint,
             "constraint_normalised": normalised, "constraint_note": note}
        if eco == "crates":
            via = r.get("via") or ""
            d["section"] = ("build-dependencies" if via.endswith("build-dependencies") else
                            "dev-dependencies" if via.endswith("dev-dependencies") else "dependencies")
        if r.get("flags"):
            d["flags"] = list(r["flags"])
        deps.append(d)
    return deps, dropped


def _manifest(eco: str, deps: list[dict]) -> tuple[str, str]:
    if eco == "crates":
        lines = ["[package]", f'name = "{PROBE}"', 'version = "0.0.0"', f'edition = "{CARGO_EDITION}"',
                 "publish = false", "", "[workspace]", ""]
        for sec in ("dependencies", "build-dependencies", "dev-dependencies"):
            ds = [d for d in deps if d["section"] == sec]
            if ds:
                lines.append(f"[{sec}]")
                lines += [f"{json.dumps(d['name'])} = {json.dumps(d['constraint'])}" for d in ds]
                lines.append("")
        return "Cargo.toml", "\n".join(lines)
    if eco == "npm":
        doc = {"name": PROBE, "version": "0.0.0", "private": True,
               "dependencies": {d["name"]: d["constraint"] for d in deps}}
        return "package.json", json.dumps(doc, indent=2) + "\n"
    return "requirements.in", "".join(f"{d['name']}{d['constraint']}\n" for d in deps)


# --------------------------------------------------------------------------
# running the tools


class _Ctx:
    """Throwaway homes and caches of one run, tool paths, child environments."""

    def __init__(self, tmp: Path, py_version: str, py_platform: str, timeout: int):
        self.tmp, self.py_version, self.py_platform, self.timeout = tmp, py_version, py_platform, timeout
        self.cargo_home = tmp / "cargo-home"
        self.npm_cache = tmp / "npm-cache"
        self.npmrc = tmp / "npmrc"
        self.uv_cache = tmp / "uv-cache"
        for d in (self.cargo_home, self.npm_cache, self.uv_cache):
            d.mkdir()
        self.npmrc.write_text("", encoding="utf-8")
        self.bin = {t: shutil.which(t) for t in ("cargo", "npm", "node", "uv")}
        self._n = 0
        self.env = {eco: self._env(eco) for eco in ECOS}

    def workdir(self) -> Path:
        self._n += 1
        w = self.tmp / "work" / f"{self._n:05d}"
        w.mkdir(parents=True)
        return w

    def _env(self, eco: str) -> dict:
        """Inherited network and CA settings; other settings of the tool dropped, ours set."""
        env = {}
        for k, v in os.environ.items():
            ku = k.upper()
            if ku.startswith("CARGO_") and not ku.startswith(("CARGO_HTTP_", "CARGO_NET_")):
                continue
            if ku.startswith("NPM_CONFIG_") and not re.search(r"PROXY|CAFILE|_CA$|STRICT_SSL|FETCH_RETR", ku):
                continue
            if ku.startswith("UV_") and ku not in ("UV_NATIVE_TLS", "UV_SYSTEM_CERTS", "UV_HTTP_TIMEOUT"):
                continue
            env[k] = v
        env.update(NO_COLOR="1", LC_ALL="C.UTF-8")
        if eco == "crates":
            env.update(CARGO_HOME=str(self.cargo_home), CARGO_TERM_COLOR="never", CARGO_TERM_PROGRESS_WHEN="never",
                       CARGO_REGISTRIES_CRATES_IO_PROTOCOL="sparse")
        elif eco == "npm":
            env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + f" --max-old-space-size={NODE_HEAP_MB}").strip()
            env["NODE_COMPILE_CACHE"] = str(self.tmp / "node-compile-cache")   # npm's own bytecode cache
        else:
            if "UV_NATIVE_TLS" in env:   # deprecated spelling of the same setting
                env.setdefault("UV_SYSTEM_CERTS", env.pop("UV_NATIVE_TLS"))
            env.update(UV_PYTHON_DOWNLOADS="never", UV_CONCURRENT_DOWNLOADS="4")
        return env

    def argv(self, eco: str, work: Path) -> list[str]:
        if eco == "crates":
            return [self.bin["cargo"], "generate-lockfile", "--manifest-path", str(work / "Cargo.toml"),
                    "--color", "never"]
        if eco == "npm":
            return [self.bin["npm"], "install", "--package-lock-only", "--ignore-scripts", "--no-audit",
                    "--no-fund", "--userconfig", str(self.npmrc), "--cache", str(self.npm_cache),
                    f"--registry={NPM_REGISTRY}", "--no-update-notifier", "--logs-max=0", "--no-progress",
                    "--color=false"]
        return [self.bin["uv"], "pip", "compile", "requirements.in", "-o", "requirements.txt",
                "--generate-hashes", "--no-build", "--python-version", self.py_version,
                "--python-platform", self.py_platform, "--default-index", PYPI_INDEX,
                "--cache-dir", str(self.uv_cache), "--no-config", "--no-python-downloads",
                "--color", "never", "--no-progress"]

    def env_overrides(self, eco: str) -> dict:
        """What the child environment changes: variables set (values) and dropped (names only)."""
        env = self.env[eco]
        return {"set": {k: self.clean(v) for k, v in sorted(env.items()) if os.environ.get(k) != v},
                "dropped": sorted(k for k in os.environ if k not in env)}

    def clean(self, text: str, work: Path | None = None) -> str:
        """Tool output without colours, host paths of this run, or elapsed times."""
        t = _ANSI.sub("", text)
        if work is not None:
            for w in {str(work), os.path.realpath(work)}:
                t = t.replace(w, "$WORK")
        for r in {str(self.tmp), os.path.realpath(self.tmp)}:
            t = t.replace(r, "$RUN")
        for tool, path in self.bin.items():
            if path:
                t = t.replace(path, tool)
        return _TIMING.sub(lambda m: f"{m.group(1)} <t>", t)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_TIMING = re.compile(r"\b(in|after) \d+(?:\.\d+)?(?:ms|s|m)\b")


def _nice() -> None:  # runs in the child before exec
    os.nice(NICE)


def _exec(argv: list[str], cwd: Path, env: dict, timeout: int) -> dict:
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True, preexec_fn=_nice)
    except OSError as e:
        return {"exit_code": None, "timed_out": False, "duration_s": 0.0, "stderr": f"{type(e).__name__}: {e}"}
    try:
        _, err = p.communicate(timeout=timeout)
        code, timed_out = p.returncode, False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        _, err = p.communicate()
        code, timed_out = None, True
    return {"exit_code": code, "timed_out": timed_out, "duration_s": round(time.monotonic() - t0, 2),
            "stderr": err.decode("utf-8", "replace")}


def _tail(text: str, n: int = TAIL) -> str:
    text = text.strip("\n")
    if len(text) <= n:
        return text
    cut = text[-n:]
    nl = cut.find("\n")
    if 0 <= nl < len(cut) - 1:
        cut = cut[nl + 1:]
    return "[...]\n" + cut


# --------------------------------------------------------------------------
# reading the lock files

_STRENGTH = ("sha512", "sha384", "sha256", "sha1", "md5")


def _sri_algo(integrity: str | None) -> str | None:
    algos = {t.split("-", 1)[0].lower() for t in (integrity or "").split() if "-" in t}
    return next((a for a in _STRENGTH if a in algos), min(algos) if algos else None)


def _parse_cargo(text: str, deps: list[dict]) -> tuple[dict, dict]:
    pkgs = tomllib.loads(text).get("package") or []
    root = next((p for p in pkgs if p.get("name") == PROBE and not p.get("source")), None)
    others = [p for p in pkgs if p is not root]
    root_ver = {}
    for e in (root or {}).get("dependencies") or []:
        parts = e.split()
        root_ver[parts[0]] = parts[1] if len(parts) > 1 else None
    entries = {}
    for d in deps:
        cands = [p for p in others if p.get("name") == d["name"]]
        if root_ver.get(d["name"]):
            cands = [p for p in cands if p.get("version") == root_ver[d["name"]]] or cands
        if cands:
            p = cands[0]
            cs = p.get("checksum")
            e = {"locked_version": p.get("version"), "digest_algo": "sha256" if cs else None,
                 "digest": f"sha256:{cs}" if cs else None}
            if p.get("source") != CRATES_IO:
                e["source"] = p.get("source")
            entries[d["name"]] = e
    with_d = [p for p in others if p.get("checksum")]
    totals = {"packages": len(others), "with_digest": len(with_d),
              "digest_algos": {"sha256": len(with_d)} if with_d else {},
              "without_digest": sorted(f"{p.get('name')} {p.get('version')}" for p in others if not p.get("checksum"))[:20]}
    return entries, totals


def _parse_npm(text: str, deps: list[dict]) -> tuple[dict, dict]:
    doc = json.loads(text)
    pk = doc.get("packages")
    if pk is None:   # lockfileVersion 1
        pk = {}

        def walk(tree, prefix):
            for n, v in (tree or {}).items():
                pk[f"{prefix}node_modules/{n}"] = v
                walk(v.get("dependencies"), f"{prefix}node_modules/{n}/")
        walk(doc.get("dependencies"), "")
    entries = {}
    for d in deps:
        e = pk.get(f"node_modules/{d['name']}")
        if e:
            integ = e.get("integrity")
            entries[d["name"]] = {"locked_version": e.get("version"), "digest_algo": _sri_algo(integ),
                                  "digest": integ}
    rest = [(k, v) for k, v in pk.items() if k]
    with_d = [v for _, v in rest if v.get("integrity")]
    # bundled dependencies ship inside the tarball of the package that bundles them, whose
    # integrity covers them; npm records no digest of their own
    bundled = [v for _, v in rest if not v.get("integrity") and v.get("inBundle")]
    no_d = sorted(k.rsplit("node_modules/", 1)[-1] + (" (link)" if v.get("link") else "")
                  for k, v in rest if not v.get("integrity") and not v.get("inBundle"))
    totals = {"packages": len(rest), "with_digest": len(with_d), "in_bundle": len(bundled),
              "digest_algos": dict(collections.Counter(_sri_algo(v["integrity"]) for v in with_d)),
              "without_digest": no_d[:20]}
    if len(no_d) > 20:
        totals["without_digest_more"] = len(no_d) - 20
    return entries, totals


_UV_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;\\]+)")
_UV_HASH = re.compile(r"--hash=([A-Za-z0-9]+):([0-9a-fA-F]+)")


def _parse_uv(text: str, deps: list[dict]) -> tuple[dict, dict]:
    logical, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        logical.append((buf + line).strip())
        buf = ""
    if buf:
        logical.append(buf.strip())
    pins = {}
    for line in logical:
        if not line or line.startswith(("#", "-")):
            continue
        m = _UV_PIN.match(line)
        if m:
            hashes = _UV_HASH.findall(line)
            algos = sorted({a.lower() for a, _ in hashes})
            pins[norm_pypi(m.group(1))] = {"locked_version": m.group(2), "hash_count": len(hashes),
                                           "digest_algo": ",".join(algos) or None}
    entries = {d["name"]: pins[norm_pypi(d["name"])] for d in deps if norm_pypi(d["name"]) in pins}
    with_d = [v for v in pins.values() if v["hash_count"]]
    totals = {"packages": len(pins), "with_digest": len(with_d),
              "digest_algos": dict(collections.Counter(v["digest_algo"] for v in with_d)),
              "without_digest": sorted(f"{k} {v['locked_version']}" for k, v in pins.items() if not v["hash_count"])[:20]}
    return entries, totals


PARSE = {"crates": _parse_cargo, "npm": _parse_npm, "pypi": _parse_uv}


def _lock(ctx: _Ctx, eco: str, deps: list[dict]) -> dict:
    """Write the manifest into a fresh directory, run the tool, read the lock it wrote."""
    work = ctx.workdir()
    fname, text = _manifest(eco, deps)
    (work / fname).write_text(text, encoding="utf-8")
    if eco == "crates":
        (work / "src").mkdir()
        (work / "src" / "lib.rs").write_text("", encoding="utf-8")
    argv = ctx.argv(eco, work)
    r = _exec(argv, work, ctx.env[eco], ctx.timeout)
    lock = work / LOCK_FILE[eco]
    r.update(lock_ok=False, entries={}, totals=None, parse_error=None)
    if r["exit_code"] == 0 and lock.exists():
        try:
            r["entries"], r["totals"] = PARSE[eco](lock.read_text(encoding="utf-8"), deps)
            r["lock_ok"] = True
        except (ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as e:
            r["parse_error"] = f"{type(e).__name__}: {e}"
    elif r["exit_code"] == 0:
        r["parse_error"] = f"exit code 0 but no {LOCK_FILE[eco]} was written"
    r["stderr"] = ctx.clean(r["stderr"], work)
    r["command"] = ctx.clean(shlex.join(argv), work)
    r["manifest"] = {"filename": fname, "content": text}
    shutil.rmtree(work, ignore_errors=True)
    return r


# --------------------------------------------------------------------------
# reading the tools' messages

_PYNAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"


def _failure(eco: str, r: dict, names: set[str]) -> tuple[str, str | None]:
    """Outcome of a failed lock, read from the tool's message, and the package the message names.
    ``names``: the declarations of the manifest (``_nkey``); a version or name failure of one of
    them is its own, of any other package a failure of a dependency.  Each tool stops at the
    first failure it meets, hence the isolated runs."""
    if r["timed_out"]:
        return "timeout", None
    if r.get("parse_error"):
        return "tool_error", None
    flat = " ".join(r["stderr"].split())

    def own(n: str) -> bool:
        return _nkey(eco, n) in names
    if eco == "crates":
        m = re.search(r"failed to select a version for the requirement `([^`\s]+) = ", flat)
        if m:   # Cargo lists the candidates that did not match (yanked ones excluded)
            return ("version_not_found" if own(m.group(1)) else "dependency_unresolvable"), m.group(1)
        m = re.search(r"no matching package named `([^`]+)` found", flat)
        if m:
            return ("not_found" if own(m.group(1)) else "dependency_unresolvable"), m.group(1)
        if "failed to select a version for `" in flat:
            return "conflict", None
        return "tool_error", None
    if eco == "npm":
        m = re.search(r"No matching version found for (@?[^@\s]+)@", flat)
        if m:   # ETARGET
            return ("version_not_found" if own(m.group(1)) else "dependency_unresolvable"), m.group(1)
        m = (re.search(r"'(@?[^'@\s]+)@[^']*' is not in this registry", flat) or
             re.search(r"404 Not Found - GET \S+/(@?[^/\s]+) - ", flat))
        if m:   # E404
            n = urllib.parse.unquote(m.group(1))
            return ("not_found" if own(n) else "dependency_unresolvable"), n
        if "ERESOLVE" in flat:
            return "conflict", None
        return "tool_error", None
    # uv: read the error report only (warnings come first, e.g. "The requested Python version 3.9 is
    # not available; ... will be used to build dependencies instead")
    lines = r["stderr"].splitlines()
    i = next((k for k, l in enumerate(lines) if l.lstrip().startswith(("×", "error"))), None)
    flat = " ".join(" ".join(lines[i:] if i is not None else lines).split())
    m = re.search(r"Wheels are required for `([^`]+)`", flat)
    if m or "building from source is disabled" in flat:
        return "needs_build", (m.group(1) if m else None)
    if (re.search(r"Because the (?:current|requested) Python version \(", flat) or
            re.search(r"no wheels with a matching (?:Python|platform)", flat) or "has no usable wheels" in flat):
        return "target_incompatible", None
    m = re.search(rf"\b({_PYNAME}) was not found in the package registry", flat)
    if m:
        return ("not_found" if own(m.group(1)) else "dependency_unresolvable"), m.group(1)
    subj = (re.findall(rf"there is no version of ({_PYNAME}?)(?=[=<>!~\s])", flat) +
            re.findall(rf"only the following versions of ({_PYNAME}) are available", flat) +
            re.findall(rf"only ({_PYNAME}?)[=<>!~]\S* is available", flat))
    if subj:
        mine = [s for s in subj if own(s)]
        return ("version_not_found" if mine else "dependency_unresolvable"), (mine or subj)[0]
    if "No solution found" in flat:
        return "conflict", None
    return "tool_error", None


def _excerpt(eco: str, stderr: str, n: int = 500) -> str:
    """The error part of a tool's stderr, for ``resolution_error``."""
    lines = stderr.splitlines()
    if eco == "crates":
        i = next((k for k, l in enumerate(lines) if l.startswith("error")), None)
        keep = lines[i:i + 6] if i is not None else lines[-4:]
    elif eco == "npm":
        keep = [l for l in lines if l.startswith("npm error") and "complete log" not in l][:6] or lines[-4:]
    else:
        i = next((k for k, l in enumerate(lines) if l.lstrip().startswith(("×", "error"))), None)
        keep = lines[i:] if i is not None else lines[-4:]
    text = " ".join(" ".join(keep).split())
    if eco == "pypi":
        text = re.sub(r"[×╰─▶│]+\s*", "", text)
    return text if len(text) <= n else text[:n - 3] + "..."


_MISSING = {
    "crates": [re.compile(r"no matching package named `([^`]+)` found")],
    "npm": [re.compile(r"'(@?[^'@\s]+)@[^']*' is not in this registry"),
            re.compile(r"404 Not Found - GET \S+/(@?[^/\s]+) - ")],
    "pypi": [re.compile(rf"\b({_PYNAME}) was not found in the package registry")],
}


def _masker(eco: str, declared: set[str]):
    """Replace names that a tool reports missing from the registry and that are not declarations
    of this manifest: an outsider could register them (PROTOCOL.md, section 9)."""
    have_key = MASK_KEY.exists()
    masked: set[str] = set()

    def mask(text: str) -> str:
        for rx in _MISSING[eco]:
            for m in list(rx.finditer(text)):
                raw = m.group(1)
                n = urllib.parse.unquote(raw)
                if _nkey(eco, n) in declared:
                    continue
                pid = mask_key(eco, None, n) if have_key else "[masked]"
                masked.add(pid)
                for s in sorted({raw, n}, key=len, reverse=True):
                    text = re.sub(rf"(?<![\w.@-]){re.escape(s)}(?![\w.-])", pid, text)
        return text
    return mask, masked


# --------------------------------------------------------------------------
# one manifest


def _dep_result(d: dict, entry: dict | None) -> None:
    if entry:
        d.update(locked_version=entry["locked_version"], digest_algo=entry["digest_algo"],
                 digest_present=entry["digest_algo"] is not None, outcome="locked", resolution_error=None)
        for k in ("digest", "hash_count", "source"):
            if k in entry:
                d[k] = entry[k]
    else:
        d.update(locked_version=None, digest_present=False, digest_algo=None)


def check_manifest(ctx: _Ctx, key: tuple, rows: list[dict]) -> dict:
    run_id, model_id, prompt_id, sample_idx, eco = key
    deps, dropped = _declared(eco, rows)
    res = _lock(ctx, eco, deps)
    mask, masked = _masker(eco, {_nkey(eco, d["name"]) for d in deps})
    rec = {"run_id": run_id, "model_id": model_id, "prompt_id": prompt_id, "sample_idx": sample_idx,
           "family": rows[0].get("family"), "eco": eco, "manifest": res["manifest"], "command": res["command"],
           "exit_code": res["exit_code"], "timed_out": res["timed_out"], "duration_s": res["duration_s"],
           "stderr_tail": mask(_tail(res["stderr"])), "lock_ok": res["lock_ok"],
           "lock_file": LOCK_FILE[eco] if res["lock_ok"] else None, "lock_totals": res["totals"],
           "failure": None}
    if dropped:
        rec["duplicates_dropped"] = dropped
    if res.get("parse_error"):
        rec["parse_error"] = res["parse_error"]
    if res["lock_ok"]:
        for d in deps:
            e = res["entries"].get(d["name"])
            _dep_result(d, e)
            if not e:
                d.update(outcome="missing_from_lock", resolution_error="the lock file has no entry for this declaration")
    else:
        names = {_nkey(eco, d["name"]) for d in deps}
        said, said_pkg = _failure(eco, res, names)
        for d in deps:
            _dep_result(d, None)
        if len(deps) == 1:
            d = deps[0]
            d.update(outcome=said, resolution_error=mask(_excerpt(eco, res["stderr"])))
            if said_pkg and _nkey(eco, said_pkg) not in names:
                d["failing_package"] = mask(said_pkg)
            culprits = deps
        else:   # which declarations break the lock: resolve each one alone
            for d in deps:
                iso = _lock(ctx, eco, [{k: v for k, v in d.items() if k in ("name", "constraint", "section")}])
                e = iso["entries"].get(d["name"]) if iso["lock_ok"] else None
                d["isolated"] = {"exit_code": iso["exit_code"], "timed_out": iso["timed_out"],
                                 "duration_s": iso["duration_s"], "lock_ok": iso["lock_ok"]}
                if e:
                    d["isolated"].update(locked_version=e["locked_version"], digest_algo=e["digest_algo"],
                                         digest_present=e["digest_algo"] is not None)
                    d.update(outcome="blocked", resolution_error=None)
                else:
                    kind, subj = _failure(eco, iso, {_nkey(eco, d["name"])})
                    d["isolated"]["stderr_tail"] = mask(_tail(iso["stderr"], ISO_TAIL))
                    d.update(outcome=kind, resolution_error=mask(_excerpt(eco, iso["stderr"])))
                    if subj and _nkey(eco, subj) != _nkey(eco, d["name"]):
                        d["failing_package"] = mask(subj)
            culprits = [d for d in deps if d["outcome"] != "blocked"]
            if not culprits:
                for d in deps:
                    d.update(outcome="conflict", resolution_error=mask(_excerpt(eco, res["stderr"])))
        rec["failure"] = {
            "cause": ("+".join(sorted({d["outcome"] for d in culprits})) if culprits
                      else "conflict_between_declarations"),
            "culprits": [d["name"] for d in culprits],
            "tool_says": {"outcome": said, "package": mask(said_pkg) if said_pkg else None}}
    rec["all_declared_pinned"] = bool(res["lock_ok"] and all(d["digest_present"] for d in deps))
    if masked:
        rec["masked_names"] = sorted(masked)
    rec["deps"] = deps
    return rec


# --------------------------------------------------------------------------
# provenance (presence only; nothing is verified)


class _Http:
    """GET with a rate limit and a gzip JSON cache file per URL (answer + retrieval time)."""

    def __init__(self, cache_dir: Path = HTTP_CACHE, rate_s: float = RATE_S):
        self.dir, self.rate_s = cache_dir, rate_s
        self._last = 0.0
        self.fetched = self.reused = 0

    def get(self, url: str, accept: str, rel: str, reuse=None, text: bool = False) -> dict:
        path = self.dir / rel
        if path.exists():
            try:
                rec = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
                if rec.get("status") == 200 and (reuse is None or reuse(rec)):
                    self.reused += 1
                    return rec
            except (OSError, ValueError, EOFError):
                pass
        wait = self._last + self.rate_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = http_get(url, headers={"Accept": accept}, timeout=60)
        self.fetched += 1
        body = None
        if r.body:
            try:
                body = r.body.decode("utf-8", "replace") if text else json.loads(r.body.decode("utf-8"))
            except ValueError:
                body = r.body.decode("utf-8", "replace")
        rec = {"url": url, "accept": accept, "status": r.status, "error": r.error, "retrieved_at": utcnow(),
               "elapsed_s": round(r.elapsed, 3), "body": body}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8"), mtime=0))
        tmp.replace(path)
        return rec


def _predicate_types(obj) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "predicateType" and isinstance(v, str):
                out.append(v)
            else:
                out += _predicate_types(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _predicate_types(v)
    return out


def npm_provenance(http: _Http, name: str, version: str) -> tuple[bool | None, dict]:
    path = quote(name).replace("%40", "@", 1) if name.startswith("@") else quote(name)
    url = f"{NPM_REGISTRY}{path}/{quote(version)}"
    rec = http.get(url, "application/json", f"npm/{quote(name)}@{quote(version)}.json.gz")
    det = {"url": url, "checked_at": rec["retrieved_at"]}
    if rec["status"] != 200 or not isinstance(rec["body"], dict):
        return None, {**det, "error": f"HTTP {rec['status']}" + (f" ({rec['error']})" if rec.get("error") else "")}
    att = (rec["body"].get("dist") or {}).get("attestations")
    det["predicate_types"] = sorted(set(_predicate_types(att))) if att else []
    return bool(att), det


def _same_version(a: str, b: str) -> bool:
    if Version is not None:
        try:
            return Version(a) == Version(b)
        except InvalidVersion:
            pass
    return a == b


_SDIST_EXT = (".tar.gz", ".zip", ".tar.bz2", ".tar.xz", ".tgz", ".tar")


def _file_version(filename: str, norm: str) -> str | None:
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        return parts[1] if len(parts) >= 5 else None
    for ext in _SDIST_EXT:
        if filename.endswith(ext):
            stem = filename[:-len(ext)]
            for i, c in enumerate(stem):
                if c == "-" and norm_pypi(stem[:i]) == norm:
                    return stem[i + 1:]
            return None
    return None


def _version_files(body: dict, norm: str, version: str) -> list[dict]:
    out = []
    for f in body.get("files") or []:
        v = _file_version(f.get("filename", ""), norm)
        if v and _same_version(v, version):
            out.append(f)
    return out


def _statement_type(att: dict) -> str | None:
    try:
        st = json.loads(base64.b64decode(att["envelope"]["statement"]))
        return st.get("predicateType")
    except (KeyError, TypeError, ValueError):
        return None


def pypi_provenance(http: _Http, name: str, version: str) -> tuple[bool | None, dict]:
    norm = norm_pypi(name)
    url = f"https://pypi.org/simple/{norm}/"
    rec = http.get(url, PEP691, f"pypi-simple/{norm}.json.gz",
                   reuse=lambda c: isinstance(c.get("body"), dict) and bool(_version_files(c["body"], norm, version)))
    det = {"url": url, "checked_at": rec["retrieved_at"]}
    if rec["status"] != 200 or not isinstance(rec["body"], dict):
        return None, {**det, "error": f"HTTP {rec['status']}" + (f" ({rec['error']})" if rec.get("error") else "")}
    files = _version_files(rec["body"], norm, version)
    if not files:
        return None, {**det, "error": "the index lists no wheel or sdist of the locked version"}
    prov = [f for f in files if f.get("provenance")]
    det.update(files=len(files), files_with_provenance=len(prov))
    if prov:
        pick = (next((f for f in sorted(prov, key=lambda f: f["filename"]) if not f["filename"].endswith(".whl")), None)
                or sorted(prov, key=lambda f: f["filename"])[0])
        p = http.get(pick["provenance"], "application/vnd.pypi.integrity.v1+json, application/json",
                     f"pypi-provenance/{norm}/{quote(pick['filename'])}.json.gz")
        det["attestation_file"] = pick["filename"]
        if p["status"] == 200 and isinstance(p["body"], dict):
            bundles = p["body"].get("attestation_bundles") or []
            atts = [a for b in bundles for a in (b.get("attestations") or [])]
            det["attestations"] = len(atts)
            det["publisher_kinds"] = sorted({(b.get("publisher") or {}).get("kind") for b in bundles} - {None})
            det["predicate_types"] = sorted({_statement_type(a) for a in atts} - {None})
        else:
            det["attestation_error"] = f"HTTP {p['status']}"
    return bool(prov), det


def crates_index_note(http: _Http, name: str) -> dict:
    """Why Cargo finds no package for a name the registry has: versions and yanked versions in
    the sparse index (Cargo never selects a yanked version for a new lock)."""
    url = f"https://index.crates.io/{crates_path(name)}"
    rec = http.get(url, "*/*", f"crates-index/{quote(name.lower())}.json.gz", text=True)
    det = {"url": url, "checked_at": rec["retrieved_at"]}
    if rec["status"] != 200 or not isinstance(rec["body"], str):
        return {**det, "error": f"HTTP {rec['status']}"}
    vers = []
    for line in rec["body"].splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        vers.append(v)
    det.update(versions=len(vers), yanked=sum(1 for v in vers if v.get("yanked")))
    if vers and det["yanked"] == len(vers):
        det["note"] = "every version is yanked"
    return det


def add_provenance(manifests: list[dict], http: _Http) -> None:
    memo: dict = {}
    for m in manifests:
        for d in m["deps"]:
            if m["eco"] == "crates" and d["outcome"] == "not_found":
                d["registry_note"] = crates_index_note(http, d["name"])
            if m["eco"] == "crates":
                d["provenance"] = None
                d["provenance_detail"] = {"note": "not available: crates.io has no attestation API"}
                continue
            if d["locked_version"] is None:
                d["provenance"] = None
                d["provenance_detail"] = {"note": "no locked version"}
                continue
            k = (m["eco"], _nkey(m["eco"], d["name"]), d["locked_version"])
            if k not in memo:
                fn = npm_provenance if m["eco"] == "npm" else pypi_provenance
                memo[k] = fn(http, d["name"], d["locked_version"])
            d["provenance"], d["provenance_detail"] = memo[k][0], dict(memo[k][1])


# --------------------------------------------------------------------------
# summary, entry points


def summarise(manifests: list[dict]) -> dict:
    out = {}
    for eco in ECOS:
        ms = [m for m in manifests if m["eco"] == eco]
        deps = [d for m in ms for d in m["deps"]]
        oc = collections.Counter(d["outcome"] for d in deps)
        locked = [d for d in deps if d["locked_version"] is not None]
        s = {"responses": len(ms),
             "lock_succeeded": sum(1 for m in ms if m["lock_ok"]),
             "lock_failed": sum(1 for m in ms if not m["lock_ok"]),
             "lock_failed_by_cause": dict(sorted(collections.Counter(
                 m["failure"]["cause"] for m in ms if not m["lock_ok"]).items())),
             "manifests_all_declared_pinned": sum(1 for m in ms if m["all_declared_pinned"]),
             "dependencies": len(deps),
             "dependencies_locked": len(locked),
             "dependencies_with_digest": sum(1 for d in deps if d["digest_present"]),
             "digest_algos": dict(sorted(collections.Counter(d["digest_algo"] for d in deps if d["digest_present"]).items())),
             "version_resolution_failures": oc.get("version_not_found", 0),
             "dependency_outcomes": dict(sorted(oc.items())),
             "constraints_normalised": sum(1 for d in deps if d["constraint_normalised"]),
             "declared_version_unknown": sum(1 for d in deps if d["declared_version"] is None
                                             and "toml_fallback" in d.get("flags", []))}
        tot = [m["lock_totals"] for m in ms if m["lock_ok"]]
        s["lock_entries"] = {"packages": sum(t["packages"] for t in tot),
                             "with_digest": sum(t["with_digest"] for t in tot)}
        if eco == "npm":
            s["lock_entries"]["in_bundle"] = sum(t.get("in_bundle", 0) for t in tot)
        s["lock_entries"]["without_digest"] = (s["lock_entries"]["packages"] - s["lock_entries"]["with_digest"]
                                               - s["lock_entries"].get("in_bundle", 0))
        if eco == "pypi":
            s["needs_build"] = oc.get("needs_build", 0)
        if eco == "crates":
            s["provenance"] = "not available (crates.io has no attestation API)"
        else:
            uniq = {(_nkey(eco, d["name"]), d["locked_version"]): d["provenance"] for d in locked}
            s["provenance"] = {"checked": len(locked),
                               "present": sum(1 for d in locked if d["provenance"] is True),
                               "absent": sum(1 for d in locked if d["provenance"] is False),
                               "unknown": sum(1 for d in locked if d["provenance"] is None),
                               "unique_locked_versions": len(uniq),
                               "unique_with_provenance": sum(1 for v in uniq.values() if v is True)}
        out[eco] = s
    return out


def _version(argv: list[str], env: dict) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
        return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def run(run_ids: list[str], *, out: str | Path | None = None, python_version: str = PY_VERSION,
        python_platform: str = PY_PLATFORM, timeout: int = TIMEOUT_S, ecos: tuple = ECOS,
        limit: int | None = None, reuse: str | Path | None = None) -> dict:
    """Lock every sample-0 manifest of ``run_ids`` with the real tools; write ``data/evidence/lockcheck.json``
    (or ``out``) and return what was written.  ``ecos`` and ``limit`` restrict the run (smoke tests).
    ``reuse``: an earlier result; a manifest whose text is the same keeps its record (marked
    ``reused_from``), so that a change of the extraction locks only the manifests it changed, against
    the registries of today, and the others keep the registries of their first check."""
    groups, inputs = load_declarations(list(run_ids))
    keys = sorted(k for k in groups if k[4] in ecos)
    if limit is not None:
        keys = keys[:limit]
    prev, prev_doc = {}, None
    if reuse:
        prev_doc = json.loads(Path(reuse).read_text(encoding="utf-8"))
        prev = {(m["run_id"], m["model_id"], m["prompt_id"], m["sample_idx"], m["eco"]): m
                for m in prev_doc.get("manifests") or []}
    tmp = Path(tempfile.mkdtemp(prefix="phantomdeps-lockcheck-"))
    try:
        ctx = _Ctx(tmp, python_version, python_platform, timeout)
        need = {"crates": ["cargo"], "npm": ["npm", "node"], "pypi": ["uv"]}
        missing = sorted({t for k in keys for t in need[k[4]] if not ctx.bin[t]})
        if missing:
            raise RuntimeError(f"lock tools not found on PATH: {', '.join(missing)}")
        tool_env = {"cargo": "crates", "npm": "npm", "node": "npm", "uv": "pypi"}
        tools = {t: _version([ctx.bin[t], "--version"], ctx.env[tool_env[t]]) if ctx.bin[t] else None
                 for t in ("cargo", "npm", "node", "uv")}
        if packaging is not None:
            tools["python-packaging"] = packaging.__version__
        env_over = {eco: ctx.env_overrides(eco) for eco in ECOS}
        manifests, n_reused = [], 0
        for i, k in enumerate(keys, 1):
            old_rec = prev.get(k)
            if old_rec is not None and (old_rec.get("manifest") or {}).get("content") == \
                    _manifest(k[4], _declared(k[4], groups[k])[0])[1]:
                rec = dict(old_rec)
                rec.setdefault("reused_from", prev_doc.get("generated_at"))
                manifests.append(rec)
                n_reused += 1
                continue
            rec = check_manifest(ctx, k, groups[k])
            rec["checked_at"] = utcnow()
            manifests.append(rec)
            state = "locked" if rec["lock_ok"] else f"FAILED ({rec['failure']['cause']})"
            print(f"[lockcheck] {i}/{len(keys)} {k[4]:6s} {k[1]} {k[2]}: {len(rec['deps'])} deps, {state}, "
                  f"{rec['duration_s']} s", file=sys.stderr, flush=True)
        if prev_doc is not None:
            print(f"[lockcheck] {n_reused} of {len(keys)} manifests unchanged, records reused from "
                  f"{prev_doc.get('generated_at')}", file=sys.stderr, flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    http = _Http()
    add_provenance(manifests, http)
    print(f"[lockcheck] provenance: {http.fetched} HTTP requests, {http.reused} cached answers reused",
          file=sys.stderr, flush=True)
    result = {
        "generated_at": utcnow(),
        "run_ids": list(run_ids),
        "inputs": inputs,
        "lockcheck_sha256": sha256_file(Path(__file__)),
        "reused": None if prev_doc is None else {
            "from_generated_at": prev_doc.get("generated_at"), "from_lockcheck_sha256": prev_doc.get("lockcheck_sha256"),
            "from_tools": prev_doc.get("tools"), "manifests": n_reused,
            "rule": "a manifest whose text is unchanged keeps its earlier record"},
        "scope": {"sample_idx": 0, "ecosystems": list(ecos), "cls": "exists",
                  "limit": limit, "unit": "one manifest per response and ecosystem"},
        "tools": tools,
        "settings": {
            "timeout_s": timeout, "nice": NICE, "sequential": True,
            "placeholders": {"$RUN": "throwaway directory of this run (tool homes and caches)",
                             "$WORK": "throwaway project directory of one manifest"},
            "environment": env_over,
            "crates": {"edition": CARGO_EDITION, "probe_package": PROBE, "registry": "crates.io sparse index"},
            "npm": {"registry": NPM_REGISTRY, "node_max_old_space_mb": NODE_HEAP_MB},
            "pypi": {"index": PYPI_INDEX, "target_python": python_version, "target_platform": python_platform,
                     "builds": "none (--no-build: wheels only; a requirement that needs an sdist is needs_build)"},
            "http": {"rate_limit_per_s": round(1 / RATE_S), "cache": str(HTTP_CACHE.relative_to(ROOT)),
                     "user_agent": USER_AGENT},
            "not_measured": ["no package code is executed and no artifact is downloaded",
                             "digests are read from the lock files, not recomputed from artifacts",
                             "attestations are not verified (no signature, certificate, log or identity check)",
                             "transitive packages are not checked for provenance"]},
        "summary": summarise(manifests),
        "manifests": manifests,
    }
    write_json(Path(out) if out else OUT, result)
    return result


def pins(path: str | Path | None = None, *, unpinned=None) -> dict:
    """``pins`` for ``gate.evidence_for``: (run_id, model_id, prompt_id, sample_idx, eco, name) -> 1 if
    the lock file written by the tool records a digest for the declaration, else ``unpinned``
    (default None, i.e. absent; pass 0 to read a missing digest as refuted).  Only the declarations
    checked here (sample 0; crates, npm, pypi; cls 'exists') are keys."""
    doc = json.loads(Path(path or OUT).read_text(encoding="utf-8"))
    return {(m["run_id"], m["model_id"], m["prompt_id"], m["sample_idx"], m["eco"], d["name"]):
            (1 if d["digest_present"] else unpinned) for m in doc["manifests"] for d in m["deps"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m phantomdeps.lockcheck",
                                 description="Lock the generated manifests with cargo, npm and uv (no builds, no "
                                             "scripts) and record digests and provenance presence.")
    ap.add_argument("run_ids", nargs="+")
    ap.add_argument("--out", default=None, help=f"output file (default {OUT.relative_to(ROOT)})")
    ap.add_argument("--python-version", default=PY_VERSION, help="uv target Python version")
    ap.add_argument("--python-platform", default=PY_PLATFORM, help="uv target platform")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S, help="seconds per tool invocation")
    ap.add_argument("--eco", action="append", choices=ECOS, help="restrict to an ecosystem (repeatable)")
    ap.add_argument("--limit", type=int, default=None, help="first N manifests only (smoke test)")
    ap.add_argument("--reuse", default=None, help="an earlier result: unchanged manifests keep their records")
    a = ap.parse_args(argv)
    res = run(a.run_ids, out=a.out, python_version=a.python_version, python_platform=a.python_platform,
              timeout=a.timeout, ecos=tuple(a.eco) if a.eco else ECOS, limit=a.limit, reuse=a.reuse)
    print(json.dumps({"tools": res["tools"], "summary": res["summary"]}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
