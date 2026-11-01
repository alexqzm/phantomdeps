"""Pseudonymisation rule for the review artifact (analysis.to_mask / entry_masked): a phantom is
shown by its pseudonym if an outsider may be able to register it, or if a phantom of another
ecosystem with the same name may be registrable, because the name would reveal that one."""

import tempfile
import unittest
from pathlib import Path

from phantomdeps import analysis as A
from phantomdeps import util

_KEY = [None, None]   # the real key path, the temporary directory


def setUpModule():
    # a test key: the tests never read the authors' key, which the review artifact does not ship
    _KEY[0], _KEY[1] = util.MASK_KEY, tempfile.TemporaryDirectory()
    util.MASK_KEY = Path(_KEY[1].name) / "mask_key"
    util.MASK_KEY.write_text("test-key-not-the-authors-key\n", encoding="utf-8")


def tearDownModule():
    util.MASK_KEY = _KEY[0]
    _KEY[1].cleanup()


class ExtendedMasking(unittest.TestCase):
    def tearDown(self):
        A.set_registrable([])

    def test_same_name_in_another_ecosystem(self):
        deps = [{"cls": "phantom", "eco": "platformio", "name": "esptoolx", "squattable": "open"},
                {"cls": "phantom", "eco": "micropython", "name": "EspToolX", "squattable": "curated"},
                {"cls": "phantom", "eco": "pypi", "name": "other", "squattable": "curated"},
                {"cls": "exists", "eco": "pypi", "name": "esptoolx"}]
        A.set_registrable(deps)
        self.assertTrue(A.to_mask(deps[0]))
        self.assertTrue(A.to_mask(deps[1]))
        self.assertFalse(A.to_mask(deps[2]))
        self.assertFalse(A.to_mask(deps[3]))      # an existing package is never pseudonymised

    def test_entries(self):
        A.set_registrable([{"cls": "phantom", "eco": "npm", "name": "esptoolx", "squattable": "open"}])
        self.assertTrue(A.entry_masked({"name": "espressif/esptoolx", "squattable": ["vendor-namespace"]}))
        self.assertTrue(A.entry_masked({"name": "[masked]/ph-0123456789", "squattable": ["vendor-namespace"]}))
        self.assertTrue(A.entry_masked({"name": "x", "squattable": ["namespace"]}))
        self.assertFalse(A.entry_masked({"name": "other", "squattable": ["curated"]}))

    def test_owned_declarations(self):
        # ArduinoJson under a wrong PlatformIO owner: only owner/name is registrable, not the bare name
        A.set_registrable([{"cls": "phantom", "eco": "platformio", "name": "ArduinoJson", "owner": "acme",
                            "squattable": "namespace"}])
        self.assertEqual(A.REG_NAMES, {"acme/arduinojson"})
        self.assertFalse(A.to_mask({"cls": "phantom", "eco": "micropython", "name": "ArduinoJson",
                                    "squattable": "curated"}))
        self.assertTrue(A.to_mask({"cls": "phantom", "eco": "espidf", "name": "arduinojson", "owner": "ACME",
                                   "squattable": "vendor-namespace"}))

    def test_scrub_fields_that_spell_a_name(self):
        A.set_registrable([{"cls": "phantom", "eco": "pypi", "name": "fooqux", "squattable": "open"}])
        ph = A.scrub({"cls": "phantom", "eco": "pypi", "name": "fooqux", "squattable": "open", "owner": None,
                      "version": "= https://github.com/org/fooqux"})
        self.assertTrue(A.is_masked(ph["name"]))
        self.assertEqual(ph["version"], A.MASK)
        url = A.scrub({"cls": "nonregistry", "eco": "espidf", "name": "https://github.com/org/fooqux.git",
                       "version": None, "owner": None})
        self.assertEqual(url["name"], A.MASK)
        ok = A.scrub({"cls": "nonregistry", "eco": "espidf", "name": "https://github.com/org/fooquxx.git",
                      "version": None, "owner": None})
        self.assertEqual(ok["name"], "https://github.com/org/fooquxx.git")     # a longer token is another name
        ex = {"cls": "exists", "eco": "npm", "name": "fooqux", "version": "^1"}
        self.assertEqual(A.scrub(ex), ex)                                       # existing packages stay

    def test_pseudonyms_are_not_registrable_names(self):
        A.set_registrable([{"cls": "phantom", "eco": "npm", "name": "ph-0123456789", "squattable": "open"}])
        self.assertEqual(A.REG_NAMES, set())

    def test_names_in_clear_carry_their_pseudonym(self):
        # the analysis orders tied phantom names by pseudonym; a name shown in clear carries it, so the
        # pseudonymised files give the same order without the key
        A.set_registrable([{"cls": "phantom", "eco": "pypi", "name": "fooqux", "squattable": "open"}])
        clear = {"cls": "phantom", "eco": "platformio", "name": "Bar_Lib", "owner": "Espressif",
                 "squattable": "vendor-namespace"}
        row = A.scrub(clear)
        self.assertEqual(row["name"], "Bar_Lib")
        self.assertEqual(row["ph"], util.mask_key("platformio", "espressif", "bar_lib"))
        masked = A.scrub({"cls": "phantom", "eco": "pypi", "name": "Foo.Qux", "squattable": "open"})
        self.assertNotIn("ph", masked)
        self.assertEqual(masked["name"], util.mask_key("pypi", None, "foo_qux"))     # PEP 503: one name
        self.assertNotIn("ph", A.scrub({"cls": "exists", "eco": "npm", "name": "left-pad"}))

    def test_pseudonymised_names_need_no_key(self):
        saved = util.MASK_KEY
        util.MASK_KEY = Path(_KEY[1].name) / "absent"
        try:
            self.assertEqual(util.mask_key("pypi", None, "ph-0123456789"), "ph-0123456789")
            with self.assertRaises(FileNotFoundError):
                util.mask_key("pypi", None, "fooqux")
        finally:
            util.MASK_KEY = saved


if __name__ == "__main__":
    unittest.main()
