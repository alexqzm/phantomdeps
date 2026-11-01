#!/usr/bin/env python3
"""Evidence for the audit of post-horizon matches (PROTOCOL.md, section 7.3).

A post-horizon match is a declaration whose name resolves to a registry record first created after
the producing model's horizon.  That fact concerns the *registry record*; it does not show that the
model invented the name, because the project may have existed elsewhere (a source repository,
documentation, another registry or another name in the same registry) before the record appeared.
For every post-horizon match, and for every declaration whose only date is an upper bound after
the horizon (PyPI, micropython-lib), this script collects three dates without judging them:

  project   earliest commit of the repository the registry record points to (git, so the author
            controls it: evidence, not proof), and the earliest tag;
  record    when the registry record under the declared name appeared (from the classification);
  artifact  the earliest version listed for the record (registry data or deps.dev).

It also lists other records of the same registry that point to the same repository or have the
same name up to case and separators (an older record of the same project under another name).
Every HTTP answer is cached in data/cache/audit/ with its retrieval time; repositories are cloned
bare and without blobs into data/snapshots/audit/.  Output: data/evidence/posthorizon_audit.json (read by the analysis, not by classification).

Usage: python3 tools/audit_posthorizon.py [results/results.json]
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache" / "audit"
CLONES = ROOT / "data" / "snapshots" / "audit"
OUT = ROOT / "data" / "evidence" / "posthorizon_audit.json"
UA = "phantomdeps-cps/1.0 (research artifact; read-only lookups)"


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_json(url: str, headers: dict | None = None):
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["answer"]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ans = json.load(r)
    except urllib.error.HTTPError as e:
        ans = {"_http": e.code}
    except Exception as e:  # noqa: BLE001
        ans = {"_error": f"{type(e).__name__}: {e}"}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "retrieved_at": utcnow(), "answer": ans}, indent=1), encoding="utf-8")
    time.sleep(0.3)
    return ans


def norm_repo(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    u = re.sub(r"^git\+", "", u)
    u = re.sub(r"^git://", "https://", u)
    u = re.sub(r"^git@github\.com:", "https://github.com/", u)
    u = re.sub(r"\.git$", "", u.rstrip("/"))
    m = re.match(r"^https?://(github\.com|gitlab\.com|bitbucket\.org)/([^/]+)/([^/#?]+)", u)
    return f"https://{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else None


def repo_dates(url: str) -> dict:
    """Earliest commit (author and committer dates) and earliest tag of a public repository."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", url.split("://", 1)[1])
    d = CLONES / name
    if not d.exists():
        CLONES.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", url, str(d)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return {"url": url, "error": r.stderr.strip()[-300:]}
    log = subprocess.run(["git", "-C", str(d), "log", "--all", "--reverse", "--format=%H|%aI|%cI|%s"],
                         capture_output=True, text=True).stdout.splitlines()
    first = log[0].split("|", 3) if log else None
    tags = subprocess.run(["git", "-C", str(d), "for-each-ref", "--sort=creatordate", "--format=%(refname:short)|%(creatordate:iso-strict)",
                           "refs/tags"], capture_output=True, text=True).stdout.splitlines()
    return {"url": url, "n_commits": len(log),
            "first_commit": {"sha": first[0], "author_date": first[1], "committer_date": first[2]}
            if first else None,
            "first_tag": dict(zip(("tag", "date"), tags[0].split("|", 1))) if tags else None,
            "earliest_author_date": min((l.split("|")[1] for l in log), default=None)}


def deps_dev(system: str, name: str) -> dict:
    from urllib.parse import quote
    a = get_json(f"https://api.deps.dev/v3/systems/{system}/packages/{quote(name, safe='')}")
    vs = a.get("versions") or []
    pub = sorted(v.get("publishedAt") for v in vs if v.get("publishedAt"))
    return {"n_versions": len(vs), "earliest_published": pub[0] if pub else None, "error": a.get("_http") or a.get("_error")}


def registry_facts(eco: str, name: str, canonical: str | None) -> dict:
    nm = canonical or name
    if eco == "npm":
        a = get_json(f"https://registry.npmjs.org/{nm.replace('/', '%2F')}")
        t = a.get("time") or {}
        return {"record_created": t.get("created"), "versions": len(a.get("versions") or {}),
                "repository": norm_repo((a.get("repository") or {}).get("url") if isinstance(a.get("repository"), dict)
                                       else a.get("repository")),
                "description": (a.get("description") or "")[:120]}
    if eco == "pypi":
        a = get_json(f"https://pypi.org/pypi/{nm}/json")
        info = a.get("info") or {}
        urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
        repo = next((norm_repo(u) for u in urls if norm_repo(u)), None)
        ups = sorted(f["upload_time_iso_8601"] for files in (a.get("releases") or {}).values() for f in files)
        return {"earliest_surviving_upload": ups[0] if ups else None, "releases": len(a.get("releases") or {}),
                "repository": repo, "summary": (info.get("summary") or "")[:120], "deps_dev": deps_dev("pypi", nm)}
    if eco == "crates":
        return {"deps_dev": deps_dev("cargo", nm)}
    if eco == "arduino":
        idx = json.loads((ROOT / "data" / "snapshots" / "raw" / "arduino_library_index.json").read_text(encoding="utf-8"))
        rels = [r for r in idx["libraries"] if r["name"] == nm]     # the Library Manager resolves exact names
        repo = norm_repo(rels[0].get("repository")) if rels else None
        squash = re.sub(r"[^a-z0-9]", "", nm.lower())
        similar = sorted({r["name"] for r in idx["libraries"] if re.sub(r"[^a-z0-9]", "", r["name"].lower()) == squash
                          and r["name"] != nm})
        reg = json.loads((ROOT / "data" / "derived" / "arduino.json").read_text(encoding="utf-8"))["registry_log"]["first"]
        sim = {}
        for s in similar:
            rr = [r for r in idx["libraries"] if r["name"] == s]
            u = re.sub(r"^https?://", "", (rr[0].get("repository") or "").lower()).rstrip("/").removesuffix(".git")
            sim[s] = {"repository": rr[0].get("repository"), "registry_first": (reg.get(u) or {}).get("date"),
                      "initial_import": (reg.get(u) or {}).get("initial_import"), "versions": len(rr)}
        return {"repository": repo, "versions": len(rels), "same_name_up_to_separators": sim}
    if eco == "ros2":
        import yaml
        raw = ROOT / "data" / "snapshots" / "raw"
        dist = yaml.safe_load((raw / "ros_humble_distribution.yaml").read_text(encoding="utf-8"))
        for rname, r in (dist.get("repositories") or {}).items():
            if nm in ((r.get("release") or {}).get("packages") or [rname]):
                url = (r.get("source") or {}).get("url") or (r.get("doc") or {}).get("url")
                return {"kind": "distribution package", "distribution_repository": rname, "repository": norm_repo(url)}
        for f in ("rosdep_base.yaml", "rosdep_python.yaml", "rosdep_ruby.yaml"):
            db = yaml.safe_load((raw / f).read_text(encoding="utf-8"))
            if nm in db:
                os_pkgs = sorted({x for v in db[nm].values() for x in (v if isinstance(v, list) else
                                  [y for w in (v or {}).values() if isinstance(w, list) for y in w])})
                return {"kind": "rosdep key", "rosdep_file": f, "repository": None, "maps_to": os_pkgs[:6]}
        return {}
    return {}


def main(results_path: Path) -> dict:
    R = json.loads(results_path.read_text(encoding="utf-8"))
    hz_items = {}
    # one item per (ecosystem, name); among the models that wrote it, the one with the earliest horizon
    # (a match is post-horizon for it first); a post-horizon match takes precedence over an upper bound
    for kind, rows in (("post-horizon match", R.get("post_horizon", [])),
                       ("upper bound after horizon", R.get("upper_only_after_horizon", []))):
        for p in sorted(rows, key=lambda p: (p.get("model_horizon") or "", p.get("model_id") or "")):
            hz_items.setdefault((p["eco"], p["name"]), {"kind": kind, **p})
    out = {"generated_at": utcnow(), "run_ids": R.get("run_ids"), "items": []}
    for (eco, name), p in sorted(hz_items.items()):
        rec = {"eco": eco, "name": name, "kind": p["kind"], "canonical": p.get("canonical"),
               "model_id": p.get("model_id"), "horizon": p.get("model_horizon"),
               "record": {"tau_lo": p.get("tau_lo"), "tau_hi": p.get("tau_hi"), "source": p.get("tau_source")},
               "registry": registry_facts(eco, name, p.get("canonical"))}
        repo = rec["registry"].get("repository")
        if eco == "crates" and not repo:
            dd = get_json(f"https://api.deps.dev/v3/systems/cargo/packages/{name}")
            for v in (dd.get("versions") or [])[:1]:
                vk = v.get("versionKey", {}).get("version")
                vd = get_json(f"https://api.deps.dev/v3/systems/cargo/packages/{name}/versions/{vk}")
                for ln in vd.get("links") or []:
                    if ln.get("label") in ("SOURCE_REPO", "HOMEPAGE") and norm_repo(ln.get("url")):
                        repo = norm_repo(ln["url"])
                        break
            rec["registry"]["repository"] = repo
        if repo:
            rec["project"] = repo_dates(repo)
        out["items"].append(rec)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    res = main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "results" / "results.json")
    for it in res["items"]:
        pr = it.get("project") or {}
        fc = (pr.get("first_commit") or {})
        print(f"{it['eco']:8s} {it['name'][:32]:32s} h={str(it['horizon'])[:10]} record={str(it['record']['tau_hi'])[:10]} "
              f"repo={it['registry'].get('repository')} first_commit={str(fc.get('author_date'))[:10]} "
              f"err={pr.get('error', '')[:60]}")
