"""Measures added for the study: the repetitive-tail mark of a repetition loop, the prefix rule of
the extension pass (shared with tools/check_extension.py) and the exact McNemar test."""

import importlib.util
import unittest
from pathlib import Path

from phantomdeps.analysis import LOOP_ZLIB, keeps_prefix, mcnemar_exact
from phantomdeps.classify import TAIL_CHARS, tail_zlib

ROOT = Path(__file__).resolve().parents[2]


def _check_extension():
    spec = importlib.util.spec_from_file_location("check_extension", ROOT / "tools" / "check_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TailTest(unittest.TestCase):
    def test_loop_compresses_below_threshold(self):
        loop = "lib_deps =\n" + "    adafruit/Adafruit Sensor Lib @ ^1.0.0\n" * 200
        self.assertLess(tail_zlib(loop), LOOP_ZLIB)

    def test_code_and_prose_do_not(self):
        # the harness's own source and protocol stand for code and prose
        code = (ROOT / "harness" / "phantomdeps" / "gate.py").read_text(encoding="utf-8")
        prose = (ROOT / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertGreater(tail_zlib(code), LOOP_ZLIB)
        self.assertGreater(tail_zlib(prose), LOOP_ZLIB)
        self.assertGreater(tail_zlib(code[:5000]), LOOP_ZLIB)

    def test_tail_only_and_empty(self):
        self.assertIsNone(tail_zlib(""))
        self.assertIsNone(tail_zlib(None))
        head = "".join(chr(33 + (i * 37) % 90) for i in range(5000))   # incompressible head
        loop = "x = 1\n" * 1000
        self.assertEqual(tail_zlib(head + loop), tail_zlib(loop[-TAIL_CHARS:]))


class PrefixTest(unittest.TestCase):
    def test_rule_and_tool_agree(self):
        ce = _check_extension()
        cases = [({"text": "abc"}, {"text": "abcdef"}),
                 ({"text": "abc"}, {"text": "abd"}),
                 ({"text": "", "reasoning_text": "think"}, {"text": "answer", "reasoning_text": "think more"}),
                 ({"text": "", "reasoning_text": "think"}, {"text": "answer", "reasoning_text": "thought"}),
                 ({"text": None}, {"text": "x"})]
        for cut, ext in cases:
            self.assertEqual(keeps_prefix(cut, ext), ce.keeps_prefix(cut, ext))
        self.assertTrue(keeps_prefix(*cases[0]))
        self.assertFalse(keeps_prefix(*cases[1]))
        self.assertTrue(keeps_prefix(*cases[2]))
        self.assertFalse(keeps_prefix(*cases[3]))
        self.assertEqual(ce.shared_chars({"text": "abcx"}, {"text": "abcy"}), (3, 4))


class McNemarTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(mcnemar_exact(1, 7), 0.0703125)        # 2 * (1 + 8) / 256
        self.assertAlmostEqual(mcnemar_exact(7, 1), mcnemar_exact(1, 7))
        self.assertAlmostEqual(mcnemar_exact(5, 5), 1.0)
        self.assertLess(mcnemar_exact(0, 20), 1e-5)


if __name__ == "__main__":
    unittest.main()
