#!/usr/bin/env python3
"""Fill the API-registry cache for a run's answers with parallel lookups, before
`phantomdeps classify`, which looks names up one at a time (a run of 2,496 answers with many
invented names took hours that way).

Each answer goes through the classifier's own code (`classify_generation`: extraction, lookups,
separator and case variants), so the cache receives exactly the entries the classifier would
fetch; only order and concurrency differ, and the classifier then answers from the cache.  The
rows are discarded.  Each warm-up is appended to data/runs/<run>/warm_meta.json; its timings are
measured with concurrent workers and do not enter the cost figures, which rest on the classifier's
sequential cold lookups (classify_meta.json).

  python3 tools/warm_registry_cache.py RUN_ID [--workers 6]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from phantomdeps.builtins import Builtins  # noqa: E402
from phantomdeps.classify import classify_generation, harness_hash  # noqa: E402
from phantomdeps.registries import Registries  # noqa: E402
from phantomdeps.util import RUNS, read_jsonl, utcnow, write_json  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    gens = [g for g in read_jsonl(RUNS / a.run_id / "generations.jsonl") if not g.get("error")]
    reg, bi = Registries(), Builtins()
    arduino_names = {k.lower() for k in reg.arduino}
    t0, done, failed = time.monotonic(), [0], []
    started = utcnow()

    def one(g):
        try:
            classify_generation(g, reg, bi, arduino_names)
        except Exception as e:  # noqa: BLE001 - a warm-up only fills the cache; classify retries
            failed.append(f"{g['model_id']} {g['prompt_id']}: {type(e).__name__}: {e}")
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"{done[0]}/{len(gens)} answers, {time.monotonic() - t0:.0f} s", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, gens))
    reg.flush()
    lat = {r.name: list(r.latencies) for r in (reg.pio, reg.esp, reg.crates, reg.pypi, reg.npm)}
    entry = {"started_at": started, "finished_at": utcnow(), "workers": a.workers,
             "n_generations": len(gens), "n_lookups": {k: len(v) for k, v in lat.items()},
             "lookup_latencies_s": lat, "failed_answers": failed, "harness_sha256": harness_hash()}
    path = RUNS / a.run_id / "warm_meta.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    runs = old.get("warmups") or ([{k: v for k, v in old.items() if k not in ("run_id", "note")}] if old else [])
    write_json(path, {"run_id": a.run_id, "note": "parallel cache warm-ups; timings measured with concurrent "
                      "workers, not used for the cost figures", "warmups": runs + [entry]})
    print({k: len(v) for k, v in lat.items()}, f"{time.monotonic() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
