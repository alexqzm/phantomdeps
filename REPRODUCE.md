# Reproducing the paper

Every number, table and figure of `paper/main.tex` is generated; the paper reads
`results/macros.tex` (one `\newcommand` per value, with its provenance in `results/macros.json`),
`results/tab_*.tex` and `results/fig_*.pdf`. Nothing is typed by hand.

## 1. From the review artifact (no GPU, no network)

The artifact ships the pseudonymised per-declaration data of every run
(`data/runs/<run>/{generations,deps,responses}_masked.jsonl`: generation records without text,
classified declarations and answers, with every phantom name that an outsider could still
register replaced by a keyed pseudonym `ph-<10 hex>`), the derived registry data that
classification read (`data/derived/`), the evidence written after classification
(`data/evidence/`: results of the lock-file check, audit of post-horizon matches, replay
summary) and, once the two annotators have finished, the counts of the manual validation
(`data/validation/*/score.json`; the sheets hold response texts and stay with the authors).

```sh
pip install -r requirements.txt
cd harness
python3 -m phantomdeps analyze study-ollama-2027 --masked
cd ../paper && ./build.sh
```

The paper reports the study (`study-ollama-2027`, with its extension run `study-ollama-2027-ext`,
which the analysis reads as a sensitivity analysis); the pilot that validated the harness is
`analyze pilot-cpu-2026-10 pilot-cpu-ext-2026-10 pilot-cpu-famcmp-2026-10 --masked` with
`\pilottrue` in `paper/main.tex`.

`--masked` reads the pseudonymised files. The analysis then writes every macro, table and
figure; `python3 tools/check_reproduction.py <runs>` (`make check-masked`, which the
repository's CI runs on every push) checks that the pseudonymised files reproduce the unmasked
analysis, as shipped in `results/`, value for value. The only value that
differs between hosts is the gate's time per decision (`\rGateUs`).

The same pseudonym always stands for the same registry name (HMAC-SHA256 of the name as the
registry compares names: PEP 503 for PyPI, exact for Arduino, case-blind elsewhere), so counts, joins across models
and runs, and every rate can be checked without the names. The key stays with the authors and
will be published, with the names, after the registry operators have been informed
(PROTOCOL.md, section 9).

## 2. Where each result comes from

| Paper element | File | Produced by |
|---|---|---|
| Table worked example | `results/tab_worked.tex` | `analyze` (one real manifest, gate evaluation) |
| Table registries | `results/tab_registries.tex` | `analyze` (snapshot dates, decidability, clean decisions) |
| Table models | `results/tab_models.tex` | `analyze` (horizons from `data/derived/model_horizons.json`, budgets, cut answers, file validity) |
| Table prevalence, Fig. prevalence | `results/tab_prevalence.tex`, `results/fig_prevalence.pdf` | `analyze` (cluster bootstrap over tasks, seed 20240628) |
| Table examples | `results/tab_examples.tex` | `analyze` (pseudonyms for registrable names) |
| Table post-horizon | `results/tab_posthorizon.tex` | `analyze` + `data/evidence/posthorizon_audit.json` (`make audit`) |
| Table gate | `results/tab_gate.tex` | `analyze` (observed world with the lock files of `data/evidence/lockcheck.json`, squat worlds) |
| Fig. threshold | `results/fig_threshold.pdf` | `analyze` (grid over theta and alpha_prov) |
| Replay paragraph | macros `rRp*` | `data/evidence/replay.json` (`make replay`) |
| Chat formats of the pilot models | PROTOCOL.md, section 4 | `data/evidence/chat_templates.json` (`make templates`) |
| Lock-file paragraph | macros `rLock*`, `rAct*`, `rProvWhatIf*` | `data/evidence/lockcheck.json` (`make lockcheck`) |
| Family comparison | macros `rFam*` (study: both families, one budget, paired by model and task); `rFamEq*` (pilot) | `analyze`; run `pilot-cpu-famcmp-2026-10` (`make famcmp`) for the pilot |
| Extension of cut answers | macros `rExt*` | run `study-ollama-2027-ext` and `data/evidence/extension_check.json` (`tools/check_extension.py`) |
| Manual validation | macros `rVal*` (after annotation) | `data/validation/<runs>/score.json` (`make validation-score`) |
| Propositions 1-4 | `harness/tests/test_gate.py` | `make test` (properties on random evidence and on the whole pipeline) |

## 3. From scratch (authors' side, or after the names are published)

Study (`config/run_study_ollama.yaml`): 13 hub GGUF files pinned by SHA-256, served by Ollama
0.40.11 with the library's templates and defaults (`config/ollama_library/`), one request at a time,
context 8,192, on a 12 GB laptop GPU; runs `study-ollama-2027` (greedy pass, F1 and F2, 1,536
tokens, 4,096 for the two reasoning models; persistence pass, 5 resamples at T = 0.7) and
`study-ollama-2027-ext` (the cut answers again with 4,096 tokens, 7,680 for the reasoning models).
The Windows kit and the order of steps are in README.md, "Running the study"; then
`make results`, `make lockcheck LOCKREUSE=data/evidence/lockcheck.json`, `make replay audit` and
`make results paper docx`.

Pilot models (GGUF files with SHA-256 in the run configurations; llama.cpp `llama-server`, commit
06230cb, 2 vCPUs, 7 GB RAM; one model at a time, greedy decoding):

| Run | Config | Models | Families, budget |
|---|---|---|---|
| `pilot-cpu-2026-10` | `config/run_pilot_cpu.yaml` | Qwen2.5-Coder-1.5B, DeepSeek-Coder-1.3B, Llama-3.2-1B | F2 384 tokens; F1 512 (Qwen only); 3 resamples per phantom answer |
| `pilot-cpu-ext-2026-10` | `config/run_pilot_cpu_ext.yaml` | Qwen2.5-Coder-7B, Llama-3.2-3B, Phi-3-mini-3.8B (Microsoft's GGUF, served with llama.cpp's built-in `phi3` chat template, which keeps the system message; Granite-3.3-8B stopped after 34 of 96 answers, removed by an `amend` entry; its answers in `data/runs/pilot-cpu-ext-granite-partial/`) | F2 1024 tokens |
| `pilot-cpu-famcmp-2026-10` | `config/run_pilot_cpu_famcmp.yaml` | Qwen2.5-Coder-1.5B | F1 and F2, 1536 tokens each, 48 tasks |

```sh
make test
make snapshots          # static indices, Arduino / ROS / micropython-lib histories (git)
make corpus
make pilot pilot-ext famcmp
make horizons           # weight-anchored horizons (Hugging Face API answers cached in data/derived/hf_cache/)
make results RUNS="pilot-cpu-2026-10 pilot-cpu-ext-2026-10 pilot-cpu-famcmp-2026-10"   # the pilot
make lockcheck          # cargo / npm / uv in resolve-only mode -> data/evidence/lockcheck.json
make audit              # record, artifact and repository dates of post-horizon matches
make replay             # squats on pip, uv, npm and Cargo against registries on 127.0.0.1
make results paper docx # re-analyze with the evidence, build PDF and Word versions
make validation-sample  # sheets for two independent annotators (data/validation/)
```

`git` clones that the artifact leaves out because of their size are re-created at the recorded
commits (`data/derived/static_meta.json`): `ros/rosdistro` at `registry_head` of `ros2` into
`data/snapshots/git/rosdistro`, and the audit's bare clones by `make audit`. The git repository
stores the other clones as `data/snapshots/git/<name>.tar`, because git cannot hold nested
repositories; `make git-snapshots` unpacks them.

## 4. Checks that stop the pipeline

* `classify` records the hashes of the harness code and of `data/derived/`; `analyze` refuses
  runs classified with other code or derived data.
* `analyze` stops if a statement the paper makes without a number no longer holds (e.g. every
  policy but P0 rejects every unsafe manifest; the gate-only rule decides exactly as the
  three-valued date filter; no policy that the paper calls safe admits a squat).
* `analyze` and `mask` refuse to write a pseudonymised file that still holds a registrable name.
* `analyze` uses the measured pins only if `data/evidence/lockcheck.json` covers every resolved
  crates.io, npm and PyPI declaration, and records whether it was computed from the declarations
  being analysed (`results.json`, `lockcheck_coverage.inputs_current`).
* `tools/check_anon.py` checks the review archive for the key, raw files, registrable names and
  identifying terms.
