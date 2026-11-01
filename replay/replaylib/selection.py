"""Deterministic choice of phantom names from the pilot classification.

Rows kept: cls == "phantom", squattable == "open", sample_idx == 0, eco in the
three replay ecosystems.  Distinct names are sorted by sha256("<eco>:<name>")
and the first three that are *valid package names for that tool* are taken.  The
horizon h of a chosen name is the model_horizon of the first row (sorted by
model_id, prompt_id) that declared it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ECOS = ("pypi", "npm", "crates")

# Package-name validity per installer (PROTOCOL.md section 2 "Name semantics").
_PEP508 = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.IGNORECASE)
_CRATE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_NPM = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def valid_name(eco: str, name: str) -> bool:
    if eco == "pypi":
        return bool(_PEP508.match(name))
    if eco == "crates":
        return bool(_CRATE.match(name)) and len(name) <= 64
    if eco == "npm":
        # npm: <=214 chars, lowercase, no leading dot/underscore, url-safe.
        return (name == name.lower() and len(name) <= 214 and not name.startswith((".", "_"))
                and bool(_NPM.match(name)))
    return False


def _sha(eco: str, name: str) -> str:
    return hashlib.sha256(f"{eco}:{name}".encode()).hexdigest()


def select_esp(deps_path: Path) -> dict | None:
    """Choose ONE ESP Component Registry phantom for the CPS replay.

    ESP declarations carry a namespace (owner), so the paper masks them as
    ``mask_key('espidf', owner, name)`` -- a pseudonym that embeds the namespace.
    We reconstruct that SAME pseudonym here and use it as the squat's operational
    component name, so (a) it is a harmless ``ph-<hex>`` placeholder that never
    resolves on the real registry, and (b) ``mask_key`` is idempotent on a
    pseudonym, so the replay's squat pseudonym equals the paper's per-declaration
    pseudonym in deps_masked.jsonl and cross-checks.

    Rows kept: cls == 'phantom', sample_idx == 0, eco == 'espidf'.  The chosen
    pseudonym is the first by sha256('espidf:<pseudonym>'); its horizon is the
    model_horizon of the first declaring row (by model_id, prompt_id).  Returns
    None if the run declared no such phantom.
    """
    from phantomdeps.util import mask_key  # harness on sys.path (set by run_replay)

    rows = [json.loads(l) for l in open(deps_path, encoding="utf-8") if l.strip()]
    esp = [r for r in rows if r.get("eco") == "espidf" and r.get("cls") == "phantom"
           and r.get("sample_idx") == 0 and r.get("name")]
    cand: dict = {}
    for r in esp:
        pseud = mask_key("espidf", r.get("owner"), r["name"])
        decl = sorted([x for x in esp if mask_key("espidf", x.get("owner"), x["name"]) == pseud],
                      key=lambda x: (x.get("model_id", ""), x.get("prompt_id", "")))
        cand.setdefault(pseud, decl[0]["model_horizon"])
    if not cand:
        return None
    pseud = sorted(cand, key=lambda p: _sha("espidf", p))[0]
    return {"name": pseud, "horizon": cand[pseud], "sha": _sha("espidf", pseud)}


def select(deps_path: Path, per_eco: int = 3) -> dict:
    """Return {eco: [ {name, horizon, sha}, ... ]} with the chosen phantoms."""
    rows = [json.loads(l) for l in open(deps_path, encoding="utf-8") if l.strip()]
    sel = [r for r in rows if r.get("cls") == "phantom" and r.get("squattable") == "open"
           and r.get("sample_idx") == 0 and r.get("eco") in ECOS]
    out: dict = {}
    for eco in ECOS:
        eco_rows = [r for r in sel if r["eco"] == eco]
        names = sorted({r["name"] for r in eco_rows}, key=lambda n: _sha(eco, n))
        chosen = []
        for n in names:
            if not valid_name(eco, n):
                continue
            declaring = sorted([r for r in eco_rows if r["name"] == n],
                               key=lambda r: (r["model_id"], r["prompt_id"]))
            chosen.append({"name": n, "horizon": declaring[0]["model_horizon"], "sha": _sha(eco, n)})
            if len(chosen) == per_eco:
                break
        out[eco] = chosen
    return out
