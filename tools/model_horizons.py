#!/usr/bin/env python3
"""Weight-anchored knowledge horizons (PROTOCOL.md, section 3.2).

A model cannot have been trained on data published after its weights existed, so the date at
which the exact weights we ran demonstrably existed is an upper bound on its training cut-off.
For every model of the run configurations this script records, from the Hugging Face hub:

* the GGUF file we ran: its LFS object id (= SHA-256 of the file, compared with the hash we
  recorded) and the commit that last changed it in the quantiser's repository (the date from
  which these exact bytes existed);
* the source weights (safetensors / bin shards of the vendor repository): their LFS ids and the
  commits that last changed them;
* the repository's createdAt (the horizon used before this revision, for comparison only:
  a repository can be created before any weights are uploaded).

Rule: the GGUF, documented as a conversion of the vendor repository, was made from weight
files that existed when it was committed.  If some source weight files were already in their
current state at the GGUF date, horizon = the latest commit date among them (the source weights
complete at that point); otherwise horizon = the GGUF date.  Both are dates at which the weights
demonstrably existed; the GGUF date is also byte-verified against the file we ran.

Every API answer is cached in data/derived/hf_cache/ with its retrieval time; the result is
data/derived/model_horizons.json.  Usage: python3 tools/model_horizons.py [--offline]
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "derived" / "hf_cache"
OUT = ROOT / "data" / "derived" / "model_horizons.json"
CONFIGS = [ROOT / "config" / "run_pilot_cpu.yaml", ROOT / "config" / "run_pilot_cpu_ext.yaml",
           ROOT / "config" / "run_pilot_cpu_famcmp.yaml", ROOT / "config" / "run_study_ollama.yaml"]
# the study's models get a weight-anchored horizon once their hub GGUF is recorded in the configuration
# (gguf_repo, gguf_file, sha256 = the Ollama blob digest of a model pulled as hf.co/<repo>:<quant>, and
# source_repo); until then they are listed as pending and classification refuses them
API = "https://huggingface.co/api/models/"
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pth")

# the pilot configuration predates the explicit gguf_repo / source_repo fields
KNOWN = {
    "qwen2.5-coder-1.5b-instruct": ("Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", "Qwen/Qwen2.5-Coder-1.5B-Instruct"),
    "deepseek-coder-1.3b-instruct": ("TheBloke/deepseek-coder-1.3b-instruct-GGUF",
                                     "deepseek-ai/deepseek-coder-1.3b-instruct"),
    "llama-3.2-1b-instruct": ("bartowski/Llama-3.2-1B-Instruct-GGUF", "meta-llama/Llama-3.2-1B-Instruct"),
}


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url: str, body: dict | None, offline: bool):
    key = hashlib.sha256((url + json.dumps(body, sort_keys=True) if body else url).encode()).hexdigest()[:24]
    path = CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["answer"]
    if offline:
        raise SystemExit(f"not cached: {url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                           "User-Agent": "phantomdeps-cps/1.0 (research artifact)"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                answer = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "body": body, "retrieved_at": utcnow(), "answer": answer},
                               indent=1), encoding="utf-8")
    return answer


def tree(repo: str, offline: bool) -> dict[str, dict]:
    """Top-level files with LFS ids and the commit that last changed each one (the expanded tree
    listing is public also for gated repositories, whose paths-info endpoint is not)."""
    return {f["path"]: f for f in fetch(f"{API}{repo}/tree/main?expand=true", None, offline)
            if f.get("type") == "file"}


def horizon_for(m: dict, offline: bool) -> dict:
    gguf_repo = m.get("gguf_repo") or KNOWN[m["id"]][0]
    source_repo = m.get("source_repo") or KNOWN[m["id"]][1]
    gguf_file = m.get("gguf_file") or Path(m["file"]).name
    info = fetch(f"{API}{source_repo}", None, offline)
    g = tree(gguf_repo, offline).get(gguf_file)
    if not g:
        raise SystemExit(f"{gguf_repo}: {gguf_file} not found")
    oid = (g.get("lfs") or {}).get("oid")
    rec = {"model_id": m["id"], "source_repo": source_repo, "source_revision": info.get("sha"),
           "source_created_at": info.get("createdAt"), "gguf_repo": gguf_repo, "gguf_file": gguf_file,
           "gguf_sha256_recorded": m.get("sha256"), "gguf_lfs_oid": oid,
           "gguf_oid_matches": oid == m.get("sha256"), "gguf_commit": g.get("lastCommit")}
    w = [f for p, f in tree(source_repo, offline).items() if p.endswith(WEIGHT_SUFFIXES)]
    if not w:              # some vendors keep the weights in a sub-directory only
        raise SystemExit(f"{source_repo}: no weight files at the top level")
    rec["source_weights"] = [{"path": x["path"], "lfs_oid": (x.get("lfs") or {}).get("oid"),
                              "size": x.get("size"), "commit": x.get("lastCommit")} for x in w]
    g_date = g["lastCommit"]["date"]
    rec["gguf_date"] = g_date
    if not rec["gguf_oid_matches"]:
        raise SystemExit(f"{m['id']}: the GGUF on the hub is not the file we ran")
    before = [x["lastCommit"]["date"] for x in w if x["lastCommit"]["date"] <= g_date]
    later = [x["path"] for x in w if x["lastCommit"]["date"] > g_date]
    rec["source_weights_date"] = max(before) if before else None
    rec["source_files_changed_after_gguf"] = later       # e.g. a safetensors copy added later
    if before:
        rec["horizon"], rec["horizon_rule"] = max(before), "source weights present when the GGUF was made"
    else:
        rec["horizon"], rec["horizon_rule"] = g_date, "GGUF bytes (no source weights in their current state then)"
    rec["horizon"] = rec["horizon"].replace(".000Z", "Z")
    rec["created_to_horizon_days"] = round(
        (dt.datetime.fromisoformat(rec["horizon"].replace("Z", "+00:00"))
         - dt.datetime.fromisoformat(info["createdAt"].replace("Z", "+00:00"))).total_seconds() / 86400, 2)
    return rec


def main(offline: bool = False) -> dict:
    out = {"generated_at": utcnow(), "rule": __doc__.split("Rule:")[1].split("\n\n")[0].strip(), "models": {}}
    for cfg_path in CONFIGS:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        for m in cfg["models"]:
            if m["id"] in out["models"]:          # the same model file in a later run configuration
                if m.get("sha256") != out["models"][m["id"]].get("gguf_sha256_recorded"):
                    raise SystemExit(f"{m['id']}: another model file in {cfg_path.name}")
                out["models"][m["id"]]["configs"].append(cfg_path.name)
                continue
            if not (m.get("sha256") and (m.get("gguf_repo") or m["id"] in KNOWN)):
                out.setdefault("pending", {})[m["id"]] = {"config": cfg_path.name,
                                                          "missing": "gguf_repo, gguf_file and sha256 of the hub GGUF"}
                continue
            out["models"][m["id"]] = horizon_for(m, offline) | {"configs": [cfg_path.name]}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    res = main("--offline" in sys.argv)
    for k, v in res["models"].items():
        print(f"{k:32s} created {v['source_created_at'][:10]}  weights {(v['source_weights_date'] or '-')[:10]}  "
              f"gguf {v['gguf_date'][:10]}  -> horizon {v['horizon'][:10]} (+{v['created_to_horizon_days']} d)  "
              f"oid match {v['gguf_oid_matches']}")
