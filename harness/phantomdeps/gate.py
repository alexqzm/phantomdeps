"""Three-state evidence for AI-originated dependency declarations.

Evidence states: 1 confirmed, 0 refuted, None absent (written ⊥ in the paper).

Per dependency d the profile expects four evidence items
  res   d resolves in the target registry at gating time       (mandatory)
  est   d was published before the producer's knowledge horizon (mandatory)
        h minus Delta; refuted when the earliest possible publication
        time is after h; absent when the bounds do not decide
  pin   the build records a content hash for the resolved artifact
  prov  verifiable publisher provenance for the artifact

Labels:     l(d)  = sum_s alpha_s [z_s = 1]       (absent counts as 0)
            l+(d) = sum_s alpha_s [z_s != 0]      (absent counts as 1)
Index:      R  = prod_d l(d),  R+ = prod_d l+(d)  (critical edges, w = 1)
Gate:       G = 0 if some z = 0;  G = ⊥ if no refutation but a mandatory
            item is absent;  G = 1 otherwise
Rule:       reject if G = 0; request evidence if G = ⊥; reject if R+ < theta;
            admit if R >= theta; manual review otherwise.

All parameters are assigned in ``config/profile.yaml``; the safety
properties tested in ``tests/test_gate.py`` do not depend on them.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from .util import parse_time

ITEMS = ("res", "est", "pin", "prov")
DECISIONS = ("admit", "review", "request", "reject")

# Ecosystems whose standard tooling records a content hash of each resolved
# artifact in a project lock file (see PROTOCOL.md for the sources).
HASH_LOCK = {"crates": True, "npm": True, "pypi": True, "espidf": True,
             "platformio": False, "arduino": False, "ros2": False, "micropython": False}

# What the resolver learns about the record of a *new* registration (a squat).
# REC_UPPER: the registry reports a time by which the record existed (API field, upload time of
#   a file, or a main-line commit of the registry repository).
# REC_LOWER: that time is also a lower bound, i.e. the registry dates the creation of the record
#   itself (PlatformIO added_at, ESP created_at, crates.io pubtime of the first version, npm
#   time.created, the main-line merge that added an Arduino repository or a ROS package).  PyPI
#   reports only upload times of files that still exist, and micropython-lib only the first
#   maintainer commit, so their dates bound the record from above only (PROTOCOL.md, 3.3).
REC_UPPER = {"arduino": True, "platformio": True, "espidf": True, "crates": True, "pypi": True,
             "npm": True, "micropython": True, "ros2": True}
REC_LOWER = {"arduino": True, "platformio": True, "espidf": True, "crates": True, "pypi": False,
             "npm": True, "micropython": False, "ros2": True}
# registries in which a new registration can be refuted from registry dates alone
TIMESTAMPS = REC_LOWER


def squat_bounds(dep: dict, t_attack: str | None, absence: bool = False) -> tuple:
    """Publication-time bounds the resolver would report for a phantom squatted at ``t_attack``.
    With ``absence``, the pipeline's own earlier lookup that found the name unregistered
    (``absent_at``, the time of our snapshot lookup) bounds the new record from below."""
    eco = dep["eco"]
    hi = t_attack if REC_UPPER.get(eco) else None
    lo = t_attack if REC_LOWER.get(eco) else (dep.get("absent_at") if absence else None)
    return lo, hi


@dataclass
class Profile:
    theta: float = 0.85
    alpha: dict = field(default_factory=lambda: {"res": 0.4, "est": 0.3, "pin": 0.2, "prov": 0.1})
    mandatory: tuple = ("res", "est")
    delta_est_days: int = 30
    age_heuristic_days: int = 30

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        p = cls()
        for k, v in d.items():
            if hasattr(p, k):
                setattr(p, k, tuple(v) if k == "mandatory" else v)
        p.validate()
        return p

    def validate(self) -> None:
        s = sum(self.alpha.values())
        if not math.isclose(s, 1.0, abs_tol=1e-9) or any(v < 0 for v in self.alpha.values()):
            raise ValueError(f"alpha weights must be non-negative and sum to 1, got {self.alpha}")
        if not 0 < self.theta <= 1:
            raise ValueError(f"theta must lie in (0, 1], got {self.theta}")
        if self.delta_est_days < 0:
            raise ValueError("delta must be non-negative (Proposition 2 needs h - delta <= h)")
        if not set(self.mandatory) <= set(ITEMS):
            raise ValueError(f"unknown mandatory items {self.mandatory}")


def est_state(tau_lo: str | None, tau_hi: str | None, horizon: str | None, delta_days: int):
    """Establishment evidence from publication-time bounds and the horizon h."""
    if horizon is None:
        return None
    h = parse_time(horizon)
    lo, hi = parse_time(tau_lo), parse_time(tau_hi)
    if hi is not None and hi <= h - dt.timedelta(days=delta_days):
        return 1
    if lo is not None and lo > h:
        return 0
    return None


def evidence_for(dep: dict, horizon: str | None, prof: Profile, *, pipeline_pin: bool,
                 world: str = "obs", t_attack: str | None = None, absence: bool = False,
                 pins: dict | None = None) -> dict | None:
    """Evidence vector for one classified declaration; None if not a dependency.

    ``world='squat'`` applies the counterfactual: every phantom name is
    registered by an adversary at ``t_attack`` (after the snapshot), with no
    provenance and a fresh publication time -- except a registered name without
    installable files (``squattable == 'owned'``: a PyPI project without files, a
    crate with every version yanked), whose record exists and belongs to its owner;
    a version its owner publishes is the takeover of an existing record, outside the
    threat model, so it stays unresolvable.  ``world='squat_open'`` registers only
    the phantoms that any account holder could register (``squattable == 'open'``);
    the others stay unregistered.
    """
    if world == "squat_open":
        world = "squat" if (dep["cls"] == "phantom" and dep.get("squattable") == "open") else "obs"
    cls = dep["cls"]
    eco = dep["eco"]
    if cls in ("local", "special"):
        return None
    if cls == "toolchain":
        return {"res": 1, "est": 1, "pin": 1, "prov": 1}
    if cls == "phantom":
        if world == "squat" and dep.get("squattable") != "owned":
            lo, hi = squat_bounds(dep, t_attack, absence)
            return {"res": 1, "est": est_state(lo, hi, horizon, prof.delta_est_days),
                    "pin": 1 if (pipeline_pin and HASH_LOCK.get(eco)) else None, "prov": None}
        return {"res": 0, "est": None, "pin": None, "prov": None}
    if cls == "exists":
        pin = 1 if (pipeline_pin and HASH_LOCK.get(eco)) else None
        if pins is not None:        # measured: the lock file the real tool wrote (lockcheck)
            pin = pins.get(_dep_key(dep))
        return {"res": 1, "est": est_state(dep.get("tau_lo"), dep.get("tau_hi"), horizon, prof.delta_est_days),
                "pin": pin, "prov": None}
    # nonregistry, legacy_id, unresolved, unsupported: resolution cannot be decided
    return {"res": None, "est": None, "pin": 1 if dep.get("commit_pinned") else None, "prov": None}


def _dep_key(dep: dict) -> tuple:
    return (dep.get("run_id"), dep.get("model_id"), dep.get("prompt_id"), dep.get("sample_idx"), dep["eco"],
            dep["name"])


def labels(z: dict, prof: Profile) -> tuple[float, float]:
    lo = sum(prof.alpha[s] for s in ITEMS if z.get(s) == 1)
    hi = sum(prof.alpha[s] for s in ITEMS if z.get(s) != 0)
    return lo, hi


def scii(evs: list[dict], prof: Profile, *, veto: bool = True, absent: str = "three") -> dict:
    """Index bracket, gate and decision for one manifest.

    ``absent`` selects how absent items are read: ``three`` (the rule of the paper),
    ``mandatory_refuted`` (two-state: an absent mandatory item counts as refuted, other
    absent items as not confirmed), ``confirmed`` (optimistic: every absent item counts
    as confirmed)."""
    if absent == "mandatory_refuted":
        evs = [{k: (0 if (v is None and k in prof.mandatory) else v) for k, v in z.items()} for z in evs]
    elif absent == "confirmed":
        evs = [{k: (1 if v is None else v) for k, v in z.items()} for z in evs]
    elif absent != "three":
        raise ValueError(absent)
    R, Rp = 1.0, 1.0
    for z in evs:
        lo, hi = labels(z, prof)
        R *= lo
        Rp *= hi
    refuted = any(v == 0 for z in evs for v in z.values())
    absent_mand = any(z.get(s) is None for z in evs for s in prof.mandatory)
    G = 0 if refuted else (None if absent_mand else 1)
    if veto and G == 0:
        d = "reject"
    elif veto and G is None:
        d = "request"
    elif Rp < prof.theta:
        d = "reject"
    elif R >= prof.theta:
        d = "admit"
    else:
        d = "review"
    return {"R": R, "Rp": Rp, "G": G, "decision": d}


def _squatted(d: dict, world: str) -> bool:
    # an owned name (registered, no installable files) is never squatted: its record belongs to its owner
    return d["cls"] == "phantom" and ((world == "squat" and d.get("squattable") != "owned")
                                      or (world == "squat_open" and d.get("squattable") == "open"))


def baseline_exists(deps: list[dict], world: str = "obs") -> str:
    """P1: reject iff some declaration is known not to resolve at gating time (URLs, paths
    and failed lookups pass, as they would at install time)."""
    for d in deps:
        if d["cls"] == "phantom" and not _squatted(d, world):
            return "reject"
    return "admit"


def baseline_age(deps: list[dict], prof: Profile, t_gate: str, world: str = "obs",
                 t_attack: str | None = None) -> str:
    """P2: P1 plus 'block packages younger than N days' where age is known.  The age of a
    package is measured from the latest time the registry reports for it (an upper bound on its
    first publication, which is what a registry's 'published' field shows)."""
    tg = parse_time(t_gate)
    for d in deps:
        if d["cls"] == "phantom":
            if not _squatted(d, world):
                return "reject"
            if REC_UPPER.get(d["eco"]) and (tg - parse_time(t_attack)).days < prof.age_heuristic_days:
                return "reject"
        elif d["cls"] == "exists" and (d.get("tau_hi") or d.get("tau_lo")):
            if (tg - parse_time(d.get("tau_hi") or d.get("tau_lo"))).days < prof.age_heuristic_days:
                return "reject"
    return "admit"


def baseline_asof(deps: list[dict], horizon: str | None, world: str = "obs") -> str:
    """P3: resolve as of the horizon (npm ``before``, uv ``exclude-newer``): a registry
    package is visible only if it is known to have been published by h; a package without
    a date is unavailable, as in uv (also packages outside the snapshots).  URLs, paths and
    failed lookups are not date-filtered."""
    h = parse_time(horizon) if horizon else None
    for d in deps:
        if d["cls"] == "phantom":
            return "reject"  # unregistered; in the squat world published after the snapshot, hence after h
        if d["cls"] == "unsupported":
            return "reject"  # a registry package without a known date (outside the snapshots): unavailable
        if d["cls"] == "exists":
            hi = parse_time(d.get("tau_hi"))
            if h is None or hi is None or hi > h:
                return "reject"
    return "admit"


def baseline_asof3(deps: list[dict], horizon: str | None, world: str = "obs", t_attack: str | None = None,
                   absence: bool = False) -> str:
    """P3-unknown: the date filter of P3 with a third outcome.  A package is visible if it is known
    to have been published by h, hidden if it is known to have been first published after h, and
    unknown otherwise (no date, bounds on both sides of h, an upper bound after h only, or a
    declaration no registry dates: URL, path, other distribution, failed lookup).  Decision:
    reject if a declaration does not resolve or is hidden; request evidence if one is unknown;
    admit otherwise.  No margin, no pinning or provenance: the simplest three-valued competitor of
    the rule (with a gate-only profile and delta = 0 the rule decides the same way)."""
    h = parse_time(horizon) if horizon else None
    unknown = False
    for d in deps:
        cls = d["cls"]
        if cls in ("local", "special", "toolchain"):
            continue
        if cls == "phantom":
            if not _squatted(d, world):
                return "reject"
            lo, hi = squat_bounds(d, t_attack, absence)
        elif cls == "exists":
            lo, hi = d.get("tau_lo"), d.get("tau_hi")
        else:
            unknown = True
            continue
        lo, hi = parse_time(lo), parse_time(hi)
        if h is None:
            unknown = True
        elif hi is not None and hi <= h:
            continue
        elif lo is not None and lo > h:
            return "reject"
        else:
            unknown = True
    return "request" if unknown else "admit"
