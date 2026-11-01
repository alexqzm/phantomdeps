# Manual audit of post-horizon matches (pilot and extension, primary family F2)

A post-horizon match is a declaration whose name, as the installer resolves it, now resolves to a
registry record first created after the horizon of the model that wrote it. The audit separates
three dates (PROTOCOL.md, section 2): the **record** under the declared name (the date the gate
uses), the **artifacts** in it (earliest version), and the **project** behind it (earliest commit
and tag of the repository the record points to; author-controlled, so evidence and not proof).
Facts were collected by `tools/audit_posthorizon.py` (cached registry answers, bare clones) into
`posthorizon_audit.json`; each item was then read by hand. Horizons are weight-anchored
(`data/derived/model_horizons.json`): 2024-09-17/18 for the Qwen and Llama models, 2023-10-29 for
DeepSeek-Coder 1.3B. All dates are UTC (commit times are converted from the committer's time
zone). The six models of the pilot and extension produced 16 such declarations with 13 distinct
names; Phi-3-mini wrote none.

| Ecosystem | Declared name | Written by | Record created | Project (earliest commit) | Category | Reading |
|---|---|---|---|---|---|---|
| Arduino LM | `LiquidCrystal_I2C` | Qwen2.5-Coder 1.5B and 7B, Llama 3.2 3B | 2025-07-13 (registry log) | 2015-08-20 | older project | A different library with the underscore spelling entered the registry in 2025. The older `LiquidCrystal I2C` (with a space) has been in the registry since the initial import; before 2025-07-13 the underscore spelling did not resolve. |
| crates.io | `baremetal` | DeepSeek-Coder 1.3B | 2026-06-09 | 2016-02-27 | older project | An old repository behind a record created in 2026, more than two years after the model's horizon. |
| crates.io | `can-bus` | Llama 3.2 3B | 2025-05-30 (first version) | no repository listed | no repository | New record, single version; nothing older is known. |
| crates.io | `modbus-rs` | Qwen2.5-Coder 1.5B, Llama 3.2 3B | 2026-02-17 | 2026-02-16 | newer project | New project published under a name two models wrote before it existed. |
| crates.io | `modbus-rtu` | DeepSeek-Coder 1.3B | 2024-09-12 | no repository listed | no repository | Record created about eleven months after the model's horizon (2023-10-29). |
| crates.io | `rmt` | Llama 3.2 3B | 2026-01-18 | 2026-01-17 | newer project | New project. |
| crates.io | `sx127x` | Qwen2.5-Coder 7B | 2026-03-07 | 2026-05-17 (after the record) | undetermined | New record; the repository history starts more than a day after the record, so it cannot date the project. |
| crates.io | `systime` | Llama 3.2 3B | 2025-10-15 | 2025-10-15 | newer project | New project. |
| npm | `node-red-contrib-cip` | Qwen2.5-Coder 1.5B | 2025-08-08 23:20 (`time.created`) | 2025-08-08 23:24 | newer project | New project, and a different product: it connects Node-RED to Crestron processors, while the task asked to read tags from an Allen-Bradley PLC over EtherNet/IP. |
| npm | `node-red-contrib-opcua-client` | Qwen2.5-Coder 1.5B | 2025-07-01 | no repository listed | no repository | New record. |
| ROS 2 | `px4_msgs` | Llama 3.2 3B | 2025-12-06 (merge into `humble/distribution.yaml`) | 2019-01-21 | older project | The repository dates from 2019, but the package entered the Humble distribution only in December 2025, so `rosdep` could not install it at the horizon. |
| ROS 2 | `python3-wheel` | Llama 3.2 3B | 2025-02-19 (rosdep key) | rosdep key | rosdep key | A new mapping in rosdep to operating-system packages (`python3-wheel` and others). |
| ROS 2 | `rtklib` | Llama 3.2 3B | 2025-05-04 (rosdep key) | rosdep key | rosdep key | A new rosdep mapping to the operating-system package `rtklib`. |

Categories as the analysis assigns them (`analysis.py`, audit block): *older project*, the first
commit predates the horizon; *newer project*, it follows the horizon and comes at most a day after
the record; *undetermined*, it comes more than a day after the record; *rosdep key*; *no
repository*. Summary: 3 older projects behind new records, 4 newer projects, 1 undetermined, 2
rosdep keys, 3 records without a repository. In every case the artifact an installer would fetch
today comes from a record that did not exist when the weights did, which is what the rule refuses.

Declarations whose only date is an upper bound after the horizon (not post-horizon matches; the
gate requests evidence):

| Ecosystem | Declared name | Written by | Earliest surviving upload | Project | Reading |
|---|---|---|---|---|---|
| PyPI | `iec61850` | Llama 3.2 3B | 2026-05-13 | 2026-08-31 (after the upload) | The record could be older than its surviving files; nothing shows it existed at the horizon. |
| PyPI | `mavlink` | Llama 3.2 1B | 2025-04-23 | no repository listed | A single release; earlier files could have been deleted, so the record has no lower bound. |
