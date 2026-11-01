"""Properties of the admission rule (Section IV-C of the paper).

P1  veto non-compensability: a manifest with a phantom is never admitted,
    for any weights and threshold.
P2  horizon soundness: a phantom registered after the producer's horizon is
    never admitted at any later gating time, for any weights and threshold.
P3  completion monotonicity: confirming an absent item never lowers R or R+
    and never turns admit into anything else.
P4  the index-only variant can admit a squatted dependency for some weights,
    so the veto is not redundant.
"""

import itertools
import random
import unittest

from phantomdeps.gate import (Profile, baseline_age, baseline_asof, baseline_asof3, baseline_exists, evidence_for,
                              scii, squat_bounds)

H = "2024-09-18T00:00:00Z"
SNAP = "2026-10-23T00:00:00Z"


def rand_profile(rng):
    w = [rng.random() for _ in range(4)]
    s = sum(w)
    alpha = dict(zip(("res", "est", "pin", "prov"), [x / s for x in w]))
    # repair rounding so the weights sum to one exactly enough for Profile
    alpha["prov"] = 1.0 - alpha["res"] - alpha["est"] - alpha["pin"]
    return Profile(theta=rng.uniform(0.05, 0.99), alpha=alpha)


def dep(cls, eco="pypi", tau=None):
    return {"cls": cls, "eco": eco, "tau_lo": tau, "tau_hi": tau}


class GateProperties(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(20240628)

    def test_p1_phantom_never_admitted(self):
        for _ in range(2000):
            prof = rand_profile(self.rng)
            deps = [dep("phantom"), dep("exists", tau="2015-01-01T00:00:00Z")]
            for pin in (False, True):
                evs = [evidence_for(d, H, prof, pipeline_pin=pin) for d in deps]
                self.assertEqual(scii(evs, prof)["decision"], "reject")

    def test_p2_squat_after_horizon_never_admitted(self):
        for _ in range(2000):
            prof = rand_profile(self.rng)
            for eco in ("pypi", "npm", "crates", "platformio", "espidf", "arduino", "micropython", "ros2"):
                deps = [dep("phantom", eco=eco)]
                evs = [evidence_for(d, H, prof, pipeline_pin=True, world="squat", t_attack=SNAP) for d in deps]
                self.assertIn(scii(evs, prof)["decision"], ("reject", "request"))

    def test_p3_completion_monotone(self):
        # all 81 single-dependency records, each absent item completed to 1, under the
        # assigned profile and 200 random profiles; also inside a two-dependency manifest
        profiles = [Profile()] + [rand_profile(self.rng) for _ in range(200)]
        states = (1, 0, None)
        other = {"res": 1, "est": 1, "pin": None, "prov": None}
        for prof in profiles:
            for z in itertools.product(states, repeat=4):
                ev = dict(zip(("res", "est", "pin", "prov"), z))
                for ctx in ([], [other]):
                    base = scii([ev] + ctx, prof)
                    for k, v in ev.items():
                        if v is None:
                            more = dict(ev)
                            more[k] = 1
                            up = scii([more] + ctx, prof)
                            self.assertGreaterEqual(up["R"], base["R"] - 1e-12)
                            self.assertGreaterEqual(up["Rp"], base["Rp"] - 1e-12)
                            if base["decision"] == "admit":
                                self.assertEqual(up["decision"], "admit")

    def test_p4_index_only_can_admit_squat(self):
        found = False
        for _ in range(5000):
            prof = rand_profile(self.rng)
            evs = [evidence_for(dep("phantom", eco="pypi"), H, prof, pipeline_pin=True, world="squat", t_attack=SNAP)]
            if scii(evs, prof, veto=False)["decision"] == "admit":
                found = True
                break
        self.assertTrue(found)

    def test_baselines(self):
        prof = Profile()
        deps = [dep("phantom", eco="pypi")]
        self.assertEqual(baseline_exists(deps, "obs"), "reject")
        self.assertEqual(baseline_exists(deps, "squat"), "admit")
        self.assertEqual(baseline_age(deps, prof, "2026-10-24T00:00:00Z", "squat", SNAP), "reject")
        self.assertEqual(baseline_age(deps, prof, "2027-01-24T00:00:00Z", "squat", SNAP), "admit")
        # the ROS distribution is dated by its main-line history: the age rule sees the new record
        self.assertEqual(baseline_age([dep("phantom", eco="ros2")], prof, "2026-10-24T00:00:00Z", "squat", SNAP), "reject")
        self.assertEqual(baseline_age([dep("phantom", eco="ros2")], prof, "2027-01-24T00:00:00Z", "squat", SNAP), "admit")

    def test_record_bounds_and_unknown_filter(self):
        """PyPI and micropython-lib date a new record from above only; the three-valued date
        filter then asks for evidence, unless the pipeline's own earlier lookup found the name
        unregistered after h; P3 hides the squat either way (its files are newer than h)."""
        for eco in ("pypi", "micropython"):
            d = dep("phantom", eco=eco)
            d["absent_at"] = "2026-10-23T00:00:00Z"
            self.assertEqual(squat_bounds(d, SNAP), (None, SNAP))
            self.assertEqual(squat_bounds(d, SNAP, absence=True), ("2026-10-23T00:00:00Z", SNAP))
            self.assertEqual(baseline_asof3([d], H, "squat", SNAP), "request")
            self.assertEqual(baseline_asof3([d], H, "squat", SNAP, absence=True), "reject")
            self.assertEqual(baseline_asof([d], H, "squat"), "reject")
            prof = Profile()
            z = evidence_for(d, H, prof, pipeline_pin=True, world="squat", t_attack=SNAP)
            self.assertIsNone(z["est"])
            self.assertEqual(scii([z], prof)["decision"], "request")
            z = evidence_for(d, H, prof, pipeline_pin=True, world="squat", t_attack=SNAP, absence=True)
            self.assertEqual(z["est"], 0)
        for eco in ("arduino", "platformio", "espidf", "crates", "npm", "ros2"):
            d = dep("phantom", eco=eco)
            self.assertEqual(squat_bounds(d, SNAP), (SNAP, SNAP))
            self.assertEqual(baseline_asof3([d], H, "squat", SNAP), "reject")
        # visible, hidden and unknown established packages
        old = {"cls": "exists", "eco": "npm", "tau_lo": "2015-01-01T00:00:00Z", "tau_hi": "2015-01-01T00:00:00Z"}
        new = {"cls": "exists", "eco": "npm", "tau_lo": SNAP, "tau_hi": SNAP}
        upper = {"cls": "exists", "eco": "pypi", "tau_lo": None, "tau_hi": SNAP}
        url = {"cls": "nonregistry", "eco": "npm"}
        self.assertEqual(baseline_asof3([old], H), "admit")
        self.assertEqual(baseline_asof3([old, new], H), "reject")
        self.assertEqual(baseline_asof3([old, upper], H), "request")
        self.assertEqual(baseline_asof3([old, url], H), "request")
        self.assertEqual(baseline_asof([old, url], H), "admit")   # P3 does not date-filter URLs

    def test_known_values(self):
        prof = Profile()  # alpha = .4 .3 .2 .1, theta = .85
        ev_ok = {"res": 1, "est": 1, "pin": 1, "prov": None}
        r = scii([ev_ok], prof)
        self.assertAlmostEqual(r["R"], 0.9)
        self.assertAlmostEqual(r["Rp"], 1.0)
        self.assertEqual(r["decision"], "admit")
        r2 = scii([ev_ok, ev_ok], prof)
        self.assertAlmostEqual(r2["R"], 0.81)
        self.assertEqual(r2["decision"], "review")
        self.assertEqual(scii([{"res": 1, "est": None, "pin": 1, "prov": None}], prof)["decision"], "request")
        self.assertEqual(scii([{"res": 1, "est": 0, "pin": 1, "prov": 1}], prof)["decision"], "reject")


class GateIsDateFilter(unittest.TestCase):
    """With a gate-only profile (pin and prov weightless) and no margin, rule (3) decides every
    dependency set exactly as the date filter with an unknown state (P3-unknown)."""

    def test_equivalence(self):
        rng = random.Random(7)
        times = [None, "2015-01-01T00:00:00Z", "2024-09-17T00:00:00Z", H, "2024-09-19T00:00:00Z", SNAP]
        classes = ["exists", "phantom", "toolchain", "nonregistry", "unsupported", "unresolved", "legacy_id"]
        ecos = ["arduino", "platformio", "espidf", "ros2", "crates", "micropython", "pypi", "npm"]
        for _ in range(4000):
            deps = []
            for _ in range(rng.randint(1, 4)):
                lo, hi = rng.choice(times), rng.choice(times)
                if lo and hi and lo > hi:
                    lo, hi = hi, lo
                d = {"cls": rng.choice(classes), "eco": rng.choice(ecos), "tau_lo": lo, "tau_hi": hi,
                     "absent_at": rng.choice(times[1:])}
                deps.append(d)
            for world in ("obs", "squat", "squat_open"):
                for absence in (False, True):
                    prof = Profile(theta=rng.uniform(0.05, 1.0), alpha={"res": 0.5, "est": 0.5, "pin": 0.0, "prov": 0.0},
                                   delta_est_days=0)
                    evs = [evidence_for(d, H, prof, pipeline_pin=True, world=world, t_attack=SNAP, absence=absence)
                           for d in deps]
                    evs = [e for e in evs if e is not None]
                    self.assertEqual(scii(evs, prof)["decision"],
                                     baseline_asof3(deps, H, world, SNAP, absence), (deps, world, absence))


if __name__ == "__main__":
    unittest.main()
