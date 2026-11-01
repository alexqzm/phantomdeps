"""Registry snapshots and resolvers that also report publication time.

Each lookup returns a :class:`Resolution` with
  * ``status``  found | found_variant | not_found | error
  * ``tau_lo``/``tau_hi``  bounds on the first publication time of the name
    (equal when the registry reports it exactly; ``tau_lo`` is ``None`` when
    only an upper bound is known, both ``None`` when nothing is known).

Static indices (Arduino Library Manager, rosdistro, rosdep, micropython-lib)
are downloaded once and hashed.  API registries (PlatformIO, ESP Component
Registry, crates.io sparse index, PyPI, npm) are queried per name and every
answer is cached with its retrieval time, which makes the cache the snapshot.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import yaml

from . import util
from .util import (CACHE, DERIVED, SNAP_GIT, SNAP_RAW, http_get, iso, load_json,
                   parse_time, quote, utcnow, write_json)

# --------------------------------------------------------------------------
# normalisation


def norm_pypi(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


# the component manager's slug grammar (idf_component_tools.manifest.constants, version 3.1.2):
# letters and digits with single '_' or '-' between them; a dependency name is a slug or
# namespace/slug (FULL_SLUG_REGEX), and a manifest whose dependency name breaks it is refused
# ("Invalid component name", manifest/models.py), as is one with two consecutive underscores
ESP_SLUG = r"[a-zA-Z\d]+(?:(?:[_-](?![_-]+))|(?:[a-zA-Z\d]))*[a-zA-Z\d]+"
ESP_FULL_SLUG = re.compile(rf"^(?:{ESP_SLUG}/{ESP_SLUG}|{ESP_SLUG})$")


def esp_name_valid(namespace: str | None, name: str) -> bool:
    full = f"{namespace}/{name}" if namespace else name
    return bool(ESP_FULL_SLUG.match(full)) and "__" not in full


def norm_url(url: str) -> str:
    u = url.strip().lower()
    u = re.sub(r"^git\+", "", u)
    u = re.sub(r"^(https?|git|ssh)://", "", u)
    u = re.sub(r"^git@([^:]+):", r"\1/", u)
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u


@dataclasses.dataclass
class Resolution:
    eco: str
    name: str
    status: str
    canonical: str | None = None
    tau_lo: str | None = None
    tau_hi: str | None = None
    tau_source: str | None = None
    registry: str | None = None
    detail: dict = dataclasses.field(default_factory=dict)
    fetched_at: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------
# static indices


ARDUINO_INDEX = SNAP_RAW / "arduino_library_index.json"
ARDUINO_REGISTRY_REPO = SNAP_GIT / "library-registry"
MPY_INDEX = SNAP_RAW / "micropython_index_v2.json"
MPY_LIB_REPO = SNAP_GIT / "micropython-lib"
ROS_FILES = {"humble": SNAP_RAW / "ros_humble_distribution.yaml",
             "jazzy": SNAP_RAW / "ros_jazzy_distribution.yaml"}
ROSDEP_FILES = [SNAP_RAW / f"rosdep_{k}.yaml" for k in ("base", "python", "ruby")]

STATIC_SOURCES = {
    "arduino_library_index.json": "https://downloads.arduino.cc/libraries/library_index.json",
    "micropython_index_v2.json": "https://micropython.org/pi/v2/index.json",
    "ros_humble_distribution.yaml": "https://raw.githubusercontent.com/ros/rosdistro/master/humble/distribution.yaml",
    "ros_jazzy_distribution.yaml": "https://raw.githubusercontent.com/ros/rosdistro/master/jazzy/distribution.yaml",
    "rosdep_base.yaml": "https://raw.githubusercontent.com/ros/rosdistro/master/rosdep/base.yaml",
    "rosdep_python.yaml": "https://raw.githubusercontent.com/ros/rosdistro/master/rosdep/python.yaml",
    "rosdep_ruby.yaml": "https://raw.githubusercontent.com/ros/rosdistro/master/rosdep/ruby.yaml",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def arduino_first_seen(repo: Path = ARDUINO_REGISTRY_REPO) -> dict:
    """First date each repository URL appears in ``repositories.txt``.

    The list was created on 2021-01-18 from the pre-existing Library Manager
    index; URLs present in that commit only get an upper bound.
    """
    # --first-parent with first-parent diffs: one entry per change that landed on the
    # registry's main line (merge commits made by GitHub, or maintainers' direct commits),
    # dated when it landed.  The submitter's own commits inside a pull request, whose dates
    # the submitter controls, are not read.
    log = _git(repo, "log", "--first-parent", "--diff-merges=first-parent", "--reverse",
               "--format=@@C %H %cI %cn|%s", "-p", "--unified=0", "--", "repositories.txt")
    first: dict[str, dict] = {}
    initial_commit = None
    commits: list[dict] = []
    cur = None
    for line in log.splitlines():
        if line.startswith("@@C "):
            parts = line.split(" ", 3)
            committer, _, msg = (parts[3] if len(parts) > 3 else "").partition("|")
            cur = {"commit": parts[1], "date": parts[2], "committer": committer, "msg": msg,
                   "add": [], "rm": []}
            commits.append(cur)
            if initial_commit is None:
                initial_commit = (cur["commit"], cur["date"])
            continue
        if cur is None:
            continue
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip().startswith("http"):
            cur["add"].append(norm_url(line[1:]))
        elif line.startswith("-") and not line.startswith("---") and line[1:].strip().startswith("http"):
            cur["rm"].append(norm_url(line[1:]))
    moved = 0
    for c in commits:
        for url in c["add"]:
            if url in first:
                continue
            rec = {"date": c["date"], "commit": c["commit"], "committer": c["committer"],
                   "initial_import": c["commit"] == initial_commit[0]}
            # A one-for-one replacement is a repository move: the library keeps
            # its history.  Require equal repository names or a move-like message.
            if len(c["add"]) == 1 and len(c["rm"]) == 1 and c["rm"][0] in first:
                old = c["rm"][0]
                same_base = old.rsplit("/", 1)[-1] == url.rsplit("/", 1)[-1]
                move_msg = re.search(r"\b(?:mov(?:e|ed|ing)|renam\w*|transfer\w*|migrat\w*)\b|\burl\b",
                                     c["msg"], re.I)
                if same_base or move_msg:
                    rec = dict(first[old])
                    rec["moved_from"] = old
                    rec["moved_at"] = c["date"]
                    moved += 1
            first[url] = rec
    return {"initial_commit": initial_commit, "head": _git(repo, "rev-parse", "HEAD").strip(),
            "first": first, "n_moves_linked": moved}


def micropython_first_seen(index: dict, repo: Path = MPY_LIB_REPO) -> dict:
    """Upper bound on when a package entered micropython-lib, from which the mip index is built.

    Git dates are set by whoever creates a commit.  The maintainers of micropython-lib put
    contributions on the main line by re-committing (rebasing) them, so a main-line commit
    whose committer integrated other people's work carries a maintainer's date.  A
    contribution that reached the main line with its author as committer carries a date the
    contributor chose; such commits are skipped, and the first maintainer commit that touched
    the package's manifest bounds the date from above instead.
    """
    rows = [ln.split("|", 2) for ln in _git(repo, "log", "--first-parent", "--format=%an|%cn|%cI").splitlines()
            if ln.count("|") >= 2]
    maintainers = sorted({cn for an, cn, _ in rows if an != cn})
    out = {}
    for p in index["packages"]:
        path = p.get("path")
        if not path:
            continue
        log = [ln.split("|", 2) for ln in _git(repo, "log", "--first-parent", "--format=%an|%cn|%cI", "--",
                                               f"{path}/manifest.py").splitlines() if ln.count("|") >= 2]
        trusted = [r for r in log if r[1] in maintainers]
        if trusted:
            an, cn, date = trusted[-1]                     # oldest maintainer commit (log is newest first)
            out[p["name"]] = {"date": date, "path": path, "committer": cn,
                              "skipped_contributor_commits": len(log) - len(trusted)}
    return {"head": _git(repo, "rev-parse", "HEAD").strip(), "maintainers": maintainers, "first": out}


ROSDISTRO_REPO = SNAP_GIT / "rosdistro"


def ros_first_seen(repo: Path = ROSDISTRO_REPO, distro: str = "humble") -> dict:
    """First date each package (and rosdep key) appears in the ROS distribution files.

    Like the Arduino registry, the ROS distribution is a git repository whose main line is
    changed by pull-request merges that GitHub creates (or maintainers' commits), so a
    first-parent change is dated by the registry, not by the submitter.  A package line
    (``- name`` in a release package list) or a repository key (``  name:``, the package of a
    single-package repository) added on the main line marks the first appearance of that name in
    the distribution file of ``distro``; rosdep keys are dated by their first line in
    ``rosdep/*.yaml``.  The distribution file of a release is created by branching (for Humble on
    2022-04-20); names in that first commit are dated by it, which is when the record of that
    distribution came into existence.
    """
    def first_added(path: str, pat: re.Pattern) -> tuple[dict, tuple]:
        log = _git(repo, "log", "-p", "--first-parent", "--reverse", "--format=@@C %H %cI %cn",
                   "--unified=0", "--", path)
        first: dict[str, dict] = {}
        cur = None
        initial = None
        for line in log.splitlines():
            if line.startswith("@@C "):
                _, h, date, *who = line.split(" ")
                cur = {"commit": h, "date": date, "committer": " ".join(who)}
                initial = initial or (h, date)
                continue
            if cur is None or not line.startswith("+") or line.startswith("+++"):
                continue
            m = pat.match(line[1:])
            if m and m.group(1) not in first:
                first[m.group(1)] = dict(cur, initial_import=cur["commit"] == initial[0])
        return first, initial

    pkg_pat = re.compile(r"^(?:\s+- ([A-Za-z0-9_][A-Za-z0-9_.-]*)|  ([A-Za-z0-9_][A-Za-z0-9_.-]*):)\s*$")

    class _P:   # one pattern, two alternatives: normalise to group(1)
        def match(self, line):
            m = pkg_pat.match(line)
            if not m:
                return None
            name = m.group(1) or m.group(2)
            return type("M", (), {"group": lambda self, i, name=name: name})()
    pkgs, init = first_added(f"{distro}/distribution.yaml", _P())
    keys: dict[str, dict] = {}
    for f in ("base", "python", "ruby"):
        got, _ = first_added(f"rosdep/{f}.yaml", re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.+-]*):\s*$"))
        for k, v in got.items():
            if k not in keys or v["date"] < keys[k]["date"]:
                keys[k] = dict(v, file=f)
    return {"head": _git(repo, "rev-parse", "HEAD").strip(), "distro": distro, "initial_commit": init,
            "packages": pkgs, "rosdep": keys}


def build_static() -> dict:
    """Derive name sets from the raw static snapshots; record hashes."""
    meta: dict = {"built_at": utcnow(), "files": {}}
    for fname in STATIC_SOURCES:
        p = SNAP_RAW / fname
        if p.exists():
            meta["files"][fname] = {"sha256": util.sha256_file(p), "bytes": p.stat().st_size,
                                    "source": STATIC_SOURCES[fname]}
    fetched = SNAP_RAW / "fetched_at.json"
    meta["fetched_at"] = load_json(fetched, {})

    # Arduino
    idx = load_json(ARDUINO_INDEX)
    # one record per exact library name: the Library Manager resolves names exactly, and names that
    # differ only in case (BME280 / Bme280, MPU6050 / mpu6050) are different libraries
    libs: dict[str, dict] = {}
    for rel in idx["libraries"]:
        k = rel["name"]
        e = libs.setdefault(k, {"name": rel["name"], "repository": rel.get("repository"),
                                "versions": 0, "includes": set()})
        e["versions"] += 1
        e["repository"] = rel.get("repository") or e["repository"]
        for h in rel.get("providesIncludes") or []:
            e["includes"].add(h)
    for e in libs.values():
        e["includes"] = sorted(e["includes"])
    # a repository whose releases carry several names was renamed: the registry log dates the
    # repository, not the later name, so later names keep only a lower bound
    by_repo: dict[str, list] = {}
    for rel in idx["libraries"]:
        by_repo.setdefault(norm_url(rel.get("repository") or ""), []).append(rel)
    for repo_url, rels in by_repo.items():
        names = {r["name"] for r in rels}
        if len(names) < 2 or not repo_url:
            continue
        def vkey(r):
            return [int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", str(r.get("version", "0")))[:3]]
        original = min(rels, key=vkey)["name"]
        for nm in names:
            if nm != original:
                libs[nm]["renamed_from_repository"] = True
    hist_t = {}
    t0 = time.perf_counter()
    fs = arduino_first_seen()
    hist_t["arduino"] = round(time.perf_counter() - t0, 2)
    write_json(DERIVED / "arduino.json", {"libraries": libs, "registry_log": fs})
    meta["arduino"] = {"names": len(libs), "registry_head": fs["head"],
                       "registry_initial_commit": fs["initial_commit"]}

    # MicroPython
    mpy = load_json(MPY_INDEX)
    t0 = time.perf_counter()
    mfs = micropython_first_seen(mpy)
    hist_t["micropython"] = round(time.perf_counter() - t0, 2)
    write_json(DERIVED / "micropython.json",
               {"packages": sorted(p["name"] for p in mpy["packages"]), "updated": mpy.get("updated"),
                "registry_log": mfs})
    meta["micropython"] = {"names": len(mpy["packages"]), "lib_head": mfs["head"]}

    # ROS 2
    ros_pkgs: dict[str, list] = {}
    for distro, f in ROS_FILES.items():
        d = yaml.safe_load(open(f, encoding="utf-8"))
        for repo_name, repo in (d.get("repositories") or {}).items():
            rel = (repo or {}).get("release") or {}
            pkgs = rel.get("packages") or ([repo_name] if rel else [])
            for pk in pkgs:
                ros_pkgs.setdefault(pk, []).append(distro)
            # source-only repositories still name a package-shaped entity
            if not rel and (repo or {}).get("source"):
                ros_pkgs.setdefault(repo_name, []).append(distro + ":source-only")
    rosdep: dict[str, str] = {}
    for f in ROSDEP_FILES:
        d = yaml.safe_load(open(f, encoding="utf-8")) or {}
        for key in d:
            rosdep.setdefault(key, f.name)
    t0 = time.perf_counter()
    rlog = ros_first_seen() if ROSDISTRO_REPO.exists() else None
    hist_t["ros2"] = round(time.perf_counter() - t0, 2)
    # wall-clock time to read each registry history once (lookups are then dictionary reads);
    # host-dependent, reported as a cost, not used by any decision
    meta["history_seconds"] = hist_t
    write_json(DERIVED / "ros2.json", {"packages": ros_pkgs, "rosdep": rosdep, "registry_log": rlog})
    meta["ros2"] = {"packages": len(ros_pkgs), "rosdep_keys": len(rosdep),
                    "registry_head": rlog["head"] if rlog else None}
    write_json(DERIVED / "static_meta.json", meta)
    return meta


# --------------------------------------------------------------------------
# cache for API registries


class Cache:
    """Thread-safe JSON cache keyed by ``registry|key``; persisted per registry."""

    def __init__(self, registry: str):
        self.path = CACHE / f"{registry}.json"
        self.lock = threading.Lock()
        self.data = load_json(self.path, {})
        self.dirty = 0

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self.lock:
            self.data[key] = value
            self.dirty += 1
            if self.dirty >= 20:
                self.flush()

    def flush(self) -> None:
        if self.dirty:
            write_json(self.path, self.data)
            self.dirty = 0


class ApiRegistry:
    name = "api"
    delay = 0.15

    def __init__(self, offline: bool = False):
        self.cache = Cache(self.name)
        self.offline = offline
        self.latencies: list[float] = []

    def _fetch(self, key: str, fn: Callable[[], dict]) -> dict:
        hit = self.cache.get(key)
        if hit is not None and hit.get("status") != "error":
            return hit
        if self.offline:
            return {"status": "error", "error": "offline and not cached"}
        t0 = time.monotonic()
        rec = fn()
        self.latencies.append(time.monotonic() - t0)
        rec["fetched_at"] = utcnow()
        self.cache.put(key, rec)
        time.sleep(self.delay)
        return rec


class PlatformIO(ApiRegistry):
    name = "platformio"
    base = "https://api.registry.platformio.org/v3"

    def lookup(self, name: str, owner: str | None = None) -> dict:
        """Exact-name lookup.  Without an owner the declaration is ambiguous
        (PlatformIO documents owner-less names as a source of conflicts), so
        the publication-time bounds span every same-name candidate."""
        key = f"{(owner or '').lower()}/{name.lower()}"

        def fn():
            q = f'name:"{name}"' + (f' owner:"{owner}"' if owner else "")
            raw, page, total = [], 1, None
            while True:
                r = http_get(f"{self.base}/search?query={quote(q)}&limit=50&page={page}")
                if r.status != 200:
                    return {"status": "error", "http": r.status, "error": r.error}
                d = r.json()
                total = d.get("total", 0)
                got = d.get("items", [])
                raw += got
                if not got or len(raw) >= total or page * 50 >= total or page >= 20:
                    break
                page += 1
            seen, items = set(), []
            for i in raw:
                k = (i.get("owner", {}).get("username", "").lower(), i.get("type"), i.get("name", "").lower())
                if k in seen:
                    continue
                seen.add(k)
                if i.get("name", "").lower() == name.lower() and \
                        (owner is None or i.get("owner", {}).get("username", "").lower() == owner.lower()):
                    items.append(i)
            if not items:
                return {"status": "not_found", "http": 200, "search_total": total}
            libs = [i for i in items if i.get("type") == "library"] or items
            times, missing = [], 0
            for it in libs:
                own = it["owner"]["username"]
                d = http_get(f"{self.base}/packages/{quote(own)}/{it.get('type', 'library')}/{quote(it['name'])}")
                t = parse_time(d.json().get("added_at")) if d.status == 200 else None
                if t:
                    times.append(t)
                else:
                    missing += 1
            best = libs[0]
            # an owner-less name may install any candidate: the bounds must cover all of them,
            # so one candidate without a date leaves both bounds undecided
            decided = bool(times) and not missing
            return {"status": "found", "canonical": f"{best['owner']['username']}/{best['name']}",
                    "tau_lo": iso(min(times)) if decided else None, "tau_hi": iso(max(times)) if decided else None,
                    "tau_source": "platformio:added_at", "n_owners": len({i['owner']['username'] for i in libs}),
                    "n_candidates": len(libs), "n_missing_dates": missing,
                    "owners": sorted({i['owner']['username'] for i in libs})[:10]}
        return self._fetch(key, fn)

    def free_search(self, text: str, limit: int = 10) -> list[str]:
        key = f"free:{text.lower()}"

        def fn():
            r = http_get(f"{self.base}/search?query={quote(text)}&limit={limit}")
            if r.status != 200:
                return {"status": "error", "http": r.status}
            return {"status": "found", "names": [f"{i['owner']['username']}/{i['name']}" for i in r.json().get("items", [])
                                                 if i.get("type") == "library"]}
        rec = self._fetch(key, fn)
        return rec.get("names", []) if rec.get("status") == "found" else []


class EspRegistry(ApiRegistry):
    name = "espidf"
    base = "https://components.espressif.com/api/components"

    def lookup(self, namespace: str, name: str) -> dict:
        key = f"{namespace.lower()}/{name.lower()}"

        def fn():
            r = http_get(f"{self.base}/{quote(namespace.lower())}/{quote(name.lower())}")
            if r.status == 404:
                return {"status": "not_found", "http": 404}
            if r.status != 200 and r.status and not esp_name_valid(namespace, name):
                # the registry holds no component under a name that the component manager refuses;
                # its API answers such a path with an error (a second '/': 405), not with 404
                return {"status": "not_found", "http": r.status, "reason": "invalid component name"}
            if r.status != 200:
                return {"status": "error", "http": r.status, "error": r.error}
            d = r.json()
            times = [parse_time(d.get("created_at"))]
            times += [parse_time(v.get("created_at")) for v in d.get("versions", [])]
            times = [t for t in times if t]
            return {"status": "found", "canonical": f"{d.get('namespace')}/{d.get('name')}",
                    "tau": iso(min(times)) if times else None, "tau_source": "espidf:created_at",
                    "n_versions": len(d.get("versions", []))}
        return self._fetch(key, fn)


def crates_path(name: str) -> str:
    n = name.lower()
    if len(n) == 1:
        return f"1/{n}"
    if len(n) == 2:
        return f"2/{n}"
    if len(n) == 3:
        return f"3/{n[0]}/{n}"
    return f"{n[:2]}/{n[2:4]}/{n}"


class Crates(ApiRegistry):
    name = "crates"
    base = "https://index.crates.io"
    delay = 0.05

    def lookup(self, name: str) -> dict:
        key = name.lower()

        def one(n):
            r = http_get(f"{self.base}/{crates_path(n)}", headers={"Accept": "*/*"})
            if r.status == 404:
                return None, 404
            if r.status != 200:
                return "error", r.status
            lines = [l for l in r.body.decode().splitlines() if l.strip()]
            import json as _j
            recs = [_j.loads(l) for l in lines]
            times = [parse_time(x.get("pubtime")) for x in recs if x.get("pubtime")]
            return {"canonical": recs[0]["name"] if recs else n,
                    "tau": iso(min(times)) if times else None,
                    "n_versions": len(recs), "n_yanked": sum(1 for x in recs if x.get("yanked"))}, 200

        def fn():
            got, code = one(name)
            if got == "error":
                return {"status": "error", "http": code}
            if got:
                return {"status": "found", "tau_source": "crates:min_pubtime", **got}
            # crates.io treats '-' and '_' as equivalent for name uniqueness
            alt = name.replace("-", "_") if "-" in name else name.replace("_", "-")
            if alt != name:
                got2, code2 = one(alt)
                if isinstance(got2, dict):
                    return {"status": "found_variant", "variant": alt,
                            "tau_source": "crates:min_pubtime", **got2}
            return {"status": "not_found", "http": 404}
        rec = self._fetch(key, fn)
        if rec.get("status") in ("found", "found_variant") and "n_yanked" not in rec and not self.offline:
            # cached before yanked versions were recorded: look the crate up again
            t0 = time.monotonic()
            rec = fn()
            self.latencies.append(time.monotonic() - t0)
            rec["fetched_at"] = utcnow()
            self.cache.put(key, rec)
        return rec


class PyPI(ApiRegistry):
    name = "pypi"
    base = "https://pypi.org/pypi"

    def lookup(self, name: str) -> dict:
        key = norm_pypi(name)

        def fn():
            r = http_get(f"{self.base}/{quote(key)}/json", timeout=60)
            if r.status == 404:
                return {"status": "not_found", "http": 404}
            if r.status != 200:
                return {"status": "error", "http": r.status, "error": r.error}
            d = r.json()
            times = []
            for files in (d.get("releases") or {}).values():
                for f in files:
                    t = parse_time(f.get("upload_time_iso_8601") or f.get("upload_time"))
                    if t:
                        times.append(t)
            return {"status": "found", "canonical": d["info"]["name"],
                    "tau": iso(min(times)) if times else None, "tau_source": "pypi:min_upload_time",
                    "n_releases": len(d.get("releases") or {})}
        return self._fetch(key, fn)


class Npm(ApiRegistry):
    name = "npm"
    base = "https://registry.npmjs.org"

    def lookup(self, name: str) -> dict:
        # exact name: npm rejects capital letters in package names, so a declaration with
        # capitals does not resolve even if its lower-case form exists
        key = name

        def fn():
            path = quote(key).replace("%40", "@") if key.startswith("@") else quote(key)
            r = http_get(f"{self.base}/{path}", timeout=60)
            if r.status == 404:
                return {"status": "not_found", "http": 404}
            if r.status != 200:
                return {"status": "error", "http": r.status, "error": r.error}
            d = r.json()
            created = (d.get("time") or {}).get("created")
            unpublished = "unpublished" in (d.get("time") or {})
            return {"status": "not_found" if unpublished else "found", "canonical": d.get("name"),
                    "tau": iso(parse_time(created)), "tau_source": "npm:time.created",
                    "unpublished": unpublished}
        return self._fetch(key, fn)


# --------------------------------------------------------------------------
# facade


class Registries:
    """Resolve declarations of every ecosystem against snapshots and caches."""

    def __init__(self, offline: bool = False):
        self.offline = offline
        a = load_json(DERIVED / "arduino.json")
        m = load_json(DERIVED / "micropython.json")
        r = load_json(DERIVED / "ros2.json")
        if a is None or m is None or r is None:
            raise FileNotFoundError("derived snapshots missing; run `make snapshots`")
        self.arduino = a["libraries"]
        self.arduino_lower: dict[str, list] = {}
        for nm in sorted(self.arduino):
            self.arduino_lower.setdefault(nm.lower(), []).append(nm)
        self.arduino_log = a["registry_log"]
        self.mpy = set(m["packages"])
        self.mpy_log = m["registry_log"]["first"]
        self.ros = r["packages"]
        self.rosdep = r["rosdep"]
        self.ros_log = r.get("registry_log") or {"packages": {}, "rosdep": {}}
        st = load_json(SNAP_RAW / "fetched_at.json", {}) or {}
        # when the static indices were read: a name absent from them was absent then
        self.static_seen = {"arduino": st.get("arduino_library_index.json"),
                            "micropython": st.get("micropython_index_v2.json"),
                            "ros2": max([v for k, v in st.items() if k.startswith(("ros_", "rosdep_"))] or [None],
                                        key=lambda x: x or "")}
        self.pio = PlatformIO(offline)
        self.esp = EspRegistry(offline)
        self.crates = Crates(offline)
        self.pypi = PyPI(offline)
        self.npm = Npm(offline)
        self.stdlib = set(getattr(sys, "stdlib_module_names", ()))

    def flush(self) -> None:
        for r in (self.pio, self.esp, self.crates, self.pypi, self.npm):
            r.cache.flush()

    # -- per ecosystem -----------------------------------------------------
    def resolve(self, eco: str, name: str, owner: str | None = None) -> Resolution:
        fn = getattr(self, f"_r_{eco}")
        return fn(name, owner)

    def _r_arduino(self, name, owner=None):
        e = self.arduino.get(name)
        status = "found"
        if not e:
            cands = self.arduino_lower.get(name.lower())
            if not cands:
                return Resolution("arduino", name, "not_found", registry="arduino-library-manager",
                                  fetched_at=self.static_seen.get("arduino"))
            e, status = self.arduino[cands[0]], "found_variant"
        repo = norm_url(e.get("repository") or "")
        fs = self.arduino_log["first"].get(repo)
        lo = hi = None
        if fs:
            hi = fs["date"]
            lo = None if fs["initial_import"] else fs["date"]
            if e.get("renamed_from_repository"):  # the name appeared at or after the repository
                lo, hi = (None if fs["initial_import"] else fs["date"]), None
        return Resolution("arduino", name, status, canonical=e["name"], tau_lo=iso(parse_time(lo)) if lo else None,
                          tau_hi=iso(parse_time(hi)) if hi else None,
                          tau_source="arduino:registry_log_first_added" if fs else None,
                          registry="arduino-library-manager",
                          detail={"repository": e.get("repository"), "case_variant": status == "found_variant",
                                  # arduino-cli adjusts the case when exactly one index name matches
                                  "case_candidates": len(self.arduino_lower.get(name.lower()) or [])})

    def _api(self, eco, name, rec, registry):
        st = rec.get("status", "error")
        lo = rec.get("tau_lo", rec.get("tau"))
        hi = rec.get("tau_hi", rec.get("tau"))
        return Resolution(eco, name, st, canonical=rec.get("canonical"), tau_lo=lo, tau_hi=hi,
                          tau_source=rec.get("tau_source") if (lo or hi) else None, registry=registry,
                          detail={k: v for k, v in rec.items()
                                  if k not in ("status", "canonical", "tau", "tau_lo", "tau_hi", "tau_source")},
                          fetched_at=rec.get("fetched_at"))

    # -- separator/case variants of non-resolving names -------------------
    @staticmethod
    def _squash(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def variant_of(self, eco: str, name: str, owner: str | None = None) -> str | None:
        """Existing name equal to ``name`` up to case and separators, if any."""
        sq = self._squash(name)
        if not sq:
            return None
        if eco == "arduino":
            if not hasattr(self, "_ard_sq"):
                self._ard_sq = {}
                for e in self.arduino.values():
                    self._ard_sq.setdefault(self._squash(e["name"]), e["name"])
            return self._ard_sq.get(sq)
        if eco == "ros2":
            if not hasattr(self, "_ros_sq"):
                self._ros_sq = {self._squash(n): n for n in list(self.ros) + list(self.rosdep)}
            return self._ros_sq.get(sq)
        if eco == "micropython":
            return {self._squash(n): n for n in self.mpy}.get(sq)
        if eco == "platformio":
            for cand in self.pio.free_search(re.sub(r"[_\-]+", " ", name)):
                if self._squash(cand.split("/", 1)[1]) == sq:
                    return cand
            return None
        if eco == "espidf":
            alt = name.replace("-", "_") if "-" in name else name.replace("_", "-")
            if alt != name and self.esp.lookup(owner or "espressif", alt).get("status") == "found":
                return f"{owner or 'espressif'}/{alt}"
            return None
        if eco == "npm":
            alt = name.replace("_", "-")
            if alt != name and self.npm.lookup(alt).get("status") == "found":
                return alt
            return None
        return None

    def _r_platformio(self, name, owner=None):
        return self._api("platformio", name, self.pio.lookup(name, owner), "platformio-registry")

    def _r_espidf(self, name, owner=None):
        ns = owner or "espressif"
        return self._api("espidf", name, self.esp.lookup(ns, name), "esp-component-registry")

    def _r_crates(self, name, owner=None):
        rec = self.crates.lookup(name)
        res = self._api("crates", name, rec, "crates.io")
        if res.status in ("found", "found_variant") and rec.get("n_versions") and rec.get("n_yanked") == rec["n_versions"]:
            # every version is yanked: without a lock file Cargo selects none ("no matching package
            # named ..."), so the name does not resolve for the installer
            res.status = "all_yanked"
            return res
        if res.status == "found" and res.canonical and res.canonical != name:
            # the index is keyed by the lower-case name, but Cargo matches the exact name
            # (Cargo 1.97: "no matching package named `loRaWAN` found ... similar names: lorawan")
            res.status = "found_variant"
        return res

    def _r_pypi(self, name, owner=None):
        res = self._api("pypi", name, self.pypi.lookup(name), "pypi")
        if res.status == "found" and res.tau_lo is None and res.tau_hi is None:
            # a project without any distribution file: pip finds nothing to install
            # ("No matching distribution found"), so the name does not resolve for the installer
            res.status = "no_files"
        elif res.status in ("found", "found_variant") and res.tau_hi:
            # PyPI reports upload times of the files that still exist.  The earliest of them bounds
            # the creation of the project record from above; it is no lower bound, because earlier
            # files (or releases) may have been deleted.
            res.tau_lo = None
            res.tau_source = "pypi:earliest_surviving_upload (upper bound)"
        return res

    def _r_npm(self, name, owner=None):
        return self._api("npm", name, self.npm.lookup(name), "npm")

    def _r_micropython(self, name, owner=None):
        if name in self.mpy:
            fs = self.mpy_log.get(name)
            return Resolution("micropython", name, "found", canonical=name,
                              tau_lo=None, tau_hi=iso(parse_time(fs["date"])) if fs else None,
                              tau_source="micropython-lib:first_maintainer_commit_of_manifest" if fs else None,
                              registry="micropython-lib")
        low = {p.lower(): p for p in self.mpy}
        if name.lower() in low:
            return Resolution("micropython", name, "found_variant", canonical=low[name.lower()],
                              registry="micropython-lib")
        return Resolution("micropython", name, "not_found", registry="micropython-lib",
                          fetched_at=self.static_seen.get("micropython"))

    def _r_ros2(self, name, owner=None):
        if name in self.ros:
            fs = self.ros_log["packages"].get(name)
            t = iso(parse_time(fs["date"])) if fs else None
            return Resolution("ros2", name, "found", canonical=name, registry="rosdistro",
                              tau_lo=t, tau_hi=t,
                              tau_source=f"rosdistro:{self.ros_log.get('distro', 'humble')}_log_first_added" if fs else None,
                              detail={"distros": self.ros[name], "first_commit": fs and fs["commit"]})
        if name in self.rosdep:
            fs = self.ros_log["rosdep"].get(name)
            t = iso(parse_time(fs["date"])) if fs else None
            return Resolution("ros2", name, "found", canonical=name, registry="rosdep", tau_lo=t, tau_hi=t,
                              tau_source="rosdep:log_first_added" if fs else None,
                              detail={"rosdep_file": self.rosdep[name], "first_commit": fs and fs["commit"]})
        return Resolution("ros2", name, "not_found", registry="rosdistro+rosdep",
                          fetched_at=self.static_seen.get("ros2"))


def fetch_static() -> dict:
    """Download the static indices (overwrites) and record retrieval time."""
    SNAP_RAW.mkdir(parents=True, exist_ok=True)
    stamp = load_json(SNAP_RAW / "fetched_at.json", {})
    for fname, url in STATIC_SOURCES.items():
        r = http_get(url, headers={"Accept": "*/*"}, timeout=600)
        if r.status != 200:
            raise RuntimeError(f"fetch {url}: {r.status} {r.error}")
        (SNAP_RAW / fname).write_bytes(r.body)
        stamp[fname] = utcnow()
    write_json(SNAP_RAW / "fetched_at.json", stamp)
    return stamp
