"""Command line: python -m phantomdeps <command> ...

  snapshots [--no-fetch]      download static indices, derive name sets and built-ins
  corpus                      render prompts from corpus/tasks_v1.yaml
  generate CONFIG [--models]  run generations against an OpenAI-compatible server
  resample CONFIG [--models] [--plan FILE]
                              persistence pass for prompts whose greedy answer had a phantom
  resample-plan CONFIG [--out FILE]
                              those pairs of a classified run, for a host without the registries
  extend-plan CONFIG [--out FILE] / extend CONFIG --plan FILE
                              greedy answers cut by the token limit, generated again with a
                              larger budget into run <run_id>-ext (sensitivity pass)
  classify RUN_ID [--offline] extract, resolve and classify declarations
  analyze RUN_ID [...]        write results/results.json, macros, tables, figures
                              (--masked: from the pseudonymised files of the review artifact)
  mask RUN_ID [...]           write the pseudonymised copies of classified runs
  validation-sample RUN_ID... stratified sample over the runs + annotation sheets (PROTOCOL.md, 5)
  validation-score RUN_ID...  precision, recall and kappa from the two filled sheets
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="phantomdeps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshots")
    s.add_argument("--no-fetch", action="store_true")
    sub.add_parser("corpus")
    g = sub.add_parser("generate")
    g.add_argument("config")
    g.add_argument("--models", nargs="*")
    g.add_argument("--limit", type=int)
    g.add_argument("--filter")
    rsm = sub.add_parser("resample")
    rsm.add_argument("config")
    rsm.add_argument("--models", nargs="*")
    rsm.add_argument("--plan", help="pairs from resample-plan (a host without the registries)")
    rp = sub.add_parser("resample-plan")
    rp.add_argument("config")
    rp.add_argument("--out")
    rp.add_argument("--keep", help="a plan already in use: its pairs stay, added pairs are listed")
    ex = sub.add_parser("extend")
    ex.add_argument("config")
    ex.add_argument("--plan", required=True)
    ex.add_argument("--models", nargs="*")
    ep = sub.add_parser("extend-plan")
    ep.add_argument("config")
    ep.add_argument("--out")
    c = sub.add_parser("classify")
    c.add_argument("run_id")
    c.add_argument("--offline", action="store_true")
    a = sub.add_parser("analyze")
    a.add_argument("run_ids", nargs="+")
    a.add_argument("--profile", default=None)
    a.add_argument("--out", default=None)
    a.add_argument("--masked", action="store_true")
    mk = sub.add_parser("mask")
    mk.add_argument("run_ids", nargs="+")
    vs = sub.add_parser("validation-sample")
    vs.add_argument("run_ids", nargs="+")
    vs.add_argument("--per-eco", type=int, default=25)
    vs.add_argument("--families", nargs="*", help="default: the families every model answered")
    vs.add_argument("--seed", type=int, default=20240628)
    vs.add_argument("--exclude", nargs="*", help="folders of earlier samples whose responses are left out")
    vc = sub.add_parser("validation-score")
    vc.add_argument("run_ids", nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "snapshots":
        from . import builtins, registries
        if not args.no_fetch:
            print(json.dumps(registries.fetch_static(), indent=1))
        print(json.dumps(registries.build_static(), indent=1, default=str)[:4000])
        b = builtins.build()
        print(json.dumps(b["sources"], indent=1))
    elif args.cmd == "corpus":
        from . import corpus
        print(json.dumps(corpus.write(), indent=1))
    elif args.cmd == "generate":
        from . import llm
        print(llm.run(args.config, args.models, args.limit, args.filter))
    elif args.cmd == "resample":
        from . import llm
        print(llm.resample(args.config, args.models, args.plan))
    elif args.cmd == "resample-plan":
        from . import llm
        print(llm.write_plan(args.config, args.out, args.keep))
    elif args.cmd == "extend":
        from . import llm
        print(llm.extend(args.config, args.plan, args.models))
    elif args.cmd == "extend-plan":
        from . import llm
        print(llm.write_extend_plan(args.config, args.out))
    elif args.cmd == "classify":
        from . import classify
        print(json.dumps(classify.run(args.run_id, args.offline), indent=1)[:2000])
    elif args.cmd == "analyze":
        from . import analysis
        analysis.MASKED_INPUT = args.masked
        print(analysis.run(args.run_ids, args.profile, args.out))
    elif args.cmd == "mask":
        from . import analysis
        for rid in args.run_ids:
            print(rid, analysis.mask_run(rid))
    elif args.cmd == "validation-sample":
        from . import validation
        print(json.dumps(validation.sample(args.run_ids, args.per_eco, seed=args.seed, families=args.families,
                                           exclude=args.exclude), indent=1))
    elif args.cmd == "validation-score":
        from . import validation
        print(json.dumps(validation.score(args.run_ids), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
