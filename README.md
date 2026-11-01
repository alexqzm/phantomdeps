# PhantomDeps-CPS

Artifact for the paper *Phantom Dependencies in AI-Generated Build Manifests for Cyber-Physical
Systems: Measurement and Horizon-Anchored Admission*.

It measures how often code models declare dependencies that do not exist (phantoms) when
they write build manifests for eight CPS ecosystems (Arduino Library Manager, PlatformIO,
ESP Component Registry, ROS 2, crates.io, MicroPython mip, PyPI, npm/Node-RED), and it
evaluates an admission gate that anchors the evidence about each dependency at the
knowledge horizon of the model that proposed it.

Every number in the paper is produced by `harness/` and read from `results/`; nothing is
typed by hand. `PROTOCOL.md` fixes the study design and logs every change; `REPRODUCE.md` maps
each table and figure to the commands that produce it; `VERIFICATION.md` records how each
reference was checked.

## Layout

```
PROTOCOL.md            study protocol (research questions, ground truth, measures, change log)
REPRODUCE.md           which command produces which table, figure and number
VERIFICATION.md        how every bibliography entry was verified
Makefile               test / snapshots / pilot / pilot-ext / famcmp / results / lockcheck / audit /
                       replay / mask / check-masked / paper / docx / anon-artifact / repo-export /
                       git-snapshots
requirements.txt       packages of the analysis, the tests and the reproduction check (pinned)
.github/workflows/     CI of the git repository: make test and make check-masked on every push
config/
  profile.yaml         gate profile (assigned weights, threshold, lags, sweep)
  run_pilot_cpu.yaml   pilot: 3 small models, llama.cpp, 2 vCPUs
  run_pilot_cpu_ext.yaml    3 larger models (7B, 3.8B, 3B), F2 with 1024 tokens (8B stopped)
  run_pilot_cpu_famcmp.yaml equal-budget F1/F2 comparison (48 tasks)
  run_study_ollama.yaml study: 13 open-weight models through Ollama
corpus/
  tasks_v1.yaml        96 tasks (8 ecosystems x 12; common / niche tiers)
  prompts_v1.0.jsonl   rendered prompts (F1 manifest + code, F2 manifest only)
harness/phantomdeps/   the measurement code (see module docstrings)
harness/tests/         unit tests: gate propositions (evidence level and whole pipeline),
                       extraction, statistics, runner, registry dating
data/
  snapshots/           static registry indices and git clones (with fetch times)
  derived/             name sets, record-date bounds, registry histories, toolchain built-ins,
                       weight-anchored model horizons (all read by classification and hashed)
  evidence/            written after classification: lock-file check (lockcheck.json), audit of
                       post-horizon matches, replay summary (read by the analysis)
  cache/               every registry API answer, with retrieval time (= the snapshot)
  runs/<run_id>/       generations, declarations, answers (+ *_masked.jsonl pseudonymised copies)
  validation/          annotation samples and sheets (two annotators), score after annotation
results/               results.json, results_masked.json, macros.tex/.json, tab_*.tex, fig_*.pdf
                       (generated)
replay/                squats on real installers against registries on 127.0.0.1 (README inside)
paper/                 IEEEtran LaTeX sources; build.sh
tools/                 model_horizons.py, audit_posthorizon.py, check_reproduction.py,
                       check_anon.py, build_docx.py, verify_refs.py
secrets/               pseudonym key and identity terms (authors only; never shipped)
```

## Requirements

* Python >= 3.11 (uses `tomllib`); packages `pyyaml`, `numpy`, `matplotlib`, `packaging`,
  `catkin_pkg` (package.xml parser of the ROS tooling), `openpyxl` (annotation sheets)
  (tested with Python 3.13, PyYAML 6.0.3, NumPy 2.5.3, Matplotlib 3.11.2, catkin_pkg 1.1.1).
* For `make lockcheck`: cargo, npm/node and uv (tested cargo 1.97.0, npm 10.9.4, uv 0.11.32).
  `make lockcheck LOCKREUSE=data/evidence/lockcheck.json` locks only the manifests that are new
  or changed since that file and keeps the other records (marked `reused_from`).
  For `make replay`: additionally pip and Verdaccio (`cd replay && npm install`), `unshare` (the
  replay runs in a loopback-only network namespace), and for its ESP-IDF build step an ESP-IDF
  checkout named by `REPLAY_ESP_IDF` (tested v6.0.3; setup in `replay/README.md`).
* `git` (registry histories), network access for `snapshots` and `classify`.
* A TeX distribution with `pdflatex` and `bibtex` (tested with TeX Live 2023).
  `paper/` ships IEEEtran.cls v1.8b and IEEEtran.bst v1.14.
* For generation: an OpenAI-compatible server. The pilot used llama.cpp `llama-server`
  (commit 06230cb); the study uses Ollama.

```sh
pip install -r requirements.txt     # the tested versions above
make test
make check-masked                   # every number and table of the paper from the pseudonymised data
```

## Reproducing the reported numbers without a GPU

The classification is cached, so the reported results regenerate offline:

```sh
cd harness
for r in pilot-cpu-2026-10 pilot-cpu-ext-2026-10 pilot-cpu-famcmp-2026-10; do
  python3 -m phantomdeps classify $r --offline; done
python3 -m phantomdeps analyze pilot-cpu-2026-10 pilot-cpu-ext-2026-10 pilot-cpu-famcmp-2026-10
cd ../paper && ./build.sh
```

From the review artifact, which has no raw generations, add `--masked` to `analyze` (see
`REPRODUCE.md`).

## Running the study (Ollama)

The study generates on the author's GPU machine; classification and analysis run where the
registry snapshots are.

1. Build the kit: `python3 tools/build_study_kit.py dist` (a zip of the runner, the harness, the
   study configuration with the pinned hub GGUF files, the Ollama library templates, the corpus
   and a pure-Python PyYAML; no data). On Windows: unpack it and double-click `run_study.cmd`
   (it installs Python and Ollama with winget if needed; see `README_RU.txt` in the kit).
   Elsewhere: `python3 tools/study_runner.py all` with Ollama >= 0.30 installed.
2. The runner starts `ollama serve` with `OLLAMA_CONTEXT_LENGTH=8192 OLLAMA_NUM_PARALLEL=1`,
   downloads each GGUF (resumable; size and SHA-256 checked), imports it with the library tag's
   template and defaults (`ollama create`, weight digest and template checked), runs a 4-prompt
   smoke test with automatic checks, then the greedy pass of every model that passed, and packs
   `results/PhantomDeps_study_greedy_<time>.zip` (run records and logs). Interrupt and restart at
   will; a model that fails is skipped and reported.
3. Here: import the archive and classify the greedy pass, then write the two plans of the
   second session and build its kit (the plans inside, `run_resample.cmd` but no `run_study.cmd`):

   ```sh
   python3 tools/import_study_results.py PhantomDeps_study_greedy_<time>.zip
   python3 tools/warm_registry_cache.py study-ollama-2027   # optional: parallel lookups first
   cd harness
   python3 -m phantomdeps classify study-ollama-2027
   python3 -m phantomdeps resample-plan ../config/run_study_ollama.yaml   # persistence pairs
   python3 -m phantomdeps extend-plan ../config/run_study_ollama.yaml     # answers cut by the budget
   cd .. && python3 tools/build_study_kit.py dist --followup data/runs/study-ollama-2027
   ```

   The study is classified against the pilot's static snapshot (PROTOCOL.md, section 2: static
   indices are downloaded once); the API registries are queried at classification time and the
   answers are cached (`data/cache/`). The classifier looks names up one at a time; the warm-up
   fills the cache through the classifier's own code with parallel lookups (its timings, measured
   under concurrency, do not enter the cost figures).
4. On the GPU machine: unpack the second kit into a new folder and run `run_resample.cmd`
   (elsewhere `python3 tools/study_runner.py followup`): it checks the models already in Ollama,
   runs the persistence pass and then the extension pass (run `study-ollama-2027-ext`), and packs
   `PhantomDeps_study_followup_<time>.zip`.
5. Here: import the archive (the plans must be the ones written here; records already imported
   must be identical; run metadata keeps paths relative to the kit and times in UTC), fill the
   registry cache, then classify, check and analyse:

   ```sh
   python3 tools/import_study_results.py PhantomDeps_study_followup_<time>.zip
   python3 tools/warm_registry_cache.py study-ollama-2027        # optional, parallel lookups
   python3 tools/warm_registry_cache.py study-ollama-2027-ext
   make results            # classify both runs, tools/check_extension.py, analyze study-ollama-2027
   make lockcheck LOCKREUSE=data/evidence/lockcheck.json   # lock the study's manifests, keep the pilot's
   make replay audit       # squats named after the pilot's phantoms; dates of post-horizon matches
   make results paper docx
   ```

   `tools/check_extension.py` writes `data/evidence/extension_check.json` (which extended answers
   repeat the cut answer as their prefix), which the analysis reads. The paper reports the study
   (`\pilotfalse`, the default); `make results RUNS="$(PILOT_RUNS)"` and `\pilottrue` give the
   pilot version. Tables switch to a one-row-per-model layout and the prevalence figure to
   single-series dot plots when there are more than four models. The host's logs land in
   `data/study_host_logs/` (they name the host's user and computer and never ship in the
   anonymous artifact).
6. Validate the extraction (PROTOCOL.md, section 5): draw the stratified sample and give
   each annotator their sheet (`labels_A.xlsx`, `labels_B.xlsx`, instructions in
   `GUIDE_en.md`, the Russian text the pilot's annotators use in `GUIDE_ru.md` when
   `tools/validation_guide_ru.md` is present, and on the sheet `guide`); rows count once marked
   `checked = y`. The two annotators are people and work independently. The study's first
   three samples (seeds 20240628 to 20240630) came back filled by a program (the third as a
   pre-check by an AI) and served only as pre-screens that found extraction errors
   (`data/validation/study-ollama-2027_llm-prescreen/`, `..._llm-prescreen-2/` and
   `..._llm-prescreen-3/`); the manual validation uses a new sample without those answers:

   ```sh
   python3 -m phantomdeps validation-sample study-ollama-2027 --seed 20240631 \
       --exclude ../data/validation/study-ollama-2027_llm-prescreen \
                 ../data/validation/study-ollama-2027_llm-prescreen-2 \
                 ../data/validation/study-ollama-2027_llm-prescreen-3   # 25 responses per ecosystem
   # fill data/validation/study-ollama-2027/labels_A.xlsx and labels_B.xlsx
   python3 -m phantomdeps validation-score study-ollama-2027
   python3 -m phantomdeps analyze study-ollama-2027               # adds \rValPrecision, \rValRecall, \rValKappa
   ```

   The sheets of seed 20240631 came back on 2026-10-29 as one joint annotation by two annotators from
   one university group (identical labels in every row; PROTOCOL.md, change log 2026-10-29);
   `score.json` in that folder holds the counts the analysis reads.

   `make validation-sample` runs the same command (`VAL_SEED`, `VAL_EXCLUDE`). For the pilot
   (pilot and extension pooled): `make validation-sample VAL_RUNS="pilot-cpu-2026-10
   pilot-cpu-ext-2026-10" VAL_SEED=20240628 VAL_EXCLUDE=`, then `make validation-score` with
   the same `VAL_RUNS`.

## Pilot (llama.cpp, CPU)

`config/run_pilot_cpu.yaml` starts one `llama-server` per model (`server_cmd`) and needs the
three GGUF files named there (SHA-256 recorded). `make pilot` runs the greedy pass
(F2 for all models, F1 for Qwen2.5-Coder-1.5B), classification and the persistence pass.
`make pilot-ext` adds three larger models (F2, 1024 tokens; Qwen2.5-Coder-7B, Llama-3.2-3B and
Phi-3-mini; a fourth, Granite-3.3-8B, was stopped by the host's time limit and is not analysed,
see the configuration) and `make famcmp` the
equal-budget F1/F2 comparison; `make horizons` derives each model's horizon from the hub
commit of the weights it ran (`tools/model_horizons.py`), and `make templates` records the prompt
each server renders (DeepSeek-Coder's GGUF has no chat template and is served with llama.cpp's
built-in `deepseek` format; Phi-3-mini with the built-in `phi3` format, which keeps the system
message).

## What the paper reads

`paper/main.tex` includes `results/macros.tex` (one `\newcommand` per number, profile value
and configuration fact, each with its provenance tag in the comment and in
`results/macros.json`), `results/tab_*.tex` and `results/fig_*.pdf`. Two switches at the
top of `main.tex`:

* `\anontrue` - double-blind version (author block and acknowledgment hidden, self-citation
  anonymised). The camera-ready author block and acknowledgment live in
  `paper/authors_camera_ready.tex` and `paper/acknowledgment_camera_ready.tex`.
* `\pilotfalse` (the default) - the study's wording; `results/` holds the analysis of
  `study-ollama-2027`. `\pilottrue` gives the pilot's wording and red box, after
  `make results RUNS="$(PILOT_RUNS)"`.

The analysis refuses to run if the classification was produced by other harness code or
other derived snapshots (hashes in `classify_meta.json`), if the assigned profile no longer
satisfies the statements Section VI makes about it, or if a statement that Section VI makes
without a number (for example, that every policy but P0 rejects every unsafe manifest)
no longer holds on the data.

## Word version

`make docx` (after `make paper`) writes `dist/paper_draft.docx` from the same
sources and the same LaTeX build: text and numbers from `paper/main.tex` and `results/`, label
and citation numbers from `paper/main.aux`, the formatted references from `paper/main.bbl`
(the script stops if that build is older than the sources). Equations are editable Word (OMML)
equations, Fig. 1 is rendered from its TikZ source and Fig. 2 from `results/fig_threshold.pdf`,
tables are rebuilt from `results/tab_*.tex`. Page layout follows the IEEE conference template
(US Letter, two 3.5" columns, Times 10 pt). The file carries no author or editor name, and Word is
told to remove personal information when it is saved again. The PDF stays the reference layout: Word sets
lines, tables and formulas differently, so pagination differs.

Requirements: pandoc (tested 3.1.3), `pip install python-docx lxml pillow` (tested
python-docx 1.2.0, lxml 6.1.3, Pillow 12.3.0; Pillow and the Liberation fonts give exact
column widths, without them the widths are estimated), `pdflatex` with TikZ, `pdftoppm`
(Poppler). Open the file in Microsoft Word: LibreOffice 24.2 draws formulas at its default
12 pt instead of the body size and may cut wide display equations.

## Anonymous artifact for review

`make anon-artifact` writes the pseudonymised copies of every run (`make mask`) and builds
`dist/phantomdeps-cps-anon.zip` without the pseudonym key, the camera-ready files, the raw
generations and declarations, the registry cache, the annotation sheets, the unmasked
`results.json`, the replay's raw log and the reference-verification notes. Names an outsider
could register are replaced by keyed pseudonyms (`ph-<10 hex>`, HMAC-SHA256 of the name as the
registry compares it), so every count can be recomputed (`analyze --masked`;
`make check-masked` compares the result with `results/` value for value). `tools/check_anon.py`
then checks the archive for the key, raw files, registrable names and identifying terms (read
from `secrets/identity_terms.txt`, which never ships). Times in the archive are UTC and git
reflogs are left out. Upload it to an anonymous host (e.g. anonymous.4open.science) and put the
link into `\rArtifactLink` (top of `paper/main.tex`).

## Git repository

The repository holds exactly the files of the anonymous artifact. `make repo-export REPO=<work
tree>` builds and checks the archive (`make anon-artifact`) and writes it into the work tree
(`tools/export_repo.py`), so nothing reaches git that `tools/check_anon.py` has not passed; the raw
runs, the key and the other authors' files (listed in `.gitignore`) never do. Git cannot hold the
nested clones under `data/snapshots/git/`, so the repository stores each one as
`data/snapshots/git/<name>.tar` and `make git-snapshots` unpacks them (the archive ships them
unpacked). On every push, CI (`.github/workflows/tests.yml`) runs `make test` and
`make check-masked` with the packages pinned in `requirements.txt`.

## Ethics

No package is registered or published in a public registry. Registry lookups are read-only,
cached and rate-limited (User-Agent `phantomdeps-cps/1.0 (research artifact; read-only
lookups)`); lock files are written in resolve-only modes that run no package code. The replay
runs registries on 127.0.0.1 only, in a network namespace whose only interface is loopback,
with harmless packages that write a marker file. Phantom
names that an outsider could register are not published before they have been reported to
the registry operators (`PROTOCOL.md`, section 9).

## Licence

Code: Apache License 2.0 (`LICENSE`). Registry snapshots keep the licences of their
sources; model outputs are released for research use.
