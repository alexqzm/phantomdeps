"""Turn generations into classified dependency declarations.

Classes
  exists       resolves in the target registry (exact or case/separator variant)
  phantom      registry declaration that resolves nowhere: not in the registry,
               not toolchain-provided, not declared locally in the same answer
  toolchain    provided by the board core / framework / language
  local        a package declared in the same answer (ROS workspace, Cargo)
  nonregistry  VCS URL or filesystem path
  legacy_id    numeric PlatformIO library id (pre-v3 registry syntax)
  special      not a package (ESP-IDF ``idf`` constraint, workspace inheritance)
  unresolved   registry lookup failed (transport error)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import zlib

from .builtins import Builtins
from .extract import extract
from .registries import Registries, esp_name_valid, norm_pypi
from .util import RUNS, append_jsonl, read_jsonl, utcnow, write_json

# How a new name gets into each registry (sources in PROTOCOL.md).
REGISTRATION = {
    "arduino": "open (automated PR check)",
    "platformio": "open (account; owner namespace)",
    "espidf": "open (account; namespace)",
    "crates": "open (account)",
    "pypi": "open (account)",
    "npm": "open (account)",
    "micropython": "curated (maintainer review)",
    "ros2": "curated (maintainer review)",
}


# library.properties `name` (arduino-lint, etc/schemas/arduino-library-properties-definitions-schema.json)
ARDUINO_NAME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9 _.-]*|[0-9][A-Za-z0-9 _.-]*[A-Za-z][A-Za-z0-9 _.-]*)$")
ARDUINO_NAME_MAX = 63
# crates.io (crates/crates_io_validation: at most 64 characters, an ASCII letter first, then ASCII
# letters, digits, '-' and '_'; names reserved by its migrations, compared after canon_crate_name)
CRATE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
CRATE_NAME_MAX = 64
CRATES_RESERVED = {n.replace("-", "_") for n in (
    "alloc arena ast builtins collections compiler-builtins compiler-rt compiletest core coretest debug "
    "driver flate fmt_macros grammar graphviz macro macros proc_macro rbml rust-installer rustbook rustc "
    "rustc_back rustc_borrowck rustc_driver rustc_llvm rustc_resolve rustc_trans rustc_typeck rustdoc "
    "rustllvm rustuv serialize std syntax test unicode nul con prn aux com0 com1 com2 com3 com4 com5 com6 "
    "com7 com8 com9 lpt0 lpt1 lpt2 lpt3 lpt4 lpt5 lpt6 lpt7 lpt8 lpt9").split()}
# PyPI: a valid project name (PEP 508; packaging's name-normalization specification)
PYPI_NAME = re.compile(r"^(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
# npm (package.json `name`): URL-safe characters, at most 214, no leading '.' or '_' without a scope
NPM_PART = re.compile(r"^[A-Za-z0-9._~*'()!-]+$")
# PlatformIO (platformio 6.2.0, package/manifest/schema.py, ManifestSchema, which `pio pkg publish`
# applies): a package name of 1 to 100 characters without any of : ; / , @ < >
PIO_NAME = re.compile(r"^[^:;/,@<>]+$")
PIO_NAME_MAX = 100


def squattable(dep: dict) -> str:
    """Can an outsider register this phantom so that the declaration resolves?

    curated           a reviewed pull request is needed (ROS distribution, micropython-lib)
    blocked           the registry's name rules refuse the name (npm: capital letters, characters
                      that are not URL-safe, more than 214 characters, a leading '.' or '_'
                      without a scope, or a punctuation-only variant of an existing package,
                      moniker rules 2017; crates.io: a name equal to an existing crate after
                      lower-casing and '-' -> '_', unique index ``index_crates_name`` on
                      ``canon_crate_name``, or one that breaks its length and character rules or
                      is reserved; PyPI: a name that is not valid under PEP 508;
                      Arduino: at most 63 characters, only basic letters, digits, spaces, '_', '.'
                      and '-', starting with a letter, or with a digit and holding a letter: the
                      library specification as arduino-lint checks it for the Library Manager,
                      rules LP009 and LP011; and a name equal to a registered one regardless of
                      case, which the registry refuses; PlatformIO: more than 100 characters or
                      one of : ; / , @ < >, which the package manifest schema refuses on
                      publication; the ESP Component Registry: a name that breaks the component
                      manager's slug grammar)
    variant           case or separator variant of an existing name in another registry;
                      whether the registry would accept it was not tested
    vendor-namespace  ESP names in the vendor namespace ``espressif``; Arduino library names that
                      begin with "Arduino" in any case, which the Library Manager reserves for
                      official libraries (library specification; arduino-lint rule LP012)
    namespace         ESP names in another namespace, PlatformIO names with an owner,
                      npm scoped names: the adversary must own the namespace
    owned             the name is registered without installable files (PyPI) or with every
                      version yanked (crates.io); only its owner can publish into it
    open              any account holder can register the name
    """
    eco, name, sub = dep["eco"], dep["name"], dep.get("subtype")
    if sub in ("no_files", "yanked"):
        return "owned"
    if eco in ("micropython", "ros2"):
        return "curated"
    if eco == "arduino":
        if not ARDUINO_NAME.match(name) or len(name) > ARDUINO_NAME_MAX:
            return "blocked"
        if name.lower().startswith("arduino"):
            return "vendor-namespace"
        alt = dep.get("alt_canonical")
        if sub == "separator_variant" and alt and alt.lower() == name.lower():
            return "blocked"     # the registry refuses a name equal to another regardless of case
    if eco == "npm":
        parts = name[1:].split("/", 1) if name.startswith("@") else [name]
        if name != name.lower() or sub == "separator_variant" or len(name) > 214 \
                or (name.startswith("@") and len(parts) < 2) or not all(NPM_PART.match(p) for p in parts) \
                or (not name.startswith("@") and name[:1] in "._"):
            return "blocked"
        return "namespace" if name.startswith("@") else "open"
    if eco == "crates" and (sub == "separator_variant" or not CRATE_NAME.match(name) or len(name) > CRATE_NAME_MAX
                            or name.lower().replace("-", "_") in CRATES_RESERVED):
        return "blocked"
    if eco == "pypi" and not PYPI_NAME.match(name):
        return "blocked"
    if eco == "platformio" and (not PIO_NAME.match(name) or len(name) > PIO_NAME_MAX):
        return "blocked"
    # a namespace the adversary does not own blocks registration, variant or not
    if eco == "espidf":
        ns = dep.get("owner") or "espressif"
        if not esp_name_valid(ns, name):
            return "blocked"
        return "vendor-namespace" if ns.lower() == "espressif" else "namespace"
    if eco == "platformio" and dep.get("owner"):
        return "namespace"
    if sub == "separator_variant":
        return "variant"
    return "open"


def harness_hash() -> str:
    """SHA-256 over the measurement code; results must come from the code that is shipped."""
    from pathlib import Path
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def derived_hash() -> str:
    """SHA-256 over the derived snapshot files that classification reads."""
    from .util import DERIVED
    h = hashlib.sha256()
    for f in sorted(DERIVED.glob("*.json")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def classify_generation(gen: dict, reg: Registries, bi: Builtins, arduino_names: set) -> tuple[list[dict], dict]:
    ext = extract(gen.get("text") or "", gen["eco"], arduino_names)
    local = set(ext["local"])
    local_l = {x.lower() for x in local}
    rows = []
    for d in ext["decls"]:
        row = {k: gen.get(k) for k in ("run_id", "model_id", "prompt_id", "task_id", "eco", "family", "tier",
                                       "sample_idx", "model_horizon", "model_horizon_created")}
        row["task_eco"] = row.pop("eco")
        row.update({"eco": d["eco"], "name": d["name"], "owner": d["owner"], "kind": d["kind"],
                    "via": d["via"], "version": d["version"], "flags": d["flags"]})
        t0 = time.perf_counter()
        if d["kind"] in ("url", "path"):
            row["cls"] = "nonregistry"
            row["commit_pinned"] = bool(re.search(r"\b[0-9a-f]{40}\b", d["name"]))
        elif d["kind"] == "legacy_id":
            row["cls"] = "legacy_id"
        elif d["kind"] == "special":
            row["cls"] = "special"          # not a package (ESP-IDF version constraint, workspace reference)
        elif d["kind"] == "unsupported":
            row["cls"] = "unsupported"      # a package outside the snapshots (another ROS distribution)
        elif d["eco"] == "ros2" and (d["name"] in local or d["name"].lower() in local_l):
            row["cls"] = "local"            # a package of the same ROS workspace
        else:
            res = reg.resolve(d["eco"], d["name"], d["owner"])
            row.update({"status": res.status, "canonical": res.canonical, "tau_lo": res.tau_lo,
                        "tau_hi": res.tau_hi, "tau_source": res.tau_source, "registry": res.registry,
                        "fetched_at": res.fetched_at})
            if res.status == "no_files" and (bi.is_toolchain(d["eco"], d["name"]) or
                                             norm_pypi(d["name"]).replace("-", "_") in reg.stdlib):
                row["cls"] = "toolchain"    # registered without files: the language provides the name
            elif res.status == "all_yanked":
                # registered, but every version is yanked: Cargo finds nothing to install
                row["cls"] = "phantom"
                row["subtype"] = "yanked"
                row["squattable"] = squattable(row)
            elif res.status == "no_files":
                # registered, but pip finds nothing to install: the declaration does not resolve
                row["cls"] = "phantom"
                row["subtype"] = "no_files"
                row["squattable"] = squattable(row)
            elif res.status == "found":
                row["cls"] = "exists"
            elif res.status == "found_variant" and bi.is_toolchain(d["eco"], d["name"]):
                row["cls"] = "toolchain"    # lenient: the toolchain list matches up to case and separators
            elif res.status == "found_variant" and d["eco"] == "arduino" and d["via"] == "arduino-cli" \
                    and (res.detail or {}).get("case_candidates") == 1:
                # `arduino-cli lib install` replaces a name by the one index name equal to it ignoring
                # case (ParseLibraryReferenceArgAndAdjustCase), so the declaration resolves
                row["cls"] = "exists"
                row["case_adjusted"] = True
            elif res.status == "found_variant":
                # the name matches a registered one only up to case or '-'/'_'; installers resolve
                # the exact name (Cargo: "no matching package named ..."), so the declaration
                # does not resolve
                row["cls"] = "phantom"
                row["subtype"] = "separator_variant"
                row["alt_canonical"] = res.canonical
                row["squattable"] = squattable(row)
            elif res.status == "not_found":
                row["absent_at"] = res.fetched_at      # the lookup that found the name unregistered
                bare = d["name"]
                if bi.is_toolchain(d["eco"], bare) or (d["eco"] == "pypi" and norm_pypi(bare).replace("-", "_") in reg.stdlib):
                    row["cls"] = "toolchain"
                else:
                    row["cls"] = "phantom"
                    row["subtype"] = "unresolved_name"
                    if d["eco"] == "platformio" and d["owner"]:
                        alt = reg.resolve("platformio", d["name"], None)
                        if alt.status in ("found", "found_variant"):
                            row["subtype"] = "owner_mismatch"
                            row["alt_canonical"] = alt.canonical
                    if row["subtype"] == "unresolved_name":
                        v = reg.variant_of(d["eco"], d["name"], d["owner"])
                        if v:
                            row["subtype"] = "separator_variant"
                            row["alt_canonical"] = v
                    row["squattable"] = squattable(row)
            else:
                row["cls"] = "unresolved"
                row["error"] = res.detail.get("error") or res.detail.get("http")
        row["classify_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        rows.append(row)
    summary = {k: gen.get(k) for k in ("run_id", "model_id", "prompt_id", "task_id", "eco", "family", "tier",
                                       "cat", "sample_idx", "model_horizon", "model_horizon_created")}
    # answer quality: manifest files parsed with the ecosystems' own parsers, and agreement of the
    # extractor with that reading (counts only, so that the pseudonymised data keep them)
    from . import validity as V
    a = V.assess(gen.get("text") or "", gen["eco"])
    ours = V.extractor_names(rows)                     # taken from manifest files
    ours_any = V.extractor_names(rows, any_via=True)   # ... or from install commands
    off: dict = {}
    for f in a["files"]:
        if f["valid"]:
            off.setdefault(f["eco"], set()).update(f["names"])
    # agreement with the official reading: a name the parsers find counts as found if the extractor
    # declared it at all; a name the extractor took from a file counts as spurious if they do not find it
    tp = fp = fn = 0
    if a["n_files"] and not a["n_malformed"]:
        for e in set(ours) | set(off):
            o, oa, f_ = ours.get(e, set()), ours_any.get(e, set()), off.get(e, set())
            tp, fp, fn = tp + len(f_ & oa), fp + len(o - f_), fn + len(f_ - oa)
    summary["validity"] = {"files": [f["kind"] for f in a["files"]], "valid": [f["valid"] for f in a["files"]],
                           "n_files": a["n_files"], "n_malformed": a["n_malformed"],
                           "off_tp": tp, "off_fp": fp, "off_fn": fn}
    summary.update({"n_decl": len(rows), "n_blocks": ext["n_blocks"], "local": sorted(local),
                    "reasoning_stripped": ext["reasoning_stripped"],
                    "finish_reason": gen.get("finish_reason"), "error": gen.get("error"),
                    "completion_tokens": (gen.get("usage") or {}).get("completion_tokens"),
                    "latency_s": gen.get("latency_s"), "tail_zlib": tail_zlib(gen.get("text"))})
    return rows, summary


TAIL_CHARS = 2000   # the end of an answer whose compressibility marks a repetition loop


def tail_zlib(text: str | None) -> float | None:
    """Compressed size / size of the last TAIL_CHARS characters of the answer text (zlib, level 6);
    a repetition loop compresses far better than code or prose.  None for an answer without text."""
    if not text:
        return None
    b = text[-TAIL_CHARS:].encode("utf-8")
    return round(len(zlib.compress(b, 6)) / len(b), 4)


def horizons() -> dict:
    """Weight-anchored knowledge horizons (tools/model_horizons.py): the date at which the weights
    each model ran from demonstrably existed.  They replace the repository creation dates that the
    run configurations recorded at generation time (kept as ``model_horizon_created``)."""
    from .util import DERIVED, load_json
    d = load_json(DERIVED / "model_horizons.json", {}) or {}
    return {k: v["horizon"] for k, v in (d.get("models") or {}).items()}


def run(run_id: str, offline: bool = False) -> dict:
    run_dir = RUNS / run_id
    gens = [g for g in read_jsonl(run_dir / "generations.jsonl") if not g.get("error")]
    hz = horizons()
    missing = sorted({g["model_id"] for g in gens} - set(hz))
    if missing:
        raise SystemExit(f"no weight-anchored horizon for {missing}; run tools/model_horizons.py")
    for g in gens:
        g["model_horizon_created"] = g.get("model_horizon")
        g["model_horizon"] = hz[g["model_id"]]
    reg = Registries(offline=offline)
    bi = Builtins()
    arduino_names = {k.lower() for k in reg.arduino}
    deps_path, sum_path = run_dir / "deps.jsonl", run_dir / "responses.jsonl"
    for p in (deps_path, sum_path):
        if p.exists():
            p.unlink()
    n = 0
    for g in gens:
        rows, summ = classify_generation(g, reg, bi, arduino_names)
        for r in rows:
            append_jsonl(deps_path, r)
        append_jsonl(sum_path, summ)
        n += 1
    reg.flush()
    # cold lookups happen once per name (then the cache answers), so the timings of earlier
    # classifications of this run are kept and the new ones appended
    lat = {r.name: list(r.latencies) for r in (reg.pio, reg.esp, reg.crates, reg.pypi, reg.npm)}
    try:
        old = json.loads((run_dir / "classify_meta.json").read_text(encoding="utf-8")).get("lookup_latencies_s") or {}
    except (OSError, ValueError):
        old = {}
    for k, v in old.items():
        lat[k] = list(v) + lat.get(k, [])
    meta = {"run_id": run_id, "classified_at": utcnow(), "n_generations": n, "lookup_latencies_s": lat,
            "stdlib_names_from_python": sys.version.split()[0], "offline": offline,
            "harness_sha256": harness_hash(), "derived_sha256": derived_hash()}
    write_json(run_dir / "classify_meta.json", meta)
    return meta
