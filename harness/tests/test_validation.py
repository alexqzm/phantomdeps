"""Scoring of the manual validation of extraction (PROTOCOL.md, section 5)."""

import unittest

from phantomdeps import validation as V


class Scoring(unittest.TestCase):
    def test_precision_recall_counts(self):
        rows = [
            {"eco": "pypi", "extracted": ["pypi:a", "pypi:b"], "missed": [], "spurious": ["pypi:b"]},
            {"eco": "npm", "extracted": ["npm:c"], "missed": ["npm:d", "npm:e"], "spurious": []},
            {"eco": "npm", "extracted": [], "missed": [], "spurious": []},
        ]
        r = V._pr(rows)
        self.assertEqual((r["extracted"], r["true_positive"], r["missed"]), (3, 2, 2))
        self.assertAlmostEqual(r["precision"][0], 2 / 3)
        self.assertAlmostEqual(r["recall"][0], 2 / 4)

    def test_spurious_outside_extracted_is_ignored(self):
        r = V._pr([{"eco": "pypi", "extracted": ["pypi:a"], "missed": [], "spurious": ["pypi:zzz"]}])
        self.assertEqual(r["true_positive"], 1)

    def test_split_accepts_semicolons_and_lines(self):
        self.assertEqual(V._split("a; b\nc ;"), ["a", "b", "c"])
        self.assertEqual(V._split(None), [])

    def test_csv_sheet_takes_the_ecosystem_from_the_id(self):
        import csv
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / "labels_A.csv", "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["sample_id", "extracted", "missed", "spurious", "checked", "note"])
                w.writerow(["arduino-630-000", "arduino:CAN", "", "", "y", ""])
                w.writerow(["pypi-012", "pypi:a", "", "", "y", ""])
            got = V._read(Path(d), "A")
        self.assertEqual({k: v["eco"] for k, v in got.items()}, {"arduino-630-000": "arduino", "pypi-012": "pypi"})

    def test_kappa(self):
        self.assertAlmostEqual(V.kappa([True, False, True, False], [True, False, True, False]), 1.0)
        self.assertAlmostEqual(V.kappa([True, True, False, False], [True, False, True, False]), 0.0)

    def test_score_counts_rows_whose_labels_coincide(self):
        # identical sheets give kappa 1 whatever the annotators did; the score records how many rows
        # coincide so that the text can say so
        import csv
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "validation" / "run-x"
            d.mkdir(parents=True)
            for who, missed in (("A", "pypi:b"), ("B", "pypi:b")):
                with open(d / f"labels_{who}.csv", "w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["sample_id", "extracted", "missed", "spurious", "checked", "note"])
                    w.writerow(["pypi-631-000", "pypi:a", missed, "", "y", ""])
                    w.writerow(["npm-631-001", "npm:c", "", "", "y", "a note of " + who])
            with mock.patch.object(V, "DATA", Path(tmp)):
                res = V.score("run-x")
        self.assertEqual((res["n_both"], res["identical_rows"], res["kappa_exact"]), (2, 2, 1.0))
        self.assertIsNone(res["errors"])            # no sample file: the error breakdown is skipped


if __name__ == "__main__":
    unittest.main()
