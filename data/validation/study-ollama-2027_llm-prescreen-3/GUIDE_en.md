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

A dependency is anything the project's build or one of the install commands below would try to
fetch.

* Manifest entries: `platformio.ini` (`lib_deps`), `idf_component.yml` (`dependencies`),
  `package.xml` (`depend`, `exec_depend`, `build_depend`, `buildtool_depend` and so on),
  `Cargo.toml` (`[dependencies]` and others), `package.json` (`dependencies`,
  `devDependencies`), `requirements.txt`, `library.properties` (`depends`), including
  dependencies by URL or path (write them as they appear in the answer).
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
`- espressif/esp-modbus:^1.0.0` declares `espidf:espressif/esp-modbus`. A name ends where the installer ends it: `arduino-cli` separates
a version only at `@`, so `arduino-cli lib install "SD#2.2.2"` declares `arduino:SD#2.2.2` (a
name it will not find).

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
