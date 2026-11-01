"""Manual validation of extraction (PROTOCOL.md, section 5).

``sample`` draws a stratified random sample of greedy responses of the primary families, those
every model of the given runs answered, as in the analysis (F2 in the pilot, F1 and F2 in the
study), with equal numbers per task ecosystem, pooled over the runs, and writes, per annotator,
a sheet to fill in:

  data/validation/<name>/sample.jsonl           responses with the extracted declarations
  data/validation/<name>/labels_<A>.xlsx        one row per response (response text included)
  data/validation/<name>/labels_<A>.csv         the same rows without the text (fallback)
  data/validation/<name>/GUIDE_en.md            instructions for the annotators (GUIDE_ru.md,
                                                the Russian text the pilot's annotators use, when
                                                tools/validation_guide_ru.md is present)

``<name>`` is the run ids joined with ``+``.  Annotators read the response text and record, for
each response,

  missed     declarations a build would install that the extractor did not report
             (``eco:name`` separated by ``;`` or new lines)
  spurious   reported declarations that a build would not install (same format)
  checked    ``y`` once the response has been read (unchecked rows are ignored)

The two annotators work independently: neither sees the other's sheet.  ``score`` reads the two
filled sheets (the .xlsx if present, else the .csv) and returns extraction precision and recall
(declaration level, Wilson intervals over the pooled counts of annotator A, the reference
annotator), the same per ecosystem, and Cohen's kappa between A and B on the binary per-response
label "extraction exact" (nothing missed, nothing spurious).
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import re
from pathlib import Path

from .stats import cluster_bootstrap, wilson
from .util import DATA, RUNS, read_jsonl, write_json

ECOS = ["arduino", "platformio", "espidf", "ros2", "crates", "micropython", "pypi", "npm"]
GUIDE_EN = """# Annotating dependency extraction (manual validation, PROTOCOL.md, section 5)

Goal: check how exactly the program extracts from a model answer **the dependencies a build
would install**. Only extraction is judged, not whether a package exists.

Two annotators (A, the reference annotator, and B) work **independently**: they do not discuss
rows or look at the other sheet before both are finished. Each fills in their own file,
`labels_A.xlsx` or `labels_B.xlsx` (sheet `labels`). Both are people: a sheet filled in by a
program or an AI model is only a pre-check and does not count as validation.

For each row (one model answer):

1. Read the task (`task`) and the whole answer (`response`).
2. Compare with the column `extracted`: what the program found, as `ecosystem:name`.
3. In `missed`, write the dependencies a build would install that the program did not find
   (format `ecosystem:name`; several separated by `;` or new lines).
4. In `spurious`, write the items the program found that a build would **not** install (for
   example a name from explanatory text, a file header, an example "do not use X"). Copy them
   from `extracted` unchanged.
5. Put `y` in `checked` once the row has been read. Rows without `y` are not counted.
6. `note` is optional (doubtful cases).

A dependency is anything the project's build or one of the install commands below would try to
fetch.

* Manifest entries: `platformio.ini` (`lib_deps`), `idf_component.yml` (`dependencies`),
  `package.xml` (`depend`, `exec_depend`, `build_depend`, `buildtool_depend` and so on),
  `Cargo.toml` (`[dependencies]` and others), `package.json` (`dependencies`,
  `devDependencies`), `requirements.txt`, `library.properties` (`depends`), including
  dependencies by URL or path (write them as they appear in the answer).
* An `idf_component.yml` written another way (dependencies as a list, or under another key, even
  inside an entry of a `components:` list) still declares the entries of its `dependencies`: a
  list item that is a name, or whose `name`, `component` or `idf_component` field gives the name;
  an item that also gives a repository (`git`, `uri`, `url`, `repo`, `repository`) or a path is a
  dependency by URL or path.
* A manifest that its tool cannot read as a whole (cut by the token limit, or broken by one bad
  line) still declares the entries that are well-formed on their own; a line that is no
  well-formed entry declares nothing, such as `stm32g4xx-hal::gpio = "0.14"` in `Cargo.toml`
  (TOML allows `::` in a key only in quotes).
* Install commands, but only in code: in a code block, in inline code or on a line that
  consists of the command alone. They are `pip install X` (also `pip3`, `python -m pip`,
  `sudo pip`), `npm install X` and its aliases (`npm i`, `npm add`), `yarn add X`,
  `pnpm add X` or `pnpm install X`, `cargo add X`, `arduino-cli lib install "X"` (with
  `--git-url` or `--zip-path`, a URL or path), `pio pkg install -l X` and `pio lib install X`
  (also `platformio` for `pio`), `idf.py add-dependency X`, `mip.install("X")`,
  `mpremote mip install X`, `micropython -m mip install X`, `upip.install("X")` or
  `pip.install("X")` on the board, `micropython -m upip install X`, and
  `sudo apt install ros-<distribution>-X` (also `apt-get`). The arguments of a command end at
  the first word of ordinary text. Unquoted words are separate arguments:
  `arduino-cli lib install Adafruit BusIO` declares `arduino:Adafruit` and `arduino:BusIO`.
  A path or URL argument is a dependency by path or URL: `pip install .` and
  `pip install -e .` declare `pypi:.`.
* A `requirements.txt` counts also when its block carries another label (`markdown`, `text`) or
  when it is written without a block right after a line naming the file; a project's file tree
  is no dependency list.

The ecosystem prefix follows where the installer fetches from: `upip.install("X")` and
`micropython -m upip install X` take packages from PyPI (`pypi:X`); `mip.install("X")`,
`mpremote mip install X` and `micropython -m mip install X` from micropython-lib
(`micropython:X`).

Write a name without its version, and do not judge the version: `ArduinoJson@6.21.0` declares
`arduino:ArduinoJson`, `knolleary/PubSubClient @ ^2.8` declares
`platformio:knolleary/PubSubClient`, and in an `idf_component.yml` that lists its dependencies,
`- espressif/esp-modbus:^1.0.0` declares `espidf:espressif/esp-modbus`. A name ends where the
installer ends it: `arduino-cli` separates a version only at `@`, so
`arduino-cli lib install "SD#2.2.2"` declares `arduino:SD#2.2.2` (a name it will not find).

Not dependencies: names in ordinary text (even "install the library X" without a command),
`#include` lines, the version constraint on ESP-IDF itself (`idf`). If the program included
such an item in `extracted`, write it in `spurious`; if it did not, write nothing. Commands other
than the ones listed above do not count either: `cargo install X` puts a tool on the developer's
machine and declares no dependency of the project (`cargo add X` does), and `mip install X`
without `mpremote`, `idf.py component add X` or `ampy run mip.py install X` are no commands of
these tools.

Commented-out entries are not dependencies either, because the build does not read them: lines
with `#` or `;` in `platformio.ini` (inside `lib_deps` too), `#` in `requirements.txt`,
`Cargo.toml` and `idf_component.yml`, `<!-- ... -->` in `package.xml`, and dependency tags
inside `<export>` in `package.xml`. A name longer than 214 characters (looping answers produce
them) does not count: no registry accepts a name that long.

If the answer was cut by the token limit (`cut_by_limit` = yes), its last entry may be cut off;
judge it as written.

Whether a package exists is not judged: phantoms and built-in libraries (for example `Wire`)
count as dependencies too if the answer declares them.

Ecosystem prefixes: `arduino`, `platformio`, `espidf`, `ros2`, `crates`, `micropython`, `pypi`,
`npm`. For PlatformIO and the ESP Component Registry, `extracted` shows `owner/name` when the
answer names the owner; write missed ones the same way (for example
`espidf:espressif/led_strip`). Keep case and separators as in the answer.

When finished, return both files; scoring: `python3 -m phantomdeps validation-score <runs>`.
"""


def guides() -> dict:
    """The English guide always; the Russian one used by the pilot's annotators when its
    source file is present (``tools/validation_guide_ru.md``, kept with the authors)."""
    out = {"GUIDE_en.md": GUIDE_EN}
    ru = DATA.parent / "tools" / "validation_guide_ru.md"
    if ru.exists():
        out["GUIDE_ru.md"] = ru.read_text(encoding="utf-8")
    return out


def name_for(run_ids) -> str:
    return "+".join(run_ids) if not isinstance(run_ids, str) else run_ids


def _dir(name: str):
    d = DATA / "validation" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def primary_families(run_ids) -> tuple:
    """Families that every model answered in the greedy pass of the runs (analysis: primary set)."""
    by_model = {}
    for rid in run_ids:
        for g in read_jsonl(RUNS / rid / "generations.jsonl"):
            if g["sample_idx"] == 0 and not g.get("error"):
                by_model.setdefault(g["model_id"], set()).add(g["family"])
    return tuple(sorted(set.intersection(*by_model.values()))) if by_model else ()


def shown(d: dict) -> str:
    """A declaration as the sheet shows it: ``eco:name``, with the owner as the answer wrote it
    (PlatformIO owner, ESP namespace), not the default namespace the extractor supplies."""
    nm = f"{d['owner']}/{d['name']}" if d.get("owner") and "implicit_namespace" not in (d.get("flags") or []) \
        else d["name"]
    return f"{d['eco']}:{nm}"


def sample(run_ids, per_eco: int = 25, seed: int = 20240628, annotators=("A", "B"),
           families=None, exclude=None) -> dict:
    """``exclude``: folders of earlier samples whose responses are left out (a sample that was
    already used to find extraction errors cannot measure the fixed extractor)."""
    run_ids = [run_ids] if isinstance(run_ids, str) else list(run_ids)
    families = tuple(families) if families else primary_families(run_ids)
    skip = set()
    for x in exclude or []:
        for r in read_jsonl(Path(x) / "sample.jsonl"):
            skip.add((r["run_id"], r["model_id"], r["prompt_id"]))
    gens, deps = {}, {}
    for rid in run_ids:
        run = RUNS / rid
        for g in read_jsonl(run / "generations.jsonl"):
            if g["sample_idx"] == 0 and not g.get("error") and g["family"] in families \
                    and (rid, g["model_id"], g["prompt_id"]) not in skip:
                gens[(rid, g["model_id"], g["prompt_id"])] = g
        for d in read_jsonl(run / "deps.jsonl"):
            if d["sample_idx"] == 0 and d["cls"] != "special":   # 'special' entries are not packages
                deps.setdefault((rid, d["model_id"], d["prompt_id"]), []).append(shown(d))
    from . import corpus
    prompts = {p["prompt_id"]: p["user"] for p in corpus.render(corpus.load())}
    rng = random.Random(seed)
    rows = []
    for eco in ECOS:
        keys = sorted(k for k, g in gens.items() if g["eco"] == eco)
        for k in rng.sample(keys, min(per_eco, len(keys))):
            g = gens[k]
            # a sample drawn with another seed gets ids of its own, so that its sheets never pass for
            # those of the first sample
            sid = f"{eco}-{len(rows):03d}" if seed == 20240628 else f"{eco}-{seed % 1000:03d}-{len(rows):03d}"
            rows.append({"sample_id": sid, "run_id": k[0], "model_id": k[1], "prompt_id": k[2],
                         "eco": eco, "task": prompts.get(k[2], ""), "text": g.get("text", ""),
                         "finish_reason": g.get("finish_reason"), "extracted": sorted(set(deps.get(k, [])))})
    d = _dir(name_for(run_ids))
    with open(d / "sample.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for fn, txt in guides().items():
        (d / fn).write_text(txt, encoding="utf-8")
    for a in annotators:
        with open(d / f"labels_{a}.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample_id", "extracted", "missed", "spurious", "checked", "note"])
            for r in rows:
                w.writerow([r["sample_id"], ";".join(r["extracted"]), "", "", "", ""])
        _xlsx(d / f"labels_{a}.xlsx", rows, a)
    write_json(d / "sample_meta.json", {"run_ids": run_ids, "per_eco": per_eco, "seed": seed,
                                        "families": list(families), "excluded": len(skip),
                                        "exclude": [Path(x).name for x in exclude or []]})
    return {"n": len(rows), "dir": str(d), "families": list(families), "seed": seed, "excluded": len(skip),
            "per_eco": {e: sum(1 for r in rows if r["eco"] == e) for e in ECOS}}


def _xlsx(path, rows, annotator: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "labels"
    head = ["sample_id", "eco", "task", "response", "cut_by_limit", "extracted", "missed", "spurious", "checked", "note"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r["sample_id"], r["eco"], r["task"], r["text"][:32000], "yes" if r["finish_reason"] == "length" else "",
                   "\n".join(r["extracted"]), "", "", "", ""])
    widths = {"A": 15, "B": 11, "C": 40, "D": 90, "E": 8, "F": 34, "G": 30, "H": 30, "I": 8, "J": 25}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    fill = PatternFill("solid", fgColor="FFF7D6")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for c in row[6:10]:
            c.fill = fill
    ws.freeze_panes = "C2"
    g = wb.create_sheet("guide")
    gd = guides()
    for ln in gd.get("GUIDE_ru.md", gd["GUIDE_en.md"]).splitlines():
        g.append([ln])
    g.column_dimensions["A"].width = 120
    g.append([])
    g.append([f"Annotator: {annotator}"])
    wb.save(path)


def _split(s) -> list[str]:
    return [x.strip() for x in re.split(r"[;\n]", str(s or "")) if x.strip()]


def _read(d, who: str) -> dict:
    xp, cp = d / f"labels_{who}.xlsx", d / f"labels_{who}.csv"
    rows = []
    if xp.exists():
        from openpyxl import load_workbook
        ws = load_workbook(xp, read_only=True)["labels"]
        it = ws.iter_rows(values_only=True)
        head = [str(h) for h in next(it)]
        rows = [dict(zip(head, r)) for r in it]
    elif cp.exists():
        with open(cp, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    out = {}
    for row in rows:
        if str(row.get("checked") or "").strip().lower() not in ("y", "yes", "1", "x", "+"):
            continue  # rows count only once the annotator has marked them as checked
        # the .csv sheets have no eco column: the id starts with it ("arduino-000", "arduino-630-000")
        out[str(row["sample_id"])] = {"eco": row.get("eco") or str(row["sample_id"]).split("-", 1)[0],
                                      "extracted": _split(row["extracted"]), "missed": _split(row["missed"]),
                                      "spurious": _split(row["spurious"])}
    return out


def kappa(a: list[bool], b: list[bool]) -> float:
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return float("nan") if pe == 1 else (po - pe) / (1 - pe)


def _pr(rows) -> dict:
    per = [(len(r["extracted"]) - len(set(r["spurious"]) & set(r["extracted"])), len(r["extracted"]),
            len(r["missed"])) for r in rows]
    tp = sum(a for a, _, _ in per)
    ext = sum(b for _, b, _ in per)
    missed = sum(c for _, _, c in per)
    # Wilson intervals treat items as independent; errors cluster within responses (one looping
    # Cargo.toml holds most missed items), so the intervals the paper reports resample responses
    # (percentile bootstrap, stats.BOOT_B replicates, stats.SEED; review 2026-10-29)
    return {"n_responses": len(rows), "extracted": ext, "true_positive": tp, "missed": missed,
            "precision": wilson(tp, ext), "recall": wilson(tp, tp + missed),
            "precision_by_response": cluster_bootstrap([(a, b) for a, b, _ in per]),
            "recall_by_response": cluster_bootstrap([(a, a + c) for a, _, c in per])}


def _errors(d, A) -> dict | None:
    """Where the errors lie: spurious items that the extractor reported for a non-registry
    declaration (URL or path; the sheet shows such a declaration by its package name, and an
    annotator may write it by URL instead), missed items written as a URL or path, and the largest
    number of missed items in one response."""
    sp = d / "sample.jsonl"
    if not sp.exists():
        return None
    from .extract import _is_path, _is_url
    keys = {r["sample_id"]: (r["run_id"], r["model_id"], r["prompt_id"]) for r in read_jsonl(sp)}
    kinds = {}
    for rid in {k[0] for k in keys.values()}:
        for x in read_jsonl(RUNS / rid / "deps.jsonl"):
            if x["sample_idx"] == 0 and x["cls"] != "special":
                kinds[(rid, x["model_id"], x["prompt_id"], shown(x))] = x["kind"]
    spurious = [(sid, s) for sid, r in A.items() for s in sorted(set(r["spurious"]) & set(r["extracted"]))]
    missed = [m.split(":", 1)[-1] for r in A.values() for m in r["missed"]]
    return {"spurious": len(spurious),
            "spurious_nonregistry": sum(1 for sid, s in spurious
                                        if sid in keys and kinds.get((*keys[sid], s)) in ("url", "path")),
            "missed": len(missed),
            "missed_nonregistry": sum(1 for m in missed if _is_url(m) or _is_path(m)),
            "missed_max_response": max((len(r["missed"]) for r in A.values()), default=0),
            # response-level concentration: the errors sit in a few responses, so item-level Wilson
            # intervals are too narrow (review 2026-10-29)
            "n_responses": len(A),
            "resp_with_missed": sum(1 for r in A.values() if r["missed"]),
            "resp_with_spurious": sum(1 for r in A.values() if set(r["spurious"]) & set(r["extracted"])),
            "resp_exact": sum(1 for r in A.values()
                              if not r["missed"] and not (set(r["spurious"]) & set(r["extracted"]))),
            "resp_exact_wilson": wilson(sum(1 for r in A.values() if not r["missed"]
                                            and not (set(r["spurious"]) & set(r["extracted"]))), len(A))}


def score(run_ids, ref: str = "A", other: str = "B") -> dict | None:
    d = DATA / "validation" / name_for(run_ids)
    if not d.exists():
        return None
    A = _read(d, ref)
    if not A:
        # the review artifact ships the counts (score.json) but not the sheets, which hold the answers
        sj = d / "score.json"
        return json.loads(sj.read_text(encoding="utf-8")) if sj.exists() else None
    res = _pr(list(A.values()))
    res["by_eco"] = {e: _pr([r for r in A.values() if r["eco"] == e]) for e in ECOS
                     if any(r["eco"] == e for r in A.values())}
    B = _read(d, other)
    if B:
        ids = sorted(set(A) & set(B))
        exact = lambda r: not r["missed"] and not r["spurious"]  # noqa: E731
        labels = lambda r: (sorted(set(r["missed"])), sorted(set(r["spurious"])))  # noqa: E731
        if ids:
            res["kappa_exact"] = kappa([exact(A[i]) for i in ids], [exact(B[i]) for i in ids])
            res["n_both"] = len(ids)
            # rows whose labels coincide: kappa measures agreement only if the sheets were filled
            # independently, which the counts cannot show
            res["identical_rows"] = sum(1 for i in ids if labels(A[i]) == labels(B[i]))
            res["other"] = _pr([B[i] for i in ids])
    res["errors"] = _errors(d, A)
    # the scored sheets (they hold the responses and stay with the authors)
    res["sheets_sha256"] = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                            for f in sorted(d.glob("labels_*.xlsx"))}
    write_json(d / "score.json", res)
    return res
