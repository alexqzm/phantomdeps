#!/usr/bin/env python3
"""Reproducible phantom-dependency replay on real installers in isolated,
localhost-only test registries, with harmless marker-writing packages.

    python3 replay/run_replay.py [--run RUN_ID ...]

The squat names are chosen from the first run's classified declarations (default: the pilot,
pilot-cpu-2026-10); their pseudonyms are cross-checked against every listed run's deps_masked.jsonl.

Writes:
  data/replay/replay_raw.json   full detail (real names; never shipped to reviewers)
  data/evidence/replay.json     masked summary (phantom/control names -> ph-... ids,
                                 no stdout/stderr, plus a per-ecosystem table)

Everything here listens on 127.0.0.1 only, never publishes to a real registry,
and kills every server on exit (normal, error or signal).  See replay/README.md.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import tomllib
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)                       # replaylib
sys.path.insert(0, os.path.join(ROOT, "harness"))  # phantomdeps

import yaml  # noqa: E402

from phantomdeps import gate as G  # noqa: E402
from phantomdeps.util import mask_key, parse_time  # noqa: E402

from replaylib import common, gate_eval, selection  # noqa: E402
from replaylib.common import base_env, iso, marker_present, now, run  # noqa: E402
from replaylib.crates_registry import CratesRegistry  # noqa: E402
from replaylib.esp_registry import EspRegistry  # noqa: E402
from replaylib.npm_registry import NODE, NPM, NpmRegistry, build_npm_pkg  # noqa: E402
from replaylib.pypi_index import PyPIIndex, _ident, normalize  # noqa: E402

UV = "/root/.local/bin/uv"
CARGO = "/root/.cargo/bin/cargo"
PY = sys.executable

SOURCE_RUN = "pilot-cpu-2026-10"   # where the squat names come from (--run)
DEPS = os.path.join(ROOT, "data", "runs", SOURCE_RUN, "deps.jsonl")
PROFILE_YAML = os.path.join(ROOT, "config", "profile.yaml")
RAW_OUT = os.path.join(ROOT, "data", "replay", "replay_raw.json")
MASKED_OUT = os.path.join(ROOT, "data", "evidence", "replay.json")   # read by the analysis
MASKED_RUNS = ["pilot-cpu-2026-10", "pilot-cpu-ext-2026-10"]  # deps_masked.jsonl cross-check

CONTROL_NAME = "phantomdeps-replay-control"
DATELESS_CONTROL_NAME = "phantomdeps-replay-dateless"
CONTROL_BACKDATE_DAYS = 180

# ESP Component Registry (CPS) replay: one harmless marker component served from a
# local static-storage mock under a placeholder, local-only namespace (never the
# real 'espressif' namespace, and never the real registry).
ESP_NAMESPACE = "replaylocal"
ESP_VERSION = "1.0.0"


def tool_versions(npm_reg_bin_probe) -> dict:
    def first_line(r):
        s = (r["stdout_tail"] or r["stderr_tail"]).strip()
        return s.splitlines()[0] if s else "unknown"
    return {
        "python": sys.version.split()[0],
        "pip": first_line(run([PY, "-m", "pip", "--version"], env=base_env())),
        "uv": first_line(run([UV, "--version"], env=base_env())),
        "npm": first_line(run([NPM, "--version"], env=base_env())),
        "node": first_line(run([NODE, "--version"], env=base_env())),
        "verdaccio": npm_reg_bin_probe,
        "cargo": first_line(run([CARGO, "--version"], env=base_env())),
        "idf_component_manager": first_line(run([PY, "-m", "idf_component_manager", "version"], env=base_env()))
        if _eco_on("esp") else "not_run",
        "esp_idf": _esp_idf_version() if _eco_on("esp") else "not_run",
    }


def _esp_idf_version() -> str:
    from replaylib.esp_registry import BUILD_TARGET, esp_idf_setup
    idf = esp_idf_setup()
    if not idf:
        return "not_configured"
    return f"v{idf['version']} ({(idf['commit'] or 'unknown')[:10]}; target {BUILD_TARGET})"


# --------------------------------------------------------------------------
# installer wrappers (each returns a normalized sub-record)

def _mkdir(*p):
    d = os.path.join(*p)
    os.makedirs(d, exist_ok=True)
    return d


def pip_install(workdir, index_url, name, marker_dir):
    venv = os.path.join(workdir, "venv_pip")
    run([PY, "-m", "venv", venv], env=base_env())
    argv = [os.path.join(venv, "bin", "python"), "-m", "pip", "install",
            "--no-cache-dir", "--index-url", index_url, name]
    common.assert_localhost(argv)
    r = run(argv, env=base_env({"REPLAY_MARKER_DIR": marker_dir}))
    r.update(tool="pip", marker=marker_present(marker_dir, "pypi"), installed=(r["exit"] == 0))
    return r


def uv_install(workdir, index_url, name, marker_dir, exclude_newer=None):
    venv = os.path.join(workdir, "venv_uv")
    run([UV, "venv", venv], env=base_env())
    argv = [UV, "pip", "install", "--index-url", index_url, "--no-cache"]
    if exclude_newer:
        argv += ["--exclude-newer", exclude_newer]
    argv += [name]
    common.assert_localhost(argv)
    r = run(argv, env=base_env({"VIRTUAL_ENV": venv, "REPLAY_MARKER_DIR": marker_dir}))
    r.update(tool="uv", marker=marker_present(marker_dir, "pypi"), installed=(r["exit"] == 0))
    return r


def cargo_build(workdir, registry: CratesRegistry, dep_name, dep_version, marker_dir):
    cargo_home = _mkdir(workdir, "cargo_home")
    registry.write_cargo_home(cargo_home)
    cons = _mkdir(workdir, "consumer")
    _mkdir(cons, "src")
    with open(os.path.join(cons, "Cargo.toml"), "w", encoding="utf-8") as f:
        f.write('[package]\nname = "replay-consumer"\nversion = "0.0.0"\nedition = "2021"\n\n'
                f'[dependencies]\n{dep_name} = "={dep_version}"\n')
    with open(os.path.join(cons, "src", "main.rs"), "w", encoding="utf-8") as f:
        f.write("fn main() {}\n")
    env = base_env({"CARGO_HOME": cargo_home, "REPLAY_MARKER_DIR": marker_dir})
    env.pop("CARGO_HTTP_CAINFO", None)  # http localhost needs no TLS CA
    argv = [CARGO, "build", "-j1", "--manifest-path", os.path.join(cons, "Cargo.toml"),
            "--target-dir", os.path.join(workdir, "target")]
    r = run(argv, env=env)
    r.update(tool="cargo", marker=marker_present(marker_dir, "crates"), installed=(r["exit"] == 0))
    return r


NOT_AVAIL_PIP = {"tool": "pip", "status": "not_available",
                 "reason": "pip has no as-of / publication-date install option"}
NOT_AVAIL_CARGO = {"tool": "cargo", "status": "not_available",
                   "reason": "cargo has no stable publication-date install filter"}
NOT_AVAIL_ESP = {"tool": "idf-cm", "status": "not_available",
                 "reason": "idf-component-manager has no publication-date / as-of install filter"}


# --------------------------------------------------------------------------
# measured pin evidence: resolve-only lock steps (no install, no package code).
# Each records argv, exit, elapsed, digest_found and marker_after_lock, and
# returns pin=1 iff the lock file records a content digest for the declared name.

def pin_pypi(workdir, index_url, name):
    """Code-free pin: uv pip compile --generate-hashes --no-build.  The controls
    are published as an inert wheel + sdist, so --no-build resolves from the wheel
    and records a hash without building (no package code runs).  marker_after_lock
    and meta_marker_after_lock prove it."""
    rin = os.path.join(workdir, "requirements.in")
    with open(rin, "w", encoding="utf-8") as f:
        f.write(name + "\n")
    md = _mkdir(workdir, "m")
    argv = [UV, "pip", "compile", "--generate-hashes", "--no-build", "--index-url", index_url, rin]
    common.assert_localhost(argv)
    r = run(argv, env=base_env({"REPLAY_MARKER_DIR": md, "UV_NO_CACHE": "1"}))
    found = "--hash=sha256:" in (r["stdout_tail"] or "")
    r.update(tool="uv", mode="pip compile --no-build", digest_found=found,
             marker_after_lock=marker_present(md, "pypi"),
             meta_marker_after_lock=common.meta_marker_present(md))
    if not found:
        r["reason"] = "uv --no-build found no installable wheel to hash"
    return (1 if found else None), [r]


def pin_cargo(workdir, registry: CratesRegistry, name, version, marker_dir):
    cargo_home = _mkdir(workdir, "cargo_home")
    registry.write_cargo_home(cargo_home)
    cons = _mkdir(workdir, "consumer")
    _mkdir(cons, "src")
    with open(os.path.join(cons, "Cargo.toml"), "w", encoding="utf-8") as f:
        f.write('[package]\nname = "replay-lock-consumer"\nversion = "0.0.0"\nedition = "2021"\n\n'
                f'[dependencies]\n{name} = "={version}"\n')
    with open(os.path.join(cons, "src", "main.rs"), "w", encoding="utf-8") as f:
        f.write("fn main() {}\n")
    env = base_env({"CARGO_HOME": cargo_home, "REPLAY_MARKER_DIR": marker_dir})
    env.pop("CARGO_HTTP_CAINFO", None)
    argv = [CARGO, "generate-lockfile", "--manifest-path", os.path.join(cons, "Cargo.toml")]
    r = run(argv, env=env)
    checksum = None
    lockpath = os.path.join(cons, "Cargo.lock")
    if os.path.isfile(lockpath):
        try:
            with open(lockpath, "rb") as f:
                lock = tomllib.load(f)
            for pkg in lock.get("package", []):
                if pkg.get("name") == name and pkg.get("checksum"):
                    checksum = pkg["checksum"]
        except (OSError, ValueError):
            pass
    r.update(tool="cargo", mode="generate-lockfile", digest_found=(checksum is not None),
             marker_after_lock=marker_present(marker_dir, "crates"))
    return (1 if checksum else None), [r]


# --------------------------------------------------------------------------
# lock_execution: a separate, explicitly labelled measurement (OUTSIDE any policy)
# of whether a resolve-only lock step runs the package's own code, per squat.

def lockexec_pypi(idx, names, work):
    out = []
    for i, name in enumerate(names):
        wd = _mkdir(work, f"lx_pypi_{i}")
        rin = os.path.join(wd, "r.in")
        with open(rin, "w", encoding="utf-8") as f:
            f.write(name + "\n")

        def one(mode, extra):
            md = _mkdir(wd, "m_" + mode)
            argv = [UV, "pip", "compile", "--generate-hashes"] + extra + ["--index-url", idx.index_url, rin]
            r = run(argv, env=base_env({"REPLAY_MARKER_DIR": md, "UV_NO_CACHE": "1"}))
            return {"mode": "uv pip compile" + ("" if not extra else " --no-build"), "argv": argv,
                    "exit": r["exit"], "elapsed_s": r["elapsed_s"],
                    "digest_found": "--hash=sha256:" in (r["stdout_tail"] or ""),
                    "code_ran_during_lock": common.meta_marker_present(md),
                    "install_marker_appeared": marker_present(md, "pypi")}

        without = one("run", [])
        withnb = one("nobuild", ["--no-build"])
        withnb["refused"] = not withnb["digest_found"] and withnb["exit"] != 0
        out.append({"eco": "pypi", "name": name, "without_no_build": without, "with_no_build": withnb})
    return out


def lockexec_npm(reg, names, work):
    out = []
    for i, name in enumerate(names):
        wd = _mkdir(work, f"lx_npm_{i}")
        md = _mkdir(wd, "m")
        _pin, recs = reg.lock_only(name, _mkdir(wd, "host"), md)
        r = recs[0]
        out.append({"eco": "npm", "name": name, "lock": {
            "mode": r["mode"], "argv": r["argv"], "exit": r["exit"], "elapsed_s": r["elapsed_s"],
            "digest_found": r["digest_found"], "code_ran_during_lock": r["marker_after_lock"]}})
    return out


def lockexec_crates(reg, name_versions, work):
    out = []
    for i, (name, version) in enumerate(name_versions):
        wd = _mkdir(work, f"lx_crates_{i}")
        md = _mkdir(wd, "m")
        _pin, recs = pin_cargo(wd, reg, name, version, md)
        r = recs[0]
        out.append({"eco": "crates", "name": name, "lock": {
            "mode": r["mode"], "argv": r["argv"], "exit": r["exit"], "elapsed_s": r["elapsed_s"],
            "digest_found": r["digest_found"], "code_ran_during_lock": r["marker_after_lock"]}})
    return out


def lockexec_esp(reg, ns, name_versions, work):
    """Resolve-only locking for espidf: solve + dump the lock (NO download/unpack).
    code_ran_during_lock is whether component code wrote its marker during the lock
    step, or the component materialised (neither happens: the lock step performs no
    download), so the espidf pin is code-free."""
    out = []
    for i, (name, version) in enumerate(name_versions):
        wd = _mkdir(work, f"lx_esp_{i}")
        _pin, recs = reg.lock_only(ns, name, version, _mkdir(wd, "lockproj"), _mkdir(wd, "m_lock"))
        r = recs[0]
        out.append({"eco": "espidf", "name": name, "lock": {
            "mode": r["mode"], "argv": r["argv"], "exit": r["exit"], "elapsed_s": r["elapsed_s"],
            "digest_found": r["digest_found"],
            "code_ran_during_lock": bool(r["marker_after_lock"] or r["code_ran"])}})
    return out


# --------------------------------------------------------------------------
# per-ecosystem scenario drivers

def scenario_shell(eco, name, role, horizon):
    return {"eco": eco, "name": name, "role": role, "horizon": horizon,
            "registry_meta": {}, "absence_lookup": {}, "policies": {}}


def origin_upload_time(index_url, name):
    """The evidence step: one GET of the record date at the PEP 691 origin index."""
    url = index_url + normalize(name) + "/"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.pypi.simple.v1+json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.load(r)
    elapsed = round(time.monotonic() - t0, 3)
    times = [f.get("upload-time") for f in data.get("files", []) if f.get("upload-time")]
    return (min(times) if times else None), 1, elapsed, url


def assert_orderings(recs, t_gate):
    """Abort unless the real sequence holds: every squat's absence lookup precedes
    its publication, and every recorded publication is at or before t_gate (so P2's
    age is never negative).  Controls are backdated by the harness and so are
    exempt from the absence<publication check."""
    for r in recs:
        pub = r.get("publication_time")
        if r["role"] == "squat":
            if not parse_time(r["absence_time"]) < parse_time(pub):
                raise SystemExit(f"ORDER VIOLATION: {r['eco']}/{r['name']} absence "
                                 f"{r['absence_time']} !< publication {pub}")
        if pub is not None and not parse_time(pub) <= parse_time(t_gate):
            raise SystemExit(f"ORDER VIOLATION: {r['eco']}/{r['name']} publication {pub} > t_gate {t_gate}")


def run_pypi(chosen, control, work):
    idx = PyPIIndex()                      # origin: PEP 503 + 691 + 700 upload-time
    h_ctrl = min(parse_time(c["horizon"]) for c in chosen)
    ctrl_time = h_ctrl - dt.timedelta(days=CONTROL_BACKDATE_DAYS)
    # Phase A -- publish in real order: per squat, absence lookup FIRST, then build+
    # publish, then read the publication time the registry records (strictly later).
    recs = []
    for c in chosen:
        name = c["name"]
        resolved_before = idx.resolves(name)
        absence_time = common.iso_us(common.now_precise())
        meta = idx.add_sdist(name)                      # registry stamps the publish instant
        recs.append({"eco": "pypi", "name": name, "role": "squat", "horizon": c["horizon"],
                     "absence_time": absence_time, "resolved_before": resolved_before,
                     "publication_time": idx.served_upload_time(name),
                     "sha256": meta["sha256"], "filename": meta["filename"], "dists": ["sdist"]})
    resolved_before = idx.resolves(control)
    absence_time = common.iso_us(common.now_precise())
    cmeta = idx.add_sdist(control, upload_time=iso(ctrl_time))   # harness-set record date
    idx.add_wheel(control, upload_time=iso(ctrl_time))
    recs.append({"eco": "pypi", "name": control, "role": "control", "horizon": iso(h_ctrl),
                 "absence_time": absence_time, "resolved_before": resolved_before,
                 "publication_time": idx.served_upload_time(control),
                 "sha256": cmeta["sha256"], "filename": cmeta["filename"], "dists": ["sdist", "wheel"],
                 "record_date_set_by_harness": True, "record_date": iso(ctrl_time)})
    t_gate = common.iso_us(common.now_precise())        # after all squats are published
    assert_orderings(recs, t_gate)
    # Phase B -- policies
    scenarios = []
    for i, r in enumerate(recs):
        name, role, horizon, pub_t = r["name"], r["role"], r["horizon"], r["publication_time"]
        sc = scenario_shell("pypi", name, role, horizon)
        sc["t_gate"] = t_gate
        sc["absence_lookup"] = {"time": r["absence_time"], "resolved_before_publish": r["resolved_before"],
                                "method": "GET /simple/<name>/ on local index"}
        rm = {"filename": r["filename"], "sha256": r["sha256"], "publication_time": pub_t,
              "dists": r["dists"], "resolves": idx.resolves(name)}
        if role == "control":
            rm["record_date_set_by_harness"] = True
            rm["record_date"] = r["record_date"]
        sc["registry_meta"] = rm
        wd = _mkdir(work, f"pypi_{i}")
        sc["policies"]["P1"] = {"installers": [
            pip_install(_mkdir(wd, "P1_pip"), idx.index_url, name, _mkdir(wd, "m_P1_pip")),
            uv_install(_mkdir(wd, "P1_uv"), idx.index_url, name, _mkdir(wd, "m_P1_uv")),
        ]}
        sc["policies"]["P3"] = {"installers": [
            dict(NOT_AVAIL_PIP),
            uv_install(_mkdir(wd, "P3_uv"), idx.index_url, name, _mkdir(wd, "m_P3_uv"),
                       exclude_newer=horizon),
        ]}
        sc["policies"]["P2"] = gate_eval.p2_decision(G, PROFILE, parse_time, pub_t, t_gate)
        block, gate_s = build_scii(
            "pypi", name, role, horizon, pub_t, r["absence_time"], wd,
            lambda lw, n=name: pin_pypi(lw, idx.index_url, n),
            lambda md, n=name: pip_install(_mkdir(wd, "SCII_pip"), idx.index_url, n, md))
        sc["policies"]["SCII"] = block
        sc["gate_time_s"] = gate_s
        sc["install_time_s"] = _p1_primary_elapsed(sc, "pypi")
        scenarios.append(sc)
    scenarios.append(pypi_dateless(idx, work, h_ctrl, ctrl_time, t_gate))
    lx = lockexec_pypi(idx, [c["name"] for c in chosen], _mkdir(work, "lockexec"))
    idx.stop()
    return scenarios, lx


def pypi_dateless(idx_origin, work, h_ctrl, ctrl_time, t_gate):
    """Dateless control: published DURING the replay with a record date the harness
    set CONTROL_BACKDATE_DAYS before the horizon, but served from an HTML-only index
    (no PEP 691 JSON / PEP 700 upload-time), as a static find-links dir or an older
    mirror would.  The PEP 691/700 origin index is the registry of record used only
    by the evidence step.  This is the case where requesting evidence lets a build
    complete that the date filter stops."""
    name = DATELESS_CONTROL_NAME
    idx_dateless = PyPIIndex(html_only=True)
    sc = scenario_shell("pypi", name, "dateless_control", iso(h_ctrl))
    resolved_before = idx_dateless.resolves(name)
    t_obs = common.iso_us(common.now_precise())
    # inert wheel + sdist at the origin (dated) and at the dateless index (undated);
    # the wheel lets the pin lock read metadata code-free under --no-build.
    wheel_fn = None
    for ix in (idx_origin, idx_dateless):
        ix.add_sdist(name, upload_time=iso(ctrl_time))
        wheel_fn = ix.add_wheel(name, upload_time=iso(ctrl_time))["filename"]
    meta_dateless = {"filename": wheel_fn}
    # the gate meets this control after it was published (its own gate time, after the lookup above)
    t_gate = common.iso_us(common.now_precise())
    sc["t_gate"] = t_gate
    sc["absence_lookup"] = {"time": t_obs, "resolved_before_publish": resolved_before,
                            "method": "GET /simple/<name>/ on the dateless index"}
    sc["registry_meta"] = {
        "dateless_index": idx_dateless.index_url, "origin_index": idx_origin.index_url,
        "served_upload_time": None, "origin_record_date": iso(ctrl_time),
        "record_date_set_by_harness": True, "dists": ["sdist", "wheel"],
        "filename": meta_dateless["filename"],
        "resolves_dateless": idx_dateless.resolves(name), "resolves_origin": idx_origin.resolves(name)}
    wd = _mkdir(work, "pypi_dateless")
    du = idx_dateless.index_url
    sc["policies"]["P1"] = {"installers": [
        pip_install(_mkdir(wd, "P1_pip"), du, name, _mkdir(wd, "m_P1_pip")),
        uv_install(_mkdir(wd, "P1_uv"), du, name, _mkdir(wd, "m_P1_uv")),
    ]}
    sc["policies"]["P3"] = {"installers": [
        dict(NOT_AVAIL_PIP),
        uv_install(_mkdir(wd, "P3_uv"), du, name, _mkdir(wd, "m_P3_uv"), exclude_newer=iso(h_ctrl)),
    ]}
    p2 = gate_eval.p2_decision(G, PROFILE, parse_time, iso(ctrl_time), t_gate)
    p2["basis"] += " (harness-set origin record date; the dateless index carries no date)"
    sc["policies"]["P2"] = p2
    # SCII order: gate items first.  First contact: no served date -> est None -> request,
    # G None, so NO lock step yet.  The request triggers one origin date lookup, which
    # establishes est; only once G==1 do we run the code-free pin lock, then decide.
    t0 = time.monotonic()
    first_contact = gate_eval.decide(G, PROFILE, None, None, iso(h_ctrl), pin=None)
    ut, http_reqs, ev_elapsed, ev_url = origin_upload_time(idx_origin.index_url, name)
    ev_pre = gate_eval.decide(G, PROFILE, None, ut, iso(h_ctrl), pin=None)   # est from origin upper bound
    lock_recs, pin = [], None
    if ev_pre["G"] == 1:
        pin, lock_recs = pin_pypi(_mkdir(wd, "lock"), du, name)             # code-free (--no-build, wheel)
        evidence_decision = gate_eval.decide(G, PROFILE, None, ut, iso(h_ctrl), pin=pin)
    else:
        evidence_decision = ev_pre
    gate_time_s = round(time.monotonic() - t0, 3)
    lock_elapsed = round(sum(r["elapsed_s"] for r in lock_recs), 3)
    block = {"pipeline_order": "first contact (request) -> origin date lookup -> lock for pin iff G==1 "
                               "-> decide -> install iff admit",
             "pin_measured": pin, "lock_step_run": bool(lock_recs), "lock": lock_recs,
             "first_contact": first_contact,
             "evidence_step": {"trigger": "first-contact decision == request",
                               "origin_lookup": {"url": ev_url, "upload_time": ut,
                                                 "http_requests": http_reqs, "elapsed_s": ev_elapsed},
                               "G_after_evidence": ev_pre["G"], "decision": evidence_decision},
             "gate_time_s": gate_time_s,
             "evidence_cost": {"extra_http_requests": http_reqs, "evidence_elapsed_s": ev_elapsed,
                               "lock_elapsed_s": lock_elapsed, "total_gate_time_s": gate_time_s}}
    if evidence_decision["decision"] == "admit":
        block["installer_invoked"] = True
        block["install"] = pip_install(_mkdir(wd, "SCII_pip"), du, name, _mkdir(wd, "m_SCII"))
    else:
        block["installer_invoked"] = False
        block["install"] = {"status": "not_invoked",
                            "reason": f"post-evidence decision was '{evidence_decision['decision']}'"}
    sc["policies"]["SCII"] = block
    sc["gate_time_s"] = gate_time_s
    sc["install_time_s"] = _p1_primary_elapsed(sc, "pypi")
    idx_dateless.stop()
    return sc


def run_npm(chosen, control, work):
    reg = NpmRegistry(_mkdir(work, "verdaccio"))
    h_ctrl = min(parse_time(c["horizon"]) for c in chosen)
    ctrl_backdate = iso(h_ctrl - dt.timedelta(days=CONTROL_BACKDATE_DAYS))
    items = [(c["name"], "squat", c["horizon"]) for c in chosen]
    items.append((control, "control", iso(h_ctrl)))
    # Phase A -- publish: per package, absence lookup FIRST, then publish; Verdaccio
    # stamps time.created at publish, strictly after the lookup.
    published = []
    for name, role, horizon in items:
        status_before = reg.lookup_status(name)
        absence_time = common.iso_us(common.now_precise())
        src = _mkdir(work, "npm_src_" + role + "_" + str(len(published)))
        build_npm_pkg(src, name)
        pub = reg.publish(src)
        published.append({"name": name, "role": role, "horizon": horizon,
                          "absence_time": absence_time, "status_before": status_before,
                          "publish_exit": pub["exit"]})
    # backdate the control's record date (stop -> edit stored time -> restart)
    backdate_ok = reg.backdate(control, ctrl_backdate)
    recs = []
    for p in published:
        created = (reg.packument_time(p["name"]) or {}).get("created")
        r = dict(p, publication_time=created, eco="npm")
        recs.append(r)
    t_gate = common.iso_us(common.now_precise())        # after all squats published
    assert_orderings(recs, t_gate)
    scenarios = []
    for i, p in enumerate(recs):
        name, role, horizon = p["name"], p["role"], p["horizon"]
        sc = scenario_shell("npm", name, role, horizon)
        sc["t_gate"] = t_gate
        pub_t = p["publication_time"]
        sc["absence_lookup"] = {"time": p["absence_time"], "status_before_publish": p["status_before"],
                                "method": "GET /<name> on local Verdaccio"}
        sc["registry_meta"] = {"time_created": pub_t, "publication_time": pub_t,
                               "resolves": reg.resolves(name), "publish_exit": p["publish_exit"]}
        if role == "control":
            sc["registry_meta"]["record_date_set_by_harness"] = True
            sc["registry_meta"]["record_date"] = ctrl_backdate
            sc["registry_meta"]["backdate_supported"] = backdate_ok
        wd = _mkdir(work, f"npm_{i}")
        sc["policies"]["P1"] = {"installers": [
            reg.install(name, _mkdir(wd, "P1"), _mkdir(wd, "m_P1"))]}
        sc["policies"]["P3"] = {"installers": [
            reg.install(name, _mkdir(wd, "P3"), _mkdir(wd, "m_P3"), before=horizon)]}
        sc["policies"]["P2"] = gate_eval.p2_decision(G, PROFILE, parse_time, pub_t, t_gate)
        block, gate_s = build_scii(
            "npm", name, role, horizon, pub_t, p["absence_time"], wd,
            lambda lw, n=name: reg.lock_only(n, _mkdir(lw, "host"), _mkdir(lw, "marker")),
            lambda md, n=name: reg.install(n, _mkdir(wd, "SCII"), md))
        sc["policies"]["SCII"] = block
        sc["gate_time_s"] = gate_s
        sc["install_time_s"] = _p1_primary_elapsed(sc, "npm")
        scenarios.append(sc)
    lx = lockexec_npm(reg, [c["name"] for c in chosen], _mkdir(work, "lockexec"))
    reg.stop()
    return scenarios, lx


def run_crates(chosen, control, work):
    reg = CratesRegistry()
    h_ctrl = min(parse_time(c["horizon"]) for c in chosen)
    ctrl_time = h_ctrl - dt.timedelta(days=CONTROL_BACKDATE_DAYS)
    # Phase A -- publish in real order (absence lookup -> publish -> read-back pubtime)
    recs = []
    for c in chosen:
        name = c["name"]
        resolved_before = reg.resolves(name)
        absence_time = common.iso_us(common.now_precise())
        meta = reg.add_crate(name)                      # registry stamps the publish instant
        recs.append({"eco": "crates", "name": name, "role": "squat", "horizon": c["horizon"],
                     "absence_time": absence_time, "resolved_before": resolved_before,
                     "publication_time": reg.served_pubtime(name), "meta": meta})
    resolved_before = reg.resolves(control)
    absence_time = common.iso_us(common.now_precise())
    cmeta = reg.add_crate(control, pubtime=iso(ctrl_time))      # harness-set record date
    recs.append({"eco": "crates", "name": control, "role": "control", "horizon": iso(h_ctrl),
                 "absence_time": absence_time, "resolved_before": resolved_before,
                 "publication_time": reg.served_pubtime(control), "meta": cmeta,
                 "record_date_set_by_harness": True, "record_date": iso(ctrl_time)})
    t_gate = common.iso_us(common.now_precise())
    assert_orderings(recs, t_gate)
    # Phase B -- policies
    scenarios = []
    for i, r in enumerate(recs):
        name, role, horizon, pub_t, meta = r["name"], r["role"], r["horizon"], r["publication_time"], r["meta"]
        sc = scenario_shell("crates", name, role, horizon)
        sc["t_gate"] = t_gate
        sc["absence_lookup"] = {"time": r["absence_time"], "resolved_before_publish": r["resolved_before"],
                                "method": "sparse index path lookup on local registry"}
        rm = {"version": meta["version"], "cksum": meta["cksum"], "index_path": meta["index_path"],
              "publication_time": pub_t, "resolves": reg.resolves(name)}
        if role == "control":
            rm["record_date_set_by_harness"] = True
            rm["record_date"] = r["record_date"]
        sc["registry_meta"] = rm
        wd = _mkdir(work, f"crates_{i}")
        sc["policies"]["P1"] = {"installers": [
            cargo_build(_mkdir(wd, "P1"), reg, name, meta["version"], _mkdir(wd, "m_P1"))]}
        sc["policies"]["P3"] = {"installers": [dict(NOT_AVAIL_CARGO)]}
        sc["policies"]["P2"] = gate_eval.p2_decision(G, PROFILE, parse_time, pub_t, t_gate)
        block, gate_s = build_scii(
            "crates", name, role, horizon, pub_t, r["absence_time"], wd,
            lambda lw, n=name, v=meta["version"]: pin_cargo(lw, reg, n, v, _mkdir(lw, "marker")),
            lambda md, n=name, v=meta["version"]: cargo_build(_mkdir(wd, "SCII"), reg, n, v, md))
        sc["policies"]["SCII"] = block
        sc["gate_time_s"] = gate_s
        sc["install_time_s"] = _p1_primary_elapsed(sc, "crates")
        scenarios.append(sc)
    lx = lockexec_crates(reg, [(c["name"], "0.1.0") for c in chosen], _mkdir(work, "lockexec"))
    reg.stop()
    return scenarios, lx


def run_esp(chosen, control, work):
    """ESP Component Registry (CPS) scenario: ONE squat + one established control,
    structured exactly like run_crates (espidf is REC_LOWER+REC_UPPER, so its record
    date bounds the gate from both sides, like crates pubtime).  P1 drives the real
    idf-component-manager to resolve+download from the local static-storage mock
    ('resolves now' admits the squat; its payload lands in managed_components, marker
    materialised, no component code run), then builds an application that declares it
    with the real ESP-IDF build system (idf.py build, linux host target), which runs
    the component's CMake code and compiles it.  The SCII gate rejects the same name
    (first contact: est refuted, G=0) before any download or build, while the
    backdated control is admitted, locked code-free and built from that lock."""
    reg = EspRegistry()
    ns, ver = ESP_NAMESPACE, ESP_VERSION
    h_ctrl = parse_time(chosen["horizon"])
    ctrl_time = h_ctrl - dt.timedelta(days=CONTROL_BACKDATE_DAYS)
    # Phase A -- publish in real order (absence lookup -> publish -> read-back record date)
    recs = []
    name = chosen["name"]
    resolved_before = reg.resolves(ns, name)
    absence_time = common.iso_us(common.now_precise())
    meta = reg.publish(ns, name, version=ver)                   # record date stamped at publish
    recs.append({"eco": "espidf", "name": name, "role": "squat", "horizon": chosen["horizon"],
                 "absence_time": absence_time, "resolved_before": resolved_before,
                 "publication_time": reg.served_created_at(ns, name), "meta": meta, "ns": ns})
    resolved_before = reg.resolves(ns, control)
    absence_time = common.iso_us(common.now_precise())
    cmeta = reg.publish(ns, control, version=ver, created_at=iso(ctrl_time))   # harness-set record date
    recs.append({"eco": "espidf", "name": control, "role": "control", "horizon": iso(h_ctrl),
                 "absence_time": absence_time, "resolved_before": resolved_before,
                 "publication_time": reg.served_created_at(ns, control), "meta": cmeta, "ns": ns,
                 "record_date_set_by_harness": True, "record_date": iso(ctrl_time)})
    t_gate = common.iso_us(common.now_precise())
    assert_orderings(recs, t_gate)
    # Phase B -- policies
    scenarios = []
    for i, r in enumerate(recs):
        name, role, horizon, pub_t, meta, ns_ = (r["name"], r["role"], r["horizon"],
                                                 r["publication_time"], r["meta"], r["ns"])
        sc = scenario_shell("espidf", name, role, horizon)
        sc["t_gate"] = t_gate
        sc["absence_lookup"] = {"time": r["absence_time"], "resolved_before_publish": r["resolved_before"],
                                "method": "GET /components/<ns>/<name>.json on local static storage"}
        rm = {"namespace": ns_, "component": reg.full_name(ns_, name), "version": meta["version"],
              "component_hash": meta["component_hash"], "publication_time": pub_t,
              "resolves": reg.resolves(ns_, name)}
        if role == "control":
            rm["record_date_set_by_harness"] = True
            rm["record_date"] = r["record_date"]
        sc["registry_meta"] = rm
        wd = _mkdir(work, f"espidf_{i}")
        v_ = meta["version"]
        sc["policies"]["P1"] = {
            "installers": [reg.resolve_download(ns_, name, v_, _mkdir(wd, "P1"), _mkdir(wd, "m_P1"))],
            "build": reg.idf_build(ns_, name, v_, _mkdir(wd, "P1_build"), _mkdir(wd, "m_P1_build"))}
        sc["policies"]["P3"] = {"installers": [dict(NOT_AVAIL_ESP)]}
        sc["policies"]["P2"] = gate_eval.p2_decision(G, PROFILE, parse_time, pub_t, t_gate)
        held = {}                            # the gate's measured lock, handed to the build

        def lock_fn(lw, n=name, v=v_, nn=ns_, held=held):
            pin, recs = reg.lock_only(nn, n, v, _mkdir(lw, "lockproj"), _mkdir(lw, "m_lock"))
            held.update(lock_path=recs[0].get("lock_path"), digest=recs[0].get("digest"))
            return pin, recs

        def installer_fn(md, n=name, v=v_, nn=ns_, held=held, wd=wd):
            rec = reg.resolve_download(nn, n, v, _mkdir(wd, "SCII"), md)
            b = reg.idf_build(nn, n, v, _mkdir(wd, "SCII_build"), _mkdir(wd, "m_SCII_build"),
                              lock_from=held.get("lock_path"))
            if b.get("lock_component_hash") is not None and held.get("digest"):
                b["pin_matches_build"] = b["lock_component_hash"] == held["digest"]
            rec["build"] = b
            return rec

        block, gate_s = build_scii("espidf", name, role, horizon, pub_t, r["absence_time"], wd,
                                   lock_fn, installer_fn)
        if not block["installer_invoked"]:
            block["build"] = {"status": "not_invoked", "reason": "the gate did not admit the component"}
        sc["policies"]["SCII"] = block
        sc["gate_time_s"] = gate_s
        sc["install_time_s"] = _p1_primary_elapsed(sc, "espidf")
        scenarios.append(sc)
    lx = lockexec_esp(reg, ns, [(chosen["name"], ver)], _mkdir(work, "lockexec"))
    reg.stop()
    return scenarios, lx


def build_scii(eco, name, role, horizon, served_pub_time, t_obs, wd, lock_fn, installer_fn):
    """Pipeline order: decide the gate items (res, est) FIRST from what the
    registry reports; run the pin lock step ONLY if the gate passes (G == 1);
    then decide with the measured pin; then install only on admit.  A squat
    (G == 0 or None) never reaches the lock step -- no unvetted package code is
    run to collect evidence.  Returns (block, gate_time_s)."""
    t0 = time.monotonic()
    # Step 1: gate items without pin (pin is non-mandatory, so it cannot change G).
    pre = gate_eval.scii_variants(G, PROFILE, eco, horizon, served_pub_time, t_obs, role, pin=None)
    g = pre["first_contact"]["G"]
    lock_recs, pin = [], None
    if g == 1:
        pin, lock_recs = lock_fn(_mkdir(wd, "lock"))          # Step 2: lock only now
        final = gate_eval.scii_variants(G, PROFILE, eco, horizon, served_pub_time, t_obs, role, pin=pin)
    else:
        final = pre                                           # gate already decided; no lock
    gate_time_s = round(time.monotonic() - t0, 3)
    block = {
        "pipeline_order": "gate items (res, est) -> lock for pin iff G==1 -> decide with pin -> install iff admit",
        "gate_G_before_lock": g,
        "lock_step_run": bool(lock_recs),
        "pin_measured": pin,
        "lock": lock_recs,
        "first_contact": final["first_contact"],
        "with_absence": final["with_absence"],
        "no_pin": {"first_contact": pre["first_contact"], "with_absence": pre["with_absence"]},
        "gate_time_s": gate_time_s,
    }
    decision = final["first_contact"]["decision"]
    if decision == "admit":
        md = _mkdir(wd, "m_SCII")
        block["installer_invoked"] = True
        block["install"] = installer_fn(md)
    else:
        block["installer_invoked"] = False
        block["install"] = {"status": "not_invoked",
                            "reason": f"SCII first-contact decision was '{decision}', not 'admit'"}
    return block, gate_time_s


def _p1_primary_elapsed(sc, eco):
    primary = {"pypi": "uv", "npm": "npm", "crates": "cargo", "espidf": "idf-cm"}[eco]
    for r in sc["policies"]["P1"]["installers"]:
        if r.get("tool") == primary:
            return r.get("elapsed_s")
    return None


PROFILE = None  # set in main
ECO_FILTER = None  # None = all ecosystems; else a set from --eco (names: pypi npm crates esp)


def _eco_on(name: str) -> bool:
    return ECO_FILTER is None or name in ECO_FILTER


# --------------------------------------------------------------------------
# masking: phantom/control names -> mask_key pseudonym; no free-text that could
# leak names.  mask_key normalises the name as the registry compares names
# (PEP 503 for PyPI, lower-case elsewhere) and hashes "<eco>:/<normalised>", so
# a replay squat's pseudonym equals the paper's per-declaration pseudonym in
# data/runs/*/deps_masked.jsonl.

_DROP_KEYS = {"stdout_tail", "stderr_tail", "data"}


def pseudonym(eco: str, name: str) -> str:
    return mask_key(eco, None, name)


def sensitive_tokens(eco: str, name: str) -> list[str]:
    # A name that is ALREADY a pseudonym (the espidf squat uses a classified
    # phantom's ph-<hex> pseudonym as its operational name) is the masked form
    # itself -- there is no real name to hide, and it is allowed to appear in the
    # masked output, so it contributes no scrub/leak token.
    if re.fullmatch(r"ph-[0-9a-f]{10}", name or ""):
        return []
    toks = {name}
    if eco == "pypi":
        toks.add(normalize(name))
        toks.add(_ident(name))
    if eco == "crates":
        toks.add(name.lower())
    # longest first so a short form never leaves a fragment of a longer one
    return sorted(toks, key=len, reverse=True)


def _token_re(tok: str):
    # whole-token match only: not flanked by a letter/digit, so a package name
    # ("port") is masked where it is the identifier but never inside a word ("reported").
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])")


def _scrub(obj, toks, maskid):
    if isinstance(obj, dict):
        return {k: _scrub(v, toks, maskid) for k, v in obj.items() if k not in _DROP_KEYS}
    if isinstance(obj, list):
        return [_scrub(v, toks, maskid) for v in obj]
    if isinstance(obj, str):
        s = obj
        for t in toks:
            if t:
                s = _token_re(t).sub(maskid, s)
        return s
    return obj


def mask_scenario(sc: dict) -> dict:
    maskid = pseudonym(sc["eco"], sc["name"])
    toks = sensitive_tokens(sc["eco"], sc["name"])
    masked = _scrub(sc, toks, maskid)
    masked["name"] = maskid           # pseudonym in place of the real name
    masked["masked"] = True
    return masked, maskid, toks


# --------------------------------------------------------------------------
# per-ecosystem summary table

def _inst(sc, policy, tool):
    for r in sc["policies"][policy]["installers"]:
        if r.get("tool") == tool:
            return r
    return None


def _ratio(k, n):
    return f"{k}/{n}"


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 3) if vals else None


_TOOLS = {"pypi": ("pip", "uv"), "npm": ("npm",), "crates": ("cargo",), "espidf": ("idf-cm",)}


def _replay_ecos(scenarios):
    """The three base ecosystems plus espidf when CPS scenarios are present."""
    ecos = list(selection.ECOS)
    if any(s["eco"] == "espidf" for s in scenarios) and "espidf" not in ecos:
        ecos.append("espidf")
    return ecos


def _tally_decision(scs, path):
    out = {}
    for s in scs:
        node = s["policies"]["SCII"]
        ok = True
        for k in path:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                ok = False
                break
        if ok and isinstance(node, dict) and "decision" in node:
            out[node["decision"]] = out.get(node["decision"], 0) + 1
    return out


def _role_metrics(eco, scs):
    n = len(scs)
    tools = _TOOLS[eco]
    p1 = {}
    for t in tools:
        inst = sum(1 for s in scs if (_inst(s, "P1", t) or {}).get("installed"))
        mark = sum(1 for s in scs if (_inst(s, "P1", t) or {}).get("marker"))
        p1[t] = {"installed": _ratio(inst, n), "marker": _ratio(mark, n)}
    p3 = {}
    for t in tools:
        recs = [_inst(s, "P3", t) for s in scs]
        if recs and recs[0] is not None and recs[0].get("status") == "not_available":
            p3[t] = {"not_available": True}
        else:
            inst = sum(1 for r in recs if r and r.get("installed"))
            blk = sum(1 for r in recs if r and not r.get("installed") and r.get("status") != "not_available")
            p3[t] = {"installed": _ratio(inst, n), "blocked": _ratio(blk, n)}
    m = {
        "n": n,
        "P1": p1,
        "P3": p3,
        "P2_reject": _ratio(sum(1 for s in scs if s["policies"]["P2"]["decision"] == "reject"), n),
        "SCII_first_contact": _tally_decision(scs, ["first_contact"]),
        "SCII_with_absence": _tally_decision(scs, ["with_absence"]),
        "SCII_no_pin_first_contact": _tally_decision(scs, ["no_pin", "first_contact"]),
        "SCII_installs_executed": sum(1 for s in scs if s["policies"]["SCII"].get("installer_invoked")),
        "median_gate_time_s": _median([s.get("gate_time_s") for s in scs]),
        "median_install_time_s": _median([s.get("install_time_s") for s in scs]),
    }
    for s in scs:
        if s["role"] == "dateless_control":
            m["SCII_post_evidence"] = _tally_decision(scs, ["evidence_step", "decision"])
            m["evidence_cost"] = s["policies"]["SCII"].get("evidence_cost")
        if "backdate_supported" in s["registry_meta"]:
            m["backdate_supported"] = s["registry_meta"]["backdate_supported"]
    if eco == "espidf":
        def tally(builds):
            if builds and all(b.get("status") == "not_available" for b in builds):
                return {"not_available": True}
            run_ = [b for b in builds if b.get("status") not in ("not_available", "not_invoked")]
            return {"invoked": _ratio(len(run_), n),
                    "built": _ratio(sum(1 for b in run_ if b.get("built")), n),
                    "cmake_marker": _ratio(sum(1 for b in run_ if b.get("cmake_marker")), n),
                    "component_lib": _ratio(sum(1 for b in run_ if b.get("component_lib")), n),
                    "pin_matches_build": _ratio(sum(1 for b in run_ if b.get("pin_matches_build")), n)}
        m["P1_download_code_ran"] = _ratio(sum(1 for s in scs if (_inst(s, "P1", "idf-cm") or {}).get("code_ran")), n)
        m["P1_build"] = tally([s["policies"]["P1"].get("build") or {} for s in scs])
        m["SCII_build"] = tally([((s["policies"]["SCII"].get("install") or {}).get("build")
                                  or s["policies"]["SCII"].get("build") or {}) for s in scs])
    return m


def summarize(scenarios: list[dict], versions: dict, lock_execution: dict) -> dict:
    out = {"tool_versions": versions}
    for eco in _replay_ecos(scenarios):
        out[eco] = {}
        for role in ("squat", "control", "dateless_control"):
            scs = [s for s in scenarios if s["eco"] == eco and s["role"] == role]
            if scs:
                out[eco][role] = _role_metrics(eco, scs)
    out["lock_execution"] = summarize_lockexec(lock_execution)
    return out


def summarize_lockexec(lx: dict) -> dict:
    out = {}
    py = lx.get("pypi", [])
    npy = len(py)
    if npy:
        out["pypi"] = {
            "n": npy,
            "without_no_build": {
                "code_ran_during_lock": _ratio(sum(1 for e in py if e["without_no_build"]["code_ran_during_lock"]), npy),
                "install_marker": _ratio(sum(1 for e in py if e["without_no_build"]["install_marker_appeared"]), npy),
                "digest": _ratio(sum(1 for e in py if e["without_no_build"]["digest_found"]), npy)},
            "with_no_build": {
                "refused": _ratio(sum(1 for e in py if e["with_no_build"]["refused"]), npy),
                "code_ran_during_lock": _ratio(sum(1 for e in py if e["with_no_build"]["code_ran_during_lock"]), npy)},
        }
    for eco in ("npm", "crates", "espidf"):
        ents = lx.get(eco, [])
        ne = len(ents)
        if not ne:
            continue
        out[eco] = {
            "n": ne,
            "code_ran_during_lock": _ratio(sum(1 for e in ents if e["lock"]["code_ran_during_lock"]), ne),
            "digest": _ratio(sum(1 for e in ents if e["lock"]["digest_found"]), ne)}
    return out


def mask_lockexec(lock_execution: dict):
    """Return (masked lock_execution, token list) with names -> pseudonyms."""
    masked, tokens = {}, []
    for eco, entries in lock_execution.items():
        masked[eco] = []
        for e in entries:
            maskid = pseudonym(eco, e["name"])
            toks = sensitive_tokens(eco, e["name"])
            me = _scrub(e, toks, maskid)
            me["name"] = maskid
            masked[eco].append(me)
            tokens.append((eco, e["name"], toks))
    return masked, tokens


def _pub_time(sc):
    rm = sc["registry_meta"]
    return rm.get("publication_time") or rm.get("record_date") or rm.get("origin_record_date")


def verify_orderings(scenarios: list[dict]) -> dict:
    """Re-check, across the assembled scenarios, that every squat has
    absence_time < publication_time <= t_gate (controls/dateless: publication, i.e.
    the harness-set record date, <= t_gate).  Abort on any violation."""
    squats = 0
    for sc in scenarios:
        tg, pub, absn = sc.get("t_gate"), _pub_time(sc), sc["absence_lookup"]["time"]
        if sc["role"] == "squat":
            squats += 1
            if not (parse_time(absn) < parse_time(pub) <= parse_time(tg)):
                raise SystemExit(f"ORDERING: {sc['eco']}/{sc['role']} absence={absn} "
                                 f"publication={pub} t_gate={tg}")
        else:
            if not parse_time(pub) <= parse_time(tg):
                raise SystemExit(f"ORDERING: {sc['eco']}/{sc['role']} publication={pub} > t_gate={tg}")
    return {"squats_checked": squats,
            "all_absence_lt_publication_le_tgate": True,
            "controls_publication_le_tgate": True}


def crosscheck_pseudonyms(scenarios: list[dict]) -> dict:
    """Assert every squat's replay pseudonym occurs in some run's deps_masked.jsonl.

    A run without deps_masked.jsonl is skipped (and reported); the assertion runs
    against the union of the runs that have it."""
    squat_ps = sorted({(s["eco"], pseudonym(s["eco"], s["name"]))
                       for s in scenarios if s["role"] == "squat"})
    present, checked, skipped = set(), [], []
    for run_id in MASKED_RUNS:
        path = os.path.join(ROOT, "data", "runs", run_id, "deps_masked.jsonl")
        if not os.path.isfile(path):
            skipped.append(run_id)
            continue
        checked.append(run_id)
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                row = json.loads(line)
                present.add((row.get("eco"), row.get("name")))
    missing = [{"eco": e, "pseudonym": p} for e, p in squat_ps if (e, p) not in present]
    return {"checked_runs": checked, "skipped_runs": skipped,
            "n_squat_pseudonyms": len(squat_ps),
            "all_found": (len(missing) == 0) if checked else None, "missing": missing}


# --------------------------------------------------------------------------

NOTES = [
    "All registries listen on 127.0.0.1 only; no package is published to any real registry.",
    "With --netns (as for the recorded run) the whole replay runs in a fresh network namespace whose only "
    "interface is loopback (network_isolation, read from /proc/self/net before any server starts), so no "
    "installer or build step can reach anything but the local registries, whatever it tries.",
    "Packages are harmless: they only write the fixed marker text to $REPLAY_MARKER_DIR.",
    "P1 = installer default (resolves-now). P3 here = the installers' own as-of-h date filters (uv "
    "--exclude-newer, npm --before); pip, cargo and the ESP-IDF component manager have no such option "
    "(recorded not_available). The paper's P3 is the record-age policy, which these filters match only "
    "when a name's earliest artifact dates its record (PROTOCOL.md, section 7).",
    "SCII pipeline ORDER: decide the gate items (res, est) first from what the registry reports; run "
    "the pin lock step ONLY if the gate passes (G==1); then decide with the measured pin; then install "
    "only on admit. A squat (G==0 or None) never reaches the lock step, so no unvetted package code is "
    "run to collect evidence.",
    "Pin evidence is CODE-FREE: npm install --package-lock-only --ignore-scripts (integrity); cargo "
    "generate-lockfile (checksum); uv pip compile --generate-hashes --no-build (sha256). The controls "
    "are published as an inert wheel + sdist so --no-build reads the wheel without building; squats are "
    "sdist-only. marker_after_lock is recorded for every lock step (false throughout). scii.no_pin keeps "
    "the pin=None decision for comparison.",
    "Resolving an sdist-only package with uv WITHOUT --no-build runs its build backend "
    "(get_requires_for_build_wheel / prepare_metadata_for_build_wheel) -- package-authored code. The "
    "top-level lock_execution section measures this directly on each squat with a distinct metadata-hook "
    "marker (code_ran_during_lock), which is exactly why the gate runs before any lock step and the pin "
    "comes from code-free locking.",
    "Bound models: npm time.created and crates pubtime bound the record from both sides; PyPI upload "
    "times bound it only from above (tau_lo=None on first contact) unless a real pre-publish absence "
    "lookup supplies tau_lo (the 'with_absence' variant).",
    "cargo has no stable publication-date install filter, so P3 is not available for crates.",
    "Controls are NOT pre-existing packages: they are published DURING the replay with a record date "
    "the harness set control_backdate_days (180) before the horizon (record_date_set_by_harness=true). "
    "They are not 'published before the horizon'.",
    "dateless_control (PyPI): such a control served from an HTML-only index with no PEP 700 upload-time; "
    "the date filter stops it, first contact yields 'request', and one lookup of the record date at the "
    "PEP 691 origin index answers the request so the build can complete.",
    "Ordering is made real per squat and asserted (ordering_check): absence lookup first, then build+"
    "publish (the registry stamps the publish instant), so absence_time < publication_time; t_gate is "
    "taken after an ecosystem's squats are published, so publication_time <= t_gate and P2 age >= 0.",
    "espidf (CPS stand-in): the REAL idf-component-manager 3.1.2 resolves+downloads one harmless "
    "marker component from a local static-storage mock on 127.0.0.1 (GET /components/<ns>/<name>.json, "
    "then the .tgz and CHECKSUMS.json), driven via its download_project_dependencies/version-solver API. "
    "P1 ('resolves now') admits the squat: its payload is unpacked into managed_components/ and the "
    "packed marker file is MATERIALISED into the build tree (marker=1). Unlike pip/npm/cargo, downloading "
    "a component runs NO component code: the component's CMake file writes a second marker when it is "
    "evaluated, and it is absent after the download (P1_download_code_ran=0).",
    "espidf build: the REAL ESP-IDF build system (esp_idf in tool_versions; a shallow release checkout "
    "without git submodules, IDF_SKIP_CHECK_SUBMODULES=1) runs 'idf.py --preview build' for the linux "
    "host target (host gcc, no cross toolchain) on a minimal application whose main/idf_component.yml "
    "declares the component. The build calls the component manager at configure time, which downloads "
    "the component from the mock; CMake evaluates its CMakeLists.txt (cmake_marker: component code ran "
    "during the build) and compiles its source into the component library (component_lib). Under P1 the "
    "squat is built; under SCII the squat is rejected before any download or build, and the admitted "
    "control is built from the lock file the gate measured (pin_matches_build: the hash in the build's "
    "lock equals the measured pin). The application is not flashed or run.",
    "espidf is REC_LOWER+REC_UPPER (like crates): the component record date (created_at) bounds the gate "
    "from both sides. The squat's record date is stamped at publish (after h), so est is refuted (G=0) and "
    "the SCII gate REJECTS it at first contact and with-absence, never reaching the lock step or installer. "
    "The backdated control (record date control_backdate_days before h) is established (est=1) and admitted.",
    "espidf pin is CODE-FREE: the version solver + lock-file dump (no download, no unpack) records the "
    "component_hash the static storage serves; the lock step creates no managed_components and runs no "
    "component code (code_ran_during_lock=0). idf-component-manager has no publication-date install "
    "filter, so P3 is not available for espidf (like cargo).",
    "The espidf scenario with role 'squat' is a stand-in, not a squat (the paper says so): every ESP-IDF "
    "phantom of the pilot's greedy pass lies in Espressif's own namespace, which no outsider can publish "
    "into. Its name is a classified ESP phantom's keyed pseudonym (mask_key('espidf', owner, name), "
    "which embeds the namespace), used verbatim as the operational component name under a placeholder "
    "local namespace. It is a harmless ph-<hex> placeholder that never resolves on the real registry, and "
    "mask_key is idempotent on it, so it equals the paper's per-declaration pseudonym and cross-checks.",
]


def write_doc(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def main(netns: bool = False):
    global PROFILE
    isolation = None
    if netns:
        common.bring_loopback_up()
        isolation = common.network_isolation()
        if not isolation["isolated"]:
            raise SystemExit(f"--netns: the network namespace is not loopback-only: {isolation}")
    with open(PROFILE_YAML, encoding="utf-8") as f:
        PROFILE = G.Profile.from_dict(yaml.safe_load(f))

    verdaccio_bin = os.path.join(ROOT, "replay", "node_modules", ".bin", "verdaccio")
    vprobe = run([NODE, verdaccio_bin, "--version"], env=base_env())
    verdaccio_ver = (vprobe["stdout_tail"] or vprobe["stderr_tail"]).strip().splitlines()[-1] \
        if vprobe["exit"] == 0 else "unknown"
    versions = tool_versions(verdaccio_ver)

    chosen = selection.select(DEPS)
    chosen_esp = selection.select_esp(DEPS)
    work = tempfile.mkdtemp(prefix="replay_run_")
    scenarios = []
    lock_execution = {}
    try:
        if _eco_on("pypi"):
            sc_p, lock_execution["pypi"] = run_pypi(chosen["pypi"], CONTROL_NAME, _mkdir(work, "pypi"))
            scenarios += sc_p
        if _eco_on("npm"):
            sc_n, lock_execution["npm"] = run_npm(chosen["npm"], CONTROL_NAME, _mkdir(work, "npm"))
            scenarios += sc_n
        if _eco_on("crates"):
            sc_c, lock_execution["crates"] = run_crates(chosen["crates"], CONTROL_NAME, _mkdir(work, "crates"))
            scenarios += sc_c
        if _eco_on("esp"):
            if chosen_esp is None:
                print(f"[esp] no espidf phantom in {SOURCE_RUN}; skipping CPS replay")
            else:
                sc_e, lock_execution["espidf"] = run_esp(chosen_esp, CONTROL_NAME, _mkdir(work, "espidf"))
                scenarios += sc_e

        summary = summarize(scenarios, versions, lock_execution)
        xcheck = crosscheck_pseudonyms(scenarios)
        order_check = verify_orderings(scenarios)
        profile_echo = {"theta": PROFILE.theta, "alpha": PROFILE.alpha,
                        "mandatory": list(PROFILE.mandatory), "delta_est_days": PROFILE.delta_est_days,
                        "age_heuristic_days": PROFILE.age_heuristic_days}
        common_doc = {"generated_at": iso(now()), "source_run": SOURCE_RUN,
                      "t_gate_per_ecosystem": {s["eco"]: s.get("t_gate") for s in scenarios},
                      "tool_versions": versions, "profile": profile_echo,
                      "control_backdate_days": CONTROL_BACKDATE_DAYS,
                      "ecosystems": _replay_ecos(scenarios), "notes": NOTES,
                      "network_isolation": isolation if isolation else {"isolated": False, "netns": False},
                      "pseudonym_crosscheck": xcheck, "ordering_check": order_check}

        raw = dict(common_doc)
        raw["control_name_template"] = CONTROL_NAME          # real names: raw file only
        raw["dateless_control_name_template"] = DATELESS_CONTROL_NAME
        raw["selection"] = chosen
        raw["scenarios"] = scenarios
        raw["lock_execution"] = lock_execution
        raw["summary"] = summary
        write_doc(RAW_OUT, raw)

        masked_scns = []
        all_tokens = []
        for sc in scenarios:
            m, maskid, toks = mask_scenario(sc)
            masked_scns.append(m)
            all_tokens.append((sc["eco"], sc["name"], toks))
        masked_lx, lx_tokens = mask_lockexec(lock_execution)
        all_tokens += lx_tokens
        masked = dict(common_doc)
        masked["masking"] = ("phantom and control names are pseudonyms from phantomdeps.util.mask_key"
                             "(eco, None, name), which normalises the name as the registry compares it "
                             "(PEP 503 for PyPI, lower-case elsewhere) so each equals the paper's "
                             "per-declaration pseudonym in data/runs/*/deps_masked.jsonl; stdout/stderr "
                             "tails are omitted; the file is verified to contain no real name")
        masked["scenarios"] = masked_scns
        masked["lock_execution"] = masked_lx
        masked["summary"] = summary

        blob = json.dumps(masked, sort_keys=True)
        leaked = []
        for eco, name, toks in all_tokens:
            for t in toks:
                if t and _token_re(t).search(blob):
                    leaked.append((eco, t))
        if leaked:
            raise SystemExit(f"MASKING FAILED: real name(s) present in masked output: {leaked}")
        write_doc(MASKED_OUT, masked)

        if xcheck["all_found"] is False:
            raise SystemExit(f"PSEUDONYM CROSS-CHECK FAILED: {xcheck['missing']} "
                             f"(checked {xcheck['checked_runs']})")

        _print_console(versions, chosen, summary, scenarios, xcheck, order_check)
    finally:
        common.run_cleanup()
        shutil.rmtree(work, ignore_errors=True)


def _print_console(versions, chosen, summary, scenarios, xcheck, order_check):
    print("\n=== tool versions ===")
    for k, v in versions.items():
        print(f"  {k:10} {v}")
    print("\n=== chosen phantom names (real; raw file only) ===")
    for eco in selection.ECOS:
        print(f"  {eco}: " + ", ".join(f"{c['name']} (h={c['horizon'][:10]})" for c in chosen.get(eco, [])))
    for sc in scenarios:
        if sc["eco"] == "espidf" and sc["role"] == "squat":
            print(f"  espidf: {sc['name']} (h={sc['horizon'][:10]})  [ph pseudonym = operational name]")
    print("\n=== per-ecosystem / per-role outcomes ===")
    for eco in _replay_ecos(scenarios):
        for role in ("squat", "control", "dateless_control"):
            if role not in summary[eco]:
                continue
            m = summary[eco][role]
            print(f"\n[{eco}/{role}]  n={m['n']}")
            print(f"  P1 installed/marker: {m['P1']}")
            print(f"  P3:                  {m['P3']}")
            print(f"  P2 reject:           {m['P2_reject']}")
            print(f"  SCII first_contact (pin): {m['SCII_first_contact']}  no_pin: {m['SCII_no_pin_first_contact']}")
            if m["SCII_with_absence"]:
                print(f"  SCII with_absence:   {m['SCII_with_absence']}")
            if "SCII_post_evidence" in m:
                print(f"  SCII post_evidence:  {m['SCII_post_evidence']}  evidence_cost={m.get('evidence_cost')}")
            print(f"  SCII installs executed: {m['SCII_installs_executed']}; "
                  f"median gate={m['median_gate_time_s']}s install={m['median_install_time_s']}s")
            if eco == "espidf":
                print(f"  P1 download ran code: {m['P1_download_code_ran']}")
                print(f"  P1 build:   {m['P1_build']}")
                print(f"  SCII build: {m['SCII_build']}")
    print("\n=== lock_execution (resolve-only locking runs package code?) ===")
    le = summary["lock_execution"]
    if "pypi" in le:
        print(f"  pypi without --no-build: code_ran {le['pypi']['without_no_build']['code_ran_during_lock']}, "
              f"install_marker {le['pypi']['without_no_build']['install_marker']}, "
              f"digest {le['pypi']['without_no_build']['digest']}")
        print(f"  pypi with --no-build:    refused {le['pypi']['with_no_build']['refused']}, "
              f"code_ran {le['pypi']['with_no_build']['code_ran_during_lock']}")
    if "npm" in le:
        print(f"  npm:    code_ran {le['npm']['code_ran_during_lock']}, digest {le['npm']['digest']}")
    if "crates" in le:
        print(f"  crates: code_ran {le['crates']['code_ran_during_lock']}, digest {le['crates']['digest']}")
    if "espidf" in le:
        print(f"  espidf: code_ran {le['espidf']['code_ran_during_lock']}, digest {le['espidf']['digest']}")
    print(f"\npseudonym cross-check: checked={xcheck['checked_runs']} skipped={xcheck['skipped_runs']} "
          f"all_found={xcheck['all_found']}")
    print(f"ordering check: squats={order_check['squats_checked']} "
          f"absence<publication<=t_gate={order_check['all_absence_lt_publication_le_tgate']}")
    print(f"raw   -> {RAW_OUT}")
    print(f"masked-> {MASKED_OUT}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", nargs="+", default=None,
                    help="runs: the first supplies the squat names, all are cross-checked (default: the pilot)")
    ap.add_argument("--eco", nargs="+", default=None, choices=["pypi", "npm", "crates", "esp"],
                    help="restrict to these ecosystems (default: all). e.g. --eco esp for the CPS case only")
    ap.add_argument("--netns", action="store_true",
                    help="run in a fresh loopback-only network namespace (re-executes under 'unshare -n')")
    a = ap.parse_args()
    if a.netns and os.environ.get("REPLAY_IN_NETNS") != "1":
        os.environ["REPLAY_IN_NETNS"] = "1"
        os.execvp("unshare", ["unshare", "-n", sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
    if a.run:
        SOURCE_RUN = a.run[0]
        DEPS = os.path.join(ROOT, "data", "runs", SOURCE_RUN, "deps.jsonl")
        MASKED_RUNS = list(a.run)
    if a.eco:
        ECO_FILTER = set(a.eco)
    main(netns=a.netns)
