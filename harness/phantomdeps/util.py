"""Small shared helpers: paths, hashing, time, JSONL, HTTP."""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(os.environ.get("PHANTOMDEPS_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
SNAP_RAW = DATA / "snapshots" / "raw"
SNAP_GIT = DATA / "snapshots" / "git"
DERIVED = DATA / "derived"
EVIDENCE = DATA / "evidence"   # audit and lock-file evidence written after classification (not hashed)
CACHE = DATA / "cache"
RUNS = DATA / "runs"
RESULTS = ROOT / "results"
CONFIG = ROOT / "config"
CORPUS = ROOT / "corpus"

USER_AGENT = "phantomdeps-cps/1.0 (research artifact; read-only lookups)"


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(s: str | None) -> _dt.datetime | None:
    """Parse ISO-8601 timestamps from heterogeneous registries into aware UTC."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = _dt.datetime.fromisoformat(s)
    except ValueError:
        # e.g. "2023-12-14 13:43:22" or fractional seconds with odd precision
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                t = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                t = None
        if t is None:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc)


def iso(t: _dt.datetime | None) -> str | None:
    return None if t is None else t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def rel_path(path) -> str:
    """A file path as run records keep it: relative to the repository (or kit) root, else the
    bare file name, so that no user or folder name of the generating host is recorded."""
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return p.name


def utc_iso(s):
    """A timestamp with a local offset (Ollama's ``modified_at``) in UTC: run records keep no
    time zone of the generating host."""
    t = parse_time(s) if isinstance(s, str) else None
    return iso(t) if t else s


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return iter(())
    def gen():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
    return gen()


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class HttpResult:
    __slots__ = ("status", "body", "elapsed", "error")

    def __init__(self, status: int, body: bytes, elapsed: float, error: str | None = None):
        self.status, self.body, self.elapsed, self.error = status, body, elapsed, error

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def http_get(url: str, headers: dict | None = None, timeout: float = 30.0,
             retries: int = 3, backoff: float = 2.0) -> HttpResult:
    """GET with retries on transport errors and 5xx; 404 is a valid answer."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return HttpResult(r.status, r.read(), time.monotonic() - t0)
        except urllib.error.HTTPError as e:
            try:
                body = e.read() if hasattr(e, "read") else b""
            except Exception:  # noqa: BLE001 - the status is the answer; an error body that stalls
                body = b""      # (npm's 404 pages can) would otherwise end the whole classification
            if e.code < 500 and e.code != 429:
                return HttpResult(e.code, body, time.monotonic() - t0)
            last_err = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 - transport errors are retried
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(backoff * (attempt + 1))
    return HttpResult(-1, b"", 0.0, last_err)


def quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def chunked(it: Iterable, n: int) -> Iterator[list]:
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf


# --------------------------------------------------------------------------
# pseudonyms for names an outsider could register (PROTOCOL.md, section 9)

MASK_KEY = ROOT / "secrets" / "mask_key"


def mask_id(eco: str, name: str) -> str:
    """Stable pseudonym ``ph-<10 hex>``: HMAC-SHA256 under a key that stays with the authors
    (``secrets/mask_key``, not in the review artifact).  The same name always gets the same
    identifier, so counts and joins can be checked without the name; publishing the key after
    the registries have been informed lets anyone verify the mapping."""
    import hmac
    key = MASK_KEY.read_text(encoding="utf-8").strip().encode()
    return "ph-" + hmac.new(key, f"{eco}:{name}".encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def reg_norm(eco: str, name: str) -> str:
    """A name as its registry compares names: PEP 503 for PyPI, exact for the Arduino Library
    Manager (its names differ by case), case-blind elsewhere."""
    from .registries import norm_pypi
    return norm_pypi(name) if eco == "pypi" else (name if eco == "arduino" else name.lower())


def mask_key(eco: str, owner: str | None, name: str) -> str:
    """Pseudonym of a registry name as the registry compares names (PEP 503 for PyPI, exact for the
    Arduino Library Manager, whose names differ by case, case-blind elsewhere), so that spellings an
    installer treats as one name share one pseudonym.  A name that already is a pseudonym is returned
    unchanged (analysis of the pseudonymised data)."""
    import re as _re
    if _re.fullmatch(r"ph-[0-9a-f]{10}", name or ""):
        return name
    return mask_id(eco, f"{(owner or '').lower()}/{reg_norm(eco, name)}")
