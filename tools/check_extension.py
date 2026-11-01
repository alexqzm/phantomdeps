#!/usr/bin/env python3
"""Check the extension pass against the greedy pass it extends (PROTOCOL.md, change log 2026-10-26
and 2026-10-28) and write data/evidence/extension_check.json, which the analysis reads.

Every cut greedy answer of RUN was generated again with a larger budget into RUN-ext, same seed
and temperature 0.  Greedy decoding should repeat the cut answer as the prefix of the extended
one (answer text and, for reasoning models, the reasoning).  Reports per model: answers extended,
prefix kept, answers that now finish, and answers that now have a final answer; for answers that
do not keep the prefix, the share of the cut answer's characters before the two part.  A model
whose library chat template writes the current date into the prompt (`currentDate`) answers a
different prompt on another day, so it is reported apart.

  python3 tools/check_extension.py [RUN] [--root DIR] [--no-write]

The evidence file holds no answer text and no package name (it ships in the review artifact, where
the analysis of the pseudonymised files, which carry no text, reads it); with the texts at hand
the analysis recomputes every entry and stops if the file is stale.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def records(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def keeps_prefix(cut: dict, ext: dict) -> bool:
    """Same rule as the analysis (phantomdeps.analysis.keeps_prefix)."""
    return ((ext.get("text") or "").startswith(cut.get("text") or "")
            and (ext.get("reasoning_text") or "").startswith(cut.get("reasoning_text") or ""))


def shared_chars(cut: dict, ext: dict) -> tuple[int, int]:
    """Characters of the cut answer (reasoning, then text) before the extended one parts from it."""
    a = (cut.get("reasoning_text") or "") + (cut.get("text") or "")
    b = (ext.get("reasoning_text") or "") + (ext.get("text") or "")
    n, i = min(len(a), len(b)), 0
    while i < n and a[i] == b[i]:
        i += 1
    return i, len(a)


def dated_template(root: Path, model_id: str) -> bool:
    t = root / "config" / "ollama_library" / model_id / "template.txt"
    return t.exists() and "currentDate" in t.read_text(encoding="utf-8")


def main(run_id: str, root: Path = ROOT, write: bool = True) -> int:
    runs = root / "data" / "runs"
    greedy = {(r["model_id"], r["prompt_id"]): r for r in records(runs / run_id / "generations.jsonl")
              if r["sample_idx"] == 0 and not r.get("error")}
    ext = {(r["model_id"], r["prompt_id"]): r for r in records(runs / f"{run_id}-ext" / "generations.jsonl")
           if not r.get("error")}
    plan_path = runs / run_id / "extend_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stats = defaultdict(Counter)
    pairs, broken = [], []
    for mid, pid in plan["pairs"]:
        s = stats[mid]
        s["planned"] += 1
        e = ext.get((mid, pid))
        if not e:
            continue
        g = greedy[(mid, pid)]
        s["extended"] += 1
        ok = keeps_prefix(g, e)
        sh, ln = shared_chars(g, e)
        s["prefix_kept"] += ok
        if not ok:
            broken.append((mid, pid))
        fin = e.get("finish_reason") != "length"
        s["finished"] += fin
        final_now = bool((e.get("text") or "").strip()) and not (g.get("text") or "").strip()
        s["final_answer_now"] += final_now
        pairs.append({"model_id": mid, "prompt_id": pid, "prefix_kept": ok, "shared_chars": sh, "cut_chars": ln,
                      "finished": fin, "final_answer_now": final_now, "cut_finish": g.get("finish_reason"),
                      "max_tokens": e.get("max_tokens")})
    dated = {mid: dated_template(root, mid) for mid in stats}
    print(f"{'model':24s} {'planned':>7s} {'extended':>8s} {'prefix':>6s} {'finished':>8s} {'new_final':>9s}  dated")
    tot = Counter()
    for mid, s in stats.items():
        tot.update(s)
        print(f"{mid:24s} {s['planned']:7d} {s['extended']:8d} {s['prefix_kept']:6d} {s['finished']:8d} "
              f"{s['final_answer_now']:9d}  {'yes' if dated[mid] else ''}")
    print(f"{'all':24s} {tot['planned']:7d} {tot['extended']:8d} {tot['prefix_kept']:6d} {tot['finished']:8d} "
          f"{tot['final_answer_now']:9d}")
    undated_parted = [p["shared_chars"] / p["cut_chars"] for p in pairs
                      if not p["prefix_kept"] and not dated[p["model_id"]] and p["cut_chars"]]
    summary = {
        "planned": tot["planned"], "extended": tot["extended"], "prefix_kept": tot["prefix_kept"],
        "finished": tot["finished"], "final_answer_now": tot["final_answer_now"],
        "dated_template_models": sorted(m for m, d in dated.items() if d),
        "dated": {"extended": sum(stats[m]["extended"] for m in stats if dated[m]),
                  "prefix_kept": sum(stats[m]["prefix_kept"] for m in stats if dated[m])},
        "undated": {"extended": sum(stats[m]["extended"] for m in stats if not dated[m]),
                    "prefix_kept": sum(stats[m]["prefix_kept"] for m in stats if not dated[m]),
                    "parted": len(undated_parted),
                    "parted_share_median": statistics.median(undated_parted) if undated_parted else None,
                    "parted_share_quartiles": statistics.quantiles(undated_parted, n=4)
                    if len(undated_parted) >= 2 else None},
        "by_model": {m: dict(s, dated_template=dated[m]) for m, s in sorted(stats.items())},
    }
    if undated_parted:
        q = summary["undated"]["parted_share_quartiles"] or [None, None, None]
        print(f"undated templates: {summary['undated']['prefix_kept']} of {summary['undated']['extended']} keep the "
              f"prefix; the {len(undated_parted)} others part after a median "
              f"{100 * summary['undated']['parted_share_median']:.1f}% of the cut answer "
              f"(quartiles {100 * q[0]:.1f}%, {100 * q[2]:.1f}%)")
    if broken:
        print("prefix not kept:", broken[:20], "..." if len(broken) > 20 else "")
    if write:
        doc = {"generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
               "run_id": run_id, "extension_run_id": f"{run_id}-ext",
               "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
               "rule": "prefix kept: the extended answer's text starts with the cut answer's text and its "
                       "reasoning with the cut answer's reasoning; shared_chars counts the characters of the "
                       "cut answer (reasoning, then text) before the two part",
               "summary": summary, "pairs": pairs}
        out = root / "data" / "evidence" / "extension_check.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out.relative_to(root)}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", default="study-ollama-2027")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.run_id, Path(a.root), write=not a.no_write))
