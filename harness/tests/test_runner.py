"""Generation runner against a fake Ollama server: limited greedy pass, resumption, persistence
pass from a resample plan (the Windows study kit generates on a host without the registries),
progress lines and host provenance without POSIX-only calls."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from phantomdeps import llm


class _Fake(BaseHTTPRequestHandler):
    calls = []
    fail_next = 0          # chat requests still to answer with HTTP 500 (a model runner that restarts)

    def log_message(self, *a):  # quiet
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/version":
            self._send({"version": "0.40.11"})
        elif self.path == "/api/tags":
            self._send({"models": [{"name": "fake-m1", "model": "fake-m1", "digest": "d1", "size": 1}]})
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            self._send({"details": {"quantization_level": "Q4_K_M"}, "parameters": "stop x", "template": "T",
                        "modified_at": "2026-10-24T00:00:00Z"})
        elif self.path == "/v1/chat/completions":
            _Fake.calls.append(body)
            if _Fake.fail_next:
                _Fake.fail_next -= 1
                self.send_error(500, "llama runner process has terminated")
                return
            self._send({"model": body["model"], "usage": {"completion_tokens": 12},
                        "choices": [{"finish_reason": "stop", "message": {
                            "role": "assistant", "content": "```ini\n[env:esp32]\nlib_deps = foo/bar\n```",
                            "reasoning": "thinking" if body.get("temperature") else None}}]})
        else:
            self.send_error(404)


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.old_runs = llm.RUNS
        llm.RUNS = Path(self.tmp.name) / "runs"
        cfg = {"run_id": "kit-test", "server": "ollama", "endpoint": f"http://127.0.0.1:{self.srv.server_port}",
               "families": ["F1", "F2"], "samples": [{"idx": 0, "temperature": 0.0, "seed": 7}],
               "resample": {"n": 2, "temperature": 0.7, "seeds_from": 1}, "params": {"top_p": 0.95,
               "max_tokens": {"F1": 64, "F2": 64}},
               "models": [{"id": "m1", "endpoint_model": "fake-m1", "horizon": "2024-01-01T00:00:00Z"}]}
        self.cfg = Path(self.tmp.name) / "cfg.yaml"
        self.cfg.write_text(json.dumps(cfg), encoding="utf-8")       # JSON is valid YAML
        _Fake.calls = []

    def tearDown(self):
        llm.RUNS = self.old_runs
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def _records(self):
        p = llm.RUNS / "kit-test" / "generations.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_limited_pass_resumes_and_reports(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            llm.run(self.cfg, limit=4)
        recs = self._records()
        self.assertEqual(len(recs), 4)
        self.assertTrue(all(not r.get("error") and r["text"] for r in recs))
        self.assertIn("m1: 4/4", err.getvalue())
        meta = json.loads((llm.RUNS / "kit-test" / "run_meta.json").read_text(encoding="utf-8"))
        inv = meta["invocations"][-1]
        self.assertTrue(inv["host"])
        self.assertEqual(inv["served"]["m1"]["ollama_version"], "0.40.11")
        with contextlib.redirect_stderr(io.StringIO()):
            llm.run(self.cfg, limit=4)                       # nothing left to do
        self.assertEqual(len(self._records()), 4)
        self.assertEqual(len(_Fake.calls), 4)

    def test_resample_from_plan(self):
        with contextlib.redirect_stderr(io.StringIO()):
            llm.run(self.cfg, limit=2)
        pid = self._records()[0]["prompt_id"]
        plan = Path(self.tmp.name) / "plan.json"
        plan.write_text(json.dumps({"run_id": "kit-test", "pairs": [["m1", pid]], "created_at": "x"}),
                        encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            llm.resample(self.cfg, plan=plan)
        rs = [r for r in self._records() if r["sample_idx"] > 0]
        self.assertEqual(sorted(r["sample_idx"] for r in rs), [1, 2])
        self.assertTrue(all(r["prompt_id"] == pid and r["temperature"] == 0.7 for r in rs))
        self.assertTrue(all(r.get("reasoning_text") == "thinking" for r in rs))
        meta = json.loads((llm.RUNS / "kit-test" / "resample_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["plan"]["n_pairs"], 1)
        self.assertEqual(len(meta["plan"]["sha256"]), 64)
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"run_id": "other", "pairs": []}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            llm.resample(self.cfg, plan=bad)


    def test_resample_plan_sets_n(self):
        with contextlib.redirect_stderr(io.StringIO()):
            llm.run(self.cfg, limit=1)
        pid = self._records()[0]["prompt_id"]
        plan = Path(self.tmp.name) / "plan.json"
        plan.write_text(json.dumps({"run_id": "kit-test", "pairs": [["m1", pid]], "n": 3}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            llm.resample(self.cfg, plan=plan)
        self.assertEqual(sorted(r["sample_idx"] for r in self._records() if r["sample_idx"] > 0), [1, 2, 3])
        meta = json.loads((llm.RUNS / "kit-test" / "resample_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["plan"]["n"], 3)

    def test_server_error_is_retried_once(self):
        old_pause, llm.RETRY_PAUSE_S = llm.RETRY_PAUSE_S, 0
        try:
            _Fake.fail_next = 1
            with contextlib.redirect_stderr(io.StringIO()):
                llm.run(self.cfg, limit=1)
            r = self._records()[0]
            self.assertTrue(r["text"] and not r.get("error"))
            self.assertIn("500", r["retried_after"])
            _Fake.fail_next = 2                                  # fails twice: an error record
            with contextlib.redirect_stderr(io.StringIO()):
                llm.run(self.cfg, limit=2)
            self.assertIn("500", self._records()[-1]["error"])
        finally:
            llm.RETRY_PAUSE_S = old_pause
            _Fake.fail_next = 0

    def test_extend_cut_answers(self):
        with contextlib.redirect_stderr(io.StringIO()):
            llm.run(self.cfg, limit=3)
        gen = llm.RUNS / "kit-test" / "generations.jsonl"
        recs = self._records()
        recs[1]["finish_reason"] = "length"                  # one answer cut by the token limit
        gen.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        plan = llm.write_extend_plan(self.cfg)
        p = json.loads(plan.read_text(encoding="utf-8"))
        self.assertEqual(p["pairs"], [["m1", recs[1]["prompt_id"]]])
        self.assertEqual(p["max_tokens"], {"m1": llm.EXTEND_TOKENS})
        n_before = len(_Fake.calls)
        with contextlib.redirect_stderr(io.StringIO()):
            llm.extend(self.cfg, plan)
            llm.extend(self.cfg, plan)                       # resumes: nothing left
        self.assertEqual(len(_Fake.calls), n_before + 1)
        self.assertEqual(_Fake.calls[-1]["max_tokens"], llm.EXTEND_TOKENS)
        self.assertEqual(_Fake.calls[-1]["temperature"], 0.0)
        ext = [json.loads(x) for x in (llm.RUNS / "kit-test-ext" / "generations.jsonl").read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual([(r["run_id"], r["prompt_id"], r["sample_idx"], r["max_tokens"]) for r in ext],
                         [("kit-test-ext", recs[1]["prompt_id"], 0, llm.EXTEND_TOKENS)])
        self.assertEqual(len(self._records()), 3)            # the greedy run is untouched


if __name__ == "__main__":
    unittest.main()
