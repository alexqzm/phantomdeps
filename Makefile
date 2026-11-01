# PhantomDeps-CPS: reproducible pipeline for the paper (REPRODUCE.md maps every table and figure
# to these targets).
#   make test          unit tests (gate properties, extraction, validity, statistics)
#   make snapshots     download static registry indices, derive name sets, built-ins, histories
#   make corpus        render prompts from corpus/tasks_v1.yaml
#   make pilot         pilot generations (llama.cpp, three small models) + persistence pass
#   make pilot-ext     larger models (F2, 1024 tokens), config/run_pilot_cpu_ext.yaml
#   make famcmp        equal-budget F1/F2 comparison, config/run_pilot_cpu_famcmp.yaml
#   make study         study generations (Ollama, config/run_study_ollama.yaml) + persistence pass
#                      + extension of the answers cut by the token budget (run study-ollama-2027-ext)
#   make horizons      weight-anchored knowledge horizons from the Hugging Face hub (cached)
#   make templates     the prompt each pilot model's server renders (data/evidence/chat_templates.json)
#   make results       classify every run in RUNS (and its extension run), then analyze -> results/
#                      (RUNS: the study by default; RUNS="$(PILOT_RUNS)" for the pilot)
#   make lockcheck     lock files written by cargo, npm and uv for the answers (data/evidence/);
#                      LOCKREUSE=<earlier lockcheck.json> keeps the records of unchanged manifests
#   make audit         dates behind the post-horizon matches (data/evidence/)
#   make replay        squats on real installers against registries on 127.0.0.1 (harmless packages)
#   make mask          pseudonymised copies of the classified runs (review artifact)
#   make check-masked  re-run the analysis from the pseudonymised files and compare every number
#   make validation-sample / validation-score   manual validation of extraction (two annotators)
#   make paper         build paper/main.pdf from results/
#   make docx          Word version of the same build (after make paper)
#   make anon-artifact zip for double-blind review (see README)
#   make repo-export   REPO=<git work tree>: the checked review archive written into the repository
#   make git-snapshots in the repository: unpack the git clones stored as data/snapshots/git/*.tar
PY      ?= python3
H        = cd harness && $(PY) -m phantomdeps
PILOT_RUNS = pilot-cpu-2026-10 pilot-cpu-ext-2026-10 pilot-cpu-famcmp-2026-10
STUDY_RUNS = study-ollama-2027
RUNS    ?= $(STUDY_RUNS)
# extension runs (cut answers generated again with a larger budget) of the runs in RUNS
EXT_RUNS = $(foreach r,$(RUNS),$(if $(wildcard data/runs/$(r)-ext/generations.jsonl),$(r)-ext))
# every classified run: the review artifact ships them all
ALL_RUNS = $(PILOT_RUNS) $(STUDY_RUNS) $(foreach r,$(STUDY_RUNS),$(if $(wildcard data/runs/$(r)-ext/generations.jsonl),$(r)-ext))
GATED   ?= pilot-cpu-2026-10 pilot-cpu-ext-2026-10 $(STUDY_RUNS)
VAL_RUNS ?= $(STUDY_RUNS)
# the study's sample for the two annotators: seed and the earlier samples it leaves out (PROTOCOL.md,
# section 5); for the pilot: make validation-sample VAL_RUNS="pilot-cpu-2026-10 pilot-cpu-ext-2026-10"
# VAL_SEED=20240628 VAL_EXCLUDE=
VAL_SEED ?= 20240631
VAL_EXCLUDE ?= ../data/validation/study-ollama-2027_llm-prescreen ../data/validation/study-ollama-2027_llm-prescreen-2 \
               ../data/validation/study-ollama-2027_llm-prescreen-3
LOCKREUSE ?=
EXT_MODELS ?= qwen2.5-coder-7b-instruct llama-3.2-3b-instruct phi-3-mini-3.8b-instruct   # granite-3.3-8b-instruct: stopped, see config

.PHONY: test snapshots corpus pilot pilot-ext famcmp study study-kit study-followup-kit horizons templates results \
        lockcheck audit replay mask \
        check-masked validation-sample validation-score paper docx anon-artifact repo-export git-snapshots clean

test:
	cd harness && $(PY) -m unittest discover -s tests -v

snapshots:
	$(H) snapshots

corpus:
	$(H) corpus

pilot:
	$(H) generate ../config/run_pilot_cpu.yaml
	$(H) classify pilot-cpu-2026-10
	$(H) resample ../config/run_pilot_cpu.yaml

# one model at a time (2 vCPUs, 7 GB RAM); the run resumes where it stopped
pilot-ext:
	for m in $(EXT_MODELS); do ($(H) generate ../config/run_pilot_cpu_ext.yaml --models $$m) || exit 1; done
	$(H) classify pilot-cpu-ext-2026-10

famcmp:
	$(H) generate ../config/run_pilot_cpu_famcmp.yaml
	$(H) classify pilot-cpu-famcmp-2026-10

study:
	$(H) generate ../config/run_study_ollama.yaml
	$(H) classify study-ollama-2027
	$(H) resample ../config/run_study_ollama.yaml
	$(H) extend-plan ../config/run_study_ollama.yaml
	$(H) extend ../config/run_study_ollama.yaml --plan ../data/runs/study-ollama-2027/extend_plan.json
	$(H) classify study-ollama-2027
	$(H) classify study-ollama-2027-ext

horizons:
	$(PY) tools/model_horizons.py

# Windows kit for generating the study on the author's GPU machine (README, "Running the study")
study-kit:
	$(PY) tools/build_study_kit.py dist

# second session on that machine, with the plans written here (README, "Running the study", step 3)
study-followup-kit:
	$(PY) tools/build_study_kit.py dist --followup data/runs/study-ollama-2027

templates:
	$(PY) tools/record_templates.py

results:
	for r in $(RUNS) $(EXT_RUNS); do ($(H) classify $$r) || exit 1; done
	for r in $(patsubst %-ext,%,$(EXT_RUNS)); do $(PY) tools/check_extension.py $$r || exit 1; done
	$(H) analyze $(RUNS)

lockcheck:
	$(H).lockcheck $(GATED) $(if $(LOCKREUSE),--reuse $(abspath $(LOCKREUSE)))

audit:
	$(PY) tools/audit_posthorizon.py results/results.json

# the squat names come from the pilot's answers (PROTOCOL.md, change log 2026-10-28)
replay:
	$(PY) replay/run_replay.py --netns --run pilot-cpu-2026-10 pilot-cpu-ext-2026-10

mask:
	$(H) mask $(ALL_RUNS)

check-masked:
	$(PY) tools/check_reproduction.py $(RUNS)

validation-sample:
	$(H) validation-sample $(VAL_RUNS) --seed $(VAL_SEED) $(if $(strip $(VAL_EXCLUDE)),--exclude $(VAL_EXCLUDE))

validation-score:
	$(H) validation-score $(VAL_RUNS)

paper:
	cd paper && ./build.sh && pdfinfo main.pdf | grep Pages

# Needs pandoc, python-docx, lxml, Pillow, pdflatex with TikZ and pdftoppm (README, "Word version").
docx:
	mkdir -p dist
	$(PY) tools/build_docx.py dist/paper_draft.docx

# Times in UTC and no extra file attributes: the archive carries no time zone or user id of the build host.
# Left out: the pseudonym key, raw generations and declarations (they name registrable phantoms), the
# registry cache (its URLs name them too), annotation sheets (response texts) and the annotators'
# Russian guide (the English one ships), the authors' response letter to the pre-submission review
# and its filler, the replay's raw log,
# the Windows kit's launch files (Russian), the study host's own logs (user and host names), camera-ready files, earlier paper versions, third-party node modules, and the large git clones that
# REPRODUCE.md re-creates at the recorded commits.
anon-artifact: mask
	mkdir -p dist
	rm -f dist/phantomdeps-cps-anon.zip
	TZ=UTC zip -qrX dist/phantomdeps-cps-anon.zip . -x 'dist/*' '*/__pycache__/*' 'secrets/*' 'paper/history/*' \
	    'paper/*_camera_ready.tex' 'paper/*.aux' 'paper/*.log' 'paper/*.blg' 'paper/*.out' 'VERIFICATION.md' \
	    'paper/refs_*.json' 'data/runs/*/generations.jsonl' 'data/runs/*/deps.jsonl' 'data/runs/*/responses.jsonl' \
	    'data/runs/*/server_*.log' 'data/runs_*.log' 'data/validation/*/sample.jsonl' 'data/validation/*/labels_*' \
	    'data/validation/*/GUIDE_ru.md' 'tools/validation_guide_ru.md' 'tools/response_letter_template.md' 'tools/response_letter_v4_template.md' \
	    'tools/fill_letter.py' 'tools/package_dist.sh' 'tools/study_kit/*' 'data/study_host_logs/*' 'dist_letter/*' \
	    'results/results.json' 'data/cache/*' 'data/replay/*' 'replay/node_modules/*' \
	    'data/snapshots/git/rosdistro/*' 'data/snapshots/audit/*' \
	    'data/snapshots/git/*/.git/logs/*' '*/.git/ORIG_HEAD' '*/.git/FETCH_HEAD'
	$(PY) tools/check_anon.py dist/phantomdeps-cps-anon.zip

# The git repository holds exactly the checked archive (README, "Git repository"); authors' side,
# since the archive is built from the raw runs.
repo-export: anon-artifact
	@test -n "$(REPO)" || { echo "usage: make repo-export REPO=<git work tree>"; exit 2; }
	$(PY) tools/export_repo.py dist/phantomdeps-cps-anon.zip $(REPO)

# git cannot hold the nested clones, so the repository stores each as data/snapshots/git/<name>.tar
git-snapshots:
	cd data/snapshots/git && for t in *.tar; do [ -f "$$t" ] || continue; \
	    [ -d "$${t%.tar}/.git" ] || tar -xf "$$t" || exit 1; done

clean:
	rm -f paper/*.aux paper/*.log paper/*.bbl paper/*.blg paper/*.out
