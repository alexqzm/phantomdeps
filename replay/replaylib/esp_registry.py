"""A local ESP Component Registry *static storage* backend on 127.0.0.1.

The real installer driven here is ``idf-component-manager`` (3.1.x, the code the
ESP-IDF build system calls to resolve and download components).  Its static
storage layout (confirmed from the installed ``idf_component_tools/registry/
storage_client.py``) is three plain files served over HTTP:

    GET {storage}/components/<ns>/<name>.json   component response (``versions``)
    GET {storage}/<version.url>                 the component archive (.tgz)
    GET {storage}/<version.checksums>           the per-file checksums (CHECKSUMS.json)

So a localhost file server serving those three paths is enough for the manager
to RESOLVE + DOWNLOAD a component into ``managed_components/`` -- mirroring how
``crates_registry.py`` / ``pypi_index.py`` serve files on 127.0.0.1.

Unlike pip/npm/cargo, resolving/downloading an ESP component runs NO component
code: the manager only unpacks the archive into the build tree.  The harmless
marker is therefore a file PACKED INTO the component (``MARKER_FILE['espidf']``)
that the installer materialises into ``managed_components/<ns>__<name>/`` on
unpack; finding it there with ``MARKER_TEXT`` proves the squat's payload reached
the build tree from the local mock (the "resolves-now admits the squat" vector),
not that component code executed.

The build step (``idf_build``) then runs the REAL ESP-IDF build system on a
minimal application whose ``main/idf_component.yml`` declares the component:
``idf.py build`` (linux host target, so no cross toolchain is needed) calls the
same manager at configure time, which downloads the component from the mock, and
CMake evaluates the component's ``CMakeLists.txt`` and compiles its source.  The
component's CMake file writes a second harmless marker (``ESP_CMAKE_MARKER``) into
``$REPLAY_MARKER_DIR`` when it is evaluated; finding it after the build, and not
after the download, measures where component code first runs.  The ESP-IDF
checkout is configured by ``REPLAY_ESP_IDF`` (see replay/README.md); without it
the build step is recorded ``not_available``.

Pointing the manager fully at 127.0.0.1 (never ``components.espressif.com``):
  * IDF_COMPONENT_STORAGE_URL = the local mock -> a static storage client.
  * IDF_COMPONENT_REGISTRY_URL = the registry default.  When the registry URL is
    the default, ``MultiStorageClient.registry_storage_url`` returns the storage
    URL WITHOUT any API call (confirmed in multi_storage_client.py), so no
    request ever leaves 127.0.0.1.  Every served request is logged and asserted
    to come from 127.0.0.1 (``requests_localhost_only``).
  * IDF_VERSION / IDF_PATH are set so the implicit ``idf`` meta-dependency
    resolves offline (no ESP-IDF toolchain is needed or used; no firmware built).

Everything listens on 127.0.0.1 only, builds only the harmless marker component,
never publishes to or fetches from a real registry, and the server is killed on
exit via ``common.register_cleanup``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common
from .common import ESP_CMAKE_MARKER, MARKER_FILE, MARKER_TEXT, file_has_marker

PY = sys.executable
ESP_MARKER = MARKER_FILE["espidf"]
BUILD_TARGET = "linux"          # ESP-IDF host target: the real build system, host gcc
APP_NAME = "replay_app"

# The real ESP default registry URL.  Set as IDF_COMPONENT_REGISTRY_URL only so the
# manager takes the "registry URL is the default -> storage_url is the download
# source" short-circuit (multi_storage_client.registry_storage_url) and makes NO
# API call.  It is never contacted: all traffic goes to IDF_COMPONENT_STORAGE_URL
# (our 127.0.0.1 mock), which the request log asserts.
_ESP_DEFAULT_REGISTRY = "https://components.espressif.com/"

# A fixed IDF version/target so the implicit ``idf`` meta-dependency resolves
# without an ESP-IDF install (idf_component_tools.build_system_tools reads these
# env vars before touching IDF_PATH).  No firmware toolchain is invoked.  When an
# ESP-IDF checkout is configured, the download and lock steps use its version and
# the build target instead, so the lock the gate measures is the one the build uses.
_IDF_VERSION = "5.3.0"
_IDF_TARGET = "esp32"


def esp_idf_setup() -> dict | None:
    """The ESP-IDF checkout for the build step, or None when it is not configured.

    REPLAY_ESP_IDF        path of an ESP-IDF checkout (e.g. a shallow clone of a release tag)
    REPLAY_ESP_IDF_PYENV  its Python environment (requirements.core.txt under the release's
                          constraints file), default <checkout>/../venv
    REPLAY_ESP_IDF_TOOLS  IDF_TOOLS_PATH holding espidf.constraints.v<major>.<minor>.txt,
                          default <checkout>/../idf_tools
    """
    import re
    idf = os.environ.get("REPLAY_ESP_IDF")
    if not idf or not os.path.isfile(os.path.join(idf, "tools", "idf.py")):
        return None
    idf = os.path.abspath(idf)
    pyenv = os.path.abspath(os.environ.get("REPLAY_ESP_IDF_PYENV") or os.path.join(idf, "..", "venv"))
    tools = os.path.abspath(os.environ.get("REPLAY_ESP_IDF_TOOLS") or os.path.join(idf, "..", "idf_tools"))
    hdr = open(os.path.join(idf, "components", "esp_common", "include", "esp_idf_version.h"),
               encoding="utf-8").read()
    ver = [re.search(rf"#define\s+ESP_IDF_VERSION_{k}\s+(\d+)", hdr).group(1) for k in ("MAJOR", "MINOR", "PATCH")]
    git = subprocess.run(["git", "-C", idf, "rev-parse", "HEAD"], capture_output=True, text=True)
    return {"idf_path": idf, "pyenv": pyenv, "tools": tools, "python": os.path.join(pyenv, "bin", "python"),
            "version": ".".join(ver), "major_minor": ".".join(ver[:2]),
            "commit": git.stdout.strip() if git.returncode == 0 else None}


# --------------------------------------------------------------------------
# harmless marker component (built in-process with the manager's own hashing so
# the served component_hash + CHECKSUMS.json always match the unpacked tree).

def build_component_dir(dirpath: str, version: str) -> None:
    """A minimal, valid ESP-IDF component: a manifest, a CMake registration, a
    trivial C source and the packed marker file.  Nothing here runs on download.
    When an ESP-IDF build evaluates the CMake file, it writes the fixed marker text
    to $REPLAY_MARKER_DIR (if set) -- its only side effect -- and the build compiles
    ``noop.c`` into the component's library."""
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "idf_component.yml"), "w", encoding="utf-8") as f:
        f.write(f'version: "{version}"\ndescription: "harmless replay component"\n')
    with open(os.path.join(dirpath, "CMakeLists.txt"), "w", encoding="utf-8") as f:
        f.write("# harmless replay component: writes a fixed marker when a build evaluates this file\n"
                "if(DEFINED ENV{REPLAY_MARKER_DIR})\n"
                f'    file(WRITE "$ENV{{REPLAY_MARKER_DIR}}/{ESP_CMAKE_MARKER}" "{MARKER_TEXT}\\n")\n'
                "endif()\n"
                'idf_component_register(SRCS "noop.c" INCLUDE_DIRS ".")\n')
    with open(os.path.join(dirpath, "noop.c"), "w", encoding="utf-8") as f:
        f.write("/* harmless replay marker component */\nvoid replay_noop(void) {}\n")
    # the packed marker the installer materialises into managed_components/ on unpack
    with open(os.path.join(dirpath, ESP_MARKER), "w", encoding="utf-8") as f:
        f.write(MARKER_TEXT)


def _pack_tgz(src_dir: str, out_path: str) -> None:
    """tar+gzip with ``arcname='./'`` -- exactly how idf_component_tools.archive_tools
    .pack_archive lays out a component archive (files at the archive root)."""
    with tarfile.open(out_path, "w:gz") as a:
        a.add(src_dir, arcname="./")


def _component_artifacts(version: str):
    """Return (component_hash, tgz_bytes, checksums_bytes) for the marker component,
    computed with the manager's own hash_dir / ChecksumsManager so a fresh download
    validates (the manager re-hashes the unpacked tree against CHECKSUMS.json)."""
    import shutil
    from pathlib import Path

    from idf_component_tools.hash_tools.calculate import hash_dir
    from idf_component_tools.hash_tools.checksums import ChecksumsManager
    from idf_component_tools.hash_tools.constants import CHECKSUMS_FILENAME

    tmp = tempfile.mkdtemp(prefix="esp_build_")
    try:
        comp = Path(tmp) / "comp"
        build_component_dir(comp.as_posix(), version)
        component_hash = hash_dir(comp)
        tgz_path = os.path.join(tmp, "a.tgz")
        _pack_tgz(comp.as_posix(), tgz_path)
        with open(tgz_path, "rb") as f:
            tgz_bytes = f.read()
        csout = Path(tmp) / "csout"
        csout.mkdir()
        ChecksumsManager(comp).dump(path=csout)          # CHECKSUMS.json over comp's files
        with open(csout / CHECKSUMS_FILENAME, "rb") as f:
            cs_bytes = f.read()
        return component_hash, tgz_bytes, cs_bytes, CHECKSUMS_FILENAME
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# the subprocess driver (pure stdlib + the installed idf packages; NO relative
# imports, so it runs standalone under ``python3 -I``).  It drives the REAL
# manager: op=resolve downloads into managed_components; op=lock is a code-free
# solve + lock-file dump (no download) that records the component_hash (pin).

_DRIVER_SRC = r'''
import json, os, sys
from pathlib import Path

def main():
    op = sys.argv[1]
    spec = json.load(open(sys.argv[2], encoding="utf-8"))
    proj = Path(spec["project_dir"])
    main_dir = proj / "main"
    lock_path = spec["lock_path"]
    mc_dir = spec["managed_components_dir"]
    marker_file = spec["marker_file"]
    marker_text = spec["marker_text"]

    from idf_component_tools.manager import ManifestManager
    from idf_component_tools.utils import ProjectRequirements
    mm = ManifestManager(main_dir.as_posix(), "main").load()
    pr = ProjectRequirements([mm])

    if op == "resolve":
        from idf_component_manager.dependencies import download_project_dependencies
        download_project_dependencies(pr, lock_path, mc_dir)
        materialized = []
        if os.path.isdir(mc_dir):
            for p in Path(mc_dir).rglob(marker_file):
                try:
                    ok = p.read_text(encoding="utf-8").strip() == marker_text
                except OSError:
                    ok = False
                materialized.append([p.as_posix(), ok])
        marker = any(ok for _, ok in materialized)
        print(json.dumps({"ok": True, "op": op, "marker": marker,
                          "materialized": materialized,
                          "managed_components_exists": os.path.isdir(mc_dir)}))
    elif op == "lock":
        from idf_component_manager.version_solver.version_solver import VersionSolver
        from idf_component_tools.lock import LockManager
        from idf_component_tools.manifest import SolvedManifest
        solver = VersionSolver(pr, old_solution=SolvedManifest.fromdict({}))
        solution = solver.solve()
        LockManager(lock_path).dump(solution)            # writes component_hash; no download
        digest = None
        try:
            import yaml
            lk = yaml.safe_load(open(lock_path, encoding="utf-8"))
            for key, dep in (lk.get("dependencies") or {}).items():
                if isinstance(dep, dict) and key.split("/")[-1] == spec["name"] and dep.get("component_hash"):
                    digest = dep["component_hash"]
        except Exception:
            digest = None
        print(json.dumps({"ok": True, "op": op, "digest": digest,
                          "managed_components_exists": os.path.isdir(mc_dir)}))
    else:
        print(json.dumps({"ok": False, "error": "unknown op " + op})); sys.exit(2)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - report, never crash silently
        import traceback
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                          "trace": traceback.format_exc()[-1500:]}))
        sys.exit(1)
'''


# --------------------------------------------------------------------------

class EspRegistry:
    """In-process static-storage mock; components live in memory, served over HTTP
    on 127.0.0.1.  ``publish`` registers one harmless marker component under a
    placeholder ``<namespace>/<name>``; the real idf-component-manager resolves and
    downloads it from here via ``resolve_download`` / locks it via ``lock_only``."""

    def __init__(self):
        self.idf = esp_idf_setup()             # None: no build step (recorded not_available)
        self.port = common.free_port()
        self._routes: dict = {}                # path -> (bytes, content_type)
        self._published: dict = {}             # (ns, name) -> {version, created_at, component_hash}
        self._log: list = []                   # (client_host, path)
        self._lock = threading.Lock()
        reg = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, body: bytes, ctype: str, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                p = self.path.split("?", 1)[0]
                with reg._lock:
                    reg._log.append((self.client_address[0], p))
                    route = reg._routes.get(p)
                if route is None:
                    self._send(b"not found", "text/plain", 404)
                    return
                body, ctype = route
                self._send(body, ctype)

            do_HEAD = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        assert self._server.server_address[0] == "127.0.0.1", "mock must bind 127.0.0.1 only"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        common.register_cleanup(self.stop)

    # -- storage surface ----------------------------------------------------

    @property
    def storage_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def full_name(self, ns: str, name: str) -> str:
        return f"{ns}/{name}"

    def managed_dir_name(self, ns: str, name: str) -> str:
        return f"{ns}__{name}"

    def resolves(self, ns: str, name: str) -> bool:
        with self._lock:
            return (ns, name) in self._published

    def requests(self):
        with self._lock:
            return list(self._log)

    def requests_localhost_only(self) -> bool:
        return all(host == "127.0.0.1" for host, _ in self.requests())

    def served_created_at(self, ns: str, name: str):
        with self._lock:
            rec = self._published.get((ns, name))
            return rec["created_at"] if rec else None

    def publish(self, ns: str, name: str, version: str = "1.0.0", created_at: str | None = None) -> dict:
        """Serve the harmless marker component at ``<ns>/<name>``.  ``created_at`` is
        the registry record date (REC_LOWER+REC_UPPER for espidf, so it bounds the
        record from both sides, like crates ``pubtime``); when None the publish
        instant is stamped, strictly after any prior absence lookup."""
        component_hash, tgz_bytes, cs_bytes, cs_name = _component_artifacts(version)
        if created_at is None:
            created_at = common.iso_us(common.now_precise())
        # cargo-style: serve a second-precision copy in the JSON (valid RFC3339), keep
        # the precise record date for the gate via served_created_at().
        created_idx = created_at.split(".")[0] + "Z" if "." in created_at else created_at
        tgz_rel = f"files/{ns}/{name}/{version}.tgz"
        cs_rel = f"files/{ns}/{name}/{version}.json"
        comp_json = {
            "name": name, "namespace": ns, "created_at": created_idx,
            "versions": [{
                "version": version, "component_hash": component_hash,
                "url": tgz_rel, "checksums": cs_rel,
                "dependencies": [], "targets": [], "docs": {}, "license": None,
                "examples": [], "created_at": created_idx,
            }],
        }
        with self._lock:
            self._routes[f"/components/{ns}/{name}.json"] = (json.dumps(comp_json).encode(), "application/json")
            self._routes[f"/{tgz_rel}"] = (tgz_bytes, "application/octet-stream")
            self._routes[f"/{cs_rel}"] = (cs_bytes, "application/json")
            self._published[(ns, name)] = {"version": version, "created_at": created_at,
                                           "component_hash": component_hash}
        return {"version": version, "component_hash": component_hash, "created_at": created_at,
                "full_name": self.full_name(ns, name), "managed_dir": self.managed_dir_name(ns, name),
                "checksums_name": cs_name}

    # -- driving the real manager (subprocess, env fully local) -------------

    def _driver_path(self, workdir: str) -> str:
        d = os.path.join(workdir, "driver")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "esp_driver.py")
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_DRIVER_SRC)
        return path

    def _project(self, workdir: str, ns: str, name: str, version: str) -> str:
        proj = os.path.join(workdir, "proj")
        main_dir = os.path.join(proj, "main")
        os.makedirs(main_dir, exist_ok=True)
        with open(os.path.join(main_dir, "idf_component.yml"), "w", encoding="utf-8") as f:
            f.write(f'dependencies:\n  {ns}/{name}:\n    version: "=={version}"\n')
        with open(os.path.join(main_dir, "CMakeLists.txt"), "w", encoding="utf-8") as f:
            f.write('idf_component_register(SRCS "m.c")\n')
        with open(os.path.join(main_dir, "m.c"), "w", encoding="utf-8") as f:
            f.write("void app_main(void) {}\n")
        return proj

    def _env(self, workdir: str, marker_dir: str | None = None) -> dict:
        idf_path = os.path.join(workdir, "fake_idf")
        os.makedirs(idf_path, exist_ok=True)
        env = common.base_env({
            "IDF_COMPONENT_STORAGE_URL": self.storage_url,                 # the only download source
            "IDF_COMPONENT_REGISTRY_URL": _ESP_DEFAULT_REGISTRY,           # default => storage short-circuit, no API call
            "IDF_COMPONENT_CACHE_PATH": os.path.join(workdir, "cache"),    # per-op cache -> always a fresh download
            "IDF_TARGET": BUILD_TARGET if self.idf else _IDF_TARGET,
            "IDF_VERSION": self.idf["version"] if self.idf else _IDF_VERSION,
            "IDF_PATH": idf_path,
        })
        if marker_dir:
            env["REPLAY_MARKER_DIR"] = marker_dir    # any component code that ran would write here
        # never let an ambient token/api redirect anything off 127.0.0.1
        for k in ("IDF_COMPONENT_API_TOKEN", "IDF_COMPONENT_API_URL"):
            env.pop(k, None)
        return env

    def _run_manager(self, op: str, ns: str, name: str, version: str, workdir: str,
                     marker_dir: str | None = None) -> dict:
        common.assert_localhost([self.storage_url])        # 127.0.0.1 only, asserted
        proj = self._project(workdir, ns, name, version)
        driver = self._driver_path(workdir)
        spec = {"storage_url": self.storage_url, "ns": ns, "name": name, "version": version,
                "project_dir": proj, "lock_path": os.path.join(proj, "dependencies.lock"),
                "managed_components_dir": os.path.join(proj, "managed_components"),
                "marker_file": ESP_MARKER, "marker_text": MARKER_TEXT}
        specfile = os.path.join(workdir, f"spec_{op}.json")
        with open(specfile, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        argv = [PY, "-I", driver, op, specfile]
        common.assert_localhost(argv)
        r = common.run(argv, env=self._env(workdir, marker_dir), cwd=workdir)
        parsed = _parse_driver(r)
        r["driver"] = parsed
        r["managed_components_dir"] = spec["managed_components_dir"]
        r["lock_path"] = spec["lock_path"]
        return r

    def resolve_download(self, ns: str, name: str, version: str, workdir: str, marker_dir=None) -> dict:
        """P1 / SCII-install path: drive the real manager to RESOLVE + DOWNLOAD the
        component from the local mock into ``managed_components/``.  ``marker`` is
        True iff the installer materialised the packed marker into the build tree;
        ``code_ran`` is True iff component code wrote its marker into ``marker_dir``
        during the download (measured; a download is expected to run none)."""
        r = self._run_manager("resolve", ns, name, version, workdir, marker_dir)
        drv = r.get("driver") or {}
        mc = r["managed_components_dir"]
        marker = _marker_in_tree(mc)
        comp_dir = os.path.join(mc, self.managed_dir_name(ns, name))
        rec = {"tool": "idf-cm", "argv": r["argv"], "exit": r["exit"],
               "stdout_tail": r["stdout_tail"], "stderr_tail": r["stderr_tail"],
               "elapsed_s": r["elapsed_s"], "timed_out": r.get("timed_out", False),
               "marker": marker, "installed": bool(r["exit"] == 0 and os.path.isdir(comp_dir)),
               "code_ran": bool(marker_dir) and file_has_marker(marker_dir, ESP_CMAKE_MARKER),
               "managed_component": self.managed_dir_name(ns, name),
               "downloaded_from": self.storage_url,
               "requests_localhost_only": self.requests_localhost_only(),
               "driver_ok": bool(drv.get("ok"))}
        if not drv.get("ok"):
            rec["reason"] = drv.get("error")
        return rec

    def lock_only(self, ns: str, name: str, version: str, workdir: str, marker_dir=None):
        """Code-free pin step: version-solve + dump the lock file (NO download, no
        unpack, no component code).  Returns (pin, [record]); pin=1 iff the lock
        recorded a ``component_hash`` for the declared name.  The record keeps the
        digest and the lock path, so the build can be handed exactly this lock."""
        r = self._run_manager("lock", ns, name, version, workdir, marker_dir)
        drv = r.get("driver") or {}
        digest = drv.get("digest")
        rec = {"tool": "idf-cm", "mode": "version-solver + lock dump (no download)",
               "argv": r["argv"], "exit": r["exit"], "stdout_tail": r["stdout_tail"],
               "stderr_tail": r["stderr_tail"], "elapsed_s": r["elapsed_s"],
               "digest_found": digest is not None, "digest": digest,
               "lock_path": r["lock_path"] if digest else None,
               "marker_after_lock": _marker_in_tree(r["managed_components_dir"]),
               "code_ran": bool(marker_dir) and file_has_marker(marker_dir, ESP_CMAKE_MARKER),
               "managed_components_created": bool(drv.get("managed_components_exists")),
               "requests_localhost_only": self.requests_localhost_only()}
        return (1 if digest else None), [rec]

    # -- the build step: the REAL ESP-IDF build system on a minimal application ---

    def _build_env(self, workdir: str, marker_dir: str) -> dict:
        idf = self.idf
        env = common.base_env({
            "IDF_PATH": idf["idf_path"],
            "IDF_PYTHON_ENV_PATH": idf["pyenv"],
            "IDF_TOOLS_PATH": idf["tools"],
            "ESP_IDF_VERSION": idf["major_minor"],         # what the export script sets
            "IDF_TARGET": BUILD_TARGET,
            # a shallow checkout without git submodules: the linux-target application
            # builds none of them, so the configure-time submodule fetch is skipped
            "IDF_SKIP_CHECK_SUBMODULES": "1",
            "IDF_COMPONENT_STORAGE_URL": self.storage_url,
            "IDF_COMPONENT_REGISTRY_URL": _ESP_DEFAULT_REGISTRY,
            "IDF_COMPONENT_CACHE_PATH": os.path.join(workdir, "cache"),
            "REPLAY_MARKER_DIR": marker_dir,
            "PATH": os.path.join(idf["pyenv"], "bin") + os.pathsep + os.environ.get("PATH", ""),
        })
        for k in ("IDF_COMPONENT_API_TOKEN", "IDF_COMPONENT_API_URL", "IDF_VERSION"):
            env.pop(k, None)
        return env

    def idf_build(self, ns: str, name: str, version: str, workdir: str, marker_dir: str,
                  lock_from: str | None = None) -> dict:
        """Run ``idf.py build`` (ESP-IDF linux host target) on a minimal application
        whose main/idf_component.yml declares ``<ns>/<name>``.  The build itself calls
        the component manager, which downloads the component from the local mock; CMake
        then evaluates its CMakeLists.txt and compiles its source.  ``lock_from`` hands
        the build the lock file the gate measured (the admitted path).  Records whether
        the build succeeded, whether the component's CMake code ran (marker), whether
        its library was compiled, and the component hash the build's lock holds."""
        if not self.idf:
            return {"tool": "idf.py", "status": "not_available",
                    "reason": "no ESP-IDF checkout configured (REPLAY_ESP_IDF)"}
        import glob
        import shutil
        import yaml
        common.assert_localhost([self.storage_url])
        proj = self._project(workdir, ns, name, version)
        with open(os.path.join(proj, "CMakeLists.txt"), "w", encoding="utf-8") as f:
            f.write("cmake_minimum_required(VERSION 3.22)\n"
                    "include($ENV{IDF_PATH}/tools/cmake/project.cmake)\n"
                    "idf_build_set_property(MINIMAL_BUILD ON)\n"
                    f"project({APP_NAME})\n")
        if lock_from:
            shutil.copyfile(lock_from, os.path.join(proj, "dependencies.lock"))
        bdir = os.path.join(proj, "build")
        argv = [self.idf["python"], os.path.join(self.idf["idf_path"], "tools", "idf.py"),
                "--preview", "-C", proj, "-B", bdir, "build"]
        r = common.run(argv, env=self._build_env(workdir, marker_dir), cwd=proj, timeout=900)
        comp = self.managed_dir_name(ns, name)
        libs = glob.glob(os.path.join(bdir, "esp-idf", comp, f"lib{comp}.a"))
        lock_hash = None
        try:
            lk = yaml.safe_load(open(os.path.join(proj, "dependencies.lock"), encoding="utf-8")) or {}
            dep = (lk.get("dependencies") or {}).get(f"{ns}/{name}") or {}
            lock_hash = dep.get("component_hash")
        except (OSError, ValueError):
            pass
        elf = os.path.join(bdir, f"{APP_NAME}.elf")
        return {"tool": "idf.py", "command": "idf.py --preview build", "target": BUILD_TARGET,
                "esp_idf": self.idf["version"], "exit": r["exit"], "elapsed_s": r["elapsed_s"],
                "timed_out": r.get("timed_out", False),
                "stdout_tail": r["stdout_tail"], "stderr_tail": r["stderr_tail"],
                "built": bool(r["exit"] == 0 and os.path.isfile(elf)),
                "cmake_marker": file_has_marker(marker_dir, ESP_CMAKE_MARKER),
                "materialized": _marker_in_tree(os.path.join(proj, "managed_components")),
                "component_lib": bool(libs),
                "lock_component_hash": lock_hash, "lock_handed_in": bool(lock_from),
                "requests_localhost_only": self.requests_localhost_only()}

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass


def _parse_driver(run_record: dict) -> dict:
    """The driver prints one JSON line on stdout; parse the last JSON object."""
    out = (run_record.get("stdout_tail") or "").strip().splitlines()
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "error": "no driver JSON on stdout"}


def _marker_in_tree(managed_components_dir: str) -> bool:
    if not os.path.isdir(managed_components_dir):
        return False
    from pathlib import Path
    for p in Path(managed_components_dir).rglob(ESP_MARKER):
        try:
            if p.read_text(encoding="utf-8").strip() == MARKER_TEXT:
                return True
        except OSError:
            pass
    return False
