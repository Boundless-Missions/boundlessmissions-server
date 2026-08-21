"""Unit tests for data/cheat_check.py — the cheat-report disqualification gate.

Run:  python -m pytest test_cheat_check.py -q
(Import-light on purpose: the module takes `enabled` as a parameter instead of
reading cfg, so these tests need no .env / Discord / Firebase setup.)
"""

import json

from data import cheat_check


def _report(tainted=True, vessels=None, tools=None):
    return json.dumps({
        "reported": True,
        "tainted": tainted,
        "vessels": vessels if vessels is not None else [
            {"name": "Cheaty McCheatface",
             "reasons": ["Orbit changed while on rails (Set Orbit, HyperEdit or similar)"],
             "first_ut": 1234.5},
        ],
        "tools_installed": tools or [],
    })


def test_tainted_report_rejects_with_reasons():
    r = cheat_check.evaluate(_report())
    assert r.reject
    assert "Cheaty McCheatface" in r.reject_message
    assert "Set Orbit" in r.reject_message


def test_clean_report_passes():
    assert not cheat_check.evaluate(_report(tainted=False)).reject


def test_absent_report_passes():
    # Older clients send no report at all — must never be rejected for it.
    assert not cheat_check.evaluate(None).reject
    assert not cheat_check.evaluate("").reject


def test_disabled_gate_passes_everything():
    assert not cheat_check.evaluate(_report(), enabled=False).reject


def test_malformed_report_passes():
    # A broken payload must degrade to "no verdict", never to a crash or a reject.
    assert not cheat_check.evaluate("{not json").reject
    assert not cheat_check.evaluate(json.dumps(["list", "not", "dict"])).reject


def test_tools_installed_alone_never_rejects():
    # Presence is not usage: HyperEdit in GameData with a clean flight is fine.
    r = cheat_check.evaluate(_report(tainted=False, tools=["HyperEdit", "VesselMover"]))
    assert not r.reject


def test_tainted_without_vessel_detail_still_rejects():
    # The tainted flag is the client's verdict; missing detail gets a generic line.
    r = cheat_check.evaluate(_report(vessels=[]))
    assert r.reject
    assert "watchdog" in r.reject_message


def test_reason_flood_is_capped():
    vessels = [{"name": f"V{i}", "reasons": [f"r{j}" for j in range(50)]}
               for i in range(50)]
    r = cheat_check.evaluate(_report(vessels=vessels))
    assert r.reject
    assert len(r.reject_message) < 5000
