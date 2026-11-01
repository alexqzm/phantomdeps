#!/usr/bin/env python3
"""Check an archive meant for double-blind review before it leaves the authors.

Fails (exit code 1) if the archive contains
  * anything under secrets/ (the pseudonym key), or the key itself inside any file;
  * raw generations, declarations or answers (only the *_masked.jsonl copies may ship);
  * annotation sheets or samples (they hold the response texts) or the replay's raw log;
  * the authors' own files: the response letter to the pre-submission review, its filler and the
    annotators' Russian guide;
  * a phantom name that an outsider may be able to register (open, namespace or variant; owner/name where
    the declaration names an owner) in a package-name position of a JSON file we produce, a declaration
    row that the masking rule hides but that is in clear, or such a name set in \\texttt{} in a TeX
    file -- the names are read from the local, unmasked declarations, which never ship.  Names that
    exist as packages somewhere, our own labels, and public registry data are not counted;
  * an identifying term in any text file, in PDF metadata or in DOCX properties.  The terms are read
    from secrets/identity_terms.txt (one per line), which never ships, so this script names nobody.

Cyrillic text hints at the authors' language: it fails outside third-party data (data/snapshots/),
where it is listed for a manual look.  So do a Windows user folder in a path (the generating host's
user name) outside third-party data, and a local time-zone offset in the run metadata
(data/runs/*/*.json: the generating host's time zone).

Usage: python3 tools/check_anon.py dist/phantomdeps-cps-anon.zip
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (".py", ".md", ".txt", ".tex", ".bib", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".sh", ".cfg", ".toml",
        ".html", ".js", ".bst", ".cls", ".sty", "Makefile", "LICENSE")
RAW = re.compile(r"data/runs/[^/]+/(generations|deps|responses)\.jsonl$")
AUTHOR_ONLY = {"tools/response_letter_template.md", "tools/response_letter_v4_template.md", "tools/fill_letter.py",
               "tools/validation_guide_ru.md", "tools/package_dist.sh"}
CYR = re.compile("[\u0400-\u04ff]")
USER_DIR = re.compile(r"[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/)", re.I)
LOCAL_OFFSET = re.compile(r'"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-](?!00:00)\d{2}:\d{2}"')
RUN_META = re.compile(r"data/runs/[^/]+/[^/]+\.json$")


sys.path.insert(0, str(ROOT / "harness"))
from phantomdeps import analysis as A  # noqa: E402  (the masking rule of the analysis itself)

# public registry and tool data (copies of registry indices, data derived from them, model-hub answers,
# the replay's own npm manifests): a phantom's spelling found there is a registry fact, not a leak
THIRD_PARTY = ("data/snapshots/", "data/derived/", "replay/package.json", "replay/package-lock.json")
# our own labels (ecosystems, policies, model names) are not leaks even when a model wrote one as a name
VOCAB = set(A.ECO_LABEL.values()) | set(A.ECO_LABEL) | set(A.POLICY_LABEL.values())


def registrable_names() -> tuple[set[str], set[str], set[str]]:
    """Registrable phantom names (owner/name where the declaration names an owner), the names that
    exist as packages in some ecosystem, and the strict set: registrable and existing nowhere."""
    deps = [json.loads(line) for f in sorted((ROOT / "data" / "runs").glob("*/deps.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()]
    A.set_registrable(deps)
    reg = {(f"{d['owner']}/{d['name']}" if d.get("owner") else d["name"]) for d in deps
           if d.get("cls") == "phantom" and d.get("squattable") in A.MASKED}
    # names that exist as packages, or are built into a toolchain (uasyncio, Wire), are public names
    exists = {d["name"] for d in deps if d.get("cls") in ("exists", "toolchain")}
    strict = {n for n in reg - exists - VOCAB if n.lower() not in {e.lower() for e in exists}}
    return reg, exists, strict


def name_values(o, key=None):
    """String values in package-name positions: under a key 'name' or in a list under 'names'."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from name_values(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from name_values(v, key if key == "names" else None)
    elif isinstance(o, str) and key in ("name", "names", "display", "declared"):
        yield o


def json_docs(name: str, data: bytes):
    txt = data.decode("utf-8")
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in txt.splitlines() if line.strip()]
    return [json.loads(txt)]


def main(path: str) -> int:
    z = zipfile.ZipFile(path)
    names = z.namelist()
    problems = []
    terms_file = ROOT / "secrets" / "identity_terms.txt"
    terms = [t.strip() for t in terms_file.read_text(encoding="utf-8").splitlines() if t.strip()] \
        if terms_file.exists() else []
    key_file = ROOT / "secrets" / "mask_key"
    key = key_file.read_text(encoding="utf-8").strip() if key_file.exists() else None
    phantoms, exists, strict = registrable_names()
    cyr = {}   # Cyrillic text hints at the authors' language: listed for a manual look, not failed
    rx_terms = re.compile("|".join(re.escape(t) for t in terms), re.I) if terms else None
    for n in names:
        if n.startswith("secrets/"):
            problems.append(f"{n}: the secrets folder must not ship")
        if RAW.search(n):
            problems.append(f"{n}: raw run file (ship the *_masked.jsonl copy)")
        if re.search(r"data/validation/.+/(sample\.jsonl|labels_)", n) or n.startswith("data/replay/"):
            problems.append(f"{n}: holds response texts or real names")
        if (n in AUTHOR_ONLY or n.endswith("/GUIDE_ru.md") or n.startswith("tools/study_kit/")
                or n.startswith("data/study_host_logs/")):
            problems.append(f"{n}: authors' file (response letter, its filler, Russian guides, the Windows kit or the study host's logs)")
    for n in names:
        if n.endswith("/"):
            continue
        data = z.read(n)
        low = n.lower()
        if low.endswith(TEXT) or Path(n).name in ("Makefile", "LICENSE"):
            try:
                txt = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if key and key in txt:
                problems.append(f"{n}: contains the pseudonym key")
            if (c := len(CYR.findall(txt))):
                if n.startswith("data/snapshots/"):
                    cyr[n] = c
                else:
                    problems.append(f"{n}: {c} Cyrillic characters (the authors' language)")
            if rx_terms and (m := rx_terms.search(txt)):
                problems.append(f"{n}: identifying term at offset {m.start()}")
            if not n.startswith(THIRD_PARTY) and (m := USER_DIR.search(txt)):
                problems.append(f"{n}: a Windows user folder at offset {m.start()} (the host's user name)")
            if RUN_META.search(n) and (m := LOCAL_OFFSET.search(txt)):
                problems.append(f"{n}: a local time-zone offset at offset {m.start()} (the host's time zone)")
            if low.endswith((".json", ".jsonl")) and phantoms and not n.startswith(THIRD_PARTY):
                try:
                    docs = json_docs(n, data)
                except ValueError:
                    docs = []
                for doc in docs:
                    # a declaration row of a phantom that the masking rule hides must carry a pseudonym, and
                    # no field of a row other than an existing package or toolchain name may spell one
                    if not isinstance(doc, dict) or "cls" not in doc:
                        continue
                    if doc.get("cls") == "phantom" and A.to_mask(doc) and not A.is_masked(doc.get("name", "")):
                        problems.append(f"{n}: a phantom row that should be pseudonymised is in clear")
                        break
                    if doc.get("cls") not in ("exists", "toolchain") and any(A.leaks(doc.get(f)) for f in A.SCRUBBED):
                        problems.append(f"{n}: a {doc.get('cls')} row spells a registrable name in "
                                        f"{[f for f in A.SCRUBBED if A.leaks(doc.get(f))]}")
                        break
                hit = sorted({v for doc in docs for v in name_values(doc) if v in strict})
                if n.endswith("results_masked.json"):
                    for doc in docs:
                        for part in ("unique", "cross_model"):
                            for u in (doc.get("phantoms") or {}).get(part, []):
                                if A.entry_masked(u) and not A.is_masked(u["name"].split("/")[-1]):
                                    hit.append(u["name"])
                if hit:
                    problems.append(f"{n}: {len(hit)} registrable phantom names, e.g. {hit[0][:3]}...")
                if n.endswith("macros.json"):
                    tx = [v["value"] for doc in docs for v in doc.values() if isinstance(v, dict)
                          and isinstance(v.get("value"), str)]
                    mh = sorted(p for p in strict if any("\\texttt{" + A.tex_escape(p) + "}" in v for v in tx))
                    if mh:
                        problems.append(f"{n}: {len(mh)} registrable phantom names in macros")
            if low.endswith(".tex") and phantoms:
                check = phantoms if n.endswith("tab_examples.tex") else strict
                hit = sorted(p for p in check if "\\texttt{" + A.tex_escape(p) + "}" in txt)
                if hit:
                    problems.append(f"{n}: {len(hit)} registrable phantom names in \\texttt")
        elif low.endswith(".pdf") and rx_terms:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as t:
                t.write(data)
                t.flush()
                info = subprocess.run(["pdfinfo", t.name], capture_output=True, text=True).stdout
                body = subprocess.run(["pdftotext", t.name, "-"], capture_output=True, text=True).stdout
            if rx_terms.search(info) or rx_terms.search(data.decode("latin-1")) or rx_terms.search(body):
                problems.append(f"{n}: identifying term in the PDF (metadata or text)")
        elif low.endswith(".docx") and rx_terms:
            with zipfile.ZipFile(__import__("io").BytesIO(data)) as dz:
                for part in dz.namelist():
                    if part.endswith(".xml") and rx_terms.search(dz.read(part).decode("utf-8", "replace")):
                        problems.append(f"{n}: identifying term in {part}")
    print(f"{path}: {len(names)} entries, {len(terms)} identity terms, {len(phantoms)} registrable names checked")
    for p in problems[:50]:
        print("  FAIL", p)
    for n, c in sorted(cyr.items(), key=lambda x: -x[1])[:15]:
        print(f"  note: {c} Cyrillic characters in {n}")
    if not terms:
        print("  note: secrets/identity_terms.txt not found; identity check skipped")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "phantomdeps-cps-anon.zip")))
