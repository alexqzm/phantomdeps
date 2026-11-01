"""tools/import_study_results.py: greedy and follow-up archives of the study kit are imported byte
for byte (CRLF lines of a Windows host included), only with the plans written here, and a second
import of the same archive changes nothing."""

import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("import_study_results", ROOT / "tools" / "import_study_results.py")
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)

CFG = b"run_id: run-x\nresample: {n: 2, temperature: 0.7, seeds_from: 1}\nmodels: []\n"


def rec(model, prompt, idx, text="t", max_tokens=64):
    return {"model_id": model, "prompt_id": prompt, "sample_idx": idx, "text": text, "max_tokens": max_tokens}


def crlf(recs):
    return b"".join(json.dumps(r).encode() + b"\r\n" for r in recs)


class Import(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "run_study_ollama.yaml").write_bytes(CFG)

    def tearDown(self):
        self.tmp.cleanup()

    def _zip(self, name, members):
        p = Path(self.tmp.name) / name
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("config/run_study_ollama.yaml", CFG)
            for k, v in members.items():
                z.writestr(k, v)
        return p

    def _run(self, path):
        with redirect_stdout(io.StringIO()) as out:
            imp.main([str(path), "--root", str(self.root)])
        return json.loads(out.getvalue())

    def test_greedy_then_followup(self):
        greedy = crlf([rec("m", "p1", 0), rec("m", "p2", 0)])
        meta = json.dumps({"run_id": "run-x", "invocations": [{
            "started_at": "a", "pass": "generate",
            "config": {"_path": "\\".join(["C:", "Us" + "ers", "someone", "Downloads",      # built at run time,
                                              "PhantomDeps-CPS_study_kit_win_1", "config", "run.yaml"])},  # see check_anon
            "served": {"m": {"modified_at": "2026-10-25T18:16:44.7551776+03:00"}}}]})
        z1 = self._zip("PhantomDeps_study_greedy_1.zip", {"data/runs/run-x/generations.jsonl": greedy,
                                                           "data/runs/run-x/run_meta.json": meta,
                                                           "logs/runner.log": "host log"})
        r = self._run(z1)
        run = self.root / "data" / "runs" / "run-x"
        self.assertEqual((run / "generations.jsonl").read_bytes(), greedy)
        self.assertEqual(r["run-x"]["added"], 2)
        self.assertTrue((self.root / "data" / "study_host_logs" / "greedy_1" / "runner.log").exists())
        self.assertEqual(self._run(z1)["run-x"]["added"], 0)          # idempotent
        inv = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))["invocations"][0]
        self.assertEqual(inv["config"]["_path"], "config/run.yaml")    # no user folder of the host
        self.assertEqual(inv["served"]["m"]["modified_at"], "2026-10-25T15:16:44Z")
        rplan = json.dumps({"run_id": "run-x", "pairs": [["m", "p1"]]}).encode()
        eplan = json.dumps({"run_id": "run-x", "pass": "extend", "pairs": [["m", "p2"]],
                            "max_tokens": {"m": 4096}}).encode()
        (run / "resample_plan.json").write_bytes(rplan)
        (run / "extend_plan.json").write_bytes(eplan)
        failed = dict(rec("m", "p1", 2, text=""), error="timed out")     # retried by the runner
        res = crlf([rec("m", "p1", 1), failed, rec("m", "p1", 2)])
        ext = crlf([rec("m", "p2", 0, max_tokens=4096)])
        fmeta = json.dumps({"run_id": "run-x", "invocations": [{"started_at": "b", "pass": "resample"}]})
        emeta = json.dumps({"run_id": "run-x-ext", "invocations": [{"started_at": "c", "pass": "extend"}]})
        z2 = self._zip("PhantomDeps_study_followup_2.zip", {
            "data/runs/run-x/generations.jsonl": res, "data/runs/run-x/run_meta.json": fmeta,
            "data/runs/run-x-ext/generations.jsonl": ext, "data/runs/run-x-ext/run_meta.json": emeta,
            "plans/resample_plan.json": rplan, "plans/extend_plan.json": eplan})
        r = self._run(z2)
        self.assertEqual((run / "generations.jsonl").read_bytes(), greedy + res)
        self.assertEqual((self.root / "data" / "runs" / "run-x-ext" / "generations.jsonl").read_bytes(), ext)
        m = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual([i["pass"] for i in m["invocations"]], ["generate", "resample"])
        self.assertEqual((r["run-x"]["added"], r["run-x"]["failed_requests_added"], r["run-x-ext"]["added"]),
                         (3, 1, 1))
        self.assertEqual(self._run(z2)["run-x"]["added"], 0)

    def test_refuses_foreign_plan_changed_record_and_bad_paths(self):
        run = self.root / "data" / "runs" / "run-x"
        run.mkdir(parents=True)
        (run / "resample_plan.json").write_bytes(b'{"run_id": "run-x", "pairs": [["m", "p1"]]}')
        other = b'{"run_id": "run-x", "pairs": [["m", "p9"]]}'
        z = self._zip("PhantomDeps_study_followup_3.zip", {"plans/resample_plan.json": other})
        with self.assertRaises(SystemExit):
            self._run(z)
        (run / "generations.jsonl").write_bytes(crlf([rec("m", "p1", 0)]))
        z = self._zip("PhantomDeps_study_greedy_4.zip", {
            "data/runs/run-x/generations.jsonl": crlf([rec("m", "p1", 0, text="other")]),
            "data/runs/run-x/run_meta.json": "{}"})
        with self.assertRaises(SystemExit):
            self._run(z)
        z = self._zip("PhantomDeps_study_greedy_5.zip", {"../evil.txt": "x"})
        with self.assertRaises(SystemExit):
            self._run(z)


if __name__ == "__main__":
    unittest.main()
