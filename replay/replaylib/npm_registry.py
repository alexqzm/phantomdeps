"""An isolated Verdaccio registry on 127.0.0.1 with NO uplinks.

Storage lives in a throwaway temp dir, a throwaway ``--userconfig`` .npmrc keeps
the real npm config untouched, and ``packages.'**'`` is open so the harness can
publish without registering a user.  The squat package declares a ``postinstall``
script that writes the marker (scripts run on the default ``npm install``).

Backdating the control: Verdaccio stamps ``time.created``/per-version time at
publish; the control's established date is applied by editing the on-disk
packument and restarting the server (so the edited ``time`` is what the registry
serves and what ``npm install --before`` reads).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

from . import common
from .common import MARKER_FILE, MARKER_TEXT

NPM = "/opt/node22/bin/npm"
NODE = "/opt/node22/bin/node"


def build_npm_pkg(src_dir: str, name: str, version: str = "1.0.0") -> None:
    os.makedirs(src_dir, exist_ok=True)
    pkg = {
        "name": name, "version": version, "description": "harmless replay package",
        "license": "MIT", "main": "index.js",
        "scripts": {"postinstall": "node postinstall.js"},
    }
    with open(os.path.join(src_dir, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f)
    with open(os.path.join(src_dir, "index.js"), "w", encoding="utf-8") as f:
        f.write("module.exports = 0;\n")
    with open(os.path.join(src_dir, "postinstall.js"), "w", encoding="utf-8") as f:
        f.write(
            'var fs = require("fs");\n'
            'var dir = process.env.REPLAY_MARKER_DIR;\n'
            'if (dir) { try { fs.writeFileSync(dir + "/%s", "%s"); } catch (e) {} }\n'
            % (MARKER_FILE["npm"], MARKER_TEXT))


class NpmRegistry:
    def __init__(self, workdir: str):
        self.port = common.free_port()
        self.workdir = workdir
        self.storage = os.path.join(workdir, "storage")
        os.makedirs(self.storage, exist_ok=True)
        self.htpasswd = os.path.join(workdir, "htpasswd")
        open(self.htpasswd, "w").close()
        self.config = os.path.join(workdir, "config.yaml")
        self.logfile = os.path.join(workdir, "verdaccio.log")
        self.npmrc = os.path.join(workdir, ".npmrc")
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(
                f"storage: {self.storage}\n"
                "auth:\n  htpasswd:\n"
                f"    file: {self.htpasswd}\n    max_users: -1\n"
                "uplinks: {}\n"
                "packages:\n  '**':\n    access: $all\n    publish: $all\n    unpublish: $all\n"
                "log: {type: stdout, format: pretty, level: warn}\n"
            )
        with open(self.npmrc, "w", encoding="utf-8") as f:
            f.write(f"registry={self.registry_url}\n//127.0.0.1:{self.port}/:_authToken=replay-local-token\n")
        self._verdaccio_bin = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "replay", "node_modules", ".bin", "verdaccio")
        self._proc = None
        common.register_cleanup(self.stop)
        self.start()

    @property
    def registry_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def verdaccio_version(self) -> str:
        r = common.run([NODE, self._verdaccio_bin, "--version"], env=common.base_env())
        return (r["stdout_tail"] or r["stderr_tail"]).strip().splitlines()[-1] if r["exit"] == 0 else "unknown"

    def start(self) -> None:
        env = common.base_env()
        self._proc = subprocess.Popen(
            [NODE, self._verdaccio_bin, "-c", self.config, "-l", f"127.0.0.1:{self.port}"],
            env=env, stdout=open(self.logfile, "a"), stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self, tries: int = 150) -> None:
        for _ in range(tries):
            if self._proc.poll() is not None:
                raise RuntimeError("verdaccio exited during startup; see " + self.logfile)
            try:
                urllib.request.urlopen(self.registry_url, timeout=1)
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        raise RuntimeError("verdaccio did not become ready")

    def _npm(self, args: list[str], cwd: str | None = None, extra_env: dict | None = None) -> dict:
        argv = [NPM] + args + ["--registry", self.registry_url, "--userconfig", self.npmrc]
        return common.run(argv, env=common.base_env(extra_env), cwd=cwd)

    def lookup_status(self, name: str):
        """Real GET of the packument; returns HTTP status (404 = does not resolve)."""
        try:
            with urllib.request.urlopen(self.registry_url + name, timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception as e:  # noqa: BLE001
            return str(e)

    def resolves(self, name: str) -> bool:
        return self.lookup_status(name) == 200

    def publish(self, src_dir: str) -> dict:
        return self._npm(["publish", "--no-audit", "--no-fund"], cwd=src_dir)

    def packument_time(self, name: str) -> dict | None:
        try:
            with urllib.request.urlopen(self.registry_url + name, timeout=3) as r:
                return json.load(r).get("time")
        except Exception:  # noqa: BLE001
            return None

    def backdate(self, name: str, iso_time: str) -> bool:
        """Edit the stored packument ``time`` and restart so the edit is served."""
        pkgfile = os.path.join(self.storage, name, "package.json")
        if not os.path.isfile(pkgfile):
            return False
        self.stop()
        with open(pkgfile, encoding="utf-8") as f:
            meta = json.load(f)
        versions = list(meta.get("versions", {}).keys())
        t = {"created": iso_time, "modified": iso_time}
        for v in versions:
            t[v] = iso_time
        meta["time"] = t
        with open(pkgfile, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        self.start()
        served = self.packument_time(name) or {}
        return served.get("created") == iso_time

    def install(self, name: str, host_dir: str, marker_dir: str, before: str | None = None) -> dict:
        os.makedirs(host_dir, exist_ok=True)
        with open(os.path.join(host_dir, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"name":"replay-host","version":"1.0.0","private":true}')
        args = ["install", name, "--no-audit", "--no-fund"]
        if before:
            args += ["--before", before]
        argv = [NPM] + args + ["--registry", self.registry_url, "--userconfig", self.npmrc]
        common.assert_localhost(argv)
        r = common.run(argv, env=common.base_env({"REPLAY_MARKER_DIR": marker_dir}), cwd=host_dir)
        r.update(tool="npm", marker=common.marker_present(marker_dir, "npm"),
                 installed=(r["exit"] == 0))
        return r

    def lock_only(self, name: str, host_dir: str, marker_dir: str):
        """Resolve-only lock step: write package-lock.json without installing or
        running scripts.  Returns (pin, record) where pin=1 iff an ``integrity``
        digest was recorded for the declared name."""
        os.makedirs(host_dir, exist_ok=True)
        with open(os.path.join(host_dir, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"name":"replay-lock-host","version":"1.0.0","private":true}')
        argv = [NPM, "install", name, "--package-lock-only", "--ignore-scripts",
                "--no-audit", "--no-fund", "--registry", self.registry_url,
                "--userconfig", self.npmrc]
        common.assert_localhost(argv)
        r = common.run(argv, env=common.base_env({"REPLAY_MARKER_DIR": marker_dir}), cwd=host_dir)
        integrity = None
        lockpath = os.path.join(host_dir, "package-lock.json")
        if os.path.isfile(lockpath):
            try:
                with open(lockpath, encoding="utf-8") as f:
                    lock = json.load(f)
                for k, v in (lock.get("packages") or {}).items():
                    if isinstance(v, dict) and k.split("/")[-1] == name and v.get("integrity"):
                        integrity = v["integrity"]
            except (OSError, ValueError):
                pass
        r.update(tool="npm", mode="package-lock-only", digest_found=(integrity is not None),
                 marker_after_lock=common.marker_present(marker_dir, "npm"))
        return (1 if integrity else None), [r]

    def stop(self):
        if self._proc is None:
            return
        p, self._proc = self._proc, None
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
