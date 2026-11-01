#!/usr/bin/env python3
"""Standalone smoke test for the ESP Component Registry (CPS) replay leg.

Stands up the localhost-only static-storage mock, drives the REAL
``idf-component-manager`` to resolve+download a harmless marker component, asserts
the marker was materialised into the build tree, that the download ran no component
code, and that every request came from 127.0.0.1, then asserts the SCII gate
REJECTS the squat (first contact and with-absence) while ADMITTING the backdated
control.  With an ESP-IDF checkout configured (REPLAY_ESP_IDF), it also builds the
squat with ``idf.py build`` (its CMake code runs, its library is compiled) and the
control from the gate's measured lock.

Run directly:   python3 replay/test_esp_replay.py
or under pytest: pytest replay/test_esp_replay.py
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)                        # replaylib
sys.path.insert(0, os.path.join(ROOT, "harness"))  # phantomdeps

import yaml  # noqa: E402

from phantomdeps import gate as G  # noqa: E402
from phantomdeps.util import mask_key, parse_time  # noqa: E402

from replaylib import common, gate_eval, selection  # noqa: E402
from replaylib.common import iso, iso_us, now_precise  # noqa: E402
from replaylib.esp_registry import EspRegistry  # noqa: E402

PROFILE = G.Profile.from_dict(yaml.safe_load(open(os.path.join(ROOT, "config", "profile.yaml"))))
NS = "replaylocal"
VER = "1.0.0"
SOURCE_DEPS = os.path.join(ROOT, "data", "runs", "pilot-cpu-2026-10", "deps.jsonl")


def _m(*p):
    d = os.path.join(*p)
    os.makedirs(d, exist_ok=True)
    return d


def _run() -> dict:
    work = tempfile.mkdtemp(prefix="esp_smoke_")
    reg = EspRegistry()
    try:
        chosen = selection.select_esp(SOURCE_DEPS)
        squat = chosen["name"] if chosen else "ph-esp-0000000000"
        horizon = chosen["horizon"] if chosen else "2024-09-18T12:38:14Z"
        h = parse_time(horizon)
        ctrl, ctrl_time = "phantomdeps-replay-control", h - dt.timedelta(days=180)

        # (1) 127.0.0.1 only -- asserted.
        common.assert_localhost([reg.storage_url])
        assert reg.storage_url.startswith("http://127.0.0.1:"), reg.storage_url

        # (2) publish squat (record date = now) + control (backdated 180d before h).
        assert reg.resolves(NS, squat) is False
        mi = reg.publish(NS, squat, version=VER)
        ci = reg.publish(NS, ctrl, version=VER, created_at=iso(ctrl_time))
        assert reg.resolves(NS, squat) and reg.resolves(NS, ctrl)

        # (3) P1 'resolves now': the REAL manager resolves+downloads the squat from the mock.
        p1 = reg.resolve_download(NS, squat, VER, _m(work, "p1_squat"), _m(work, "m1"))
        assert p1["driver_ok"], p1.get("reason")
        assert p1["installed"] is True, p1
        assert p1["marker"] is True, "squat payload must be materialised into managed_components/"
        assert p1["requests_localhost_only"] is True

        # (3b) the tarball was actually fetched from 127.0.0.1 (not just metadata).
        tgz_reqs = [(host, pa) for host, pa in reg.requests() if pa.endswith(VER + ".tgz")]
        assert tgz_reqs, "no tarball GET was served"
        assert all(host == "127.0.0.1" for host, _ in tgz_reqs), tgz_reqs
        # (3c) downloading ran no component code (the CMake marker is written only by a build).
        assert p1["code_ran"] is False, "a download must not evaluate the component"

        # (3d) with an ESP-IDF checkout configured, the real build system builds the squat:
        # its CMake code runs and its library is compiled (skipped when not configured).
        b1 = reg.idf_build(NS, squat, VER, _m(work, "b_squat"), _m(work, "mb1"))
        if b1.get("status") != "not_available":
            assert b1["built"] and b1["cmake_marker"] and b1["component_lib"], {
                k: b1.get(k) for k in ("exit", "built", "cmake_marker", "component_lib")}
            assert b1["requests_localhost_only"] is True

        # (4) the SCII gate REJECTS the squat at first contact AND with-absence.
        t_obs = iso_us(now_precise())
        sv = gate_eval.scii_variants(G, PROFILE, "espidf", horizon, mi["created_at"], t_obs, "squat")
        assert sv["first_contact"]["G"] == 0, sv["first_contact"]
        assert sv["first_contact"]["decision"] == "reject", sv["first_contact"]
        assert sv["with_absence"]["decision"] == "reject", sv["with_absence"]

        # (5) control: code-free pin (solve + lock dump, NO download), then gate ADMITS, then install.
        pin, recs = reg.lock_only(NS, ctrl, VER, _m(work, "lock_ctrl"))
        assert pin == 1 and recs[0]["digest_found"], recs[0]
        assert recs[0]["marker_after_lock"] is False, "lock step must not materialise/run code"
        assert recs[0]["managed_components_created"] is False
        cv = gate_eval.scii_variants(G, PROFILE, "espidf", iso(h), ci["created_at"], t_obs, "control", pin=pin)
        assert cv["first_contact"]["decision"] == "admit", cv["first_contact"]
        c1 = reg.resolve_download(NS, ctrl, VER, _m(work, "p1_ctrl"), _m(work, "mc"))
        assert c1["installed"] and c1["marker"] and c1["requests_localhost_only"], c1
        # (5b) the admitted control builds from the lock the gate measured (same hash).
        cb = reg.idf_build(NS, ctrl, VER, _m(work, "b_ctrl"), _m(work, "mcb"), lock_from=recs[0]["lock_path"])
        if cb.get("status") != "not_available":
            assert cb["built"] and cb["lock_component_hash"] == recs[0]["digest"], {
                k: cb.get(k) for k in ("exit", "built", "lock_component_hash")}

        # (6) the squat name is a real classified-phantom pseudonym (idempotent -> cross-checks).
        if chosen:
            assert mask_key("espidf", None, squat) == squat

        result = {"squat": squat, "squat_first_contact": sv["first_contact"]["decision"],
                  "squat_with_absence": sv["with_absence"]["decision"],
                  "squat_G": sv["first_contact"]["G"], "control_gate": cv["first_contact"]["decision"],
                  "p1_squat_installed": p1["installed"], "p1_squat_marker": p1["marker"],
                  "p1_download_code_ran": p1["code_ran"],
                  "p1_squat_build": b1.get("status") or ("built" if b1["built"] else "failed"),
                  "control_build": cb.get("status") or ("built" if cb["built"] else "failed"),
                  "pin": pin, "localhost_only": reg.requests_localhost_only()}
        print("ESP CPS smoke test PASSED:", result)
        return result
    finally:
        reg.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_esp_replay():
    r = _run()
    assert r["squat_first_contact"] == "reject"
    assert r["control_gate"] == "admit"
    assert r["p1_squat_marker"] is True
    assert r["localhost_only"] is True


if __name__ == "__main__":
    _run()
