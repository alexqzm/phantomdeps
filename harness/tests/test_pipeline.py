"""Proposition 2 through the whole pipeline: resolver answer -> classification ->
evidence -> decision.  The registries are replaced by fakes whose answers model the
registry state at gating time, so the test covers what the gate actually sees:
registry precedence over toolchain names, local names, owner-less PlatformIO
candidates, undated registries, and names that only match up to case or separator.
"""

import random
import subprocess
import tempfile
import unittest
from pathlib import Path

from phantomdeps.classify import classify_generation
from phantomdeps.gate import Profile, baseline_asof, evidence_for, scii
from phantomdeps.registries import Resolution

H = "2024-09-18T00:00:00Z"          # producer horizon
OLD = "2019-05-01T00:00:00Z"        # established long before h
SQUAT = "2026-10-24T00:00:00Z"      # registered by the adversary after h


class FakeRegistries:
    """Registry state at gating time: name -> (status, tau_lo, tau_hi)."""

    def __init__(self, state):
        self.state = state
        self.stdlib = {"json", "time"}

    def resolve(self, eco, name, owner=None):
        st = self.state.get((eco, (owner or "").lower(), name))
        if st is None:
            return Resolution(eco, name, "not_found")
        status, lo, hi = st
        return Resolution(eco, name, status, canonical=name, tau_lo=lo, tau_hi=hi, tau_source="fake")

    def variant_of(self, eco, name, owner=None):
        return None


class FakeBuiltins:
    def is_toolchain(self, eco, name):
        return (eco, name) in {("platformio", "Wire"), ("arduino", "Wire"), ("espidf", "esp_wifi")}


ANSWER = """```ini
[env:esp32]
lib_deps =
    Wire
    bblanchon/ArduinoJson @ ^6.21
    SquatLib
```
```yaml
dependencies:
  espressif/led_strip: "^2.5"
  acme/fresh_component: "*"
```
```xml
<package format="3"><name>my_robot</name><depend>rclcpp</depend><depend>new_ros_pkg</depend>
<depend>my_robot</depend></package>
```
```toml
[dependencies]
embedded-hal = "1.0"
fresh-crate = "0.1"
```
```bash
pip install requests fresh-pypi
npm install node-red-node-serialport node-red-contrib-fresh
```
```python
import mip
mip.install("fresh_mpy")
```"""


def generation():
    return {"run_id": "t", "model_id": "m", "prompt_id": "p-01-F2", "task_id": "p-01", "eco": "platformio",
            "family": "F2", "tier": "common", "cat": "iot", "sample_idx": 0, "model_horizon": H, "text": ANSWER}


def rand_profile(rng):
    w = [rng.random() for _ in range(4)]
    s = sum(w)
    a = dict(zip(("res", "est", "pin", "prov"), [x / s for x in w]))
    a["prov"] = 1.0 - a["res"] - a["est"] - a["pin"]
    return Profile(theta=rng.uniform(0.05, 1.0), alpha=a, delta_est_days=rng.choice([0, 30, 90]))


# one answer per ecosystem: an established, dated package next to a name the model invented
PER_ECO = {
    "platformio": ("```ini\n[env:esp32]\nlib_deps =\n    bblanchon/ArduinoJson @ ^6.21\n    SquatLib\n```",
                   "SquatLib"),
    "arduino": ("```bash\narduino-cli lib install \"ArduinoJson\" \"SquatArd\"\n```", "SquatArd"),
    "espidf": ("```yaml\ndependencies:\n  espressif/led_strip: \"^2.5\"\n  acme/fresh_component: \"*\"\n```",
               "fresh_component"),
    "crates": ("```toml\n[dependencies]\nembedded-hal = \"1.0\"\nfresh-crate = \"0.1\"\n```", "fresh-crate"),
    "pypi": ("```bash\npip install requests fresh-pypi\n```", "fresh-pypi"),
    "npm": ("```bash\nnpm install node-red-node-serialport node-red-contrib-fresh\n```", "node-red-contrib-fresh"),
    "micropython": ("```python\nimport mip\nmip.install(\"ssd1306\")\nmip.install(\"fresh_mpy\")\n```",
                    "fresh_mpy"),
    "ros2": ("```xml\n<package format=\"3\"><name>my_robot</name><depend>rclcpp</depend>"
             "<depend>new_ros_pkg</depend></package>\n```", "new_ros_pkg"),
}

ESTABLISHED = {
    ("platformio", "bblanchon", "ArduinoJson"): ("found", OLD, OLD),
    ("arduino", "", "ArduinoJson"): ("found", OLD, OLD),
    ("espidf", "espressif", "led_strip"): ("found", OLD, OLD),
    ("ros2", "", "rclcpp"): ("found", None, None),            # ROS: no dates
    ("crates", "", "embedded-hal"): ("found", OLD, OLD),
    ("pypi", "", "requests"): ("found", OLD, OLD),
    ("npm", "", "node-red-node-serialport"): ("found", OLD, OLD),
    ("micropython", "", "ssd1306"): ("found", None, OLD),     # upper bound only
}
# every name the model invented has since been registered by the adversary
SQUATS = {
    ("platformio", "", "SquatLib"): ("found", SQUAT, SQUAT),
    ("arduino", "", "SquatArd"): ("found", SQUAT, SQUAT),
    ("espidf", "acme", "fresh_component"): ("found", SQUAT, SQUAT),
    ("ros2", "", "new_ros_pkg"): ("found", None, None),       # undated registry
    ("crates", "", "fresh-crate"): ("found", SQUAT, SQUAT),
    ("pypi", "", "fresh-pypi"): ("found", SQUAT, SQUAT),
    ("npm", "", "node-red-contrib-fresh"): ("found", SQUAT, SQUAT),
    ("micropython", "", "fresh_mpy"): ("found", None, SQUAT),  # upper bound only
}


def gen_for(eco, text):
    g = generation()
    g.update({"eco": eco, "prompt_id": f"{eco}-01-F2", "task_id": f"{eco}-01", "text": text})
    return g


class Proposition2Pipeline(unittest.TestCase):
    def setUp(self):
        self.reg_before = FakeRegistries(ESTABLISHED)
        self.reg_after = FakeRegistries({**ESTABLISHED, **SQUATS})

    def decide(self, eco, reg, prof, pin, drop=None):
        rows, _ = classify_generation(gen_for(eco, PER_ECO[eco][0]), reg, FakeBuiltins(), arduino_names=set())
        deps = [r for r in rows if r["cls"] not in ("local", "special") and r["name"] != drop]
        evs = [evidence_for(d, H, prof, pipeline_pin=pin) for d in deps]
        return rows, scii([e for e in evs if e is not None], prof)

    def test_classification(self):
        rows, _ = classify_generation(generation(), self.reg_before, FakeBuiltins(), arduino_names=set())
        cls = {r["name"]: r["cls"] for r in rows}
        self.assertEqual(cls["Wire"], "toolchain")
        self.assertEqual(cls["my_robot"], "local")           # ROS workspace package
        self.assertEqual(cls["SquatLib"], "phantom")
        self.assertEqual(cls["fresh_mpy"], "phantom")

    def test_never_admitted_before_or_after_squat(self):
        """One squat per manifest, next to an established package, in every ecosystem.  The
        same manifest without the squat must be admissible under some profile (except in ROS,
        whose packages carry no dates), so the test fails if establishment stops refuting."""
        rng = random.Random(20240628)
        profiles = [rand_profile(rng) for _ in range(300)]
        for eco, (_, squat) in PER_ECO.items():
            with self.subTest(eco=eco):
                rows, _ = self.decide(eco, self.reg_before, Profile(), True)
                self.assertIn(squat, {r["name"] for r in rows if r["cls"] == "phantom"})
                admissible = False
                for prof in profiles:
                    for pin in (False, True):
                        for reg in (self.reg_before, self.reg_after):
                            _, res = self.decide(eco, reg, prof, pin)
                            self.assertNotEqual(res["decision"], "admit")
                        _, clean = self.decide(eco, self.reg_after, prof, pin, drop=squat)
                        admissible |= clean["decision"] == "admit"
                if eco != "ros2":
                    self.assertTrue(admissible, "the squat-free manifest is never admitted: the test is vacuous")

    def test_registry_takes_precedence_over_toolchain(self):
        # an adversary registers a toolchain name after h: it is now a registry package
        reg = FakeRegistries({("platformio", "", "Wire"): ("found", SQUAT, SQUAT)})
        rows, _ = classify_generation(generation(), reg, FakeBuiltins(), arduino_names=set())
        wire = [r for r in rows if r["name"] == "Wire"][0]
        self.assertEqual(wire["cls"], "exists")
        ev = evidence_for(wire, H, Profile(), pipeline_pin=True)
        self.assertEqual(ev["est"], 0)

    def test_undated_registry_requests(self):
        rows, _ = classify_generation(generation(), self.reg_after, FakeBuiltins(), arduino_names=set())
        ros = [r for r in rows if r["name"] == "new_ros_pkg"][0]
        ev = evidence_for(ros, H, Profile(), pipeline_pin=True)
        self.assertIsNone(ev["est"])
        self.assertEqual(scii([ev], Profile())["decision"], "request")


class ResolutionSemantics(unittest.TestCase):
    """Declarations count as resolved only if the installer would install them."""

    def test_pypi_project_without_files(self):
        reg = FakeRegistries({("pypi", "", "requests"): ("found", OLD, OLD),
                              ("pypi", "", "pydbc"): ("no_files", None, None),
                              ("pypi", "", "csv"): ("no_files", None, None)})
        reg.stdlib = {"json", "time", "csv"}
        rows, _ = classify_generation(gen_for("pypi", "```bash\npip install requests pydbc csv\n```"), reg,
                                      FakeBuiltins(), arduino_names=set())
        cls = {r["name"]: (r["cls"], r.get("subtype"), r.get("squattable")) for r in rows}
        self.assertEqual(cls["pydbc"], ("phantom", "no_files", "owned"))
        self.assertEqual(cls["csv"][0], "toolchain")
        self.assertEqual(cls["requests"][0], "exists")

    def test_crates_exact_case(self):
        from phantomdeps.registries import Registries

        class C:
            def lookup(self, name):
                return {"status": "found", "canonical": "lorawan", "tau": OLD}
        r = object.__new__(Registries)
        r.crates = C()
        self.assertEqual(r._r_crates("loRaWAN").status, "found_variant")
        self.assertEqual(r._r_crates("lorawan").status, "found")

    def test_crates_all_versions_yanked(self):
        from phantomdeps.registries import Registries

        class C:
            def lookup(self, name):
                return {"status": "found", "canonical": name, "tau": OLD, "n_versions": 2,
                        "n_yanked": 2 if name == "gone" else 1}
        r = object.__new__(Registries)
        r.crates = C()
        self.assertEqual(r._r_crates("gone").status, "all_yanked")
        self.assertEqual(r._r_crates("partly").status, "found")
        reg = FakeRegistries({("crates", "", "serde"): ("found", OLD, OLD),
                              ("crates", "", "gone"): ("all_yanked", OLD, OLD)})
        rows, _ = classify_generation(gen_for("crates", "```toml\n[dependencies]\nserde = \"1\"\ngone = \"0.1\"\n```"),
                                      reg, FakeBuiltins(), arduino_names=set())
        cls = {r["name"]: (r["cls"], r.get("subtype"), r.get("squattable")) for r in rows}
        self.assertEqual(cls["gone"], ("phantom", "yanked", "owned"))
        self.assertEqual(cls["serde"][0], "exists")

    def test_arduino_cli_adjusts_case_to_the_one_matching_library(self):
        from phantomdeps.registries import Registries
        r = object.__new__(Registries)
        r.arduino = {"MCP_CAN_lib": {"name": "MCP_CAN_lib"}, "Foo": {"name": "Foo"}, "FOO": {"name": "FOO"}}
        r.arduino_lower = {}
        for nm in r.arduino:
            r.arduino_lower.setdefault(nm.lower(), []).append(nm)
        r.arduino_log, r.static_seen = {"first": {}}, {}
        self.assertEqual(r._r_arduino("mcp_can_lib").detail["case_candidates"], 1)
        self.assertEqual(r._r_arduino("foo").detail["case_candidates"], 2)

        class Reg(FakeRegistries):
            def resolve(self, eco, name, owner=None):
                return r._r_arduino(name, owner)
        got = {}
        for text in ('```bash\narduino-cli lib install mcp_can_lib foo\n```', '```ini\ndepends=mcp_can_lib\n```'):
            rows, _ = classify_generation(gen_for("arduino", text), Reg({}), FakeBuiltins(), arduino_names=set())
            got.update({(x["name"], x["via"]): (x["cls"], x.get("squattable")) for x in rows})
        # arduino-cli replaces the name by the one index name equal to it ignoring case
        self.assertEqual(got[("mcp_can_lib", "arduino-cli")], ("exists", None))
        # two candidates: no adjustment; the registry refuses a name equal to another regardless of case
        self.assertEqual(got[("foo", "arduino-cli")], ("phantom", "blocked"))
        # library.properties dependencies are looked up as written
        self.assertEqual(got[("mcp_can_lib", "library.properties")], ("phantom", "blocked"))

    def test_local_names_only_in_ros(self):
        text = ("```xml\n<package format=\"3\"><name>my_robot</name><depend>rclcpp</depend></package>\n```\n"
                "```bash\npip install my_robot\n```")
        reg = FakeRegistries({("ros2", "", "rclcpp"): ("found", None, None)})
        rows, _ = classify_generation(gen_for("ros2", text), reg, FakeBuiltins(), arduino_names=set())
        pip = [r for r in rows if r["eco"] == "pypi"][0]
        self.assertEqual(pip["cls"], "phantom")

    def test_unsupported_ros_distribution_requests(self):
        text = "```bash\nsudo apt install ros-rolling-nav2-bringup\n```"
        rows, _ = classify_generation(gen_for("ros2", text), FakeRegistries({}), FakeBuiltins(), arduino_names=set())
        self.assertEqual([r["cls"] for r in rows], ["unsupported"])
        ev = evidence_for(rows[0], H, Profile(), pipeline_pin=True)
        self.assertEqual(scii([ev], Profile())["decision"], "request")
        self.assertEqual(baseline_asof(rows, H), "reject")


class Policies(unittest.TestCase):
    def test_asof_and_variants(self):
        prof = Profile()
        old = {"cls": "exists", "eco": "pypi", "tau_lo": OLD, "tau_hi": OLD}
        undated = {"cls": "exists", "eco": "ros2", "tau_lo": None, "tau_hi": None}
        self.assertEqual(baseline_asof([old], H), "admit")
        self.assertEqual(baseline_asof([old, undated], H), "reject")      # undated = unavailable (uv)
        self.assertEqual(baseline_asof([{"cls": "phantom", "eco": "npm"}], H, "squat"), "reject")
        ros_squat = {"res": 1, "est": None, "pin": None, "prov": None}
        self.assertEqual(scii([ros_squat], prof)["decision"], "request")
        self.assertEqual(scii([ros_squat], prof, absent="mandatory_refuted")["decision"], "reject")
        self.assertEqual(scii([ros_squat], prof, absent="confirmed")["decision"], "admit")

    def test_profile_validation(self):
        with self.assertRaises(ValueError):
            Profile.from_dict({"delta_est_days": -30})
        with self.assertRaises(ValueError):
            Profile.from_dict({"theta": 1.5})


class Registrability(unittest.TestCase):
    """Who could register a phantom so that the declaration resolves."""

    def test_categories(self):
        from phantomdeps.classify import squattable
        cases = [
            ({"eco": "crates", "name": "stm32f4xx_hal", "subtype": "separator_variant"}, "blocked"),
            ({"eco": "crates", "name": "fresh-crate", "subtype": "unresolved_name"}, "open"),
            ({"eco": "npm", "name": "Node-Red-Thing"}, "blocked"),
            ({"eco": "npm", "name": "node_red_x", "subtype": "separator_variant"}, "blocked"),
            ({"eco": "npm", "name": "@acme/thing"}, "namespace"),
            ({"eco": "arduino", "name": "liquidcrystal_i2c", "subtype": "separator_variant"}, "variant"),
            ({"eco": "espidf", "name": "led_strip2"}, "vendor-namespace"),
            ({"eco": "espidf", "name": "esp_modbus", "subtype": "separator_variant"}, "vendor-namespace"),
            ({"eco": "platformio", "name": "arduinojson", "owner": "bblanchon", "subtype": "separator_variant"},
             "namespace"),
            ({"eco": "platformio", "name": "arduinojson", "subtype": "separator_variant"}, "variant"),
            ({"eco": "espidf", "name": "thing", "owner": "acme"}, "namespace"),
            ({"eco": "platformio", "name": "Thing", "owner": "acme"}, "namespace"),
            ({"eco": "platformio", "name": "Thing"}, "open"),
            ({"eco": "ros2", "name": "new_ros_pkg"}, "curated"),
            ({"eco": "micropython", "name": "fresh_mpy"}, "curated"),
            ({"eco": "pypi", "name": "fresh-pypi"}, "open"),
        ]
        for dep, want in cases:
            self.assertEqual(squattable(dep), want, dep)


class PlatformIOCandidates(unittest.TestCase):
    """An owner-less name may install any same-name package: bounds cover every candidate,
    and a candidate without a date leaves the bounds undecided."""

    def run_lookup(self, dates):
        import json
        from unittest import mock
        from phantomdeps import registries as R

        class Resp:
            def __init__(self, status, body):
                self.status, self.body, self.error = status, json.dumps(body).encode(), None

            def json(self):
                return json.loads(self.body)

        items = [{"name": "SquatLib", "type": "library", "owner": {"username": f"o{i}"}} for i in range(len(dates))]

        def fake_get(url, **kw):
            if "/search?" in url:
                return Resp(200, {"page": 1, "limit": 50, "total": len(items), "items": items})
            i = int(url.split("/packages/o")[1].split("/")[0])
            return Resp(200, {"added_at": dates[i]}) if dates[i] else Resp(404, {})

        pio = R.PlatformIO(offline=False)
        pio.cache.data, pio.cache.path = {}, Path(tempfile.mkdtemp()) / "pio.json"
        with mock.patch.object(R, "http_get", fake_get):
            return pio.lookup("SquatLib")

    def test_bounds_span_candidates(self):
        rec = self.run_lookup([OLD, SQUAT])
        self.assertEqual((rec["tau_lo"][:10], rec["tau_hi"][:10]), (OLD[:10], SQUAT[:10]))

    def test_missing_date_leaves_bounds_undecided(self):
        rec = self.run_lookup([OLD, None])
        self.assertIsNone(rec["tau_lo"])
        self.assertIsNone(rec["tau_hi"])


class ArduinoRegistryDates(unittest.TestCase):
    """The registry date of a library is the date its pull request was merged into the
    registry's main line, not the date of the submitter's own commit."""

    def test_first_parent_merge_date(self):
        from phantomdeps.registries import arduino_first_seen
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args, date="2021-01-01T00:00:00+00:00", who="GitHub"):
                env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_NAME": who,
                       "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": "a@b",
                       "PATH": "/usr/bin:/bin", "HOME": tmp}
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, env=env)

            git("init", "-q", "-b", "main")
            (repo / "repositories.txt").write_text("https://github.com/a/old\n")
            git("add", "repositories.txt")
            git("commit", "-q", "-m", "initial import")
            git("checkout", "-q", "-b", "pr")
            (repo / "repositories.txt").write_text("https://github.com/a/old\nhttps://github.com/x/new\n")
            git("add", "repositories.txt")
            # the submitter backdates his own commit
            git("commit", "-q", "-m", "add new", date="2020-01-01T00:00:00+00:00", who="submitter")
            git("checkout", "-q", "main")
            git("merge", "-q", "--no-ff", "pr", "-m", "Merge pull request", date="2025-07-13T20:20:31+00:00")
            fs = arduino_first_seen(repo)
            rec = fs["first"]["github.com/x/new"]
            self.assertEqual(rec["date"][:10], "2025-07-13")
            self.assertEqual(rec["committer"], "GitHub")


if __name__ == "__main__":
    unittest.main()


class RosDistributionDates(unittest.TestCase):
    """A ROS package is dated by the main-line change that added it to the distribution file, not by
    a contributor's commit on the branch that was merged; rosdep keys by their first line."""

    def test_first_parent_dates(self):
        from phantomdeps.registries import ros_first_seen
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args, date="2022-04-20T00:00:00+00:00", who="GitHub"):
                env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_NAME": who,
                       "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": "a@b",
                       "PATH": "/usr/bin:/bin", "HOME": tmp}
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, env=env)

            (repo / "humble").mkdir()
            (repo / "rosdep").mkdir()
            dist = repo / "humble" / "distribution.yaml"
            dist.write_text("repositories:\n  old_pkg:\n    release:\n      packages:\n      - old_pkg\n")
            for f in ("base", "python", "ruby"):
                (repo / "rosdep" / f"{f}.yaml").write_text("old-key:\n  ubuntu: [old]\n")
            git("init", "-q", "-b", "master")
            git("add", "-A")
            git("commit", "-q", "-m", "branch humble")
            git("checkout", "-q", "-b", "pr")
            dist.write_text(dist.read_text() + "  new_repo:\n    release:\n      packages:\n      - new_pkg\n")
            (repo / "rosdep" / "base.yaml").write_text("new-key:\n  ubuntu: [new]\nold-key:\n  ubuntu: [old]\n")
            git("add", "-A")
            # the contributor backdates the commit on the branch
            git("commit", "-q", "-m", "add new_pkg", date="2021-01-01T00:00:00+00:00", who="contributor")
            git("checkout", "-q", "master")
            git("merge", "-q", "--no-ff", "pr", "-m", "Merge pull request", date="2025-12-06T16:12:41+00:00")
            fs = ros_first_seen(repo, "humble")
            self.assertEqual(fs["packages"]["new_pkg"]["date"][:10], "2025-12-06")
            self.assertEqual(fs["packages"]["old_pkg"]["date"][:10], "2022-04-20")
            self.assertTrue(fs["packages"]["old_pkg"]["initial_import"])
            self.assertFalse(fs["packages"]["new_pkg"]["initial_import"])
            self.assertEqual(fs["rosdep"]["new-key"]["date"][:10], "2025-12-06")


class ArduinoExactNames(unittest.TestCase):
    """Library names that differ only in case are different libraries with their own records:
    each resolves exactly and is dated from its own repository; another case resolves as a variant."""

    def test_case_colliding_libraries(self):
        from phantomdeps.registries import Registries
        reg = Registries.__new__(Registries)
        reg.arduino = {"MPU6050": {"name": "MPU6050", "repository": "https://github.com/a/old.git"},
                       "mpu6050": {"name": "mpu6050", "repository": "https://github.com/b/new.git"}}
        reg.arduino_lower = {"mpu6050": ["MPU6050", "mpu6050"]}
        reg.arduino_log = {"first": {"github.com/a/old": {"date": "2021-04-08T20:45:49-07:00", "initial_import": True},
                                     "github.com/b/new": {"date": "2025-05-27T20:55:42+00:00", "initial_import": False}}}
        reg.static_seen = {"arduino": "2026-10-23T18:49:11Z"}
        old, new, var = reg._r_arduino("MPU6050"), reg._r_arduino("mpu6050"), reg._r_arduino("Mpu6050")
        self.assertEqual((old.status, old.tau_lo, old.tau_hi[:10]), ("found", None, "2021-04-09"))
        self.assertEqual((new.status, new.tau_lo[:10]), ("found", "2025-05-27"))
        self.assertEqual(var.status, "found_variant")
        self.assertEqual(reg._r_arduino("MPU-6050").status, "not_found")


class MicroPythonDates(unittest.TestCase):
    """micropython-lib: a contribution that reached the main line with its author as committer
    carries a date the contributor chose; the first maintainer commit bounds the date instead."""

    def test_maintainer_commit_bounds_date(self):
        from phantomdeps.registries import micropython_first_seen
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args, date, author, committer):
                env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_NAME": author,
                       "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": committer, "GIT_COMMITTER_EMAIL": "c@d",
                       "PATH": "/usr/bin:/bin", "HOME": tmp}
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, env=env)

            git("init", "-q", "-b", "master", date="2020-01-01T00:00:00+00:00", author="m", committer="m")
            (repo / "old").mkdir()
            (repo / "old" / "manifest.py").write_text("module('old.py')\n")
            git("add", "old/manifest.py", date="2021-01-01T00:00:00+00:00", author="m", committer="m")
            # a contribution re-committed by the maintainer: the maintainer is identified as such
            git("commit", "-q", "-m", "old", date="2021-01-01T00:00:00+00:00", author="contrib", committer="Maint")
            (repo / "fresh").mkdir()
            (repo / "fresh" / "manifest.py").write_text("module('fresh.py')\n")
            git("add", "fresh/manifest.py", date="2026-01-01T00:00:00+00:00", author="x", committer="x")
            # fast-forwarded contribution, backdated by its author
            git("commit", "-q", "-m", "fresh", date="2019-01-01T00:00:00+00:00", author="x", committer="x")
            (repo / "fresh" / "manifest.py").write_text("module('fresh.py', opt=3)\n")
            git("add", "fresh/manifest.py", date="2026-02-01T00:00:00+00:00", author="Maint", committer="Maint")
            git("commit", "-q", "-m", "tidy", date="2026-02-01T00:00:00+00:00", author="Maint", committer="Maint")
            fs = micropython_first_seen({"packages": [{"name": "old", "path": "old"},
                                                      {"name": "fresh", "path": "fresh"}]}, repo)
            self.assertEqual(fs["maintainers"], ["Maint"])
            self.assertEqual(fs["first"]["old"]["date"][:10], "2021-01-01")
            self.assertEqual(fs["first"]["fresh"]["date"][:10], "2026-02-01")
            self.assertEqual(fs["first"]["fresh"]["skipped_contributor_commits"], 1)
