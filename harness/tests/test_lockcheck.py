"""Lock-file check, reuse mode: a manifest whose text is unchanged keeps its earlier record (and its
date), a new or changed one is locked again.  The tools are not run here (check_manifest is
replaced), so the test needs no cargo, npm or uv."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phantomdeps import lockcheck as lc


def _row(prompt: str, name: str, version: str | None = None) -> dict:
    return {"run_id": "r", "model_id": "m", "prompt_id": prompt, "sample_idx": 0, "eco": "npm",
            "name": name, "version": version, "cls": "exists"}


class _Ctx:
    def __init__(self, *args, **kwargs):
        self.bin = {t: "/usr/bin/true" for t in ("cargo", "npm", "node", "uv")}
        self.env = {eco: {} for eco in lc.ECOS}

    def env_overrides(self, eco: str) -> dict:
        return {}


class ReuseTest(unittest.TestCase):
    def test_unchanged_manifest_keeps_record_changed_one_is_locked_again(self):
        k1, k2, k3 = (("r", "m", p, 0, "npm") for p in ("p1", "p2", "p3"))
        groups = {k1: [_row("p1", "left-pad", "^1.3.0")], k2: [_row("p2", "lodash")],
                  k3: [_row("p3", "express", "4")]}
        same = lc._manifest("npm", lc._declared("npm", groups[k1])[0])
        old_date = "2026-10-24T19:07:42Z"

        def rec(k, content, mark, **extra):
            return {"run_id": k[0], "model_id": k[1], "prompt_id": k[2], "sample_idx": k[3], "eco": k[4],
                    "manifest": {"file": "package.json", "content": content}, "deps": [], "lock_ok": True,
                    "mark": mark, **extra}

        old = {"generated_at": old_date, "lockcheck_sha256": "0" * 64, "tools": {"npm": "10.9.4"},
               "manifests": [rec(k1, same[1], "old"),
                             rec(k2, '{"dependencies": {"lodahs": "*"}}\n', "old"),   # text changed since
                             ]}                                                          # k3: new manifest
        checked = []

        def check(ctx, k, rows):
            checked.append(k[2])
            return dict(rec(k, lc._manifest("npm", lc._declared("npm", rows)[0])[1], "new"), duration_s=0.0)

        with tempfile.TemporaryDirectory() as d:
            prev, out = Path(d) / "old.json", Path(d) / "new.json"
            prev.write_text(json.dumps(old), encoding="utf-8")
            with mock.patch.object(lc, "load_declarations", return_value=(groups, {})), \
                    mock.patch.object(lc, "_Ctx", _Ctx), mock.patch.object(lc, "_version", return_value="v"), \
                    mock.patch.object(lc, "check_manifest", side_effect=check), \
                    mock.patch.object(lc, "add_provenance"), mock.patch.object(lc, "summarise", return_value={}):
                res = lc.run(["r"], out=out, reuse=prev)
            written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(checked, ["p2", "p3"])
        by = {m["prompt_id"]: m for m in written["manifests"]}
        self.assertEqual(by["p1"]["mark"], "old")
        self.assertEqual(by["p1"]["reused_from"], old_date)
        self.assertNotIn("checked_at", by["p1"])
        for p in ("p2", "p3"):
            self.assertEqual(by[p]["mark"], "new")
            self.assertIn("checked_at", by[p])
            self.assertNotIn("reused_from", by[p])
        self.assertEqual(written["reused"]["manifests"], 1)
        self.assertEqual(written["reused"]["from_generated_at"], old_date)
        self.assertEqual(res["reused"], written["reused"])

    def test_reused_record_keeps_its_first_date(self):
        k = ("r", "m", "p1", 0, "npm")
        groups = {k: [_row("p1", "left-pad")]}
        content = lc._manifest("npm", lc._declared("npm", groups[k])[0])[1]
        old = {"generated_at": "2026-10-27T10:13:23Z", "manifests": [
            {"run_id": "r", "model_id": "m", "prompt_id": "p1", "sample_idx": 0, "eco": "npm",
             "manifest": {"content": content}, "deps": [], "lock_ok": True,
             "reused_from": "2026-10-24T19:07:42Z"}]}
        with tempfile.TemporaryDirectory() as d:
            prev, out = Path(d) / "old.json", Path(d) / "new.json"
            prev.write_text(json.dumps(old), encoding="utf-8")
            with mock.patch.object(lc, "load_declarations", return_value=(groups, {})), \
                    mock.patch.object(lc, "_Ctx", _Ctx), mock.patch.object(lc, "_version", return_value="v"), \
                    mock.patch.object(lc, "check_manifest", side_effect=AssertionError("must not lock")), \
                    mock.patch.object(lc, "add_provenance"), mock.patch.object(lc, "summarise", return_value={}):
                res = lc.run(["r"], out=out, reuse=prev)
        self.assertEqual(res["manifests"][0]["reused_from"], "2026-10-24T19:07:42Z")


if __name__ == "__main__":
    unittest.main()
