# Phantom-dependency replay (real installers, isolated local registries)

This directory runs the "resolves-now admits a phantom" argument of the paper as
a handful of **reproducible scenarios on real installers**, against **isolated
test registries on `127.0.0.1`**, with **harmless packages**. It is the artifact
for the reviewer request *"show a few reproducible scenarios on REAL installers
in an ISOLATED test registry with HARMLESS packages."*

```sh
python3 replay/run_replay.py --netns      # the recorded run (loopback-only network namespace)
```

One command brings up a local registry per ecosystem, publishes a harmless squat
for each chosen phantom name (and one harmless **control** per ecosystem, plus a
PyPI "dateless control" served from an HTML-only index), runs the real installers
under each policy, evaluates the SCII gate (with a measured pin), tears every
server down, and writes two files. Controls are **not** pre-existing packages:
they are published during the replay with a record date the harness sets
`control_backdate_days` (180) before the horizon (`record_date_set_by_harness`),
never "published before the horizon".

**Timestamp ordering is real and asserted.** Per squat the harness does the
absence lookup *first*, then builds and publishes, and the registry stamps the
publish instant — so `absence_time < publication_time` for every squat (recorded
at microsecond precision). `t_gate` is taken *after* an ecosystem's squats are
published, so `publication_time ≤ t_gate` and P2's age is never negative. Both
orderings are asserted in code (the run aborts on violation) and summarised in
`ordering_check`.

## What a "squat" package does here

Each published package is **harmless**: its only side effect is writing a fixed
marker string (`replay-marker-ok`) into the directory named by the
`REPLAY_MARKER_DIR` environment variable. Finding that marker after an install is
the proof that *the installer executed the package's own build/install code* —
i.e. that a resolves-now check admitted attacker-controlled code. The packages do
no network or file access of any other kind.

* **PyPI** — a **squat** is an sdist built by an in-tree PEP 517 backend
  (`pyproject.toml` with `backend-path`, `requires = []`,
  `dynamic = ["dependencies"]` so resolving it *must* call the backend). The
  backend's `build_wheel` writes the install marker; its metadata hooks write a
  second, distinct marker (the resolve-time execution vector). An in-tree backend
  is used instead of a classic `setup.py` *on purpose*: a fresh venv has neither
  `setuptools` nor `wheel`, so a `setup.py` sdist would make `pip`/`uv` fetch a
  build backend from an index — which must not happen when the only index is our
  local one. The **controls** (harness-set record date 180 days before the
  horizon) are additionally published as an **inert wheel** (built by the harness,
  runs no code at install or resolve) so the pin lock can read them with
  `--no-build`; squats stay sdist-only.
* **npm** — a `postinstall` script (`node postinstall.js`) writes the marker.
  Scripts run on the default `npm install`.
* **crates** — a `build.rs` writes the marker when the crate is compiled as a
  dependency of a tiny consumer binary (`cargo build -j1`).

## Registries (all `127.0.0.1`, no uplinks, torn down on exit)

* **PyPI** — an in-process PEP 503 (HTML) + PEP 691 (JSON) "simple" index. The
  JSON project page carries PEP 700 `upload-time` for every file so that
  `uv pip install --exclude-newer H` can date-filter.
* **npm** — an isolated **Verdaccio** (`node_modules/.bin/verdaccio`, pinned
  `6.10.7`) with **no uplinks** (`uplinks: {}`), storage in a throwaway temp dir,
  a throwaway `--userconfig` `.npmrc` (the real npm config is never touched), and
  `packages.'**'` open so the harness can publish without registering a user.
* **crates** — an in-process **sparse** registry (`config.json` with a `dl`
  template, one newline-JSON index entry per crate with `cksum` = sha256 of the
  `.crate`, plus a `pubtime` field). A throwaway `CARGO_HOME` replaces
  `crates-io` with this local source, so cargo never contacts the real
  crates.io.

Verdaccio is the only third-party code kept on disk (`replay/node_modules/`); it
is installed once with `npm install` in this directory.

## ESP Component Registry (CPS): a stand-in, downloaded and built

One ESP-IDF phantom of the pilot is replayed as a **stand-in**, not a squat: every
ESP-IDF phantom of the pilot's greedy pass lies in Espressif's own namespace, which no
outsider can publish into. The stand-in is a harmless component under the phantom's
keyed pseudonym (`ph-…`, which never resolves on the real registry) in a placeholder
namespace (`replaylocal`), declared by a minimal application (not the model's
manifest), served from an in-process **static-storage** mock of the ESP Component
Registry (`replaylib/esp_registry.py`: `components/<ns>/<name>.json`, the `.tgz` and
its `CHECKSUMS.json`), with one control dated 180 days before `h`. It shows what the
build does once such a name resolves, whoever published it.

* **Download.** The real `idf-component-manager` (3.1.2) resolves and downloads
  the component into `managed_components/`. This runs **no component code**: the
  packed marker file is materialised (`marker`), and the component's CMake marker
  is absent (`code_ran = false`).
* **Build.** The real ESP-IDF build system builds a minimal application whose
  `main/idf_component.yml` declares the component: `idf.py --preview build` for
  ESP-IDF's **linux host target** (host gcc, no cross toolchain). The build calls
  the component manager at configure time, which downloads the component from the
  mock; CMake evaluates the component's `CMakeLists.txt`, which writes the
  harmless marker `ESP_COMPONENT_CMAKE_RAN` into `$REPLAY_MARKER_DIR`
  (`cmake_marker`), and compiles its source into `lib<ns>__<name>.a`
  (`component_lib`). The application is not flashed or run.
* **Policies.** P1 downloads and builds the squat. P3 is `not_available` (the
  component manager has no date filter). Under SCII the record date bounds the
  record from both sides, so the squat is rejected before any download or build;
  the control is admitted, locked code-free (version solver and lock dump, no
  download) and built from that lock file (`pin_matches_build`: the hash in the
  build's lock equals the measured pin).

Setup (once, outside the replay; the paths are examples):

```sh
git clone --depth 1 --branch v6.0.3 https://github.com/espressif/esp-idf.git esp/esp-idf-v6.0.3
python3 -m venv esp/venv && mkdir -p esp/idf_tools
curl -o esp/idf_tools/espidf.constraints.v6.0.txt \
  https://dl.espressif.com/dl/esp-idf/espidf.constraints.v6.0.txt
esp/venv/bin/pip install -r <(sed 's/^idf-component-manager.*/idf-component-manager==3.1.2/' \
  esp/esp-idf-v6.0.3/tools/requirements/requirements.core.txt) -c esp/idf_tools/espidf.constraints.v6.0.txt
apt-get install libbsd-dev            # the linux target links libbsd
export REPLAY_ESP_IDF=$PWD/esp/esp-idf-v6.0.3   # venv and idf_tools are found next to it
```

The checkout has no git submodules; the build sets `IDF_SKIP_CHECK_SUBMODULES=1`
because the linux-target application needs none of them. Without
`REPLAY_ESP_IDF` the build step is recorded `not_available` and everything else
runs. `python3 replay/test_esp_replay.py` is a standalone smoke test of this leg.

## Network isolation (`--netns`)

With `--netns` the script re-executes itself under `unshare -n`, brings `lo` up,
and checks from `/proc/self/net` that loopback is the only interface and that no
route leaves it, before any server starts (`network_isolation` in the output). In
that namespace an installer or build step cannot reach a real registry whatever it
tries; the recorded run uses it.

## Policies run per (ecosystem, name)

| Policy | Meaning | How |
|---|---|---|
| **P1** | resolves-now (installer default) | `pip install`, `uv pip install`, `npm install`, `cargo build` |
| **P3** | as-of the horizon `h`: the installers' own date filters (the paper's P3 is the record-age policy; see PROTOCOL.md, section 7) | `uv pip install --exclude-newer H`, `npm install --before H`; **pip, cargo and the ESP-IDF component manager have no such option → recorded `not_available`** |
| **P2** | age ≥ 30 days | a **computed decision** from the reported publication time (no clock faking); no install |
| **SCII** | horizon-anchored gate in front of the installer | evidence is built from what the local registry reports; the installer runs **only if the decision is `admit`** |

**Pipeline order (important).** Resolving an **sdist-only** package with `uv`
*without* `--no-build` runs the package's own build backend
(`get_requires_for_build_wheel` / `prepare_metadata_for_build_wheel`) — that is
package-authored code executing during "resolve-only" locking, before the gate
has decided. A pipeline must not run unvetted package code to collect evidence.
So SCII here decides the gate items **first**:

1. `res = 1` if the name resolves; `est` from publication-time bounds vs `h`
   (`gate.est_state`), using the absence record where applicable; `prov = None`.
   This fixes `G` (pin is non-mandatory and cannot change `G`).
2. **Only if the gate passes (`G = 1`)** run the pin lock step; a squat
   (`G = 0` or `G = ⊥`) never reaches it (`lock_step_run = false`).
3. Decide with the measured pin; install **only on `admit`**.

The pin lock step is **code-free**:

* **npm** — `npm install --package-lock-only --ignore-scripts` → `pin = 1` iff
  `package-lock.json` records an `integrity`.
* **crates** — `cargo generate-lockfile` → `pin = 1` iff `Cargo.lock` records a
  `checksum`.
* **PyPI** — `uv pip compile --generate-hashes --no-build`. The controls
  (harness-set record date) are published as an **inert wheel + the sdist**, so
  `--no-build` resolves from the wheel and records a `--hash=sha256:` **without
  building** (squats stay sdist-only). Each lock step records `marker_after_lock` and
  (PyPI) `meta_marker_after_lock`, both `false` throughout, proving no package
  code ran.

The SCII block reports the decision with the measured pin and keeps the
`pin = None` decision as `no_pin` for comparison. With `est = 1` and `pin = 1` the
backdated control reaches `R = 0.9 ≥ θ = 0.85` → **admit**, and the installer is
then actually run under SCII. `gate_time_s` (gate + any lock, excluding install)
and `install_time_s` are recorded per scenario; squats show `gate_time_s ≈ 0`
because the lock step is skipped.

Two bound models are reported for every squat:

* **first_contact** — the gate sees the name for the first time. npm `time.created`
  and the crates index `pubtime` bound the record's creation from *both* sides, so
  `tau_lo = tau_hi = that timestamp`; PyPI upload times bound it only from *above*,
  so `tau_lo = None`.
* **with_absence** — a *real* pre-publish lookup observed the name absent at
  `t_obs` (recorded), giving `tau_lo = t_obs`.

### PyPI "dateless control"

A fourth PyPI scenario models a control published during the replay with a
harness-set record date 180 days before `h`, served from a
**second local index that speaks PEP 503 HTML only** (no PEP 691 JSON, no
PEP 700 `upload-time`), as a static find-links dir or an older mirror would; the
PEP 691/700 index stays up as the **origin registry**. P1 (pip, uv) installs it;
P3 `uv pip install --exclude-newer H` **refuses it** (uv: "missing an upload date
... No solution found"). Under SCII first contact there is no date → `est = None`
→ **request**; the evidence step then does **one** lookup of the record date at
the origin index (PEP 691 `upload-time`, an upper bound) → `est = 1` → with the
measured pin → **admit**, and the installer runs against the dateless index. The
evidence-step cost (extra HTTP requests, seconds) and total gate time are
recorded next to the install time. This is the case *"requesting evidence lets a
build complete that the date filter stops."*

### `lock_execution` — the execution vector, measured explicitly

Separately from any policy, the run measures on **each squat** whether a
resolve-only lock step runs the package's own code. The PyPI squat's in-tree
backend writes a *second, distinct* marker from its metadata hooks
(`get_requires_for_build_wheel` / `prepare_metadata_for_build_wheel`), separate
from the install marker written by `build_wheel`. Then, outside any policy:

* PyPI, `uv pip compile --generate-hashes` **without** `--no-build` →
  `code_ran_during_lock = true` (the metadata-hook marker appears), `digest` found,
  install marker **not** written.
* PyPI, the same **with** `--no-build` → **refused** (an sdist-only squat has no
  wheel to read), no marker.
* npm `--package-lock-only --ignore-scripts` and cargo `generate-lockfile` →
  `code_ran_during_lock = false` (expected: no code).

This is the evidence that forces the design above: the gate runs before any lock
step, and the pin comes from code-free locking. Results are under the top-level
`lock_execution` of `data/evidence/replay.json` and summarised in `summary`.

## Outputs

* `data/replay/replay_raw.json` — full detail with **real names** (tool versions,
  per-scenario exact argv, exit codes, trimmed stdout/stderr, marker presence,
  timings, gate inputs/outputs, served registry timestamps). **Never ship this to
  reviewers.**
* `data/evidence/replay.json` — the same structure **masked**: every phantom and control
  name is the `phantomdeps.util.mask_key(eco, None, name)` pseudonym (`ph-…`),
  which normalises the name as the registry compares it (PEP 503 for PyPI,
  lower-case elsewhere) so each squat's pseudonym **equals the paper's
  per-declaration pseudonym** in `data/runs/*/deps_masked.jsonl`; stdout/stderr are
  dropped, and the file is verified to contain **no real name** (whole-token
  search; the run aborts if any remains). The run also asserts that every squat
  pseudonym occurs in some `deps_masked.jsonl` of `pilot-cpu-2026-10` /
  `pilot-cpu-ext-2026-10` (a run without that file is skipped and reported, under
  `pseudonym_crosscheck`).

  It carries a compact **`summary`** keyed per ecosystem and role
  (`squat` / `control` / `dateless_control`) for turning into LaTeX macros without
  parsing scenarios: `n`; P1 installed-with-marker per tool; P3 blocked / installed
  / not_available per tool; P2 reject count; SCII first-contact decision counts
  (measured pin), `SCII_with_absence`, `SCII_no_pin_first_contact`; installs
  executed under SCII; median gate and install time (s); the dateless control's
  evidence-step cost; a `lock_execution` block (per ecosystem: `code_ran_during_lock`
  and `digest` ratios; PyPI also `refused` under `--no-build`); and `tool_versions`.

## Requirements (versions found on this machine)

| Tool | Version |
|---|---|
| Python | 3.13.17 |
| pip | 24.0 |
| uv | 0.11.32 (`/root/.local/bin/uv`) |
| npm | 10.9.4 (`/opt/node22/bin`) |
| node | v22.22.0 |
| Verdaccio | 6.10.7 (in `replay/node_modules`) |
| cargo | 1.97.0 (`/root/.cargo/bin`) |
| idf-component-manager | 3.1.2 (system Python and the ESP-IDF venv) |
| ESP-IDF | v6.0.3 (`76f5dedd99`), linux host target; CMake 3.28.3, Ninja 1.11.1, gcc 13 |

The gate and name selection are imported from the project
(`harness/phantomdeps`, `config/profile.yaml`); nothing in `harness/`, `paper/`,
`results/` or `config/` is modified.

## Safety / ethics

* Every server listens on `127.0.0.1` only and is killed at the end — on normal
  exit, on exception, and on SIGTERM/SIGINT (`replaylib.common.register_cleanup`).
* **No package is published, downloaded or executed against any real registry.**
  All replay installs are constrained to `127.0.0.1` (`--index-url` only / no
  `--extra-index-url`; `uv --index-url`; `npm --registry`; cargo source
  replacement) and each install argv is asserted local before it runs.
* Packages are harmless (marker-only). Temp dirs are fresh each run; the run is
  idempotent and deterministic apart from timestamps.
* The recorded run uses `--netns`: a loopback-only network namespace, checked
  before any server starts.

## Honest limitations (nothing faked)

* **pip, cargo and the ESP-IDF component manager have no publication-date install
  filter**, so P3 is recorded as `not_available` for them rather than emulated.
* The ESP-IDF case is **one** squat and one control in a placeholder namespace,
  built for the **linux host target**; the firmware is neither cross-compiled for a
  chip nor flashed.
* **Resolving an sdist-only package with `uv` without `--no-build` runs its build
  backend** (the metadata hooks) — package-authored code. The gate therefore runs
  **before** any lock step, and the pin evidence comes from **code-free** locking:
  `--no-build` against the control's wheel, `--package-lock-only --ignore-scripts`,
  `generate-lockfile`. The `lock_execution` section measures the vector directly
  (squats: `code_ran_during_lock = true` without `--no-build`, and `--no-build`
  `refused`).
* With the **measured pin**, the control (harness-set record date before `h`)
  reaches `R = 0.90 ≥ θ = 0.85` → **admit** under SCII (`pin = 1` from the lock
  file), and the installer is run. The `no_pin` decision (`R = 0.70`) is kept
  beside it to show the pin is what lifts an established-looking package from
  `review` to `admit`.
* The control is installed **from its inert wheel**, so its P1/SCII install marker
  is `false` (no code runs) while it is still `installed` — a clean contrast with
  the sdist-only squat, whose install marker is `true`.
* The **with_absence** SCII variant is **not applicable to the control**: its
  record date is set before `h`, and inventing a "now" absence observation for it
  would misrepresent it, so it is reported as N/A instead of faked.
