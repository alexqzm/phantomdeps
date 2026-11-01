"""A local Cargo sparse registry on 127.0.0.1 (index + config.json + .crate).

Each index entry carries ``cksum`` = sha256 of the ``.crate`` and a ``pubtime``
field (our own metadata: cargo ignores unknown keys, and cargo has no stable
publication-date install filter, so P3 is reported as "not available" for
cargo).  The squat crate has a ``build.rs`` that writes the marker when the
crate is compiled as a dependency of a tiny consumer binary.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common
from .common import MARKER_FILE, MARKER_TEXT

_BUILD_RS = f'''\
use std::env;
use std::fs;
use std::path::Path;

fn main() {{
    if let Ok(d) = env::var("REPLAY_MARKER_DIR") {{
        let _ = fs::write(Path::new(&d).join("{MARKER_FILE['crates']}"), "{MARKER_TEXT}");
    }}
    println!("cargo:rerun-if-changed=build.rs");
}}
'''


def _index_path(name: str) -> str:
    n = name.lower()
    if len(n) == 1:
        return f"1/{n}"
    if len(n) == 2:
        return f"2/{n}"
    if len(n) == 3:
        return f"3/{n[0]}/{n}"
    return f"{n[:2]}/{n[2:4]}/{n}"


def build_crate(name: str, version: str = "0.1.0") -> bytes:
    """A gzip-tar ``.crate`` with Cargo.toml, src/lib.rs and a marker build.rs."""
    base = f"{name}-{version}"
    cargo_toml = (
        "[package]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        'edition = "2021"\n'
        'build = "build.rs"\n'
        'description = "harmless replay crate"\n'
        'license = "MIT"\n'
    )
    members = {
        f"{base}/Cargo.toml": cargo_toml.encode(),
        f"{base}/src/lib.rs": b"pub fn noop() {}\n",
        f"{base}/build.rs": _BUILD_RS.encode(),
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for arc, data in members.items():
            ti = tarfile.TarInfo(arc)
            ti.size = len(data)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(data))
    return out.getvalue()


class CratesRegistry:
    """In-process sparse registry; crates live in memory, served over HTTP."""

    def __init__(self):
        self.port = common.free_port()
        self._crates: dict = {}  # name -> {"version", "data", "cksum", "pubtime"}
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
                p = self.path.split("?", 1)[0].lstrip("/")
                if p == "config.json":
                    body = json.dumps({
                        "dl": f"http://127.0.0.1:{reg.port}/api/v1/crates/{{crate}}/{{version}}/download",
                        "api": f"http://127.0.0.1:{reg.port}",
                    }).encode()
                    self._send(body, "application/json")
                    return
                with reg._lock:
                    for name, c in reg._crates.items():
                        if p == _index_path(name):
                            # cargo 1.97 parses `pubtime` and accepts only second-precision
                            # RFC3339, so the index line carries a truncated copy; the gate
                            # reads the registry's precise record time via served_pubtime().
                            pt = c["pubtime"]
                            pt_idx = pt.split(".")[0] + "Z" if pt and "." in pt else pt
                            line = json.dumps({
                                "name": name, "vers": c["version"], "deps": [],
                                "cksum": c["cksum"], "features": {}, "yanked": False,
                                "pubtime": pt_idx,
                            })
                            self._send((line + "\n").encode(), "text/plain")
                            return
                        if p == f"api/v1/crates/{name}/{c['version']}/download":
                            self._send(c["data"], "application/octet-stream")
                            return
                self._send(b"not found", "text/plain", 404)

            do_HEAD = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        common.register_cleanup(self.stop)

    @property
    def registry_url(self) -> str:
        return f"sparse+http://127.0.0.1:{self.port}/"

    def resolves(self, name: str) -> bool:
        with self._lock:
            return name in self._crates

    def add_crate(self, name: str, pubtime: str = None, version: str = "0.1.0") -> dict:
        data = build_crate(name, version)
        cksum = hashlib.sha256(data).hexdigest()
        # Stamp the publish instant after building when none is given, so it is
        # strictly later than any prior absence lookup (see run_replay ordering).
        if pubtime is None:
            pubtime = common.iso_us(common.now_precise())
        with self._lock:
            self._crates[name] = {"version": version, "data": data, "cksum": cksum, "pubtime": pubtime}
        return {"version": version, "cksum": cksum, "pubtime": pubtime, "size": len(data),
                "index_path": _index_path(name)}

    def served_pubtime(self, name: str):
        with self._lock:
            c = self._crates.get(name)
            return c["pubtime"] if c else None

    def write_cargo_home(self, cargo_home: str) -> None:
        """Replace crates-io with our local sparse source so cargo never leaves 127.0.0.1."""
        os.makedirs(cargo_home, exist_ok=True)
        with open(os.path.join(cargo_home, "config.toml"), "w", encoding="utf-8") as f:
            f.write(
                "[source.crates-io]\n"
                'replace-with = "replay"\n\n'
                "[source.replay]\n"
                f'registry = "{self.registry_url}"\n'
            )

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
