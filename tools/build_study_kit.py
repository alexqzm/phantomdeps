#!/usr/bin/env python3
"""Assemble the Windows study kit: everything the author's GPU machine needs to generate the study
answers, and nothing else (no registry snapshots, no pilot data, no pseudonym key).

  run_study.cmd / run_resample.cmd / run_study.ps1 / README_RU.txt   (tools/study_kit/)
  tools/study_runner.py, harness/phantomdeps/*.py, config/run_study_ollama.yaml,
  config/ollama_library/, corpus/, vendor/yaml (PyYAML, pure Python, MIT), LICENSE, KIT.json

Second-stage kit (persistence pass and extension of cut answers), with the plans written where
the greedy pass was classified:  python3 tools/build_study_kit.py OUT_DIR --followup RUN_DIR
It carries run_resample.cmd but not run_study.cmd (a greedy pass started in the new folder by
mistake would repeat the whole first stage) and README_followup_RU.txt as README_RU.txt.

Usage: python3 tools/build_study_kit.py OUT_DIR [--followup RUN_DIR]
       (writes OUT_DIR/<name>/ and OUT_DIR/<name>.zip)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CRLF = {".cmd", ".ps1", ".txt"}
BOM = {".ps1", ".txt"}            # Windows PowerShell 5.1 reads a BOM-less script as ANSI


def put(src: Path, dst: Path, windows_text: bool = False) -> None:
    """Copy byte for byte; only the kit's own launch files and README become CRLF (and BOM)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if windows_text and src.suffix in CRLF:
        text = src.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
        data = text.encode("utf-8")
        if src.suffix in BOM:
            data = b"\xef\xbb\xbf" + data
        if src.suffix == ".cmd":
            text.encode("ascii")                       # cmd.exe files stay ASCII
        dst.write_bytes(data)
    else:
        shutil.copyfile(src, dst)


PLANS = ("resample_plan.json", "extend_plan.json")
STAGE1 = ("run_study.cmd", "run_resample.cmd", "run_study.ps1", "README_RU.txt")
STAGE2 = {"run_resample.cmd": "run_resample.cmd", "run_study.ps1": "run_study.ps1",
          "README_followup_RU.txt": "README_RU.txt"}


def _hours(lo: float, hi: float) -> str:
    """A range of hours as the README writes it: whole hours from 10 h up, else one decimal."""
    a, b = lo / 3600, hi / 3600
    if b >= 10:
        return f"{a:.0f}\u2013{b:.0f}"
    return f"{a:.1f}\u2013{b:.1f}".replace(".", ",")


def estimate(run_dir: Path) -> dict:
    """README figures for the second stage from its plans and the greedy pass they came from:
    persistence, n answers per pair at the mean greedy latency of that model on answers that
    ended (low) or on all its answers (high); extension, the cut answer's own latency (low) or the
    new budget at the model's speed on its cut answers (high: a repetition loop runs to the end)."""
    gens = [json.loads(x) for x in (run_dir / "generations.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    greedy = [g for g in gens if g.get("sample_idx") == 0 and not g.get("error")]
    by = {}
    for g in greedy:
        by.setdefault(g["model_id"], []).append(g)
    rp = json.loads((run_dir / PLANS[0]).read_text(encoding="utf-8"))
    ep = json.loads((run_dir / PLANS[1]).read_text(encoding="utf-8"))
    cfg = yaml.safe_load((ROOT / "config" / "run_study_ollama.yaml").read_text(encoding="utf-8"))
    n = int(rp.get("n") or cfg["resample"]["n"])
    last = cfg["models"][-1]["id"]               # the runner's last persistence pass (study_runner.py)
    r_lo = r_hi = e_lo = e_hi = l_lo = l_hi = 0.0
    for mid, _ in rp["pairs"]:
        rs = by[mid]
        ended = [g["latency_s"] for g in rs if g.get("finish_reason") != "length"] or [g["latency_s"] for g in rs]
        lo, hi = n * sum(ended) / len(ended), n * sum(g["latency_s"] for g in rs) / len(rs)
        r_lo, r_hi = r_lo + lo, r_hi + hi
        if mid == last:
            l_lo, l_hi = l_lo + lo, l_hi + hi
    key = {(g["model_id"], g["prompt_id"]): g for g in greedy}
    for mid, pid in ep["pairs"]:
        cut = [g for g in by[mid] if g.get("finish_reason") == "length"]
        speed = sum((g.get("usage") or {}).get("completion_tokens") or 0 for g in cut) / sum(g["latency_s"] for g in cut)
        e_lo += key[(mid, pid)]["latency_s"]
        e_hi += int(ep["max_tokens"][mid]) / speed
    return {"PAIRS": str(len(rp["pairs"])), "N": str(n), "ANSWERS": str(n * len(rp["pairs"])),
            "CUT": str(len(ep["pairs"])), "HOURS_R": _hours(r_lo, r_hi),
            "HOURS_E": _hours(e_lo, e_hi), "HOURS_LAST": _hours(l_lo, l_hi),
            "LAST": last, "_seconds": [r_lo, r_hi, e_lo, e_hi, l_lo, l_hi]}


def main(out_dir: str, followup: str | None = None) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    name = f"PhantomDeps-CPS_study_{'followup' if followup else 'kit'}_win_{stamp}"
    kit = Path(out_dir) / name
    if kit.exists():
        shutil.rmtree(kit)
    src = ROOT / "tools" / "study_kit"
    launch = STAGE2 if followup else {f: f for f in STAGE1}
    for f, dst in launch.items():
        put(src / f, kit / dst, windows_text=True)
    if followup:
        # the runner warns when the second session runs on another Ollama version than the first
        meta = json.loads((Path(followup) / "run_meta.json").read_text(encoding="utf-8"))
        vers = sorted({s.get("ollama_version") for inv in meta.get("invocations") or []
                       if inv.get("pass", "generate") == "generate"
                       for s in (inv.get("served") or {}).values()} - {None})
        if len(vers) != 1:
            raise SystemExit(f"the greedy pass ran on Ollama versions {vers}")
        gen = Path(followup) / "generations.jsonl"
        (kit / "first_session.json").write_text(json.dumps(
            {"run_id": meta.get("run_id"), "ollama_version": vers[0],
             "greedy_sha256": hashlib.sha256(gen.read_bytes()).hexdigest()}, indent=1) + "\n", encoding="utf-8")
        est = dict(estimate(Path(followup)), OLLAMA=vers[0])
        readme = kit / "README_RU.txt"
        text = readme.read_bytes().decode("utf-8")
        for k, v in est.items():
            if not k.startswith("_"):
                text = text.replace("{" + k + "}", v)
        assert "{" not in text, "unfilled README field"
        readme.write_bytes(text.encode("utf-8"))
        print("estimate (h):", [round(x / 3600, 1) for x in est["_seconds"]])
        for f in PLANS:
            plan = json.loads((Path(followup) / f).read_text(encoding="utf-8"))
            if plan.get("run_id") != yaml.safe_load((ROOT / "config" / "run_study_ollama.yaml").read_text(
                    encoding="utf-8"))["run_id"]:
                raise SystemExit(f"{f} is for run {plan.get('run_id')}")
            put(Path(followup) / f, kit / f)
    put(ROOT / "tools" / "study_runner.py", kit / "tools" / "study_runner.py")
    for f in sorted((ROOT / "harness" / "phantomdeps").glob("*.py")):
        put(f, kit / "harness" / "phantomdeps" / f.name)
    put(ROOT / "config" / "run_study_ollama.yaml", kit / "config" / "run_study_ollama.yaml")
    for f in sorted((ROOT / "config" / "ollama_library").rglob("*")):
        if f.is_file():
            put(f, kit / "config" / "ollama_library" / f.relative_to(ROOT / "config" / "ollama_library"))
    for f in ("tasks_v1.yaml", "prompts_v1.0.jsonl", "prompts_v1.0.meta.json"):
        put(ROOT / "corpus" / f, kit / "corpus" / f)
    ydir = Path(yaml.__file__).parent
    for f in sorted(ydir.glob("*.py")):
        put(f, kit / "vendor" / "yaml" / f.name)
    lic = next(Path(ydir.parent).glob("[Pp]y[Yy][Aa][Mm][Ll]-*.dist-info/licenses/LICENSE"), None) or \
        next(Path(ydir.parent).glob("[Pp]y[Yy][Aa][Mm][Ll]-*.dist-info/LICENSE"))
    put(lic, kit / "vendor" / "PyYAML-LICENSE")
    put(ROOT / "LICENSE", kit / "LICENSE")
    (kit / "data" / "runs").mkdir(parents=True, exist_ok=True)
    files = {str(p.relative_to(kit)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(kit.rglob("*")) if p.is_file()}
    (kit / "KIT.json").write_text(json.dumps({"built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                              "pyyaml": yaml.__version__, "files": files}, indent=1) + "\n",
                                  encoding="utf-8")
    zpath = Path(out_dir) / f"{name}.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(kit.rglob("*")):
            if p.is_file():
                zi = zipfile.ZipInfo(f"{name}/{p.relative_to(kit).as_posix()}", date_time=(2026, 10, 25, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                z.writestr(zi, p.read_bytes())
    print(zpath, f"{zpath.stat().st_size / 1e6:.1f} MB, {len(files)} files")
    return zpath


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", nargs="?", default=str(ROOT / "dist"))
    ap.add_argument("--followup", metavar="RUN_DIR", help="folder with resample_plan.json and extend_plan.json")
    a = ap.parse_args()
    main(a.out_dir, a.followup)
