#!/usr/bin/env python3
"""Import a results archive of the study kit (PhantomDeps_study_<phase>_<stamp>.zip, written by
tools/study_runner.py on the generating host).

  python3 tools/import_study_results.py ZIP [--logs-name NAME] [--root DIR]

The archive's config/run_study_ollama.yaml must be byte-identical to this repository's.

greedy (the archive has no plans/): data/runs/<run_id>/generations.jsonl and run_meta.json are
  copied in; a run that already exists must hold the same records (a later archive of a resumed
  pass may add records and invocations).
followup (the archive has plans/): its plans must be the ones written here (same SHA-256); the
  persistence records (sample_idx > 0, pairs of the resample plan) are appended to the study run's
  generations.jsonl, the extension records go to run <run_id>-ext, and the invocations of each
  archived run_meta.json are appended to the run's own.  resample_meta.json is copied.
In both, a record whose (model, prompt, sample) is already present must be identical, and the kit's
logs go to data/study_host_logs/<name>/: they hold the host's user and computer names, so they
never ship in the anonymous artifact (Makefile, check_anon.py).  Imported run metadata is
normalized (`normalize`): configuration paths relative to the kit folder (an earlier kit recorded
the absolute path, the host's user folder included) and Ollama's local timestamps in UTC.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = ("data/runs/", "plans/", "logs/", "config/")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def jsonl(b: bytes) -> list[dict]:
    return [json.loads(x) for x in b.decode("utf-8").splitlines() if x.strip()]


def jsonl_lines(b: bytes) -> list[tuple[bytes, dict]]:
    """(line as written by the generating host, record): imported lines are kept byte for byte,
    line ends included (CRLF from a Windows host)."""
    out = []
    for x in b.splitlines(keepends=True):
        if x.strip():
            out.append((x if x.endswith(b"\n") else x + b"\n", json.loads(x)))
    return out


def key(r: dict) -> tuple:
    return (r["model_id"], r["prompt_id"], int(r["sample_idx"]))


def merge_records(dst: Path, new: list[tuple[bytes, dict]], check=None) -> dict:
    """Append records not yet in dst; an answer already present must be identical.  A failed
    request is kept as a record of its own (the runner retries it and appends the answer; analysis
    reads answers only), so failed records never conflict with answers."""
    old = jsonl(dst.read_bytes()) if dst.exists() else []
    have = {key(r): r for r in old if not r.get("error")}
    failed = {json.dumps(r, sort_keys=True) for r in old if r.get("error")}
    added, same, n_err = [], 0, 0
    for line, r in new:
        if check:
            check(r)
        if r.get("error"):
            s = json.dumps(r, sort_keys=True)
            if s in failed:
                same += 1
            else:
                failed.add(s)
                added.append(line)
                n_err += 1
            continue
        k = key(r)
        if k in have:
            if have[k] != r:
                raise SystemExit(f"{dst}: record {k} differs from the one already imported")
            same += 1
            continue
        have[k] = r
        added.append(line)
    if added:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("ab") as f:
            for line in added:
                f.write(line)
    return {"added": len(added), "failed_requests_added": n_err, "already_present": same,
            "total": len(old) + len(added)}


LOCAL_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}$")


def host_free_path(v: str) -> str:
    """A path recorded by an earlier kit (absolute, with the host's user folder) relative to the
    kit folder, whose name the kit's zip fixes (``PhantomDeps-CPS_...``)."""
    parts = [x for x in re.split(r"[\\/]", v) if x]
    kit = [i for i, x in enumerate(parts) if x.startswith("PhantomDeps-CPS_")]
    if kit:
        return "/".join(parts[kit[-1] + 1:])
    return "/".join(parts[parts.index("config"):]) if "config" in parts else (parts[-1] if parts else v)


def normalize(obj):
    """Run metadata as the review artifact may carry it: configuration paths relative to the kit,
    timestamps with a local offset (Ollama's ``modified_at``) in UTC.  No measured value changes."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "_path" and isinstance(v, str) and (re.match(r"^[A-Za-z]:[\\/]", v) or v.startswith("/")):
                out[k] = host_free_path(v)
            elif isinstance(v, str) and LOCAL_TIME.match(v):
                out[k] = utc(v)
            else:
                out[k] = normalize(v)
        return out
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    return obj


def utc(s: str) -> str:
    t = dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", s))
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def merge_meta(dst: Path, new: dict) -> int:
    """Append the archive's invocations to the run's run_meta.json (by start time and pass)."""
    old = json.loads(dst.read_text(encoding="utf-8")) if dst.exists() else {}
    inv = list(old.get("invocations") or [])
    seen = {(i.get("started_at"), i.get("pass")) for i in inv}
    add = [i for i in new.get("invocations") or [] if (i.get("started_at"), i.get("pass")) not in seen]
    out = dict(new, **{k: v for k, v in old.items() if k != "invocations"})
    out["invocations"] = inv + add
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(normalize(out), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(add)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", nargs="?")
    ap.add_argument("--logs-name", help="folder under data/study_host_logs/ (default: from the archive name)")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--normalize", nargs="+", metavar="RUN_META",
                    help="normalize run_meta.json files imported before (paths, time zones) and exit")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.normalize:
        for f in a.normalize:
            meta = json.loads(Path(f).read_text(encoding="utf-8"))
            Path(f).write_text(json.dumps(normalize(meta), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print("normalized", f)
        return 0
    if not a.zip:
        ap.error("an archive is needed")
    z = zipfile.ZipFile(a.zip)
    names = [n for n in z.namelist() if not n.endswith("/")]
    for n in names:
        p = PurePosixPath(n)
        if p.is_absolute() or ".." in p.parts or not n.startswith(ALLOWED):
            raise SystemExit(f"unexpected member {n!r}")
    cfg_bytes = (root / "config" / "run_study_ollama.yaml").read_bytes()
    if sha(z.read("config/run_study_ollama.yaml")) != sha(cfg_bytes):
        raise SystemExit("the archive's run_study_ollama.yaml differs from this repository's")
    import yaml
    cfg = yaml.safe_load(cfg_bytes)
    rid = cfg["run_id"]
    plans = {PurePosixPath(n).name: z.read(n) for n in names if n.startswith("plans/")}
    phase = "followup" if plans else "greedy"
    report = {"archive": Path(a.zip).name, "sha256": sha(Path(a.zip).read_bytes()), "phase": phase}
    runs = root / "data" / "runs"

    if phase == "greedy":
        recs = jsonl_lines(z.read(f"data/runs/{rid}/generations.jsonl"))
        bad = [key(r) for _, r in recs if int(r["sample_idx"]) != 0]
        if bad:
            raise SystemExit(f"non-greedy records in a greedy archive: {bad[:3]}")
        report[rid] = merge_records(runs / rid / "generations.jsonl", recs)
        report[rid]["invocations_added"] = merge_meta(runs / rid / "run_meta.json",
                                                      json.loads(z.read(f"data/runs/{rid}/run_meta.json")))
    else:
        for name, b in plans.items():
            # the plan written here, or an earlier version of it kept as <name>_v<k>.json (a session
            # that started before the plan was superseded carries the earlier one)
            here = [runs / rid / name] + sorted((runs / rid).glob(Path(name).stem + "_v*.json"))
            if not any(c.exists() and sha(c.read_bytes()) == sha(b) for c in here):
                raise SystemExit(f"plans/{name} is none of the plans written here ({here[0]})")
        rplan = json.loads(plans["resample_plan.json"]) if "resample_plan.json" in plans else None
        eplan = json.loads(plans["extend_plan.json"]) if "extend_plan.json" in plans else None
        src = f"data/runs/{rid}/generations.jsonl"
        if rplan and src in names:
            pairs = {tuple(x) for x in rplan["pairs"]}
            n = int(rplan.get("n") or cfg["resample"]["n"])

            def check_resample(r, pairs=pairs, n=n):
                if (r["model_id"], r["prompt_id"]) not in pairs or not 1 <= int(r["sample_idx"]) <= n:
                    raise SystemExit(f"record {key(r)} is not in the resample plan")
            report[rid] = merge_records(runs / rid / "generations.jsonl", jsonl_lines(z.read(src)), check_resample)
            report[rid]["invocations_added"] = merge_meta(runs / rid / "run_meta.json",
                                                          json.loads(z.read(f"data/runs/{rid}/run_meta.json")))
            if f"data/runs/{rid}/resample_meta.json" in names:
                (runs / rid / "resample_meta.json").write_bytes(z.read(f"data/runs/{rid}/resample_meta.json"))
        ext = rid + "-ext"
        src = f"data/runs/{ext}/generations.jsonl"
        if eplan and src in names:
            pairs = {tuple(x) for x in eplan["pairs"]}

            def check_extend(r, pairs=pairs, eplan=eplan):
                if ((r["model_id"], r["prompt_id"]) not in pairs or int(r["sample_idx"]) != 0
                        or int(r["max_tokens"]) != int(eplan["max_tokens"][r["model_id"]])):
                    raise SystemExit(f"record {key(r)} is not in the extend plan")
            report[ext] = merge_records(runs / ext / "generations.jsonl", jsonl_lines(z.read(src)), check_extend)
            report[ext]["invocations_added"] = merge_meta(runs / ext / "run_meta.json",
                                                          json.loads(z.read(f"data/runs/{ext}/run_meta.json")))

    stem = Path(a.zip).stem.replace("PhantomDeps_study_", "")
    logs = root / "data" / "study_host_logs" / (a.logs_name or stem)
    n_logs = 0
    for n in names:
        if n.startswith("logs/"):
            dst = logs / PurePosixPath(n).name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(z.read(n))
            n_logs += 1
    report["host_logs"] = {"dir": str(logs.relative_to(root)), "files": n_logs}
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
