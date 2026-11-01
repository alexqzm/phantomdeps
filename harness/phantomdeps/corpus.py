"""Task corpus: render prompts from ``corpus/tasks_v1.yaml``.

Prompt identifiers are ``<eco>-<NN>-<family>``.  The rendered prompt set is
written to ``corpus/prompts_v1.jsonl`` together with the SHA-256 of the YAML
source, so a run can prove which corpus it used.
"""

from __future__ import annotations

import yaml

from .util import CORPUS, append_jsonl, rel_path, sha256_file, write_json

SYSTEM_PROMPT = "You are a helpful assistant for embedded and cyber-physical systems development."
FAMILIES = ("F1", "F2")


def load(path=None) -> dict:
    p = path or (CORPUS / "tasks_v1.yaml")
    with open(p, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    doc["_sha256"] = sha256_file(p)
    doc["_path"] = rel_path(p)
    return doc


def render(doc: dict | None = None, families=FAMILIES) -> list[dict]:
    doc = doc or load()
    out = []
    for eco, tasks in doc["tasks"].items():
        tmpl = doc["ecosystems"][eco]["templates"]
        for i, t in enumerate(tasks, start=1):
            for fam in families:
                prompt = " ".join(tmpl[fam].format(board=t["board"], task=t["task"]).split())
                out.append({"prompt_id": f"{eco}-{i:02d}-{fam}", "task_id": f"{eco}-{i:02d}",
                            "eco": eco, "family": fam, "tier": t["tier"], "cat": t["cat"],
                            "board": t["board"], "system": SYSTEM_PROMPT, "user": prompt,
                            "corpus_version": doc["version"], "corpus_sha256": doc["_sha256"]})
    return out


def write(doc: dict | None = None) -> dict:
    doc = doc or load()
    prompts = render(doc)
    path = CORPUS / f"prompts_v{doc['version']}.jsonl"
    if path.exists():
        path.unlink()
    for p in prompts:
        append_jsonl(path, p)
    meta = {"version": doc["version"], "tasks_sha256": doc["_sha256"], "n_prompts": len(prompts),
            "prompts_sha256": sha256_file(path),
            "n_tasks": sum(len(v) for v in doc["tasks"].values()),
            "ecosystems": {e: len(v) for e, v in doc["tasks"].items()}}
    write_json(CORPUS / f"prompts_v{doc['version']}.meta.json", meta)
    return meta
