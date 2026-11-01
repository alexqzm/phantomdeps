"""Toolchain-provided names that are legitimately absent from registries.

A declaration such as ``Wire`` in ``lib_deps`` or ``nvs_flash`` in an
``idf_component.yml`` does not name a registry package: it names a library
shipped with the board core or framework.  Counting such names as phantoms
would inflate the phantom rate, so they are classified as *toolchain* names.

The lists are derived from the upstream repositories (directory names under
``libraries/`` or ``components/``) at recorded commits, never typed by hand.
Union over cores is deliberately lenient: it can only lower the phantom count.
"""

from __future__ import annotations

import re

import subprocess
from pathlib import Path

from .util import DERIVED, SNAP_GIT, load_json, utcnow, write_json

# repo directory under data/snapshots/git, git ref, subdirectory, family
SOURCES = [
    ("espressif_arduino-esp32", "HEAD", "libraries", "arduino_core"),
    ("esp8266_Arduino", "HEAD", "libraries", "arduino_core"),
    ("arduino_ArduinoCore-avr", "HEAD", "libraries", "arduino_core"),
    ("arduino_ArduinoCore-samd", "HEAD", "libraries", "arduino_core"),
    ("arduino_ArduinoCore-renesas", "HEAD", "libraries", "arduino_core"),
    ("arduino_ArduinoCore-mbed", "HEAD", "libraries", "arduino_core"),
    ("arduino_ArduinoCore-megaavr", "HEAD", "libraries", "arduino_core"),
    ("earlephilhower_arduino-pico", "HEAD", "libraries", "arduino_core"),
    ("stm32duino_Arduino_Core_STM32", "HEAD", "libraries", "arduino_core"),
    ("espressif_esp-idf", "HEAD", "components", "espidf_component"),
    ("espressif_esp-idf", "refs/remotes/origin/release-v5.5", "components", "espidf_component"),
    ("micropython_micropython", "HEAD", "docs/library", "micropython_module"),
]

# Names that are part of a language or build system rather than a registry.
# Each entry documents why it is not a package name.
LANGUAGE_NAMES = {
    "crates": {"std": "Rust standard library", "core": "Rust core library",
               "alloc": "Rust alloc library", "proc_macro": "Rust compiler crate",
               "test": "Rust test harness"},
    "espidf": {"idf": "ESP-IDF version constraint in idf_component.yml"},
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def build() -> dict:
    out: dict = {"built_at": utcnow(), "sources": [], "names": {}}
    for d, ref, sub, family in SOURCES:
        repo = SNAP_GIT / d
        if not repo.exists():
            raise FileNotFoundError(f"missing clone {repo}; run `make snapshots`")
        commit = _git(repo, "rev-parse", ref).strip()
        listing = _git(repo, "ls-tree", "--name-only", f"{ref}", f"{sub}/").split()
        names = []
        for p in listing:
            leaf = p.rsplit("/", 1)[-1]
            if family == "micropython_module":
                if not leaf.endswith(".rst") or leaf in ("index.rst",):
                    continue
                leaf = leaf[:-4]
            if leaf.startswith(".") or leaf.lower() in ("readme.md", "cmakelists.txt", "kconfig"):
                continue
            names.append(leaf)
        out["sources"].append({"repo": d, "ref": ref, "commit": commit, "subdir": sub,
                               "family": family, "count": len(names)})
        bucket = out["names"].setdefault(family, {})
        for n in names:
            bucket.setdefault(n, []).append(d)
    out["language_names"] = LANGUAGE_NAMES
    write_json(DERIVED / "builtins.json", out)
    return out


class Builtins:
    def __init__(self, path: Path | None = None):
        data = load_json(path or (DERIVED / "builtins.json"))
        if data is None:
            raise FileNotFoundError("data/derived/builtins.json missing; run `make snapshots`")
        self.data = data
        fam = data["names"]
        self.arduino = {n.lower() for n in fam.get("arduino_core", {})}
        self.espidf = {n.lower() for n in fam.get("espidf_component", {})}
        self.micropython = {n.lower() for n in fam.get("micropython_module", {})}
        # MicroPython documents some modules with a "u" prefix historically
        self.micropython |= {"u" + n for n in list(self.micropython)}
        self.language = {eco: set(v) for eco, v in data.get("language_names", {}).items()}

    @staticmethod
    def squash(name: str) -> str:
        """Case and separator folding: the toolchain union is lenient on purpose."""
        return re.sub(r"[-_ .]", "", name.lower())

    def is_toolchain(self, eco: str, name: str) -> bool:
        n = name.lower()
        if n in self.language.get(eco, set()):
            return True
        if not hasattr(self, "_folded"):
            self._folded = {"arduino": {self.squash(x) for x in self.arduino},
                            "espidf": {self.squash(x) for x in self.espidf},
                            "micropython": {self.squash(x) for x in self.micropython}}
        if eco in ("arduino", "platformio"):
            return n in self.arduino or self.squash(n) in self._folded["arduino"]
        if eco == "espidf":
            b = n.split("/")[-1]
            return b in self.espidf or self.squash(b) in self._folded["espidf"]
        if eco == "micropython":
            return n in self.micropython or self.squash(n) in self._folded["micropython"]
        return False
