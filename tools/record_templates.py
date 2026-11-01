#!/usr/bin/env python3
"""Record how each pilot model's server formats a prompt (the chat template actually applied).

For every model of the llama.cpp run configurations this script starts the model's own
`server_cmd` on a spare port, asks the server to apply its chat template to the corpus system
message and one fixed user prompt (POST /apply-template, no generation), and writes the rendered
prompt with the server flags to data/evidence/chat_templates.json.  It shows, for example, that
DeepSeek-Coder's GGUF carries no template of its own (the server is told to use its built-in
`deepseek` format), that Phi-3-mini keeps the system message through the built-in `phi3` format, and
that the Llama 3.2 template adds its own knowledge-cutoff line and the date of the run.

Usage: python3 tools/record_templates.py [MODEL_ID ...]
"""
from __future__ import annotations

import datetime as dt
import json
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = [ROOT / "config" / "run_pilot_cpu.yaml", ROOT / "config" / "run_pilot_cpu_ext.yaml",
           ROOT / "config" / "run_pilot_cpu_famcmp.yaml"]
OUT = ROOT / "data" / "evidence" / "chat_templates.json"
PORT = 8095
PROMPT_ID = "platformio-07-F2"


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _ready(port: int, timeout: float = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - loading (503) or not listening yet
            pass
        time.sleep(1)
    return False


def main(only: list[str]) -> int:
    prompts = {json.loads(x)["prompt_id"]: json.loads(x)
               for x in (ROOT / "corpus" / "prompts_v1.0.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    p = prompts[PROMPT_ID]
    msgs = [{"role": "system", "content": p["system"]}, {"role": "user", "content": p["user"]}]
    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"models": {}}
    seen = set()
    for cfg_path in CONFIGS:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        for m in cfg["models"]:
            if m["id"] in seen or (only and m["id"] not in only) or "server_cmd" not in m:
                continue
            seen.add(m["id"])
            cmd = m["server_cmd"].format(file=m["file"]).replace("--port 8081", f"--port {PORT}")
            log = open(ROOT / "data" / "evidence" / f".template_{m['id']}.log", "w")
            proc = subprocess.Popen(shlex.split(cmd), stdout=log, stderr=subprocess.STDOUT)
            try:
                if not _ready(PORT):
                    print(f"{m['id']}: server not ready", file=sys.stderr)
                    continue
                rendered = _post(f"http://127.0.0.1:{PORT}/apply-template", {"messages": msgs})["prompt"]
                props = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/props", timeout=10).read())
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log.close()
                (ROOT / "data" / "evidence" / f".template_{m['id']}.log").unlink(missing_ok=True)
            out["models"][m["id"]] = {
                "config": cfg_path.name, "server_flags": cmd.split("--no-webui", 1)[-1].strip(),
                "template_source": (f"llama.cpp built-in '{cmd.split('--chat-template', 1)[1].split()[0]}'"
                                    if "--chat-template" in cmd else "the GGUF's own template"),
                "prompt_id": PROMPT_ID, "rendered_prompt": rendered,
                "system_message_kept": p["system"] in rendered,
                "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            print(f"{m['id']}: system message kept = {p['system'] in rendered}")
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
