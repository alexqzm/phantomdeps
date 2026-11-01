"""SCII evidence construction and policy decisions for one declared dependency.

The declaration is the single phantom (or control) name.  Evidence is built from
what the *local* registry reports:

  res   1 (the name resolves after the squat / control is published)
  est   from publication-time bounds and the horizon h (gate.est_state)
  pin   None   (not modelled here)
  prov  None   (not modelled here)

Bound semantics (the point of the revision):
  * npm ``time.created`` and the crates index ``pubtime`` bound the registry
    record's creation from BOTH sides -> tau_lo = tau_hi = that timestamp.
  * PyPI upload times bound it only from ABOVE (older files may have been
    deleted) -> tau_lo = None on first contact.
  * "with absence record": a real pre-publish lookup observed the name absent at
    t_obs, giving tau_lo = t_obs for every ecosystem.
"""

from __future__ import annotations


def _decision(G, profile, tau_lo, tau_hi, horizon, pin=None):
    est = G.est_state(tau_lo, tau_hi, horizon, profile.delta_est_days)
    z = {"res": 1, "est": est, "pin": pin, "prov": None}
    d = G.scii([z], profile)
    return {"evidence": z, "tau_lo": tau_lo, "tau_hi": tau_hi, "horizon": horizon,
            "R": d["R"], "Rp": d["Rp"], "G": d["G"], "decision": d["decision"]}


def bounds_first_contact(eco: str, served_pub_time: str):
    """(tau_lo, tau_hi) when the gate sees the name for the first time."""
    if eco == "pypi":
        return None, served_pub_time          # upper bound only
    return served_pub_time, served_pub_time    # npm/crates: record-creation both sides


def bounds_with_absence(eco: str, served_pub_time: str, t_obs: str):
    """(tau_lo, tau_hi) when a prior lookup recorded the name absent at t_obs.

    tau_lo becomes the observation time for every ecosystem; the upper bound is
    unchanged (PyPI = min upload time, npm/crates = record-creation time).
    """
    return t_obs, served_pub_time


def scii_variants(G, profile, eco: str, horizon: str, served_pub_time: str,
                  t_obs: str, role: str, pin=None) -> dict:
    """Return the SCII evaluation under both bound models for a given pin value.

    For a squat both variants are reported.  For an (established) control the
    with-absence variant does not apply: the control is meant to predate h, so a
    real "now" absence lookup would misrepresent it -- we record that honestly
    instead of inventing a pre-h observation.
    """
    lo, hi = bounds_first_contact(eco, served_pub_time)
    out = {"first_contact": _decision(G, profile, lo, hi, horizon, pin)}
    if role == "squat":
        lo2, hi2 = bounds_with_absence(eco, served_pub_time, t_obs)
        out["with_absence"] = _decision(G, profile, lo2, hi2, horizon, pin)
    else:
        out["with_absence"] = {
            "not_applicable": True,
            "reason": "control predates h; no genuine pre-h absence observation exists, "
                      "so no tau_lo from a real lookup (not faked)",
        }
    return out


def decide(G, profile, tau_lo, tau_hi, horizon, pin=None) -> dict:
    """Public single-shot SCII decision for explicit bounds (used by the evidence step)."""
    return _decision(G, profile, tau_lo, tau_hi, horizon, pin)


def p2_decision(G, profile, parse_time, served_pub_time: str, t_gate: str) -> dict:
    """P2 as a computed decision from the reported publication time (no clock faking)."""
    age_days = (parse_time(t_gate) - parse_time(served_pub_time)).days
    decision = "admit" if age_days >= profile.age_heuristic_days else "reject"
    return {"decision": decision, "age_days": age_days,
            "age_heuristic_days": profile.age_heuristic_days,
            "basis": "reported publication time; t_gate - pub_time", "pub_time": served_pub_time}
