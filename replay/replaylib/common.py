"""Process, port, timing and cleanup helpers shared by the replay scenarios."""

from __future__ import annotations

import atexit
import datetime as dt
import os
import signal
import socket
import subprocess
import time

# Fixed marker payload written by every harmless replay package.  A scenario
# proves "the installer executed the package's own code" by finding this exact
# text in the per-invocation marker directory.
MARKER_TEXT = "replay-marker-ok"
MARKER_FILE = {"pypi": "PKG_BUILD_RAN", "npm": "NPM_POSTINSTALL_RAN", "crates": "CARGO_BUILD_RAN",
               # ESP-IDF components are not executed on download: the installer only resolves,
               # downloads and unpacks the component into managed_components/.  So the espidf
               # marker is a file PACKED INTO the component that the installer MATERIALISES into
               # the build tree on unpack -- it proves the squat's payload reached the build tree
               # from the local mock, not that component code ran (see esp_registry.py / NOTES).
               "espidf": "ESP_COMPONENT_MATERIALIZED"}
# Second ESP-IDF marker: the harmless component's CMakeLists.txt writes it into
# $REPLAY_MARKER_DIR when an ESP-IDF build evaluates the component.  Present after
# ``idf.py build`` and absent after a download proves that the build, not the
# download, runs component code.
ESP_CMAKE_MARKER = "ESP_COMPONENT_CMAKE_RAN"
# Second PyPI marker, written from the metadata hooks of the sdist's build backend
# (prepare_metadata_for_build_wheel / get_requires_for_build_wheel).  It makes the
# "resolving an sdist runs package code" vector a distinct, measurable signal,
# separate from the install marker above.
PYPI_META_MARKER = "PKG_METADATA_HOOK_RAN"

TAIL = 1500  # chars of stdout/stderr kept in the raw record


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def now_precise() -> dt.datetime:
    """Full-resolution aware UTC now (keeps microseconds), for ordering checks."""
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_us(t: dt.datetime) -> str:
    """ISO-8601 with microseconds and a literal Z, so events a few microseconds
    apart (absence lookup then build+publish) are distinguishable."""
    return t.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def iso_ms(t: dt.datetime) -> str:
    """ISO with milliseconds and a literal Z (the shape npm/Verdaccio ``time`` uses)."""
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bring_loopback_up() -> None:
    """Set IFF_UP on ``lo`` (a fresh network namespace starts with it down; the image
    has no ``ip`` binary, so this is the SIOCSIFFLAGS ioctl it would issue)."""
    import fcntl
    import struct
    siocgifflags, siocsifflags, iff_up = 0x8913, 0x8914, 0x1
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        flags = struct.unpack("16sh", fcntl.ioctl(s, siocgifflags, struct.pack("16sh", b"lo", 0)))[1]
        fcntl.ioctl(s, siocsifflags, struct.pack("16sh", b"lo", flags | iff_up))
    finally:
        s.close()


def network_isolation() -> dict:
    """What this process's network namespace can reach, read from /proc/self/net (no
    packet is sent): the interface names and the routes not on ``lo``.  Isolated iff
    the only interface is ``lo`` and no route leaves it, so no request can reach
    anything but 127.0.0.1, whatever a tool tries."""
    def rows(path, skip):
        try:
            with open(path, encoding="utf-8") as f:
                return [ln.split() for ln in f.read().splitlines()[skip:] if ln.strip()]
        except OSError:
            return None
    dev = rows("/proc/self/net/dev", 2) or []
    ifaces = sorted(r[0].rstrip(":") for r in dev)
    v4 = [r for r in (rows("/proc/self/net/route", 1) or []) if r and r[0] != "lo"]
    v6 = [r for r in (rows("/proc/self/net/ipv6_route", 0) or []) if r and r[-1] != "lo"]
    return {"interfaces": ifaces, "routes_off_loopback": len(v4) + len(v6),
            "isolated": ifaces == ["lo"] and not v4 and not v6}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def base_env(extra: dict | None = None) -> dict:
    """Subprocess environment that keeps all replay traffic off the agent proxy.

    127.0.0.1/localhost are added to NO_PROXY (belt and suspenders: they are
    already there in this image) so pip/uv/npm/cargo talk straight to our
    local servers and never to a real registry.
    """
    env = dict(os.environ)
    no = "127.0.0.1,localhost,::1"
    env["NO_PROXY"] = no
    env["no_proxy"] = no
    if extra:
        env.update(extra)
    return env


def tail(s: str | None, n: int = TAIL) -> str:
    if not s:
        return ""
    s = s if len(s) <= n else s[-n:]
    return s


def run(argv: list[str], env: dict | None = None, cwd: str | None = None,
        timeout: float = 600.0) -> dict:
    """Run a command, capturing exit code, trimmed output and wall time."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"argv": argv, "exit": p.returncode, "stdout_tail": tail(p.stdout),
                "stderr_tail": tail(p.stderr), "elapsed_s": round(time.monotonic() - t0, 3),
                "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"argv": argv, "exit": None, "stdout_tail": tail(e.stdout and e.stdout.decode(errors="replace")),
                "stderr_tail": tail(e.stderr and e.stderr.decode(errors="replace")),
                "elapsed_s": round(time.monotonic() - t0, 3), "timed_out": True}


def file_has_marker(marker_dir: str, filename: str) -> bool:
    path = os.path.join(marker_dir, filename)
    try:
        return os.path.isfile(path) and open(path, encoding="utf-8").read().strip() == MARKER_TEXT
    except OSError:
        return False


def marker_present(marker_dir: str, eco: str) -> bool:
    return file_has_marker(marker_dir, MARKER_FILE[eco])


def meta_marker_present(marker_dir: str) -> bool:
    return file_has_marker(marker_dir, PYPI_META_MARKER)


def assert_localhost(argv: list[str]) -> None:
    """Fail loudly if an install argv could reach anything but 127.0.0.1."""
    forbidden = {"--extra-index-url"}
    for i, a in enumerate(argv):
        if a in forbidden:
            raise AssertionError(f"forbidden flag in replay install: {a}")
        if a.startswith(("http://", "https://", "sparse+http://", "sparse+https://")):
            host = a.split("//", 1)[1]
            if not (host.startswith("127.0.0.1:") or host.startswith("127.0.0.1/")):
                raise AssertionError(f"non-local URL in replay install: {a}")
        if a.startswith("--index-url=") or a.startswith("--registry="):
            if "127.0.0.1" not in a:
                raise AssertionError(f"non-local index/registry: {a}")


# --------------------------------------------------------------------------
# cleanup registry: every server registers a stop() callable; we call them on
# normal exit, on exception, and on SIGTERM/SIGINT so no server is left running.

_CLEANUP: list = []
_INSTALLED = False


def register_cleanup(fn) -> None:
    _CLEANUP.append(fn)
    _install_handlers()


def run_cleanup() -> None:
    while _CLEANUP:
        fn = _CLEANUP.pop()
        try:
            fn()
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass


def _install_handlers() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    atexit.register(run_cleanup)

    def _sig(signum, frame):
        run_cleanup()
        raise SystemExit(128 + signum)

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass
