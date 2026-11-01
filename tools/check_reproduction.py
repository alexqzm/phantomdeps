#!/usr/bin/env python3
"""Re-run the analysis from the pseudonymised files and compare it with results/.

The review artifact ships the pseudonymised generations (without text), declarations and answers
(`phantomdeps mask`) instead of the raw files, which name phantoms that an outsider could still
register.  This script runs `analyze --masked` into a scratch directory and compares every macro
of results/macros.json and every table of results/ with the masked re-run.  Two kinds of value may
differ and are reported but not counted: timings of this host (the gate's microseconds per
decision) and nothing else.  Exit code 1 if anything else differs.

Usage: python3 tools/check_reproduction.py RUN_ID [RUN_ID ...]
"""
from __future__ import annotations

import filecmp
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMING = {"rGateUs"}


def main(run_ids: list[str]) -> int:
    ref = json.loads((ROOT / "results" / "macros.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="phantomdeps-masked-") as tmp:
        r = subprocess.run([sys.executable, "-m", "phantomdeps", "analyze", *run_ids, "--masked", "--out", tmp],
                           cwd=ROOT / "harness", capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-4000:], sep="\n")
            return 1
        got = json.loads((Path(tmp) / "macros.json").read_text(encoding="utf-8"))
        diff = sorted(k for k in set(ref) | set(got)
                      if (ref.get(k) or {}).get("value") != (got.get(k) or {}).get("value"))
        hard = [k for k in diff if k not in TIMING]
        tabs = sorted(p.name for p in (ROOT / "results").glob("tab_*.tex"))
        tdiff = [t for t in tabs if not (Path(tmp) / t).exists()
                 or not filecmp.cmp(ROOT / "results" / t, Path(tmp) / t, shallow=False)]
    print(f"macros: {len(ref)} in results/, {len(got)} from the pseudonymised files; "
          f"{len(diff)} differ ({', '.join(diff) or 'none'}), of which timings: {len(diff) - len(hard)}")
    print(f"tables: {len(tabs)} compared, {len(tdiff)} differ ({', '.join(tdiff) or 'none'})")
    for k in hard[:20]:
        print(f"  {k}: {(ref.get(k) or {}).get('value')!r} vs {(got.get(k) or {}).get('value')!r}")
    return 1 if hard or tdiff else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
