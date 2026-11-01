"""A local PEP 503 (HTML) + PEP 691 (JSON) "simple" index on 127.0.0.1.

The JSON project page carries PEP 700 ``upload-time`` for every file so that
``uv pip install --exclude-newer H`` can date-filter.  Squat distributions are
sdists whose PEP 517 build runs package-authored code that writes the marker;
the backend declares ``requires = []`` and an in-tree ``backend-path`` so the
build needs nothing from any index (it runs fully offline against 127.0.0.1,
with no setuptools fetch).
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common
from .common import MARKER_FILE, MARKER_TEXT, PYPI_META_MARKER

_NORM = re.compile(r"[-_.]+")


def normalize(name: str) -> str:
    return _NORM.sub("-", name).lower()


def _ident(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", name)


# In-tree PEP 517 backend baked into every sdist.  It writes the marker in
# build_wheel (the build step) and emits a minimal, valid, importable wheel.
# Placeholders (__NAME__ etc.) are substituted per package; the names are
# identifier/path safe, so plain replacement needs no escaping.
_BACKEND_TMPL = '''\
import base64, hashlib, io, os, zipfile

NAME = "__NAME__"
VERSION = "__VERSION__"
MODULE = "__MODULE__"
MARKER_FILE = "__MARKER_FILE__"
META_MARKER_FILE = "__META_MARKER_FILE__"
MARKER_TEXT = "__MARKER_TEXT__"


def _write(fname):
    d = os.environ.get("REPLAY_MARKER_DIR")
    if not d:
        return
    try:
        with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
            f.write(MARKER_TEXT)
    except OSError:
        pass


def get_requires_for_build_wheel(config_settings=None):
    # package code the installer runs while merely RESOLVING an sdist (no --no-build)
    _write(META_MARKER_FILE)
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def _metadata():
    return "Metadata-Version: 2.1\\nName: %s\\nVersion: %s\\nSummary: harmless replay package\\n" % (NAME, VERSION)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    # package code the installer runs while merely RESOLVING an sdist (no --no-build)
    _write(META_MARKER_FILE)
    dist = "%s-%s.dist-info" % (MODULE, VERSION)
    os.makedirs(os.path.join(metadata_directory, dist), exist_ok=True)
    with open(os.path.join(metadata_directory, dist, "METADATA"), "w", encoding="utf-8") as f:
        f.write(_metadata())
    return dist


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _write(MARKER_FILE)
    whl = "%s-%s-py3-none-any.whl" % (MODULE, VERSION)
    dist = "%s-%s.dist-info" % (MODULE, VERSION)
    files = {
        "%s/__init__.py" % MODULE: b"# harmless replay package\\n",
        "%s/METADATA" % dist: _metadata().encode(),
        "%s/WHEEL" % dist: b"Wheel-Version: 1.0\\nGenerator: replay 0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
    }
    rec = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in files.items():
            z.writestr(arc, data)
            h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rec.append("%s,sha256=%s,%d" % (arc, h, len(data)))
        rec.append("%s/RECORD,," % dist)
        z.writestr("%s/RECORD" % dist, "\\n".join(rec) + "\\n")
    with open(os.path.join(wheel_directory, whl), "wb") as f:
        f.write(buf.getvalue())
    return whl


def build_sdist(sdist_directory, config_settings=None):
    raise NotImplementedError("replay sdists are prebuilt")
'''


def build_sdist(name: str, version: str = "0.0.1") -> tuple[str, bytes]:
    """Return (filename, bytes) of an sdist whose build writes the marker."""
    module = _ident(name)
    backend = (_BACKEND_TMPL
               .replace("__NAME__", name)
               .replace("__VERSION__", version)
               .replace("__MODULE__", module)
               .replace("__MARKER_FILE__", MARKER_FILE["pypi"])
               .replace("__META_MARKER_FILE__", PYPI_META_MARKER)
               .replace("__MARKER_TEXT__", MARKER_TEXT))
    # dynamic = ["dependencies"] forces the installer to call the backend's
    # metadata hook to resolve the sdist (it cannot read deps statically), which
    # is the "resolving an sdist runs package code" vector the squats demonstrate.
    pyproject = (
        "[build-system]\n"
        'requires = []\n'
        'build-backend = "replay_backend"\n'
        'backend-path = ["."]\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        'description = "harmless replay package"\n'
        'dynamic = ["dependencies"]\n'
    )
    pkginfo = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    base = f"{module}-{version}"
    members = {
        f"{base}/pyproject.toml": pyproject.encode(),
        f"{base}/replay_backend.py": backend.encode(),
        f"{base}/PKG-INFO": pkginfo.encode(),
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for arc, data in members.items():
            ti = tarfile.TarInfo(arc)
            ti.size = len(data)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(data))
    return f"{module}-{version}.tar.gz", out.getvalue()


def build_wheel(name: str, version: str = "0.0.1") -> tuple[str, bytes]:
    """Return (filename, bytes) of an INERT wheel built by the harness (not by the
    package).  Installing a wheel runs no package code, and resolving one needs no
    build, so ``uv --no-build`` can lock a control from it code-free.  Used for the
    established controls; squats stay sdist-only so their build runs on install."""
    module = _ident(name)
    dist = f"{module}-{version}.dist-info"
    files = {
        f"{module}/__init__.py": b"# harmless replay package (inert wheel)\n",
        f"{dist}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                             "Summary: harmless replay control (inert wheel)\n").encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: replay 0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rec, buf = [], io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in files.items():
            z.writestr(arc, data)
            h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rec.append(f"{arc},sha256={h},{len(data)}")
        rec.append(f"{dist}/RECORD,,")
        z.writestr(f"{dist}/RECORD", "\n".join(rec) + "\n")
    return f"{module}-{version}-py3-none-any.whl", buf.getvalue()


class PyPIIndex:
    """In-process threaded index; serves /simple/<name>/ and /packages/<file>."""

    def __init__(self, html_only: bool = False):
        # html_only=True: speak PEP 503 HTML only (no PEP 691 JSON, no PEP 700
        # upload-time), the way a static find-links dir or an older mirror serves.
        self.port = common.free_port()
        self.html_only = html_only
        self._pkgs: dict = {}  # normalized name -> {"raw": str, "files": [ {...} ]}
        self._lock = threading.Lock()
        idx = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def _send(self, body: bytes, ctype: str, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path.startswith("/simple/"):
                    rest = path[len("/simple/"):].strip("/")
                    norm = normalize(rest)
                    with idx._lock:
                        pkg = idx._pkgs.get(norm)
                    if pkg is None:
                        self._send(b"not found", "text/plain", 404)
                        return
                    accept = self.headers.get("Accept", "")
                    wants_json = ("application/vnd.pypi.simple.v1+json" in accept or "json" in accept)
                    if wants_json and not idx.html_only:
                        self._send(idx._json(norm, pkg), "application/vnd.pypi.simple.v1+json")
                    else:
                        self._send(idx._html(norm, pkg), "text/html")
                    return
                if path.startswith("/packages/"):
                    fn = path[len("/packages/"):]
                    with idx._lock:
                        for pkg in idx._pkgs.values():
                            for f in pkg["files"]:
                                if f["filename"] == fn:
                                    self._send(f["data"], "application/octet-stream")
                                    return
                    self._send(b"not found", "text/plain", 404)
                    return
                self._send(b"not found", "text/plain", 404)

            do_HEAD = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        common.register_cleanup(self.stop)

    @property
    def index_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/simple/"

    def file_url(self, filename: str) -> str:
        return f"http://127.0.0.1:{self.port}/packages/{filename}"

    def _json(self, norm: str, pkg: dict) -> bytes:
        import json
        files = [{"filename": f["filename"], "url": self.file_url(f["filename"]),
                  "hashes": {"sha256": f["sha256"]}, "upload-time": f["upload_time"],
                  "size": f["size"]} for f in pkg["files"]]
        return json.dumps({"meta": {"api-version": "1.1"}, "name": norm, "files": files}).encode()

    def _html(self, norm: str, pkg: dict) -> bytes:
        # PEP 503 carries the digest in the URL fragment; PEP 700 upload-time is
        # JSON-only, so the html_only (dateless) index never emits a date.
        def anchor(f):
            attr = "" if self.html_only else f' data-upload-time="{f["upload_time"]}"'
            return (f'<a href="{self.file_url(f["filename"])}#sha256={f["sha256"]}"{attr}>'
                    f'{f["filename"]}</a><br/>')
        anchors = "".join(anchor(f) for f in pkg["files"])
        return (f"<!DOCTYPE html><html><head><title>{norm}</title></head>"
                f"<body>{anchors}</body></html>").encode()

    def resolves(self, name: str) -> bool:
        with self._lock:
            return normalize(name) in self._pkgs

    def _add(self, name, filename, data, upload_time, kind):
        # When no time is given the registry stamps the publish instant AFTER the
        # artifact is built, so it is strictly later than any prior absence lookup.
        if upload_time is None:
            upload_time = common.iso_us(common.now_precise())
        rec = {"filename": filename, "data": data, "size": len(data),
               "sha256": hashlib.sha256(data).hexdigest(), "upload_time": upload_time, "kind": kind}
        with self._lock:
            self._pkgs.setdefault(normalize(name), {"raw": name, "files": []})["files"].append(rec)
        return {"filename": filename, "sha256": rec["sha256"], "size": rec["size"],
                "upload_time": upload_time, "kind": kind}

    def add_sdist(self, name: str, upload_time: str = None, version: str = "0.0.1") -> dict:
        filename, data = build_sdist(name, version)
        return self._add(name, filename, data, upload_time, "sdist")

    def add_wheel(self, name: str, upload_time: str = None, version: str = "0.0.1") -> dict:
        filename, data = build_wheel(name, version)
        return self._add(name, filename, data, upload_time, "wheel")

    def served_upload_time(self, name: str):
        """Minimum upload-time the registry serves for the name (the recorded date)."""
        with self._lock:
            pkg = self._pkgs.get(normalize(name))
            if not pkg:
                return None
            times = [f["upload_time"] for f in pkg["files"] if f.get("upload_time")]
        return min(times) if times else None

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
