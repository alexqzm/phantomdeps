#!/usr/bin/env python3
"""Run the study (config/run_study_ollama.yaml) on a local machine with Ollama, unattended.

Steps (each one resumable; run the same command again after any interruption):

  check     environment: Python, Ollama version, GPU, free disk, access to the model hub
  prepare   for every model: download the pinned hub GGUF (resume, size and SHA-256 check),
            import it with `ollama create` using the chat template, default system message and
            default parameters of the Ollama library tag (config/ollama_library/<id>/), verify the
            imported weight blob digest, then a smoke test of 4 prompts with automatic checks
  greedy    the greedy pass for every model that passed its smoke test
  resample  the persistence pass, pairs from a plan file written by `phantomdeps resample-plan`
            on the host that classified the greedy pass
  pack      zip the run records and logs into results/ for sending back
  all       check, prepare, greedy, pack

The script starts its own `ollama serve` with OLLAMA_CONTEXT_LENGTH=8192 and OLLAMA_NUM_PARALLEL=1
(PROTOCOL.md, section 4) and stops it at the end; on Windows it first closes the Ollama tray app
so that these settings apply.  It keeps the computer awake while it works.

Usage: python tools/study_runner.py [check|prepare|greedy|resample|pack|all] [--models ID ...]
       [--plan FILE] [--models-dir DIR] [--ollama-models DIR] [--hf-endpoint URL] [--keep-gguf]
       [--no-server] [--force]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
if (ROOT / "vendor").is_dir():
    sys.path.insert(0, str(ROOT / "vendor"))

import yaml  # noqa: E402  (vendored in the Windows kit)

CONFIG = ROOT / "config" / "run_study_ollama.yaml"
LIB = ROOT / "config" / "ollama_library"
LOGS = ROOT / "logs"
RESULTS = ROOT / "results"
STATUS = LOGS / "status.json"
MIN_OLLAMA = (0, 30, 0)          # library templates of gemma3 and gpt-oss require 0.30.0
SERVER_ENV = {"OLLAMA_CONTEXT_LENGTH": "8192", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_KEEP_ALIVE": "10m"}
UA = "phantomdeps-cps-study/1.0"
WIN = os.name == "nt"
# special tokens of the chat formats in the study: in an answer they mean a wrong prompt format
LEAKS = ("<|im_end|>", "<|im_start|>", "<|im_sep|>", "<|eot_id|>", "<|start_header_id|>", "<|end_header_id|>",
         "<end_of_turn>", "<start_of_turn>", "<|endoftext|>", "<｜end▁of▁sentence｜>", "<|end|>", "<|return|>",
         "<|channel|>", "<|message|>", "<|start_of_role|>", "<|end_of_role|>", "<|end_of_text|>",
         "### Instruction:")


# ----------------------------------------------------------------------------- helpers

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / "runner.log", "a", encoding="utf-8") as f:
        f.write(f"{now()} {msg}\n")


def load_status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"models": {}}


def save_status(st: dict) -> None:
    LOGS.mkdir(exist_ok=True)
    st["updated_at"] = now()
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATUS)


def set_model(st: dict, mid: str, **kw) -> None:
    st.setdefault("models", {}).setdefault(mid, {}).update(kw)
    save_status(st)


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def no_quick_edit() -> None:
    """Windows: a mouse click in the console window (QuickEdit selection) would pause all output and
    with it the run until a key is pressed; switch QuickEdit off for this console."""
    if WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)                          # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value | 0x80) & ~0x40)   # ENABLE_EXTENDED_FLAGS, no QUICK_EDIT


def keep_awake(on: bool) -> None:
    """Windows: no idle sleep while the runner works (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)."""
    if WIN:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def selected(cfg: dict, only: list[str] | None) -> list[dict]:
    ms = cfg["models"]
    if only:
        unknown = set(only) - {m["id"] for m in ms}
        if unknown:
            raise SystemExit(f"unknown model ids: {sorted(unknown)}")
        ms = [m for m in ms if m["id"] in only]
    return ms


# ----------------------------------------------------------------------------- Ollama

class Ollama:
    def __init__(self, base: str, exe: str | None, models_dir: str | None):
        self.base = base.rstrip("/")
        self.exe = exe or shutil.which("ollama") or self._default_exe()
        self.models_dir = models_dir
        self.proc = None

    @staticmethod
    def _default_exe() -> str:
        if WIN:
            p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if p.exists():
                return str(p)
        return "ollama"

    def api(self, path: str, body: dict | None = None, timeout: float = 60) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def up(self) -> bool:
        try:
            self.api("/api/version", timeout=3)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def version(self) -> tuple[int, ...]:
        v = self.api("/api/version").get("version", "0")
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)

    def start(self) -> None:
        """Our own server with the protocol's settings (the tray app's server would ignore them)."""
        pidfile = LOGS / "ollama_server.pid"
        if self.up() and pidfile.exists():               # left behind by an interrupted run of ours
            try:
                pid = int(pidfile.read_text().strip())
                if WIN:                                   # /T: the model runners it started, too
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    os.kill(pid, 15)
                for _ in range(20):
                    if not self.up():
                        break
                    time.sleep(1)
            except (ValueError, OSError):
                pass
        if self.up():
            if WIN:
                log("Ollama is already running; closing it to start it with the protocol settings")
                for im in ("ollama app.exe", "ollama.exe"):
                    subprocess.run(["taskkill", "/IM", im, "/T", "/F"], capture_output=True)
                for _ in range(30):
                    if not self.up():
                        break
                    time.sleep(1)
            if self.up():
                raise SystemExit("a server already answers at " + self.base + "; stop it and run again "
                                 "(or use --no-server if it runs with OLLAMA_CONTEXT_LENGTH=8192 "
                                 "and OLLAMA_NUM_PARALLEL=1)")
        env = dict(os.environ, **SERVER_ENV)
        env["OLLAMA_HOST"] = self.base.split("://", 1)[-1]
        if self.models_dir:
            Path(self.models_dir).mkdir(parents=True, exist_ok=True)
            env["OLLAMA_MODELS"] = self.models_dir
        LOGS.mkdir(exist_ok=True)
        out = open(LOGS / "ollama_server.log", "ab")
        flags = 0x08000000 if WIN else 0          # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([self.exe, "serve"], env=env, stdout=out, stderr=subprocess.STDOUT,
                                     creationflags=flags)
        pidfile.write_text(str(self.proc.pid))
        for _ in range(120):
            if self.up():
                log(f"Ollama {'.'.join(map(str, self.version()))} started "
                    f"(context {SERVER_ENV['OLLAMA_CONTEXT_LENGTH']}, one request at a time)")
                return
            if self.proc.poll() is not None:
                break
            time.sleep(1)
        raise SystemExit(f"ollama serve did not start; see {LOGS / 'ollama_server.log'}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None and WIN:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc:
            (LOGS / "ollama_server.pid").unlink(missing_ok=True)

    def show(self, name: str) -> dict | None:
        try:
            return self.api("/api/show", {"model": name}, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    @staticmethod
    def weight_digest(show: dict) -> str | None:
        """SHA-256 of the weight blob, from the FROM line of the stored Modelfile."""
        m = re.search(r"^FROM\s+.*?sha256[-:]([0-9a-f]{64})", show.get("modelfile") or "", re.M)
        return m.group(1) if m else None

    def create(self, name: str, modelfile: Path) -> None:
        r = subprocess.run([self.exe, "create", name, "-f", str(modelfile)], cwd=str(modelfile.parent),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        (LOGS / f"create_{name}.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError(f"ollama create {name} failed: {(r.stderr or r.stdout).strip()[-400:]}")

    def delete(self, name: str) -> None:
        subprocess.run([self.exe, "rm", name], capture_output=True)

    def ps(self) -> list[dict]:
        try:
            return self.api("/api/ps").get("models", [])
        except (urllib.error.URLError, OSError, ValueError):
            return []


# ----------------------------------------------------------------------------- download

def download(url: str, dest: Path, size: int, sha256: str, tries: int = 8) -> None:
    """Resumable download with size and SHA-256 check; the hash covers the whole file."""
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, tries + 1):
        h = hashlib.sha256()
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have:
            with open(part, "rb") as f:                        # resume: hash what is already there
                for chunk in iter(lambda: f.read(8 << 20), b""):
                    h.update(chunk)
        try:
            if have < size:
                req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Range": f"bytes={have}-"} if have else {})})
                with urllib.request.urlopen(req, timeout=60) as r:
                    if have and r.status != 206:                  # server ignored the range: start over
                        have, h = 0, hashlib.sha256()
                        mode = "wb"
                    else:
                        mode = "ab"
                    t0, got, last = time.monotonic(), 0, 0.0
                    with open(part, mode) as f:
                        for chunk in iter(lambda: r.read(8 << 20), b""):
                            f.write(chunk)
                            h.update(chunk)
                            got += len(chunk)
                            el = time.monotonic() - t0
                            if el - last >= 15:
                                last = el
                                speed = got / el if el else 0
                                done = have + got
                                eta = int((size - done) / speed) if speed else 0
                                print(f"    {dest.name}: {human(done)} / {human(size)}, {human(speed)}/s, "
                                      f"~{eta // 60} min left", flush=True)
            if part.stat().st_size != size:
                raise OSError(f"size {part.stat().st_size} != {size}")
            if h.hexdigest() != sha256:
                part.unlink()
                raise OSError("SHA-256 mismatch, the file was discarded")
            os.replace(part, dest)
            return
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
            log(f"  download of {dest.name}: attempt {attempt}/{tries} failed ({e!r})")
            if attempt == tries:
                raise
            time.sleep(min(60, 10 * attempt))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- Modelfile

def modelfile_text(m: dict, source: str) -> str:
    """Modelfile with the library tag's template, default system message and parameters.  `source`
    is ./<gguf> for a fresh import or the model's own name to re-create it on its stored weights."""
    d = LIB / m["id"]
    tpl = expected_template(m)
    sysmsg = (d / "system.txt").read_text(encoding="utf-8") if (d / "system.txt").exists() else None
    params = json.loads((d / "params.json").read_text(encoding="utf-8"))
    for s in [tpl] + ([sysmsg] if sysmsg else []):
        if '"""' in s:
            raise SystemExit(f"{m['id']}: template contains triple quotes")
    lines = [f"FROM {source}", f'TEMPLATE """{tpl}"""']
    if sysmsg:
        lines.append(f'SYSTEM """{sysmsg}"""')
    for k, v in params.items():
        for x in (v if isinstance(v, list) else [v]):
            lines.append(f'PARAMETER {k} "{x}"' if isinstance(x, str) else f"PARAMETER {k} {x}")
    return "\n".join(lines) + "\n"


def write_modelfile(path: Path, m: dict, source: str) -> None:
    # newline="\n": in Windows text mode every \n would become \r\n, and Ollama keeps those \r in the
    # template, which changes every prompt (seen on the first Windows run; the template check caught it)
    path.write_text(modelfile_text(m, source), encoding="utf-8", newline="\n")


def expected_template(m: dict) -> str:
    return (LIB / m["id"] / "template.txt").read_text(encoding="utf-8")


def expected_params(m: dict) -> list[tuple[str, str]]:
    params = json.loads((LIB / m["id"] / "params.json").read_text(encoding="utf-8"))
    return sorted((k, str(x)) for k, v in params.items() for x in (v if isinstance(v, list) else [v]))


def stored_params(info: dict) -> list[tuple[str, str]]:
    out = []
    for line in (info.get("parameters") or "").splitlines():
        if line.strip():
            k, _, v = line.strip().partition(" ")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            out.append((k, v))
    return sorted(out)


def _same_params(a: list, b: list) -> bool:
    def norm(x):
        try:
            return float(x)
        except ValueError:
            return x
    return [(k, norm(v)) for k, v in a] == [(k, norm(v)) for k, v in b]


def model_problems(info: dict, m: dict) -> list[str]:
    """What differs between a stored model and the protocol: weights, template, system, defaults."""
    probs = []
    if Ollama.weight_digest(info) != m["sha256"]:
        probs.append(f"weight digest {Ollama.weight_digest(info)} != {m['sha256']}")
    tpl = info.get("template") or ""
    if tpl != expected_template(m):
        i = next((k for k, (a, b) in enumerate(zip(tpl, expected_template(m))) if a != b),
                 min(len(tpl), len(expected_template(m))))
        probs.append(f"template differs from the library template at character {i} "
                     f"(stored {tpl[i:i + 12]!r}, expected {expected_template(m)[i:i + 12]!r})")
    sp = LIB / m["id"] / "system.txt"
    if sp.exists() and (info.get("system") or "") != sp.read_text(encoding="utf-8"):
        probs.append("default system message differs from the library one")
    if not _same_params(stored_params(info), expected_params(m)):
        probs.append(f"default parameters {stored_params(info)} != library {expected_params(m)}")
    return probs


# ----------------------------------------------------------------------------- steps

def check(args, cfg) -> dict:
    env = {"time": now(), "python": sys.version.split()[0], "platform": platform.platform(),
           "machine": platform.machine(), "processor": platform.processor()}
    log(f"Python {env['python']} on {env['platform']}")
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is needed")
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=30)
        env["gpu"] = r.stdout.strip() or r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired):
        env["gpu"] = None
    log(f"GPU: {env['gpu'] or 'no NVIDIA GPU found (no nvidia-smi): the models would run on the CPU, very slowly'}")
    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    ol_dir = Path(args.ollama_models or os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    ol_dir.mkdir(parents=True, exist_ok=True)
    st = load_status()
    todo = [m for m in selected(cfg, args.models) if not st["models"].get(m["id"], {}).get("imported")]

    def on_disk(m):                                   # bytes of this GGUF already downloaded
        for p in (models_dir / m["gguf_file"], models_dir / (m["gguf_file"] + ".part")):
            if p.exists():
                return min(p.stat().st_size, m["size"])
        return 0
    # files are downloaded one at a time, imported (a copy into Ollama's store) and then deleted
    need_blobs = sum(m["size"] for m in todo)
    need_tmp = max([m["size"] - on_disk(m) for m in todo] or [0])
    free_dl = shutil.disk_usage(models_dir).free
    free_ol = shutil.disk_usage(ol_dir).free
    same = os.stat(models_dir).st_dev == os.stat(ol_dir).st_dev
    margin = 3 << 30
    env.update({"models_dir": str(models_dir), "ollama_models": str(ol_dir), "free_download_dir": free_dl,
                "free_ollama_dir": free_ol, "need_bytes": need_blobs + need_tmp, "same_disk": same})
    (LOGS / "env.json").write_text(json.dumps(env, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"Disk needed: {human(need_blobs)} for the Ollama models ({ol_dir}) and up to {human(need_tmp)} "
        f"for one download at a time ({models_dir}); free: {human(free_ol)}"
        + ("" if same else f" and {human(free_dl)}"))
    short = (free_ol < need_blobs + need_tmp + margin) if same else \
        (free_ol < need_blobs + margin or free_dl < need_tmp + margin)
    if short and not args.force:
        raise SystemExit("not enough free disk space: free some space or use another disk, e.g. "
                         "run_study.cmd -ModelsDir D:\\pdcps\\download -OllamaModels D:\\pdcps\\ollama")
    try:
        req = urllib.request.Request(args.hf_endpoint.rstrip("/") + "/api/models/Qwen/Qwen3-8B-GGUF",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30):
            env["hub"] = "ok"
    except (urllib.error.URLError, OSError) as e:
        env["hub"] = f"error: {e}"
        log(f"No access to {args.hf_endpoint}: {e}. A mirror can be given: -HfEndpoint https://hf-mirror.com")
    (LOGS / "env.json").write_text(json.dumps(env, indent=1, ensure_ascii=False), encoding="utf-8")
    return env


def ensure_model(ol: Ollama, m: dict, args, st: dict) -> None:
    name = m["endpoint_model"]
    dl = Path(args.models_dir)
    gguf = dl / m["gguf_file"]
    info = ol.show(name)
    if info and Ollama.weight_digest(info) == m["sha256"]:
        probs = model_problems(info, m)
        if probs:                                     # weights verified: re-create on the stored blob
            log(f"  {name}: weights verified; re-creating it with the library template and defaults "
                f"({probs[0]})")
            mf = dl / f"{m['id']}.Modelfile"
            dl.mkdir(parents=True, exist_ok=True)
            write_modelfile(mf, m, name)
            ol.create(name, mf)
            mf.unlink()
            info = ol.show(name) or {}
            probs = model_problems(info, m)
            if probs:
                raise RuntimeError(f"{name}: " + "; ".join(probs))
        _ready(m, info, args, st, gguf)
        return
    dl.mkdir(parents=True, exist_ok=True)
    if gguf.exists() and gguf.stat().st_size == m["size"]:
        log(f"  {m['gguf_file']}: already downloaded, checking SHA-256")
        if sha256_of(gguf) != m["sha256"]:
            gguf.unlink()
    if not gguf.exists():
        url = f"{args.hf_endpoint.rstrip('/')}/{m['gguf_repo']}/resolve/{m['gguf_revision']}/{m['gguf_file']}"
        log(f"  downloading {m['gguf_repo']}/{m['gguf_file']} ({human(m['size'])})")
        download(url, gguf, m["size"], m["sha256"])
    log(f"  {m['gguf_file']}: size and SHA-256 match; importing into Ollama")
    mf = dl / f"{m['id']}.Modelfile"
    write_modelfile(mf, m, "./" + gguf.name)
    ol.create(name, mf)
    mf.unlink()
    info = ol.show(name) or {}
    probs = model_problems(info, m)
    if probs:
        raise RuntimeError(f"{name}: " + "; ".join(probs))
    _ready(m, info, args, st, gguf)


def _ready(m: dict, info: dict, args, st: dict, gguf: Path) -> None:
    """A verified model: record it and remove its download, which Ollama now holds as a blob."""
    if not st["models"].get(m["id"], {}).get("imported") or st["models"][m["id"]].get("prepare_error"):
        set_model(st, m["id"], imported=now(), weight_sha256=Ollama.weight_digest(info),
                  template_sha256=hashlib.sha256((info.get("template") or "").encode("utf-8")).hexdigest(),
                  parameters=info.get("parameters"), details=info.get("details"), prepare_error=None)
        log(f"  {m['endpoint_model']}: ready (weights {m['sha256'][:12]}..., template and defaults of "
            f"{m['library_tag']})")
    if gguf.exists() and not args.keep_gguf:
        gguf.unlink()
        log(f"  {gguf.name}: removed from the download folder (Ollama holds it)")


def _records(run_id: str) -> list[dict]:
    p = ROOT / "data" / "runs" / run_id / "generations.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass                                   # a line cut by a crash; the runner redoes it
    return out


def smoke_check(cfg: dict, m: dict) -> tuple[bool, list[str], dict]:
    """The 4 smoke answers of a model: answered, no special tokens, code blocks, not all cut."""
    from phantomdeps import corpus
    first = [p["prompt_id"] for p in corpus.render(families=tuple(cfg["families"]))][:4]
    latest = {}
    for r in _records(cfg["run_id"]):
        if r["model_id"] == m["id"] and r.get("sample_idx") == 0 and r["prompt_id"] in first:
            latest[r["prompt_id"]] = r
    recs = [latest[p] for p in first if p in latest]
    problems = []
    if len(recs) < 4:
        problems.append(f"only {len(recs)} of 4 answers")
    errs = [r for r in recs if r.get("error")]
    if errs:
        problems.append("server errors: " + "; ".join(sorted({r['error'][:160] for r in errs})))
    ok = [r for r in recs if not r.get("error")]
    texts = [re.sub(r"<think>.*?</think>", "", r.get("text") or "", flags=re.S) for r in ok]
    empty = sum(1 for t in texts if not t.strip())
    if empty > 1:
        problems.append(f"empty answers: {empty} of {len(ok)}"
                        + (" (the reasoning did not fit the token budget)" if any(r.get('reasoning_text') for r in ok) else ""))
    leak = sorted({tok for t in texts for tok in LEAKS if tok in t})
    if leak:
        problems.append("special tokens of a chat format in the answers: " + ", ".join(leak))
    fenced = sum(1 for t in texts if "```" in t)
    if ok and fenced < 2:
        problems.append(f"code blocks in only {fenced} of {len(ok)} answers")
    cut = sum(1 for r in ok if r.get("finish_reason") == "length")
    if cut > 2:
        problems.append(f"{cut} of {len(ok)} answers cut by the token limit")
    lat = [r.get("latency_s") or 0 for r in ok]
    toks = [((r.get("usage") or {}).get("completion_tokens") or 0) for r in ok]
    stats = {"answers": len(recs), "mean_latency_s": round(sum(lat) / len(lat), 1) if lat else None,
             "tokens_per_s": round(sum(toks) / sum(lat), 1) if lat and sum(lat) else None,
             "reasoning_separate": sum(1 for r in ok if r.get("reasoning_text")), "cut": cut}
    return not problems, problems, stats


def prepare(args, cfg) -> None:
    from phantomdeps import llm
    st = load_status()
    ol = Ollama(cfg["endpoint"], args.ollama_exe, args.ollama_models)
    ms = selected(cfg, args.models)
    for i, m in enumerate(ms, 1):
        log(f"Model {i}/{len(ms)}: {m['id']} ({m['gguf_repo']}, {m['quant']}, {human(m['size'])})")
        try:
            ensure_model(ol, m, args, st)
        except (RuntimeError, OSError, ValueError, urllib.error.URLError, http.client.HTTPException,
                subprocess.SubprocessError) as e:
            set_model(st, m["id"], prepare_error=str(e)[:500], smoke_ok=False)
            log(f"  ERROR: {e}; this model is skipped, the others continue")
            continue
        if st["models"].get(m["id"], {}).get("smoke_ok") and not args.force:
            log("  smoke test already passed")
            continue
        log("  smoke test: 4 answers")
        llm.run(CONFIG, [m["id"]], 4)
        good, problems, stats = smoke_check(cfg, m)
        loaded = [p for p in ol.ps() if p.get("name", "").startswith(m["endpoint_model"])]
        if loaded:
            p = loaded[0]
            vram = p.get("size_vram") or 0
            stats["gpu_share"] = round(vram / p["size"], 2) if p.get("size") else None
            stats["context_length"] = p.get("context_length")
        set_model(st, m["id"], smoke_ok=good, smoke_problems=problems, smoke_stats=stats, smoke_at=now(),
                  prepare_error=None)
        gpu = stats.get("gpu_share")
        log(f"  smoke test {'passed' if good else 'FAILED'}: ~{stats.get('mean_latency_s')} s per answer, "
            f"{stats.get('tokens_per_s')} tokens/s" + (f", {int(gpu * 100)}% in GPU memory" if gpu is not None else "")
            + (f", context {stats['context_length']}" if stats.get("context_length") else ""))
        for p in problems:
            log(f"    - {p}")
        tps = stats.get("tokens_per_s") or 0
        if gpu == 0 or (m.get("params_b", 99) <= 9 and 0 < tps < 15):
            log("    ! the model seems to run on the CPU, not the GPU: check the NVIDIA driver and that "
                "the laptop is on mains power")
        if stats.get("context_length") and int(stats["context_length"]) < 8192:
            log("    ! context below 8192: the server does not run with the protocol settings")


def greedy(args, cfg) -> None:
    from phantomdeps import llm
    st = load_status()
    ms = [m for m in selected(cfg, args.models)
          if args.force or st["models"].get(m["id"], {}).get("smoke_ok")]
    skipped = [m["id"] for m in selected(cfg, args.models) if m not in ms]
    if skipped:
        log("Not in the greedy pass (preparation or smoke test failed): " + ", ".join(skipped))
    if not ms:
        log("No model for the greedy pass")
        return
    log(f"Greedy pass: {len(ms)} model(s), 192 answers each")
    for attempt in (1, 2):                           # a second pass retries answers that failed
        llm.run(CONFIG, [m["id"] for m in ms])
        errs = {}
        done = {}
        for r in _records(cfg["run_id"]):
            if r.get("sample_idx") == 0:
                k = (r["model_id"], r["prompt_id"])
                if r.get("error"):
                    errs.setdefault(k, r["error"])
                else:
                    done[k] = True
        left = {k for k in errs if k not in done}
        for m in ms:
            n = sum(1 for k in done if k[0] == m["id"])
            set_model(st, m["id"], greedy_answers=n, greedy_failed=sum(1 for k in left if k[0] == m["id"]))
        if not left:
            break
        log(f"{len(left)} answers missing; {'retrying' if attempt == 1 else 'they remain for the next start'}")
    log("Greedy pass finished")


def resample(args, cfg) -> None:
    from phantomdeps import llm
    plan = Path(args.plan) if args.plan else ROOT / "resample_plan.json"
    if not plan.exists():
        raise SystemExit(f"no plan file {plan}: put resample_plan.json into the kit folder")
    log(f"Persistence pass from {plan.name}")
    llm.resample(CONFIG, args.models, plan)
    log("Persistence pass finished")


def followup(args, cfg) -> None:
    """Second session on the GPU host: the persistence pass and the extension of cut answers, from
    the plans written where the greedy pass was classified (both optional, both resumable)."""
    from phantomdeps import llm
    rplan = Path(args.plan) if args.plan else ROOT / "resample_plan.json"
    eplan = ROOT / "extend_plan.json"
    plans = [(x, json.loads(x.read_text(encoding="utf-8"))) for x in (rplan, eplan) if x.exists()]
    if not plans:
        raise SystemExit("no plan file: put resample_plan.json and extend_plan.json into the kit folder")
    ids = {pair[0] for _, pl in plans for pair in pl["pairs"]}
    ol = Ollama(cfg["endpoint"], args.ollama_exe, args.ollama_models)
    first = ROOT / "first_session.json"          # written by build_study_kit.py --followup
    if first.exists():
        want = json.loads(first.read_text(encoding="utf-8")).get("ollama_version")
        have = ".".join(map(str, ol.version()))
        if want and have != want:
            log(f"WARNING: Ollama {have} here, the greedy pass ran on Ollama {want}; the answers are "
                f"generated and the version is recorded, but an extended answer may then not continue "
                f"the cut one exactly")
    st = load_status()
    for m in cfg["models"]:                  # the models must be the verified ones of the greedy pass
        if m["id"] in ids:
            ensure_model(ol, m, args, st)
    log(f"Models verified (weights, template, defaults): {len(ids)}")
    for path, pl in plans:
        n = int(pl.get("n") or (cfg.get("resample") or {}).get("n", 0)) if pl.get("pass") != "extend" else 1
        log(f"{path.name}: {len(pl['pairs'])} (model, prompt) pairs x {n} answers")
    # The largest model (last in the configuration; on a 12 GB GPU part of it runs on the CPU)
    # answers about as slowly as all the others together, so its persistence pass comes last:
    # everything else is done first, and an interrupted session leaves only that tail.
    last = cfg["models"][-1]["id"]
    sel = list(args.models) if args.models else [m["id"] for m in cfg["models"]]
    head, tail = [x for x in sel if x != last], [x for x in sel if x == last]
    if rplan.exists() and head:
        log(f"Persistence pass from {rplan.name}" + (f" (every model but {last}, which comes last)" if tail else ""))
        llm.resample(CONFIG, head, rplan)
    if eplan.exists():
        log(f"Extension of cut answers from {eplan.name}")
        llm.extend(CONFIG, eplan, sel)
        log("Extension finished")
    if rplan.exists() and tail:
        log(f"Persistence pass of {last}")
        llm.resample(CONFIG, tail, rplan)
    if rplan.exists():
        log("Persistence pass finished")


def pack(args, cfg, phase: str) -> Path:
    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = RESULTS / f"PhantomDeps_study_{phase}_{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rid in (cfg["run_id"], cfg["run_id"] + "-ext"):
            run_dir = ROOT / "data" / "runs" / rid
            for p in sorted(run_dir.glob("*")) if run_dir.is_dir() else []:
                if p.is_file():
                    z.write(p, f"data/runs/{rid}/{p.name}")
        for p in (ROOT / "resample_plan.json", ROOT / "extend_plan.json"):
            if p.exists():
                z.write(p, f"plans/{p.name}")
        for p in sorted(LOGS.glob("*")):
            if p.is_file() and p.suffix in (".json", ".log", ".txt"):
                z.write(p, f"logs/{p.name}")
        z.write(CONFIG, "config/run_study_ollama.yaml")
    log(f"Results: {out} ({human(out.stat().st_size)})")
    (RESULTS / "latest.txt").write_text(str(out), encoding="utf-8")
    return out


def summary(cfg) -> None:
    st = load_status()
    log("Models:")
    for m in cfg["models"]:
        s = st["models"].get(m["id"], {})
        state = ("preparation failed" if s.get("prepare_error") else
                 "smoke test failed" if s.get("smoke_ok") is False else
                 f"{s.get('greedy_answers', 0)}/192 answers" if s.get("smoke_ok") else "not started")
        log(f"  {m['id']:24s} {state}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="study_runner")
    ap.add_argument("step", nargs="?", default="all",
                    choices=["check", "prepare", "greedy", "resample", "followup", "pack", "all"])
    ap.add_argument("--models", nargs="*")
    ap.add_argument("--plan")
    ap.add_argument("--models-dir", default=str(ROOT / "models"))
    ap.add_argument("--ollama-models", default=None, help="OLLAMA_MODELS for the server this script starts")
    ap.add_argument("--ollama-exe", default=os.environ.get("OLLAMA_EXE"))
    ap.add_argument("--hf-endpoint", default=os.environ.get("HF_ENDPOINT", "https://huggingface.co"))
    ap.add_argument("--keep-gguf", action="store_true", help="keep the downloaded GGUF after import")
    ap.add_argument("--no-server", action="store_true", help="use the Ollama server that is already running")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    sys.stderr = sys.stdout              # progress lines on stdout: a console shows them as plain text
    no_quick_edit()
    os.environ.setdefault("PHANTOMDEPS_PROGRESS_LOG", str(LOGS / "progress.log"))
    LOGS.mkdir(exist_ok=True)
    cfg = load_config()
    log(f"=== {args.step} ({cfg['run_id']}, {len(cfg['models'])} models in the study) ===")
    if args.step == "pack":
        pack(args, cfg, "manual")
        return 0
    if args.step in ("check", "all"):
        check(args, cfg)
        if args.step == "check":
            return 0
    ol = Ollama(cfg["endpoint"], args.ollama_exe, args.ollama_models)
    if not args.no_server:
        ol.start()
    elif not ol.up():
        raise SystemExit("Ollama is not running")
    if ol.version() < MIN_OLLAMA:
        ol.stop()
        raise SystemExit(f"Ollama {'.'.join(map(str, MIN_OLLAMA))} or newer is needed; update it from ollama.com")
    keep_awake(True)
    try:
        if args.step in ("prepare", "all"):
            prepare(args, cfg)
        if args.step in ("greedy", "all"):
            greedy(args, cfg)
        if args.step == "resample":
            resample(args, cfg)
        if args.step == "followup":
            followup(args, cfg)
    except KeyboardInterrupt:
        log("Interrupted. Start again: the work continues where it stopped.")
        return 130
    finally:
        keep_awake(False)
        if not args.no_server:
            ol.stop()
    if args.step in ("all", "prepare", "greedy"):
        summary(cfg)
    if args.step in ("all", "greedy", "resample", "followup"):
        pack(args, cfg, {"resample": "resample", "followup": "followup"}.get(args.step, "greedy"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
