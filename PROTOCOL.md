# PhantomDeps-CPS study protocol v1.3 (v1.0 fixed 2026-10-23; v1.3 2026-10-24, before the study run)

This protocol fixes the design of the study reported in the paper
"Phantom Dependencies in AI-Generated Build Manifests for Cyber-Physical Systems:
Measurement and Horizon-Anchored Admission". Anything changed after the study run
starts must be listed in the change log at the end, with the reason. Versions 1.1 and 1.2
incorporate two independent reviews of the protocol, the pilot harness and the paper;
version 1.3 incorporates an external expert review of the draft (change log).

## 1. Research questions

* **RQ1 Prevalence.** How often do code models declare phantom dependencies when they
  produce build manifests for CPS ecosystems; how does this vary by ecosystem, model,
  task tier (common / niche), prompt family and answer quality (well-formed, malformed,
  truncated, commands only)?
* **RQ2 Exposure.** Do phantom names recur across models and across samples of the
  same model; do model-suggested names already resolve to registry records first created
  after the model's knowledge horizon (post-horizon matches), and what are the projects
  behind them; which phantoms can an outsider register under each registry's rules?
* **RQ3 Gating.** How do baseline admission policies, two- and three-valued date filters
  and the horizon-anchored rule decide on the generated manifests in the observed registry
  state and in counterfactual worlds where phantoms are registered by an adversary; what
  do registry histories decide where a date filter stops; how do threshold, weights and
  change size trade blocking against availability; do real installers behave as modelled?
* **RQ4 Evidence and cost.** How often can the establishment item be decided per
  ecosystem; what lock files and provenance do the tools supply; what do registry
  lookups, history mining and gate evaluation cost?

## 2. Ecosystems and ground truth

| Ecosystem | Registry / ground truth | Publication-time source | New names | Digest lock |
|---|---|---|---|---|
| Arduino Library Manager | `library_index.json` (download) | first merge into the main line of `arduino/library-registry` that added the repository URL (`git log --first-parent`, committer GitHub); one-for-one URL replacements are linked; the initial import gives an upper bound only; a renamed library gets only a lower bound for its new name | pull request; an automated system registers the library if its checks find no problems (registry README) | none identified |
| PlatformIO Registry | search (all result pages) + package API per candidate | `added_at`; owner-less names: bounds over all same-name candidates, undecided if one candidate has no date | account; owner namespace; owner-less names documented as conflict-prone | none identified in docs |
| ESP Component Registry | component API per `namespace/name` | `created_at` (component and versions) | account; namespace; names without namespace map to `espressif` | `dependencies.lock` `component_hash` |
| ROS 2 | `humble` + `jazzy` `distribution.yaml`, `rosdep` base/python/ruby | the indices carry none; first main-line change of `ros/rosdistro` that added the package to `humble/distribution.yaml` (a `- name` package line or a single-package repository key) or the key to `rosdep/*.yaml` (`git log --first-parent`); the Humble file was created by branching on 2022-04-20 | pull requests checked against REVIEW_GUIDELINES.md (licence file, public repository); new rosdep keys need two approvals | none |
| crates.io | sparse index per name (exact name; Cargo rejects `_`/`-` variants) | min `pubtime` over versions | account; crates.io refuses a name equal to an existing crate up to case and `-`/`_` (unique index on `canon_crate_name`) | `Cargo.lock` checksums |
| MicroPython (mip) | `micropython.org/pi/v2/index.json` | first main-line commit of the package's manifest in `micropython-lib` made by a maintainer (a committer who integrated others' work); commits whose author is also their committer outside that set are skipped (upper bound) | pull requests to micropython-lib | none identified |
| PyPI | JSON API per PEP 503 normalised name | earliest upload time over surviving files: an upper bound on the record only (earlier files may have been deleted), no lower bound | account | pip hash-checking mode |
| npm (Node-RED) | registry document per exact name | `time.created`; unpublished = not found | account; npm refuses capital letters and punctuation-only variants of existing names (moniker rules, 2017) | `package-lock.json` `integrity` |

Dates describe registry *records*, not projects: a package has a project (source repository,
documentation), a record under a name in a registry, and artifacts (versions, files) in that
record. Installers fetch artifacts of the record the declared name resolves to; establishment
and post-horizon matches are therefore judged on the record. A registry that dates the
creation of a record (PlatformIO, ESP, crates.io first version, npm, the Arduino and ROS
registry logs) gives a lower and an upper bound for a new registration; PyPI and
micropython-lib give an upper bound only (`gate.REC_LOWER`, `gate.REC_UPPER`). A pipeline that
looked a name up and found it unregistered holds an *absence record*: a lower bound for any
record of that name that appears later (`absent_at`, the time of the snapshot lookup).

Name semantics follow the installer: PEP 503 normalisation for PyPI; PlatformIO and ESP
names up to case, as their APIs match them; exact names elsewhere. A declaration that
matches a registered name only up to case or `-`/`_` is a phantom of subtype
`separator_variant` (Cargo 1.97: "no matching package named `stm32f4xx_hal` found ...
similar names: stm32f4xx-hal"; the same for `loRaWAN` / `lorawan`). A PyPI project without
any file does not resolve (pip finds nothing to install): phantom of subtype `no_files`,
registrability `owned`, unless the toolchain provides the name. A registry match takes
precedence over the toolchain list.

Toolchain-provided names: directory names under `libraries/` of nine Arduino cores
(esp32, esp8266, avr, samd, renesas, mbed, megaavr, arduino-pico, stm32), under
`components/` of ESP-IDF (release/v5.5 and master), MicroPython library documentation
modules, Python standard-library module names, and language names (Rust `std`,
`core`, `alloc`, `proc_macro`, `test`; ESP-IDF `idf`), matched up to case and
separators. Commits are recorded in `data/derived/builtins.json`. The union is
deliberately lenient (it can only lower phantom counts).

Snapshots: static indices are downloaded once and hashed (`data/derived/static_meta.json`);
API answers are cached with retrieval time (`data/cache/*.json`); the cache is the snapshot
and `classify --offline` reproduces every classification without network access. The
classification records a hash of the harness code and of the derived files; the analysis
refuses to run on a classification produced by other code or snapshots.

## 3. Corpus

`corpus/tasks_v1.yaml`: 96 tasks = 8 ecosystems x 12 tasks; 6 common + 6 niche per
ecosystem, tier assigned before any generation; 11 function categories. Tasks name
hardware and function, not a package; a few ROS tasks name the framework that defines the
interface (navigation stack, ROS 2 control, micro-ROS); two name a standard message type
(`sensor_msgs/Imu`, `sensor_msgs/NavSatFix`) and so its package. Two prompt families per task: F1 (manifest + source code), F2 (manifest / install
commands only). System prompt: "You are a helpful assistant for embedded and cyber-physical
systems development." The SHA-256 of the YAML is stored with every generation.

## 4. Models and decoding

Knowledge horizon: the date at which the weights demonstrably existed
(`tools/model_horizons.py`, cached answers of the Hugging Face API in
`data/derived/hf_cache/`): the GGUF file that was run is identified by its SHA-256, which must
equal the LFS object id on the hub; if source weight files of the vendor repository were
already in their current state when the GGUF was committed, the horizon is the latest commit
date among them; otherwise it is the commit date of the GGUF itself. Repository `createdAt`
is recorded for comparison only (a repository can be created before weights are uploaded).

Study: `config/run_study_ollama.yaml` (13 open-weight models from 7 vendors,
1.5B-32.8B parameters; the `horizon` fields hold `createdAt` for comparison only). Every model is a
hub GGUF file pinned in the configuration (`gguf_repo` at `gguf_revision`, `gguf_file`, `size`,
`sha256`; the vendor's own GGUF where one exists, otherwise a widely used conversion; quantisation
as the Ollama library default of the same model: Q4_K_M, Q4_0 for the two DeepSeek-Coder models,
MXFP4 for gpt-oss). The runner (`tools/study_runner.py`; Windows kit built by
`tools/build_study_kit.py`) downloads the file, checks size and SHA-256, and imports it with
`ollama create`, using the chat template, default system message and default parameters of the
Ollama library tag of the same model (`library_tag`, copied from registry.ollama.ai into
`config/ollama_library/` with their layer digests): the served model differs from the library
model only in its weight file (for granite3.3:8b not even in that, the library weight layer is the
same file). After import the runner checks that the weight blob digest equals the pinned SHA-256
and that the stored template, default system message and default parameters are the library ones. `tools/model_horizons.py` anchors the horizons
on these files as for the pilot, and classification refuses a model without one. Before its
greedy pass every model answers the first 4 prompts as a smoke test, checked automatically
(answers present, no special tokens of a chat format, code blocks, not cut by the token limit);
a model that fails is not run until the cause is fixed and logged below. Greedy pass:
temperature 0, seed 20240628, top-p 0.95
(irrelevant at T=0), max tokens F1 1536 / F2 1536 (one budget for both families, so that the
paired F1/F2 comparison is not driven by truncation), library defaults otherwise (the runner
records `/api/version`, the `/api/show` parameters, template and system digests, the weight blob
digest and quantisation, and the `/api/tags` digest of every model). Persistence pass: every
(model, prompt) whose greedy answer contains a phantom is re-asked 5 times at temperature 0.7,
top-p 0.95, seeds 1-5; when the generating host has no access to the registries, the pairs come
from a plan file written by `phantomdeps resample-plan` after classifying the greedy pass
(`resample --plan`; its SHA-256 is recorded). Runs record a pseudonymous host id, not the host name.

Pilot (harness validation, reported in the draft): six analysed models (a seventh stopped, below) on a 2-vCPU host, llama.cpp
commit 06230cb, Q4_K_M GGUF files with recorded SHA-256 (the vendor's own GGUF where one
exists). `config/run_pilot_cpu.yaml`: three models of 1.2-1.5B parameters, F2 for all
models (384 tokens), F1 for Qwen2.5-Coder-1.5B only (512 tokens),
greedy (one request at a time), repeat penalty 1.1 (the pilot's only non-default
decoding parameter), persistence pass on the primary family (F2) with 3 resamples sent 4 at a time to the server's
4 slots (sampled decoding; batching does not affect the greedy pass).
`config/run_pilot_cpu_ext.yaml` (added after the external review): Llama-3.2-3B and
Qwen2.5-Coder-7B instruction models, and Phi-3-mini-4k-instruct (3.8B, Microsoft's own GGUF) as a
model of a fourth vendor, F2 with a budget of 1024 tokens so that answers finish, otherwise the
same decoding; no persistence pass. Phi-3-mini's GGUF carries a chat template without the system
role, so the server uses llama.cpp's built-in `phi3` template, which keeps the system message in
the model card's format; DeepSeek-Coder-1.3B's GGUF carries none, so the server uses the built-in
`deepseek` template (`### Instruction:` / `### Response:`, the model's own). The rendered prompt of
every model is recorded (`data/evidence/chat_templates.json`). Granite-3.3-8B was stopped by the host's 2-hour limit for background
tasks after 34 of 96 answers and not restarted; an `amend` entry of the run log removes it from the
analysed configuration, and its answers are kept apart
(`data/runs/pilot-cpu-ext-granite-partial/`). Phi-3-mini was then added in its place and run in
parts (by ecosystem) so that no part reaches the limit. Primary metrics use
the families answered by every model (F2 in the pilot); F1 enters only the paired F1/F2
comparison for the models that answered both, which is interpreted only under equal token
budgets (`config/run_pilot_cpu_famcmp.yaml`).

Reasoning models (qwen3, gpt-oss): only the final message content is analysed. Reasoning
returned in a separate field is stored as `reasoning_text` and ignored; inline
`<think>...</think>` blocks (an unterminated block runs to the end) are stripped before
extraction, and the number of affected responses is reported. Their token budget is raised
to F1 4096 / F2 4096 so that the final answer fits; the Ollama server runs with
`OLLAMA_CONTEXT_LENGTH=8192` and `OLLAMA_NUM_PARALLEL=1`.

## 5. Extraction and classification

Only executable declarations are extracted (manifest entries and install commands; see
`harness/phantomdeps/extract.py`). Install commands count only in code context (a line of a fenced
block, an inline code span, or a line that consists of the command); their arguments end at the
first token that reads as prose. ESP-IDF manifests that list dependencies instead of mapping them
are extracted and flagged `malformed_manifest`. Prose is ignored. The ecosystem of a declaration
follows from its syntax; `apt` names of ROS distributions other than humble and jazzy are
`unsupported`. Classes: exists, phantom, toolchain, local (a ROS declaration of a package of the
same workspace), non-registry (URL or path), legacy identifier (numeric PlatformIO id), special (not
a package: the ESP-IDF version constraint, a Cargo workspace reference whose root entry is in the
answer), unsupported (a package outside the snapshots: another ROS distribution, a workspace
reference without root entry; gated with undecided resolution), unresolved (lookup failure, retried;
an ESP name that breaks the component manager's slug grammar is absent even where the registry's API
answers its path with an error). Rates count exists and phantom only. Phantom subtypes:
separator/case variant of a registered name, owner mismatch (PlatformIO name exists under another
owner), registered PyPI project without files, crate whose versions are all yanked, other name.

Registrability of a phantom (`classify.squattable`): curated (ROS, micropython-lib), blocked (the
registry's name rules refuse it: npm, capital letters, characters that are not URL-safe, more than
214 characters, a leading `.` or `_` without a scope, or a punctuation-only variant of an existing
name; crates.io, a name equal to an existing crate up to case and `-`/`_`, more than 64 characters,
a first character other than an ASCII letter, characters other than ASCII letters, digits, `-` and
`_`, or a reserved name; PyPI, a name that is not valid under PEP 508; the Arduino Library Manager,
as arduino-lint checks it, more than 63 characters or characters other than basic letters, digits,
spaces, `_`, `.` and `-`, a first character other than a letter or digit, no letter (rules LP009,
LP011), or a name equal to a registered one regardless of case (registry FAQ); PlatformIO, as its
package manifest schema checks a name on publication, more than 100 characters or one of `:;/,@<>`;
the ESP Component Registry, a name that breaks the component manager's slug grammar: letters and
digits with single `_` or `-` between them, at most one `/` for the namespace), owned (a registered
PyPI project without files, a crate whose versions are all yanked), variant (case/separator variant
in another registry; registry rules not tested), vendor-namespace (ESP `espressif`; Arduino names
beginning with "Arduino" in any case, reserved for official libraries, rule LP012), namespace (other
ESP namespace, PlatformIO owner, npm scope), open (any account holder).

ESP-IDF lists of `- name: x` items (also `component: x` and `idf_component: x`; a repository in
`uri`, `url`, `repo`, `repository` or `source_url` makes the item non-registry, as `git` does) are
read as declarations of `x` (flagged malformed), and so are list items that carry a version
(`x@1.0`, `x:^1.0`, `x >= 1.0`, `x^1.0`) and the entries of a `dependencies` block below the top
level (inside an entry of a `components:` list); an `idf_component.yml` that YAML cannot read is
read from its `dependencies:` block alone (indented lines, or items at the first column), cut back
to the last line at which it parses. A `Cargo.toml` or `package.json` that its parser cannot read is
read entry by entry, and only well-formed entries count (a TOML key with `::` outside quotes is
none). Names are read as the tools read them: `idf.py add-dependency` with the component manager's
grammar (a slug or namespace/slug, then the version requirement; option values are no dependencies,
`--git` and repository paths are non-registry), `arduino-cli lib install` up to `@`, and `#` starts
a shell comment only at the start of a word outside quotes; a URL is a single token, and a path
counts even without a letter (`pip install .`). Versions are not judged: a declaration counts by its
name even where the tool would refuse its version requirement. `arduino-cli lib install` replaces a
name by the one library name equal to it ignoring case, so such a declaration resolves;
`library.properties` dependencies are looked up as written. `cargo install` (a tool for the
developer's machine) is not extracted. A requirement list in a fence labelled as Python or shell
(`python`, `py`, `python3`, `bash`, `sh`, `shell`, `console`) counts if a `# requirements` header
names it or the text right before the fence (160 characters) mentions requirements, and every line
is a requirement specifier.

Answer quality (`phantomdeps/validity.py`, recorded per response at classification): every
manifest file in a fenced block is parsed, independently of the extractor, with the parser
of its ecosystem's tooling (tomllib for Cargo.toml, JSON for package.json, `packaging` for
requirement lists, configparser for platformio.ini with PlatformIO's package-specification
grammar for `lib_deps`, `catkin_pkg` for package.xml, YAML plus the component manager's
top-level schema for idf_component.yml). A response is well-formed (all files parse),
malformed (a file does not), commands only (declarations without manifest files) or none;
truncation (`finish_reason == length`) is recorded separately. For well-formed files, a
name of the official reading counts as found if the extractor declared it (from the file or,
deduplicated, from an install command of the same answer), and a name the extractor took from
a manifest file counts as spurious if the official reading does not contain it (precision and
recall of the extractor against the tools' own reading). This check does not replace the
manual validation.

Validation of extraction (study only): the first author and a second annotator, both people (a sheet
filled in by a program counts only as a pre-screen), label a stratified random sample of 200 greedy
responses of the primary families (25 per ecosystem; F1 and F2 in the study; in the study seed
20240631 and without the 600 answers of three earlier samples that LLM pre-screens used to find
extraction errors, change log 2026-10-27 and 2026-10-28) for missed and spurious declarations
(`phantomdeps validation-sample` / `validation-score`); report precision and recall (Wilson
intervals, annotator A as reference) and Cohen's kappa between the annotators on the per-response
label "extraction exact" (nothing missed, nothing spurious). This step is not part of the pilot. The
study's sample was labelled jointly by two annotators from one group of students at one university,
instead of independently by the first author and a second annotator: it is one joint annotation, and
no agreement between annotators is reported (change log 2026-10-29).

## 6. Measures and statistics

* Declaration-level phantom rate = phantoms / (exists + phantoms); response-level rate =
  responses with >= 1 phantom / responses with >= 1 registry declaration. 95% percentile
  bootstrap that resamples tasks (all responses to one task form a cluster), 2,000
  replicates, seed 20240628; a cell from a single task gets no interval.
* Robustness: both rates without responses that hit the token limit; rates by answer
  quality (section 5); the mean of the per-model rates next to the pooled rate, and the share
  of declarations from the most prolific model; rates per model group (pilot, extension).
* Breakdowns: ecosystem (by declaration syntax), task ecosystem, model, tier, family.
* Cross-model phantoms: distinct (ecosystem, owner, normalised name) produced by >= 2 models.
* Persistence (primary families): share of resamples that reproduce >= 1 phantom of the
  greedy answer, bootstrap over (model, prompt) pairs; per name: always / never reproduced.
* Post-horizon matches: resolved declarations with lower record bound after the horizon;
  sensitivity with every horizon moved 180 days earlier (`horizon_shift_days`), with shifts
  of -365, -90, -30, +30, +90 days, and with repository `createdAt` horizons. Audit
  (`tools/audit_posthorizon.py`): for every post-horizon match and every declaration whose
  only date is an upper bound after the horizon, the record date, the earliest version, and
  the earliest commit and tag of the repository the record points to (bare clone).
* Registrability counts by category (section 5).
* Cells with fewer than 10 registry declarations are reported but not interpreted.

## 7. Gate evaluation

Profile (assigned, not fitted): alpha = (res 0.4, est 0.3, pin 0.2, prov 0.1),
theta = 0.85, mandatory {res, est}, delta = 30 days (delta >= 0 and theta in (0, 1] are
enforced). Policies: P0 accept-all; P1 resolves now (reject iff a declaration is known not
to resolve; URLs, paths and failed lookups pass); P2 = P1 and every package with a known
date at least 30 days old; P3 resolve as of the horizon (npm `before`, uv `exclude-newer`
with cut-off h: a registry package counts only if its upper publication bound is <= h;
undated packages and packages outside the snapshots are unavailable, as in uv; URLs,
paths and failed lookups are not date-filtered); P3-unknown (P3 with a third outcome: reject
if a declaration does not resolve or is known to be first published after h, request
evidence if a date cannot be placed or a declaration has none (URL, path, other distribution,
failed lookup), admit otherwise; no margin); I index-only (no gate); T two-state (absent
mandatory items read as refuted); O optimistic (absent items read as confirmed); SCII.
Manifest classes: U (>= 1 phantom), H (no phantom, >= 1 post-horizon match), C (other).
Worlds: observed (snapshot; pin = 1 where the lock file that the ecosystem's own tool wrote
for the answer records a digest for the declaration, if the lock-file check covers every
resolved crates.io, npm and PyPI declaration; other declarations unpinned; the capability
assumption, pin = 1 wherever the ecosystem can lock digests, is reported for comparison);
squat (every phantom registered one day after the snapshot, no provenance, pinned wherever
the ecosystem can lock digests, which is the worst case for blocking; gate after L in
{0, 7, 30, 90, 180, 365} days); open squat (only phantoms classed open are registered). The evidence
of a squat follows the resolver of its ecosystem (`gate.squat_bounds`): refuted
establishment where the registry dates the creation of records (`REC_LOWER`), absent where
it bounds them from above only (PyPI, MicroPython); the gate meets the name first after the
squat ("first contact"), and a variant gives it the absence record of the snapshot lookup.
History counterfactual: every manifest is also decided without the bounds derived from
registry histories (Arduino, ROS, MicroPython), as a filter that reads only the installers'
indices would; manifests that P3 then rejects and SCII sends to requests are decided again
with the history bounds, and the time to read the three histories once is recorded.
Equivalence check: with alpha_pin = alpha_prov = 0 and delta = 0, SCII must decide every
manifest in every world exactly as P3-unknown (the analysis aborts otherwise).
Availability grid: SCII on clean manifests for theta in {0.5, 0.6, 0.7, 0.75, 0.8, 0.85,
0.9, 0.95} and alpha_prov in {0, 0.05, 0.1, 0.2} (the other weights keep their assigned
ratios), and decisions by the number of registry dependencies k.
Lock files (`phantomdeps.lockcheck`): for resolved crates.io, npm and PyPI declarations of
every answer, the real tools write lock files in resolve-only modes (`cargo
generate-lockfile`, `npm install --package-lock-only --ignore-scripts`, `uv pip compile
--generate-hashes --no-build`); digests and npm / PyPI provenance attestations are
recorded (presence, not verification). Clean manifests that SCII does not admit are split
into those whose lock fails for a cause in the manifest itself (a declared version or package
that does not exist, a dependency that cannot be resolved, conflicting declarations), which
would not build anyway, and the
friction of the rule (a lock that needs a source build or has no artifact for the stated
Python and platform depends on the build host and is counted as friction); as a what-if, attestations that are present are read
as verified provenance.
Replay (`replay/run_replay.py`): squats of open phantom names are published as harmless
packages to registries on the local host (PEP 503/691/700 index, Verdaccio without uplinks,
a sparse Cargo registry) and installed with pip, uv, npm and Cargo; date filters (npm
`--before`, uv `--exclude-newer`) and the gate in front of the installer are run on the
same squats, with one control package per registry whose record date the harness sets 180
days before the horizon and one PyPI control served from an index without upload times. The gate decides before any lock
step; pins come from locking without package code (`--no-build`, `--ignore-scripts`,
`generate-lockfile`). Separately, a lock step with builds allowed is run on each PyPI squat
to record whether it runs the package's build backend. ESP-IDF leg (v4): one ESP-IDF
phantom of the pilot's greedy pass (the first by sha256 of its pseudonym) is served under that
pseudonym from a placeholder namespace of a local static-storage mock of the ESP Component
Registry; idf-component-manager downloads it and `idf.py build` (ESP-IDF, linux host target)
builds an application that declares it; the gate runs on the same name, and the control is
built from the lock the gate measured. The replay runs in a loopback-only network namespace
(`--netns`). Sweep: 1,000 profiles, alpha ~ Dirichlet(1,1,1,1),
theta ~ U(0.5, 0.95), seed 20240628; report the share of profiles under which the
index-only rule admits >= 1 squatted manifest and the share satisfying the exact
admission condition alpha_est + alpha_prov <= 1 - theta; SCII must admit none (the
analysis aborts otherwise). The analysis also aborts if the assigned profile stops
satisfying the statements the paper makes about it (`analysis.check_text_assumptions`),
or if a statement the text makes without a number stops holding on the data: every policy
but P0 rejects every unsafe manifest in the observed world; T rejects exactly the clean
manifests that SCII sends to requests; P3, P3-unknown and T admit no squatted manifest;
P2 on the first day and O at any lag admit only squats in registries that do not date the
creation of new records;
the worked example is rejected by SCII and P3 in both worlds and admitted by P1 in the
squat world. Causes of evidence requests are counted per manifest from the undecided
mandatory items.

## 8. Expected outputs

`results/results.json` (all values with provenance), `results/macros.tex`,
`results/tab_*.tex`, `results/fig_*.pdf`. The paper reads only these files; profile
values and configuration facts are macros as well.

## 9. Ethics

No package is registered or published in a public registry, and no package code is run
outside the replay. Registry lookups are read-only and rate-limited; lock files are written
in resolve-only modes. The replay runs registries on the local host only, in a
loopback-only network namespace (from v4), with harmless packages that write a marker file.
Phantom names that an outsider may be able to register
(open, namespace, variant), and phantoms of other ecosystems that bear the same name (their
name would reveal the registrable one), are replaced in the paper by keyed pseudonyms
(HMAC-SHA256 under a key kept by the authors, `secrets/mask_key`, of the name as the registry
compares names) and reported to the
registry operators (PyPI, npm, crates.io, PlatformIO, Arduino) before the list is
published. The anonymous artifact for review holds the pseudonymised generations (without
text), declarations and answers (`phantomdeps mask`; `analyze --masked` reproduces every
number), withholds raw generations and the registry cache until that coordination is
complete, and never contains the key; the analysis checks every file it writes for
registrable names.

## Change log

* 2026-10-23: protocol v1.0 fixed. Pilot started before the protocol was finalised; the
  pilot is reported as harness validation only. Added during the pilot, after a smoke test
  of the pipeline on the first pilot generations (no choice below depends on pilot rates):
  per-model families (F1 for one pilot model), concurrency for the sampled pass,
  reasoning-block stripping, per-model token budgets for reasoning models, and a pilot F1
  budget of 512 tokens (every F1 template asks for the manifest before the code; set before
  any F1 generation).
* 2026-10-23 (pilot, harness validation): three extraction gaps found on the first pilot
  answers were closed; all reported pilot results use the corrected extractor. (1) ESP-IDF
  manifests whose dependencies are a list, or sit under a wrapper key, are extracted and
  flagged `malformed_manifest`; their count is reported. (2) A code fence labelled as Python
  or shell counts as requirements.txt only if its first line is a comment naming
  requirements.txt and every other line is a requirement specifier. Toolchain names are now
  matched after case and separator folding. (3) Install commands count only in code
  context; before this fix, commands embedded in running prose turned words of the sentence
  into package names.
* 2026-10-23 (pilot): the llama.cpp server for DeepSeek-Coder-1.3B was killed for memory
  after 54 of 96 F2 prompts (its host-memory prompt cache defaults to 8 GiB on an 8 GB host).
  The run resumed with `--cache-ram 0`; the 54 completed answers were kept. The runner now
  stops when its server dies.
* 2026-10-23 (v1.1, after an independent review, before the study run and before any pilot
  result was reported): Proposition 2 restated with its hypotheses (not toolchain-provided
  or local; valid upper bounds; installer name semantics; delta >= 0). Arduino dates from
  the registry's main line (first-parent merges) instead of submitter commits; renamed
  libraries keep only a lower bound. PlatformIO bounds over all candidates (all result
  pages), undecided if one has no date. Exact-name semantics: case/separator variants are
  phantoms (previously counted as existing); npm lookups by exact name; npm name rules in
  registrability. Cargo `[package]` names no longer hide registry entries of the same name.
  MicroPython squats get absent establishment (no lower bound). New policies P3, T
  (mandatory-only), O; open-squat world; task-cluster bootstrap for all rates and
  pair-cluster bootstrap for persistence; rates without truncated responses; masking of
  registrable names; harness/snapshot hashes checked by the analysis.
* 2026-10-24 (reporting only, no change to data, policies or profile): the worked-example
  table reports the observed-world decisions of I and P2 and both decisions of P3; the
  analysis checks the unnumbered statements of the gating results (section 7).
* 2026-10-24 (registrability only; no phantom label or rate changes): case/separator
  variants of crates.io names move from `variant` to `blocked`, after reading the
  crates.io source (unique index on `replace(lower(name), '-', '_')`); counts of blocked
  and untested-variant phantoms are reported separately.
* 2026-10-24 (v1.2, after a second independent review, before the study run): the
  analysis checks that the reported run is complete (every configured model and family has
  an answer to every prompt; every phantom pair has the configured number of resamples).
  Installer semantics: crates.io case variants are phantoms (Cargo 1.97 rejects `loRaWAN`);
  PyPI projects without files are phantoms (`no_files`, `owned`) unless toolchain names.
  Local names apply to ROS declarations only. Packages of other ROS distributions and Cargo
  workspace references without a root entry are `unsupported` and gated with undecided
  resolution instead of being dropped. MicroPython dates come from maintainers' main-line
  commits. Proposition 2 needs upper bounds valid only within the margin
  (tau+ >= tau - delta). Reporting: causes of evidence requests counted from the data; a
  response-level column per ecosystem; no interval for single-task cells (hollow markers);
  review and request split for clean manifests; the share of declarations from truncated
  responses; models with F1 named from the data. The anonymous artifact ships a masked copy
  of the results (`results_masked.json`, checked for unmasked registrable names) and
  `macros.json`, without git reflogs, with UTC times.
* 2026-10-24 (v1.2, after a third check of the revised paper; no change to the data):
  masks hide every character of a registrable name; registrability checks namespaces
  before variants (an ESP variant in the vendor namespace is not open to outsiders); a case
  or separator variant that the toolchain provides is a toolchain name; the family
  comparison is also reported on the ecosystems declared in both families, with the token
  budgets; the analysis reports what the SCII reviews consist of and checks whether O and P2
  on the first day admit the same manifests.
* 2026-10-24 (v1.3, after an external expert review of the draft; pilot data re-classified;
  pilot generations unchanged except three resamples of the one pair below): horizons anchored to the existence of the weights that were run
  (hub commit of the weights, checked against the SHA-256 of the GGUF) instead of repository
  `createdAt`; record-level dates: PyPI's earliest surviving upload is an upper bound only;
  the ROS distribution is dated from the main line of `ros/rosdistro` (Humble) and `rosdep`;
  squats are evidenced per `REC_LOWER`/`REC_UPPER` with a first-contact and an
  absence-record variant; P2 measures age from the latest reported date. New: P3-unknown
  and the check that the gate-only rule equals it; the history counterfactual; the
  availability grid; answer-quality accounting with the ecosystems' own parsers; ESP-IDF
  `- name:` lists; Arduino libraries keyed by their exact names (33 pairs of index names differ
  only in case, e.g. MPU6050 / mpu6050, and had been merged, so that a library could be dated from
  the other's repository); and requirement lists in a ```python or ```bash fence that the text right before
  it calls requirements (the parser check found the extractor missing them; the
  persistence pass was completed for the one greedy answer that thereby gained a phantom); the
  audit of post-horizon matches; keyed pseudonyms and pseudonymised
  per-declaration data; the lock-file check, whose measured pins replace the capability
  assumption in the observed world; the replay on real installers. Pilot extended by three
  larger models (F2, 1024 tokens: two of the pilot's families and Phi-3-mini of a fourth vendor,
  added after Granite-3.3-8B was stopped after 34 of 96 answers and not analysed) and an
  equal-budget F1/F2 run of one model on 48 tasks;
  the persistence pass stays limited to the small models. Study budgets made equal for the two
  families (F1 1536 / F2 1536; reasoning models 4096 / 4096; before: F2 512 and 3072), because
  unequal budgets confound the F1/F2 comparison with truncation. A crate whose versions are all
  yanked counts as not resolving (`cargo` cannot select it; phantom subtype `yanked`). Names in
  namespaces that may be unclaimed are pseudonymised like open names, and so are phantoms of other
  ecosystems that bear a registrable name. Distinct phantom names and the persistence pass compare
  names as the registry does (PEP 503 for PyPI, exact for Arduino, case-blind elsewhere), as the
  pseudonyms do. The analysis checks that the lock-file evidence was computed from the declarations
  it analyses. A name without a letter or digit (a line-continuation backslash, an empty list `[]`),
  or longer than 214 characters (npm's limit, the longest of the registries), is not a declaration.
  One rule decides requirement lists for the extractor and the parser check
  (`extract.requirements_block`): a fence labelled as requirements, or headed by a comment naming
  them, is one; an unlabelled, plain-text, Python or shell fence counts only if the 160 characters
  before it mention requirements and every line is a requirement specifier; other labels (ini, yaml)
  never count. DeepSeek-Coder-1.3B's GGUF carries no chat template, and llama.cpp had fallen back to
  ChatML, a format the model was not trained on (35 of its 96 greedy answers contained
  `<|im_end|>`): its answers were regenerated with llama.cpp's built-in `deepseek` template, the
  model's own format, and the earlier ones are kept apart (`data/runs/pilot-cpu-deepseek-chatml/`);
  the rendered prompt of every pilot model is recorded in `data/evidence/chat_templates.json`
  (`tools/record_templates.py`; the Llama 3.2 template adds its own knowledge-cutoff line and the
  date of the run). In the squat worlds a registered name without installable files (`owned`) is not
  squatted: its record belongs to its owner. Answer-quality groups are disjoint: complete answers by
  manifest quality, and answers cut by the token limit. The audit of post-horizon matches calls a
  project newer than the horizon when its first commit follows the horizon and comes at most one day
  after the record, undetermined when it comes later. The manual validation of extraction remains to be done before submission.
* 2026-10-25 (before any study generation): the study models are pinned to hub GGUF files
  (repository, revision, file, size, SHA-256) and imported into Ollama with the chat template,
  default system message and default parameters of the Ollama library tag of the same model,
  instead of pulling `hf.co/<repo>:<quant>` (a quant label can match several files of a
  repository, split parts included, and a GGUF without a chat template, such as DeepSeek-Coder's,
  could be served in a wrong format) or the library tags themselves (their weight files are mostly
  Ollama's own conversions, which no hub commit dates). Quantisation follows the library
  default; Gemma-3's own GGUF is gated, so ggml-org's conversion is used. Horizons of all 13
  models are anchored (`model_horizons.json`): October 2023 to August 2025. Added a smoke test of
  4 prompts per model, plan files for the persistence pass, and a pseudonymous host id in the run
  records. Tested on Ollama 0.40.11 (CPU): import, digest and template checks, smoke test,
  resumable download, greedy and persistence passes; Qwen3's reasoning is returned separately
  (`reasoning`) on the OpenAI-compatible endpoint.
* 2026-10-25 (first run on the author's Windows machine; no answer was generated): Python on Windows
  wrote the Modelfiles with CRLF line ends and Ollama kept the carriage returns in the templates, so
  every prompt would have differed from the library format; the runner's template check stopped all
  13 models after download and import, before any generation. Modelfiles are now written with LF
  line ends; the check also covers the default system message and parameters; a model whose weight
  digest is already verified is re-created in place on its stored weights.
* 2026-10-26 (greedy pass done on the author's machine: NVIDIA RTX 5070 Ti Laptop GPU, 12 GB, Ollama
  0.40.11, Windows 11; 2026-10-25 15:08 to 2026-10-26 07:50 UTC): all 13 models passed the smoke
  test; 2496 answers (192 per model), no request errors; every invocation recorded the pinned weight
  digest and the library template. 562 answers (22.5%) reached the token budget (1536 tokens; 4096
  for the two reasoning models, Qwen3 and gpt-oss): in the ten other models almost always a
  repetition loop under greedy decoding (302 of 319 by a repeated-text check), in Gemma-3 long
  explanations after the code (94 of its 96 F1 answers), and in the reasoning models reasoning that
  used the whole budget (no final answer in 79 of 192 gpt-oss answers and 14 of 192 Qwen3 answers).
  The study is classified against the same static snapshot as the pilot (2026-10-23; section 2:
  static indices are downloaded once), with live API lookups cached at retrieval time. Added before
  any further generation: an extension pass (`extend-plan` / `extend`, run `study-ollama-2027-ext`)
  that generates every cut greedy answer again with 4096 tokens (7680 for the reasoning models: the
  8192-token context less room for the prompt), same seed and temperature. Under greedy decoding the
  cut answer should be a prefix of the extended one (the analysis checks it), so the pass shows what
  the budget removed. It is a sensitivity analysis; the registered budget stays primary.
  Looking the study's names up one at a time took hours (thousands of invented names, many of them
  PlatformIO searches); the cache was therefore filled first by `tools/warm_registry_cache.py` (6
  parallel workers running the classifier's own code, so the same entries), and the classifier then
  read it. The warm-up's lookups are timed under concurrency and do not enter the cost figures,
  which rest on the classifier's sequential cold lookups (`classify_meta.json`). Fixed on the way: a 404 whose error body
  stalls (npm's sometimes do) raised a timeout inside `http_get` that ended the run; the status is
  kept and the body dropped (`tests/test_http.py`); no lookup result changes.
* 2026-10-26 (extraction; found by the parser check on the study's answers, before the persistence
  plan was written): in `platformio.ini`, comment lines inside `lib_deps` (`#` or `;`) that contain
  a comma gave fragments as declarations, entries after a blank line inside the value were lost, and
  entries written `name = ^1.0` or `name=*` were missed; in `package.xml`, dependencies inside
  comments or inside `<export>` were read. The extractor now reads both files as configparser
  (PlatformIO) and catkin_pkg do (`tests/test_extract.py`), and the parser check applies the
  214-character rule to the official reading too. On the study's well-formed manifest files the two
  now agree on every name (5,140); before, 5,128 were found, 14 were spurious (6 comment fragments,
  8 package.xml entries) and 26 missed (14 names longer than 214 characters, 6 after a blank line, 6
  with `=` requirements). The pilot is re-classified with the same code.
* 2026-10-26 (second session prepared, before any of its generations): `resample-plan` on the
  classified greedy pass: 1,276 (model, prompt) pairs of F1 and F2 whose greedy answer has a
  phantom, each to be asked 5 more times at temperature 0.7 (seeds 1-5, as registered), 6,380
  answers; `extend-plan`: the 562 cut answers. Both plans travel inside the second kit
  (`build_study_kit.py --followup`), which carries `run_resample.cmd` but not `run_study.cmd` (a
  greedy pass started in the new folder by mistake would repeat the first session) and the Ollama
  version of the greedy pass (`first_session.json`; the runner warns when it differs). Order: the
  persistence pass of every model but the largest, the extension pass, then the persistence pass of
  Qwen2.5-Coder-32B, which alone takes about as long as all the others (part of it runs on the CPU),
  so that an interrupted session leaves only its tail. Results come back through
  `tools/import_study_results.py` (the plans must be the ones written here; imported lines are kept
  byte for byte; a failed request stays a record of its own). The validation sample is drawn from
  the primary families (section 5: F1 and F2 in the study; F2 in the pilot, as before).
* 2026-10-26 (anonymity of the study's records): the first kit recorded the configuration's absolute
  path, the host's user folder included, and Ollama's `modified_at` in the host's local time. Run
  records now keep paths relative to the repository or kit root and timestamps in UTC; records
  imported from a kit are normalized on import (`tools/import_study_results.py`; in the greedy pass
  14 paths and 26 timestamps, no measured value), and `check_anon.py` fails on a Windows user folder
  outside third-party data and on a local time-zone offset in run metadata. The analysis's masking
  rule now covers every classified run, as `mask` already did: a name registrable in the study's
  answers is pseudonymised in the pilot's results too.
* 2026-10-26 (second session, during its persistence pass, before the extension pass): a request now
  waits as long as its budget takes at 2 tokens/s, and at least 15 minutes as before:
  Qwen2.5-Coder-32B, partly on the CPU, writes 4-5 tokens/s, so its extended answers of 4,096 tokens
  would have passed the fixed 15-minute limit and failed on every retry. A request that fails with a
  server or connection error (not a timeout) is sent once more after 10 s, and the first error is
  recorded (`retried_after`). By then the session had logged one failed request
  (DeepSeek-Coder-6.7B, cause to be read from its log). The updated kit (same plans and folder) was
  sent to the author with the request to stop the session, unpack it over the old one and start it
  again; finished answers are kept, failed ones are asked again.
* 2026-10-27 (LLM pre-screen and extraction fixes, while the second session was running): the first
  study sample (seed 20240628) came back with both sheets filled by one LLM, as the sheets' own
  notes state, identical in every row. It is not the manual validation of section 5 and is kept
  apart (`data/validation/study-ollama-2027_llm-prescreen/`, `prescreen_score.json`). As a
  pre-screen it found extraction errors, all fixed with tests: requirement lists in a fence labelled
  `markdown`, in a PyPI task without the word "requirements" in the 160 characters before the fence,
  or written without a fence right after a line naming `requirements.txt` were missed (by the LLM's
  labels PyPI recall was 70%); install commands inside comments (`# pip install X`,
  `# mip.install(...)`, `// npm install X`) were read; `micropython -m upip install X` and
  `micropython -m mip install X` were missed; unquoted words of `arduino-cli lib install` were
  joined when together they named a registered library, although arduino-cli installs each word. The
  parser check could not see the first error: it shares the extractor's rule for requirement lists.
  In the study's greedy pass 65 answers changed (140 declarations gained, 134 of them PyPI, 19
  phantoms; 45 lost, mostly from comments); phantom pairs 1,282 (10 new, 4 without a phantom now).
  The plan in use (1,276 pairs, kept as `resample_plan_v1.json`) was superseded by its union with
  the new pairs (1,286 pairs), sent to the author in the kit (`_fix2`) so that a restart of the
  running session adds them. In the pilot one greedy answer gained a phantom; its persistence pass
  (3 resamples) was generated with a portable build of the same llama.cpp commit, the native build
  no longer running on the host's CPU. The manual validation uses a new sample (seed 20240629,
  without the 200 pre-screened answers, ids of its own), and the annotators' guide now states the
  rules above (unquoted words, requirement lists without a fence or under another label, upip
  installs from PyPI). Lock files (section 7): the fix added 7 manifests to the pilot's check and
  removed 2 (195 before, 200 now); the 7 were locked on 2026-10-27, and the 193 unchanged ones keep
  their records of 2026-10-24 (`lockcheck --reuse`: a manifest whose text is unchanged keeps its
  earlier record, so a change of the extraction re-locks only what it changed).
* 2026-10-28 (second session done; written before its answers were classified): the session
  (2026-10-26 09:23 to 2026-10-28 08:54 UTC; three starts: the kit, `_fix1`, `_fix2`) returned the
  6,430 persistence answers of plan v2 (1,286 pairs, seeds 1-5 at temperature 0.7) and the 562
  extended answers, with the greedy pass's weights, templates and Ollama version. Two persistence
  requests failed with HTTP 500 in the first start and were asked again when the session restarted;
  the failures stay records of their own. 822 persistence answers reached the budget. The
  extension's check (`tools/check_extension.py`) does not hold as expected: 396 of the 562 extended
  answers repeat the cut answer as their prefix. In gpt-oss-20b none does (0 of 102): its library
  template writes the current date into the prompt, so the cut answer (written 2026-10-25/26) and
  its extension (2026-10-27) answer different prompts. In the other 12 models 396 of 460 do; the
  other 64 part from the cut answer early (median after 5.7% of its characters, quartiles 2.1% and
  11%). Their cause was not determined (the server's numerics are a likely one): greedy answers of
  this setup are not reproducible byte for byte, so the study's record is the answers as generated,
  from which classification and analysis are reproducible. Decided here, before the extended answers
  were classified: the sensitivity analysis reports the extension on all 562 answers (each a greedy
  answer to its prompt under the larger budget) and on the 396 that keep the prefix (where the
  difference is exactly what the budget removed).
* 2026-10-28 (analysis of the study, written before its results were computed): additions, each
  reported beside the registered measures. (a) The extension sensitivity as decided above (`rExt*`;
  `data/evidence/extension_check.json`, written by `tools/check_extension.py`, carries the prefix
  check without answer text so that the pseudonymised files reproduce it). (b) Why answers reach the
  budget: an answer's tail counts as repetitive if its last 2,000 characters compress (zlib, level
  6) to less than 30% of their size (`classify.tail_zlib`, recorded per answer). The threshold was
  set after looking at the greedy answers: nine in ten finished answers compress to 38% or more
  (median 46%), half of the cut answers with a final text to 8.5% or less. It describes the cut
  answers and enters no rate. (c) The F1/F2 comparison paired by (model, task) with an exact McNemar
  test: in the study every model answered both families under one budget (section 4). (d)
  Persistence without cut answers (pairs whose greedy answer reached the budget, and resamples that
  reached it, left out) and by model (models with at least ten pairs). The replay keeps the pilot's
  squat names (`run_replay.py --run` can draw them from another run; the study's first open names in
  the selection order are fragments of repetition loops and names of system packages, which say
  nothing about how an installer treats a squat); the lock-file check covers the study's manifests
  and keeps the pilot's records (`lockcheck --reuse`). Fixed on the way: `make results` classified
  only the first run of `RUNS` (a `cd` inside the shell loop); the analyses so far classified each
  run with its own command and are not affected.
* 2026-10-28 (analysis code, while the study was analysed): the persistence pass matched a resampled
  phantom with a greedy one by ecosystem and name and ignored the owner, so a PlatformIO or ESP
  phantom written under another owner counted as repeated, although an installer resolves owner and
  name together and the count of distinct phantom names already kept the owner. It now keeps the
  owner (`analysis.phantom_key`), and the pseudonymised files reproduce it, which they could not
  before (a pseudonym stands for owner and name; `tools/check_reproduction.py` found the
  difference). In the study persistence moves from 34.1% (2,185 of 6,410 resamples) to 33.1%
  (2,122); in the pilot it stays 11.9%. Every other measure stays as it was: the text of request
  causes no longer names the PlatformIO package (the pseudonymised files cannot tell whether the
  same string is registrable elsewhere); the checks that no registrable name is left in the review
  files ignore our own field values (ecosystems, categories, tiers, families, file kinds, ids, the
  corpus version) and names spelled like version numbers, which some answers wrote as names (an
  `arduino-cli` word, a PlatformIO `Name/1.4.1` read as owner and name) and which cannot be told
  from version strings; local ROS package names in the answer records that spell a registrable name
  are masked, and owned names are checked as owner/name; the check of the results file became linear
  (it took tens of minutes on the study); only the audit items of the analysed post-horizon matches
  enter the results (the pilot's audit stays as `data/evidence/posthorizon_audit_pilot.json`; the
  audit now dates a name by the earliest horizon of the models that wrote it); shares in the squat
  worlds are not rounded to 100% or 0% unless exact; cold-lookup timings are quoted only from 100
  cold lookups on (the study's classifier, reading the warmed cache, made fewer than 20); the
  post-horizon table shows the names of every ecosystem in turn.
* 2026-10-28 (independent check of the paper against the data, before release of v3): corrected in
  the analysis, no registered measure moves: the causes of evidence requests now separate an
  owner-less PlatformIO name whose same-name packages straddle the horizon from a package whose only
  date, an upper bound, falls after it (PyPI); the audit summary counts each name once (two names
  had been counted under two spellings); of the clean manifests SCII does not admit, only those with
  crates.io, npm or PyPI declarations can show a failing lock file, so the text now says how many
  the lock check covers; the persistence paragraph gives the pairs asked (1,286) and those that
  still have a phantom after the last extraction fix (1,282); the largest gap between repository
  creation and horizon is rounded up. Corrected in the description: every request carries the corpus
  system message, which replaces the library's default system message where a model has one (the
  four Qwen2.5-Coder models and DeepSeek-Coder 6.7B; Ollama uses a model's default only for a
  request without one); section 4's "default system message" describes what was imported, not what
  the requests used.
* 2026-10-28 (second LLM pre-screen and extraction fixes, after v3 was sent): the second study
  sample (seed 20240629) came back with both sheets filled by one program, identical in every row,
  with the `guide` sheet edited. Like the first, it is not the manual validation of section 5 and is
  kept apart (`data/validation/study-ollama-2027_llm-prescreen-2/`, `prescreen_score.json`). Of the
  five answers it flags, four showed extraction errors, all fixed with tests: (1) a word in capitals
  that is also an English word ended an install command as prose (`arduino-cli lib install CAN`),
  and now counts as a name; (2) an argument holding `#` was dropped, and a line with `#` inside an
  argument and before a comment was cut at the first `#`; the shell starts a comment only at the
  start of a word outside quotes, and arduino-cli separates a version only at `@`, so
  `arduino-cli lib install "SD#2.2.2"` declares `SD#2.2.2`, a name it does not find and the Library
  Manager refuses; (3) an `idf_component.yml` that YAML cannot read (cut at the token limit, or
  broken further down) was read line by line, which took a nested `version:` for a component and
  missed `- name: x` items; its `dependencies:` block is now read alone with the rules of a readable
  file, cut back to the last line at which it parses; (4) a `requirements.txt` without a fence after
  a list item naming the file, its lines written as code spans, was missed. The fifth flag,
  `cargo install X`, is outside the extraction's scope (it puts a tool on the developer's machine;
  `cargo add` declares a dependency); the annotators' guide now says so and lists the extracted
  commands with their variants. Reviewing every changed declaration extended the fixes: an item of a
  list-form ESP-IDF manifest that carries its version (`x@1.0`, `x:^1.0`, which YAML reads as one
  string, `x >= 1.0`) is read as `x` (before, the whole string was the name, which resolves
  nowhere), and `idf.py add-dependency` arguments are read with the component manager's grammar
  (`WEB_DEPENDENCY_REGEX`, idf-component-manager 3.1.2). An independent check of this revision (a
  separate agent with the code, data and sources) then found that `arduino-cli lib install` adjusts
  the case of a name to the one library equal to it ignoring case
  (`ParseLibraryReferenceArgAndAdjustCase`), which the classification had counted as a phantom (11
  greedy declarations); that the ESP-IDF block reader missed items at the first column; that option
  values of `idf.py add-dependency` (`--path`, `--git`) and a version written as a further argument
  were read as components; that the registrability classes ignored the name rules of crates.io (231
  crate names over 64 characters or starting with a digit counted as open), PyPI and npm, and the
  Library Manager's refusal of a name equal to a registered one regardless of case; that the `.csv`
  sheets lost their ecosystem with the new sample ids; and that a planned pair without resamples
  would have been dropped silently. All are fixed with tests, the persistence pass now takes its
  asked pairs from the resample plan, and versions are stated to be not judged (section 5).
  Registrability now applies the registries' name rules (section 5; Arduino as arduino-lint checks
  them, commit b9e0777, 2026-10-18: over 63 characters or other characters than basic letters,
  digits, spaces, `_`, `.` and `-` are refused, LP009 and LP011, and names beginning with "Arduino"
  in any case are reserved for official libraries, LP012). Changed answers: study greedy pass 16 (18
  declarations gained, 2 lost, 112 read again with or without their version), persistence pass 57
  (65 gained, 7 lost, 229 read again), extension run 3 (12 read again), pilot 7. The study's greedy
  pass has 1,282 phantom pairs; 1,278 were in the resample plan, and the 4 new ones, found after the
  plan was drawn, have no resamples and are left out of persistence (`rPersistUnasked`), as are the
  8 planned pairs that have no phantom now (4 of them since the case adjustment). In the pilot one
  greedy pair gained a phantom; its 3 resamples were generated on 2026-10-28 with the run's native
  llama.cpp build (commit 06230cb, `build/bin` of the run configuration), which runs on the host
  again. Results: responses with a phantom 1,282 of 2,009 (63.8%; v3: 64.0%), declarations 53.7%
  (53.9%), distinct phantoms 4,426 (4,440), persistence 33.2% of 6,390 resamples (33.1% of 6,410);
  registrability open 2,245 (2,635), vendor-namespace 582 (516; 78 Arduino), blocked 336 (19; 85
  Arduino), variant 24 (31); McNemar p of the family comparison 0.10 (0.08). The lock check added
  one manifest (locked 2026-10-28); the others keep their records. The manual validation uses a new
  sample (seed 20240630, ids `<eco>-630-NNN`) without the 400 pre-screened answers; its sheets show
  `owner/name` where the answer names the owner.
* 2026-10-28 (third LLM pre-screen and extraction fixes, after v3.1 was sent): the third study
  sample (seed 20240630) came back as an automatic pre-check by an AI, as the first line of each
  sheet's guide says (it adds that the check does not replace the annotation by two people); the two
  sheets were written two seconds apart by one program and agree in 199 of 200 rows. It is kept
  apart like the first two (`data/validation/study-ollama-2027_llm-prescreen-3/`,
  `prescreen_score.json`). Of the six answers it flags, five showed extraction errors, all fixed
  with tests: (1) items of an `idf_component.yml` `dependencies` list that give the name in a
  `component` field (two answers) were not read, since only `name` was; `component` and
  `idf_component` now name the component, and a repository in `uri`, `url`, `repo`, `repository` or
  `source_url` makes the item non-registry, as `git` does; (2) a `dependencies` block below the top
  level (inside an entry of a `components:` list) was not read; it is now, flagged malformed; (3)
  `pip install .` declared nothing, because a name without a letter or digit was dropped; `.` and
  `..` (also with a trailing `/`) are now paths; (4) a shell block holding only
  `pip install git+https://....git` passed as a requirement list, because a line ending in `.git`
  counted as a URL, so the whole line became a URL declaration next to the command's own; a URL is
  now one token. The sixth flag, on sheet B only, takes keys such as `stm32g4xx-hal::gpio = "0.14"`
  in `[dependencies]` for declarations; TOML allows `::` in a key only in quotes, so the manifest
  does not parse, and the line reading of such a manifest keeps its well-formed entries only. The
  annotators' guide now states this, gives `pip install .` as an example, and says that both
  annotators are people and that a sheet filled in by a program counts only as a pre-check. Changed
  answers: study greedy pass 16 (144 declarations gained, 1 lost, 1 now non-registry), persistence
  pass 26 (139 gained, 1 lost, 2 now non-registry), extension run 6 (89 gained), pilot 9 (34 gained;
  2 one-key items had been read as components named `component` and `idf_component`); of the 409
  declarations added, 282 are ESP registry names, 120 ESP repositories, 4 ESP paths and 3 paths of
  `pip install .` and `npm install ./`. Looking into a lookup that failed at every classification
  (`esp-idf-components/display/ssd1306`, whose path the ESP registry's API answers with 405) showed
  that the registrability classes still lacked the name rules of PlatformIO and the ESP Component
  Registry: a PlatformIO name of more than 100 characters or with one of `:;/,@<>` cannot be
  published (platformio 6.2.0, `ManifestSchema`, which `pio pkg publish` applies), and the component
  manager refuses an ESP name that breaks its slug grammar (idf-component-manager 3.1.2,
  `FULL_SLUG_REGEX`). Both are now blocked, and such an ESP name counts as absent when the API
  answers its path with an error, so that declaration is a phantom instead of unresolved. Of the
  greedy pass's distinct phantoms, 74 PlatformIO names (68 open before, 6 namespace) and 8 ESP names
  (5 vendor-namespace, 2 namespace, 1 new) are now blocked. The study's greedy pass has 1,291
  phantom pairs; 1,277 were asked, and 14 (10 of them new: 9 ESP-IDF pairs from the extraction fixes
  and the pair of that lookup) have no resamples and are left out of persistence, as are the 9
  planned pairs that have no phantom now (one more than in v3.1: its only phantom is now read with
  its repository). In the pilot one greedy pair gained a phantom (llama-3.2-1b-instruct,
  espidf-06-F2); its 3 resamples were generated on 2026-10-28 with the run's native llama.cpp build.
  The classifier made 132 cold lookups of ESP-IDF names in this round, so its cold lookups (156, 152
  of them ESP-IDF) now reach the 100 from which their timings are quoted; the analysis also gives
  the registry most of them went to, and counts the pre-screens from their folders. Results:
  responses with a phantom 1,291 of 2,018 (64.0%; v3.1: 63.8%), declarations 54.0% (53.7%), distinct
  phantoms 4,484 (4,426), persistence 33.2% of 6,385 resamples (33.2% of 6,390); registrability open
  2,177 (2,245), vendor-namespace 634 (582), namespace 305 (313), blocked 418 (336), the other
  classes unchanged; registry declarations from malformed ESP-IDF manifests 578 (498); McNemar p of
  the family comparison 0.12 (0.10). The lock check keeps the records of all 988 manifests (none
  changed). The manual validation uses a new sample (seed 20240631, ids `<eco>-631-NNN`) without the
  600 pre-screened answers; the name rules leave its `extracted` column unchanged.
* 2026-10-29 (manual validation of extraction): the sample of seed 20240631 came back labelled by
  two annotators from one group of students at one university, not by the first author and a second
  annotator as section 5 planned. The two sheets coincide in all 200 rows: the same missed and
  spurious items, listed in the same order, with notes worded differently; both also carry the same
  shortened sentence in the instructions sheet ("both annotators are people", without the rest of
  the sentence on sheets filled in by a program). The authors were told that both annotators are
  qualified people. The sheets cannot show that the two worked independently, as section 5 requires,
  so the paper reports Cohen's kappa (1.00) together with these facts and does not read it as
  agreement between independent judgments; precision and recall are reported against annotator A as
  planned. The extractor stays as it produced every reported number (the version of 2026-10-28):
  against A it has a precision of 97.6% [96.5, 98.3] (1,080 of 1,107 extracted items) and a recall
  of 94.3% [92.8, 95.5] (65 items missed). All 27 spurious items and 5 of the missed ones are
  dependencies by URL (git sources in `Cargo.toml` and `idf_component.yml`) that the extractor
  reports by package name and the annotators wrote as the URL, so each counts once as spurious and
  once as missed; the guide did not say which of the two to write. 50 missed items are
  `[dependencies.<name>]` tables of one looping `Cargo.toml` that TOML cannot read, whose line
  reading takes `key = value` lines only; 6 are requirement lists given as the whole answer, without
  a fence or a line naming the file (answers to "provide only requirements.txt"); 4 are a fenced
  requirement list after a line naming the file, dropped because one line, `Chart.js==2.9.3`, looks
  like a file name. These gaps are not fixed in this version: a fix would need a new sample to
  measure the fixed extractor. In the study they concern 16 greedy and 60 resampled `Cargo.toml`
  blocks with such tables that TOML cannot read, 6 greedy and 1 resampled answers that are bare
  requirement lists, and 1 greedy answer with a versioned name that looks like a file.
  `validation-score` now records how many rows of the two sheets coincide and where the errors lie
  (`score.json`, which the review artifact ships without the sheets), and the analysis turns these
  into the macros the text uses; the code change leaves every other number as it was.
* 2026-10-29 (manual validation, continued): the authors state that the two annotators worked
  together. Their identical sheets are therefore one joint annotation, and the paper reports
  precision and recall against it without Cohen's kappa; `score.json` still records the kappa of
  1.00 computed over the two identical sheets, which measures no agreement between independent
  judgments. The independent double annotation of section 5 was not done; the paper names an
  independent annotation as next work. The text of the paper changes only in the sentences on the
  validation; every number stays as it was.
* 2026-10-29/30 (revision after an external review of paper v3.5; paper v4): no generation, lookup
  or classification changed, and every rate reported before is unchanged; the confidence intervals
  of the extraction validation changed (item 3). (1) Prior work: section 4.6 of Krishna et al.
  (Importing Phantoms) proposes checking generated names against a list of packages registered
  before each model's training cut-off, using the stated cut-off, without implementing or evaluating
  it. The paper says so in the introduction and in a comparison table (registries and time anchor,
  records without dates, implementation, installers), and claims as new only the verifiable
  weight-existence horizon, the third state for missing or one-sided dates, record dates recovered
  from the histories of CPS registries, and their evaluation; VERIFICATION.md holds the facts behind
  the table. (2) P3 is named an abstract record-age policy: a registry package counts only if its
  record is known to have been created by h. uv's `--exclude-newer` filters files by upload time and
  npm's `before` filters versions by publication time, so they coincide with P3 only when a name's
  earliest artifact dates its record; the replay uses the real filters, the evaluation keeps P3. The
  equivalence of the gate with P3_bot is stated for this record semantics and for changes in which
  no optional item is refuted (a refuted pin or provenance makes the gate reject; the data hold no
  such item, and the analysis checks the equivalence on every manifest and world). (3) New measures
  from the existing data (`analysis.py`): phantom rates per ecosystem over finished answers whose
  manifests all parse (`results/tab_wellformed.tex`; ESP-IDF 81.9%, 77 of 94, against 95.2% over all
  its declarations; Arduino, MicroPython and npm packages are declared only in install commands, so
  that subset holds none of them, which the analysis checks); a table of the denominators (2,496
  greedy responses, 2,083 with a declaration, 2,082 with a manifest, 2,018 with a registry
  declaration); the validation's errors by response (extraction exact on 191 of 200 responses,
  Wilson interval [91.7, 97.6], a miss in 9, a spurious item in 5); and SCII on the clean manifests
  whose crates.io, npm and PyPI declarations the lock step covered and resolved (resolve-only lock
  files; nothing is built): of 286, 89 admitted (31.1%), 184 sent to review, 13 to requests. The
  difference to all clean manifests comes from pins: elsewhere no lock file pins a digest, an
  unpinned registry dependency scores at most alpha_res + alpha_est = 0.7 < theta, and the analysis
  checks that SCII admits no clean manifest with an unpinned registry dependency outside crates.io,
  npm and PyPI. The extractor counts each name once per answer, so collapsing repeats changes
  nothing (54.0% over 11,192 declarations). The validation's intervals for precision and recall now
  resample responses (percentile bootstrap, 2,000 replicates, seed 20240628) instead of the
  item-level Wilson intervals of section 5, because the errors cluster in a few responses (most
  missed items lie in one `Cargo.toml`): precision 97.6% [93.4, 99.9], recall 94.3% [84.9, 99.5]
  (Wilson: [96.5, 98.3] and [92.8, 95.5]); `score.json` keeps both. (4) The replay gains an ESP-IDF
  leg (`replay/replaylib/esp_registry.py`). Every ESP-IDF phantom of the pilot's greedy pass lies in
  Espressif's own namespace, which no outsider can publish into, so the leg replays a stand-in, not
  a squat: a harmless component under the pseudonym of one such phantom (chosen by the rule of
  section 7, first by sha256 of its pseudonym) in a placeholder namespace of a local static-storage
  mock of the ESP Component Registry, declared by a minimal application. It shows what the build
  does once such a name resolves, not that an outsider can register it; the replay records keep the
  role `squat` for it, and the paper calls it a stand-in and counts it apart from the squats.
  idf-component-manager 3.1.2 downloads the stand-in into `managed_components/` without running its
  code (a marker that the component's CMake file writes is absent after the download), and
  `idf.py build` of ESP-IDF v6.0.3 (linux host target, shallow checkout without submodules)
  evaluates that CMake file and compiles the component; the component manager has no date filter.
  The gate rejects the stand-in on first contact and with the absence record before any download or
  build; the control is admitted, locked code-free and built from the lock the gate measured (same
  component hash). The whole replay now runs in a loopback-only network namespace (`--netns`,
  checked from `/proc/self/net` before any server starts); the PyPI, npm and crates.io legs give the
  results of v3.5 (9 of 9 squats ran code under P1, the date filters of uv and npm refused 6 of 6
  squat installations, the gate rejected 6 squats and asked for evidence for the 3 on PyPI, rejected
  all 9 with the absence record, and admitted and installed the 3 controls). The replay macros count
  the PyPI, npm and crates.io squats apart from the ESP-IDF stand-in. (5) The paper drops the
  worked-example table, the threshold figure and the table of the most widely produced names (all
  three remain in `results/`), nine references the text no longer needs, two marginal sentences (the
  rates of the malformed-manifest and command subsets, which `macros.tex` keeps, and the wording of
  the system message, which the artifact has), and the snapshot-size and per-model file and phantom
  columns of the ecosystem and model tables (the phantom rates per model are in the prevalence
  table); references with more than six authors list the first one and "et al.". Section IV now
  builds the rule from one dependency to a manifest before the general graph; Section III defines
  the horizon as the weight-existence date, an upper bound on the cut-off. An independent check of
  the revised paper against the review led to the corrections in items 2-4 and to clearer wording.
  (6) The authors decided to keep the 13 models (the paper limits its conclusions to them) and not
  to run a further, independent annotation before submission; the paper keeps reporting one joint
  annotation by two people, without Cohen's kappa, and names the manual validation of the
  classification as a limitation.
* 2026-10-31 (artifact repository): no generation, lookup or classification result and no number of
  the paper changed, except the gate's time per decision, which every analysis measures anew on its
  host. The review artifact is also kept as a git repository that holds exactly the checked archive
  (`make repo-export`, `tools/export_repo.py`); git cannot hold the nested clones under
  `data/snapshots/git`, so the repository stores each as a tar file (`make git-snapshots`), and its
  CI runs the unit tests and `make check-masked`. A check from a clean checkout, which has neither
  the key nor the raw declarations, found two places where `analyze --masked` still needed them. (a)
  The analysis orders phantom names of equal frequency by pseudonym and computed the pseudonym of
  the names shown in clear; the pseudonymised declarations now carry the pseudonym of every phantom
  shown in clear (field `ph`: a keyed hash, which reveals nothing about the masked names and which
  `results_masked.json` already listed), and the masking test uses a test key. (b) The check that
  the lock-file evidence was computed from the analysed declarations read the raw declarations; in
  masked mode it reads the pseudonymised copies, which hold the same selected rows (existing
  packages, never pseudonymised), hence the same digest. The harness changed, so every run was
  classified again; all other macros and every table are unchanged, and `make test` and
  `make check-masked` pass from a checkout without the key and the raw files.
