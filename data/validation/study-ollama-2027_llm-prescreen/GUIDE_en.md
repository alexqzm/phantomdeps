# Annotating dependency extraction (manual validation, PROTOCOL.md, section 5)

Goal: check how exactly the program extracts from a model answer **the dependencies a build
would install**. Only extraction is judged, not whether a package exists.

Two annotators (A, the reference annotator, and B) work **independently**: they do not discuss
rows or look at the other sheet before both are finished. Each fills in their own file,
`labels_A.xlsx` or `labels_B.xlsx` (sheet `labels`).

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

A dependency is anything a build or an install command would try to fetch.

* Manifest entries: `platformio.ini` (`lib_deps`), `idf_component.yml` (`dependencies`),
  `package.xml` (`depend`, `exec_depend`, `build_depend`, `buildtool_depend` and so on),
  `Cargo.toml` (`[dependencies]` and others), `package.json` (`dependencies`,
  `devDependencies`), `requirements.txt`, including dependencies by URL or path (write them as
  they appear in the answer).
* Install commands (`pip install X`, `npm install X`, `cargo add X`,
  `arduino-cli lib install "X"`, `pio pkg install -l X`, `mip.install("X")`,
  `sudo apt install ros-humble-X`), but only in code: in a code block, in inline code or on a
  line that consists of the command alone. The arguments of a command end at the first word of
  ordinary text.

Not dependencies: names in ordinary text (even "install the library X" without a command),
`#include` lines, the version constraint on ESP-IDF itself (`idf`). If the program included
such an item in `extracted`, write it in `spurious`; if it did not, write nothing.

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
`npm`. For PlatformIO and the ESP Component Registry `owner/name` is allowed (for example
`espidf:espressif/led_strip`). Keep case and separators as in the answer.

When finished, return both files; scoring: `python3 -m phantomdeps validation-score <runs>`.
