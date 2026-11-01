"""OpenAI-compatible chat client and a resumable generation runner.

Works with any server exposing ``/v1/chat/completions``: Ollama, llama.cpp
``llama-server``, LM Studio, vLLM.  For llama.cpp the runner can start and
stop one server per model (``server_cmd`` in the run configuration).

Records are appended to ``data/runs/<run_id>/generations.jsonl`` and keyed by
(model_id, prompt_id, sample_idx); an interrupted run resumes where it
stopped.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import corpus
from .util import (ROOT, RUNS, append_jsonl, read_jsonl, rel_path, sha256_file, sha256_text, utc_iso, utcnow,
                   write_json)


RETRY_PAUSE_S = 10.0     # before the one retry of a request that failed with a server error


def chat(base_url: str, model: str, system: str, user: str, *, temperature: float, top_p: float,
         max_tokens: int, seed: int | None, extra: dict | None = None, timeout: float = 900) -> dict:
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
            "temperature": temperature, "top_p": top_p, "max_tokens": max_tokens, "stream": False}
    if seed is not None:
        body["seed"] = seed
    if extra:
        body.update(extra)
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    resp["_latency_s"] = round(time.monotonic() - t0, 3)
    return resp


def _wait_health(base_url: str, timeout: float = 300) -> None:
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    raise TimeoutError(f"server at {base_url} not healthy after {timeout}s")


class Server:
    """Start a llama.cpp server for one model (pilot runs on a single host)."""

    def __init__(self, cmd: list[str] | None, base_url: str, log: Path):
        self.cmd, self.base_url, self.log, self.proc = cmd, base_url, log, None

    def __enter__(self):
        if self.cmd:
            # never talk to a stale server left behind by an interrupted run
            try:
                with urllib.request.urlopen(self.base_url.rstrip("/") + "/health", timeout=3):
                    pass
                raise RuntimeError(f"a server already answers at {self.base_url}; stop it first "
                                   "(e.g. pkill -x llama-server)")
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            self.log.parent.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(self.cmd, stdout=open(self.log, "ab"), stderr=subprocess.STDOUT,
                                         preexec_fn=os.setsid)
            _wait_health(self.base_url)
        return self

    def alive(self) -> bool:
        return self.proc is None or self.proc.poll() is None

    def __exit__(self, *exc):
        if self.proc:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)


def ollama_info(base_url: str, tag: str) -> dict:
    """Provenance of a model served by Ollama: version, digest, quantisation and the
    Modelfile parameters that apply when a request does not set them."""
    base = base_url.rstrip("/")
    out: dict = {}
    try:
        with urllib.request.urlopen(base + "/api/version", timeout=30) as r:
            out["ollama_version"] = json.load(r).get("version")
        req = urllib.request.Request(base + "/api/show", data=json.dumps({"model": tag}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        wm = re.search(r"^FROM\s+.*?sha256[-:]([0-9a-f]{64})", d.get("modelfile") or "", re.M)
        out.update({"details": d.get("details"), "parameters": d.get("parameters"),
                    "modified_at": utc_iso(d.get("modified_at")), "capabilities": d.get("capabilities"),
                    "template_sha256": sha256_text(d.get("template") or ""),
                    "system_sha256": sha256_text(d["system"]) if d.get("system") else None,
                    "weights_sha256": wm.group(1) if wm else None})
        with urllib.request.urlopen(base + "/api/tags", timeout=30) as r:
            names = {tag, tag if ":" in tag else tag + ":latest"}
            for m in json.load(r).get("models", []):
                if names & {m.get("name"), m.get("model")}:
                    out["digest"], out["size"] = m.get("digest"), m.get("size")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def host_id() -> str:
    """Pseudonymous machine id: tells runs on different hosts apart without recording a host
    name, which may carry a person's name into the review artifact."""
    return "host-" + sha256_text(platform.node())[:10]


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_path"] = rel_path(path)          # no user or folder name of the host in the records
    return cfg


def _model_params(cfg: dict, m: dict, family: str) -> tuple[int, float, dict]:
    """Decoding parameters for one request: run-level ``params`` overridden by
    the model's own ``params`` (e.g. a larger token budget for reasoning models)."""
    params = dict(cfg.get("params", {}))
    params.update(m.get("params") or {})
    mt = params.pop("max_tokens", {})
    max_tokens = mt.get(family, 512) if isinstance(mt, dict) else int(mt)
    top_p = params.pop("top_p", 1.0)
    return max_tokens, top_p, params


def _one(cfg: dict, m: dict, base: str, p: dict, s: dict) -> dict:
    max_tokens, top_p, extra = _model_params(cfg, m, p["family"])
    top_p = s.get("top_p", top_p)
    rec = {"run_id": cfg["run_id"], "model_id": m["id"], "model_horizon": m.get("horizon"),
           "prompt_id": p["prompt_id"], "task_id": p["task_id"], "eco": p["eco"],
           "family": p["family"], "tier": p["tier"], "cat": p["cat"], "sample_idx": s["idx"],
           "temperature": s["temperature"], "seed": s.get("seed"), "top_p": top_p,
           "max_tokens": max_tokens, "extra": extra, "started_at": utcnow()}
    # A request may take its whole budget at 2 tokens/s, and at least 15 minutes: the 32B model,
    # partly on the CPU of a 12 GB GPU, writes 4-5 tokens/s and slows down as its context grows, so
    # an extended answer of 4,096 tokens would pass a fixed 15-minute limit.
    timeout = max(900.0, 0.5 * max_tokens)
    try:
        try:
            resp = chat(base, m.get("endpoint_model", m["id"]), p["system"], p["user"],
                        temperature=s["temperature"], top_p=top_p, max_tokens=max_tokens,
                        seed=s.get("seed"), extra=extra, timeout=timeout)
        except (urllib.error.URLError, ConnectionError) as e:
            # a server or connection error (a model runner that restarts) is retried once; a timeout
            # is not, it would only repeat
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 or \
                    isinstance(getattr(e, "reason", None), TimeoutError):
                raise
            rec["retried_after"] = f"{type(e).__name__}: {e}"
            time.sleep(RETRY_PAUSE_S)
            resp = chat(base, m.get("endpoint_model", m["id"]), p["system"], p["user"],
                        temperature=s["temperature"], top_p=top_p, max_tokens=max_tokens,
                        seed=s.get("seed"), extra=extra, timeout=timeout)
        ch = resp["choices"][0]
        msg = ch["message"]
        if m.get("file") and resp.get("model") and resp["model"] != m["file"]:
            raise KeyError(f"served model {resp['model']} is not {m['file']}")
        rec.update({"text": msg.get("content") or "", "finish_reason": ch.get("finish_reason"),
                    "usage": resp.get("usage"), "timings": resp.get("timings"),
                    "latency_s": resp["_latency_s"], "served_model": resp.get("model")})
        # Reasoning models: some servers return the reasoning separately.  It is
        # kept for inspection but never analysed (PROTOCOL.md, section 4).
        for k in ("reasoning", "reasoning_content", "thinking"):
            if msg.get(k):
                rec["reasoning_text"] = msg[k]
                break
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, OSError) as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["finished_at"] = utcnow()
    return rec


def _generate(cfg: dict, todo_by_model: dict, run_dir: Path, out: Path, concurrency: int = 1) -> None:
    """todo_by_model: model_id -> list of (prompt, sample) pairs still to generate.

    ``concurrency`` > 1 sends that many requests at once (server slots).  Greedy
    passes use 1: batched decoding is not guaranteed to be bit-identical to
    single-sequence decoding."""
    lock = threading.Lock()
    for m in cfg["models"]:
        todo = todo_by_model.get(m["id"])
        if not todo:
            continue
        base = m.get("endpoint") or cfg["endpoint"]
        cmd = None
        if m.get("server_cmd"):
            cmd = [c.format(root=str(ROOT), **m) for c in shlex.split(m["server_cmd"])]
        log = run_dir / f"server_{m['id']}.log"
        prog = {"n": len(todo), "done": 0, "errors": 0, "t0": time.monotonic()}
        with Server(cmd, base, log) as srv:
            def work(ps, m=m, base=base, srv=srv, log=log, prog=prog):
                rec = _one(cfg, m, base, *ps)
                with lock:
                    append_jsonl(out, rec)
                    _progress(m["id"], prog, rec)
                if rec.get("error") and not srv.alive():
                    # e.g. killed for memory; stop instead of logging one error per prompt
                    raise RuntimeError(f"server for {m['id']} exited ({rec['error']}); see {log}")
            if concurrency <= 1:
                for ps in todo:
                    work(ps)
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as ex:
                    list(ex.map(work, todo))


def _progress(model_id: str, prog: dict, rec: dict) -> None:
    """One line per answer on stderr (and in $PHANTOMDEPS_PROGRESS_LOG): count, speed and ETA."""
    prog["done"] += 1
    prog["errors"] += 1 if rec.get("error") else 0
    el = time.monotonic() - prog["t0"]
    per = el / prog["done"]
    eta = int(per * (prog["n"] - prog["done"]))
    toks = ((rec.get("usage") or {}).get("completion_tokens")) or 0
    line = (f"[{utcnow()}] {model_id}: {prog['done']}/{prog['n']}"
            f"{' ERROR ' + rec['error'][:120] if rec.get('error') else ''}"
            f"  {rec.get('latency_s') or 0:.1f} s, {toks} tok"
            f"  ~{per:.0f} s/answer, left {eta // 3600}:{eta % 3600 // 60:02d}"
            f"{'  errors ' + str(prog['errors']) if prog['errors'] else ''}")
    print(line, file=sys.stderr, flush=True)
    path = os.environ.get("PHANTOMDEPS_PROGRESS_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _prepare(cfg: dict) -> tuple[Path, Path, set, dict]:
    run_dir = RUNS / cfg["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "generations.jsonl"
    done = {(r["model_id"], r["prompt_id"], r["sample_idx"]) for r in read_jsonl(out) if not r.get("error")}
    doc = corpus.load()
    for m in cfg["models"]:
        f = m.get("file")
        if f and Path(f).exists() and not m.get("sha256"):
            m["sha256"] = sha256_file(Path(f))
    return run_dir, out, done, doc


def _families(cfg: dict) -> tuple[str, ...]:
    fams = list(cfg["families"])
    for m in cfg["models"]:
        for f in m.get("families") or []:
            if f not in fams:
                fams.append(f)
    return tuple(fams)


def _log_invocation(path: Path, entry: dict) -> None:
    """run_meta.json keeps every invocation (generate and resample passes)."""
    meta = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    if "invocations" not in meta:
        meta = {"run_id": entry["run_id"], "invocations": [meta] if meta else []}
    meta["invocations"].append(entry)
    write_json(path, meta)


def run(config_path: str | Path, only_models: list[str] | None = None, limit: int | None = None,
        prompt_filter: str | None = None) -> Path:
    cfg = load_config(config_path)
    run_dir, out, done, doc = _prepare(cfg)
    prompts = corpus.render(doc, families=_families(cfg))
    if cfg.get("tasks"):                      # a fixed subset of tasks (e.g. the equal-budget family comparison)
        keep = set(cfg["tasks"])
        prompts = [p for p in prompts if p["task_id"] in keep]
    if prompt_filter:                         # substrings of the prompt id, comma-separated (any matches)
        subs = [x for x in prompt_filter.split(",") if x]
        prompts = [p for p in prompts if any(x in p["prompt_id"] for x in subs)]
    if limit:
        prompts = prompts[:limit]
    todo = {}
    for m in cfg["models"]:
        if only_models and m["id"] not in only_models:
            continue
        fams = set(m.get("families") or cfg["families"])
        todo[m["id"]] = [(p, s) for p in prompts if p["family"] in fams for s in cfg["samples"]
                         if (m["id"], p["prompt_id"], s["idx"]) not in done]
    conc = int(cfg.get("concurrency", 1))
    entry = {"run_id": cfg["run_id"], "pass": "generate", "config": cfg, "corpus_sha256": doc["_sha256"],
             "started_at": utcnow(), "host": host_id(), "platform": platform.platform(),
             "python": platform.python_version(), "concurrency": conc,
             "n_todo": {k: len(v) for k, v in todo.items()}}
    if cfg.get("server") == "ollama":
        entry["served"] = {m["id"]: ollama_info(m.get("endpoint") or cfg["endpoint"], m.get("endpoint_model", m["id"]))
                           for m in cfg["models"] if m["id"] in todo}
    _generate(cfg, todo, run_dir, out, conc)
    entry["finished_at"] = utcnow()
    _log_invocation(run_dir / "run_meta.json", entry)
    return out


def phantom_pairs(cfg: dict, run_dir: Path) -> set:
    """(model, prompt) pairs whose greedy answer had a phantom in a classified run (deps.jsonl),
    on the primary families (those every model answers)."""
    rs = cfg.get("resample") or {}
    fams = set(rs.get("families") or cfg["families"])
    return {(d["model_id"], d["prompt_id"]) for d in read_jsonl(run_dir / "deps.jsonl")
            if d.get("cls") == "phantom" and d.get("sample_idx") == 0 and d.get("family") in fams}


def write_plan(config_path: str | Path, out_path: str | Path | None = None,
               keep: str | Path | None = None) -> Path:
    """Resample plan for a run generated on another host: the phantom pairs of the classified
    greedy pass, with the provenance of that classification.  The host that generates the
    persistence pass runs ``resample CONFIG --plan FILE`` without needing the registries.
    ``keep``: a plan already in use (after a change of the classification); its pairs stay in the
    new plan, so that the answers generated from it remain plan answers, and the pairs it adds and
    the ones that no longer have a phantom are listed."""
    cfg = load_config(config_path)
    run_dir = RUNS / cfg["run_id"]
    meta_path = run_dir / "classify_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    now = phantom_pairs(cfg, run_dir)
    old = None
    if keep:
        old = json.loads(Path(keep).read_text(encoding="utf-8"))
        if old.get("run_id") != cfg["run_id"]:
            raise SystemExit(f"{keep} is a plan for run {old.get('run_id')}")
    before = {tuple(x) for x in old["pairs"]} if old else set()
    pairs = sorted(now | before)
    plan = {"run_id": cfg["run_id"], "created_at": utcnow(), "n_pairs": len(pairs),
            "classification": {k: meta.get(k) for k in ("harness_sha256", "derived_sha256", "finished_at",
                                                         "classified_at", "generated_sha256") if k in meta},
            "deps_sha256": sha256_file(run_dir / "deps.jsonl"), "pairs": [list(p) for p in pairs]}
    if old:
        plan["supersedes"] = {"sha256": sha256_file(Path(keep)), "n_pairs": len(before),
                              "created_at": old.get("created_at")}
        plan["pairs_added"] = [list(p) for p in sorted(now - before)]
        plan["pairs_without_phantom_now"] = [list(p) for p in sorted(before - now)]
    out = Path(out_path) if out_path else run_dir / "resample_plan.json"
    write_json(out, plan)
    return out


def resample(config_path: str | Path, only_models: list[str] | None = None,
             plan: str | Path | None = None) -> Path:
    """Persistence pass: re-ask every (model, prompt) whose greedy answer had a
    phantom, ``resample.n`` times at ``resample.temperature``.  The pairs come from
    the classified run (deps.jsonl) or, on a host without the registries, from a plan
    written by ``write_plan`` on the host that classified the greedy pass."""
    cfg = load_config(config_path)
    rs = cfg.get("resample") or {"n": 3, "temperature": 0.7, "seeds_from": 1}
    run_dir, out, done, doc = _prepare(cfg)
    plan_info = None
    if plan:
        p = json.loads(Path(plan).read_text(encoding="utf-8"))
        if p.get("run_id") != cfg["run_id"]:
            raise SystemExit(f"plan is for run {p.get('run_id')}, not {cfg['run_id']}")
        phantom_keys = {tuple(x) for x in p["pairs"]}
        if p.get("n"):                        # a plan may set the number of samples; it is recorded
            rs = dict(rs, n=int(p["n"]))
        plan_info = {"path": Path(plan).name, "sha256": sha256_file(Path(plan)), "n_pairs": len(phantom_keys),
                     "n": rs["n"], "created_at": p.get("created_at"), "classification": p.get("classification")}
    else:
        phantom_keys = phantom_pairs(cfg, run_dir)
    prompts = {p["prompt_id"]: p for p in corpus.render(doc, families=_families(cfg))}
    samples = [{"idx": i, "temperature": rs["temperature"], "seed": rs.get("seeds_from", 1) + i - 1,
                "top_p": rs.get("top_p", 0.95)} for i in range(1, rs["n"] + 1)]
    todo = {}
    for (mid, pid) in sorted(phantom_keys):
        if only_models and mid not in only_models:
            continue
        for s in samples:
            if (mid, pid, s["idx"]) not in done and pid in prompts:
                todo.setdefault(mid, []).append((prompts[pid], s))
    conc = int(rs.get("concurrency", 1))
    entry = {"run_id": cfg["run_id"], "pass": "resample", "resample": rs, "started_at": utcnow(),
             "n_pairs": len(phantom_keys), "n_todo": {k: len(v) for k, v in todo.items()},
             "concurrency": conc, "plan": plan_info, "host": host_id()}
    if cfg.get("server") == "ollama":
        entry["served"] = {m["id"]: ollama_info(m.get("endpoint") or cfg["endpoint"], m.get("endpoint_model", m["id"]))
                           for m in cfg["models"] if m["id"] in todo}
    _generate(cfg, todo, run_dir, out, conc)
    entry["finished_at"] = utcnow()
    write_json(run_dir / "resample_meta.json", entry)
    _log_invocation(run_dir / "run_meta.json", entry)
    return out


EXTEND_TOKENS = 4096          # answers cut at the study budget, non-reasoning models
EXTEND_TOKENS_REASONING = 7680  # reasoning models: the 8192-token context less room for the prompt


def write_extend_plan(config_path: str | Path, out_path: str | Path | None = None) -> Path:
    """Every greedy answer cut by the token limit is generated again with a larger budget (a
    sensitivity pass, run ``<run_id>-ext``).  Greedy decoding repeats the first tokens, so the cut
    answer is a prefix of the extended one and shows what the limit removed."""
    cfg = load_config(config_path)
    run_dir = RUNS / cfg["run_id"]
    gen = run_dir / "generations.jsonl"
    cut = sorted({(r["model_id"], r["prompt_id"]) for r in read_jsonl(gen)
                  if r.get("sample_idx") == 0 and not r.get("error") and r.get("finish_reason") == "length"})
    base = _model_params(cfg, {}, "F1")[0]
    budgets = {}
    for m in cfg["models"]:
        reasoning = _model_params(cfg, m, "F1")[0] > base
        budgets[m["id"]] = EXTEND_TOKENS_REASONING if reasoning else EXTEND_TOKENS
    used = {mid for mid, _ in cut}
    plan = {"run_id": cfg["run_id"], "pass": "extend", "created_at": utcnow(),
            "rule": "greedy answers with finish_reason 'length', same seed and temperature, larger budget",
            "max_tokens": {k: v for k, v in budgets.items() if k in used}, "greedy_sha256": sha256_file(gen),
            "n_pairs": len(cut), "pairs": [list(x) for x in cut]}
    out = Path(out_path) if out_path else run_dir / "extend_plan.json"
    write_json(out, plan)
    return out


def extend(config_path: str | Path, plan: str | Path, only_models: list[str] | None = None) -> Path:
    """Generate the answers of an extend plan again with its budgets into run ``<run_id>-ext``."""
    cfg = load_config(config_path)
    p = json.loads(Path(plan).read_text(encoding="utf-8"))
    if p.get("run_id") != cfg["run_id"] or p.get("pass") != "extend":
        raise SystemExit(f"{plan} is not an extend plan for run {cfg['run_id']}")
    ext = dict(cfg)
    ext["run_id"] = cfg["run_id"] + "-ext"
    ext["models"] = [dict(m, params=dict(m.get("params") or {}, max_tokens=int(p["max_tokens"][m["id"]])))
                     for m in cfg["models"] if m["id"] in p["max_tokens"]]
    run_dir, out, done, doc = _prepare(ext)
    prompts = {q["prompt_id"]: q for q in corpus.render(doc, families=_families(cfg))}
    greedy = next(s for s in cfg["samples"] if s["idx"] == 0)
    todo = {}
    for mid, pid in sorted(tuple(x) for x in p["pairs"]):
        if (only_models and mid not in only_models) or (mid, pid, 0) in done or pid not in prompts:
            continue
        todo.setdefault(mid, []).append((prompts[pid], greedy))
    entry = {"run_id": ext["run_id"], "pass": "extend", "started_at": utcnow(), "host": host_id(),
             "platform": platform.platform(), "base_run": cfg["run_id"], "config": ext,
             "plan": {"path": Path(plan).name, "sha256": sha256_file(Path(plan)), "n_pairs": p.get("n_pairs"),
                      "max_tokens": p["max_tokens"], "greedy_sha256": p.get("greedy_sha256")},
             "n_todo": {k: len(v) for k, v in todo.items()}}
    if cfg.get("server") == "ollama":
        entry["served"] = {m["id"]: ollama_info(m.get("endpoint") or cfg["endpoint"], m.get("endpoint_model", m["id"]))
                           for m in ext["models"] if m["id"] in todo}
    _generate(ext, todo, run_dir, out, 1)
    entry["finished_at"] = utcnow()
    _log_invocation(run_dir / "run_meta.json", entry)
    return out
