"""
data/orbit_constraints.py — Orbit-type ("orbital regime") extraction & verification.

A contract's mission text may name a *specific* orbit the craft has to reach, the
same way some weekly missions do: "reach a polar orbit around Kerbin", "place a
satellite in keostationary orbit", "establish a Molniya orbit". This module turns
that natural-language text into a structured orbit constraint and verifies a
vessel's reported orbital elements (inclination, eccentricity, period) against it.

The same canonical schema is enforced in two places (mirrors
data/mission_constraints.py — but there is no editor enforcement here, because an
orbit is a flight state, not a part choice):
  • the KSP submit-button gate (client-side pre-check — see UI/SubmitWindow.cs +
    OrbitConstraint.cs)
  • the bot's /submit endpoint (authoritative re-check — see api_server.py)

Canonical constraint dict (omitted/empty == no orbit requirement):
    {
      "requirements": [str],   # tokens from REQUIREMENTS, e.g. ["polar", "circular"]
      "notes":        str,     # human-readable summary (optional)
    }

The orbital elements are reported by the (untrusted) KSP client, exactly like the
craft's used-parts list and Δv. The telemetry-consistency check
(data/telemetry_check.py) independently rejects a snapshot whose apo/peri/sma/ecc
are mutually impossible, so a forger can't trivially fake "I'm in a polar orbit"
by editing one field and leaving the rest inconsistent.
"""
from __future__ import annotations

import math
import re

import settings

# ── Vocabulary ────────────────────────────────────────────────────────────────

# Natural-language phrase -> canonical requirement token. Phrases are matched as
# whole words (so "polar" doesn't fire on "bipolar"); a leading "_cue" entry means
# the phrase only counts when an orbit cue word ("orbit"/"orbital"/"yörünge") is
# also present, so "polar regions" (a landing site) doesn't read as a polar orbit.
# The inherently-orbital named regimes (geostationary, Molniya, …) need no cue.
_ALIASES: dict[str, str] = {
    # Equatorial (inclination ~0 or ~180).
    "equatorial": "equatorial", "ekvatoral": "equatorial", "ekvatoryal": "equatorial",
    # Polar (inclination ~90).
    "polar": "polar", "kutupsal": "polar", "kutup yörünge": "polar",
    # Direction.
    "retrograde": "retrograde", "geri yönlü": "retrograde", "ters yörünge": "retrograde",
    "prograde": "prograde", "ileri yönlü": "prograde",
    # Shape.
    "circular": "circular", "dairesel": "circular", "circularize": "circular",
    "elliptical": "elliptical", "elliptic": "elliptical", "eccentric": "elliptical",
    "highly elliptical": "elliptical", "eliptik": "elliptical",
    # Synchronous family (need the body's rotation period to verify).
    "geostationary": "stationary", "keostationary": "stationary",
    "kerbistationary": "stationary", "stationary orbit": "stationary",
    "geosynchronous": "synchronous", "keosynchronous": "synchronous",
    "kerbisynchronous": "synchronous", "geosync": "synchronous",
    "synchronous orbit": "synchronous", "eşzamanlı yörünge": "synchronous",
    "sabit yörünge": "stationary",
    "semi-synchronous": "semisynchronous", "semisynchronous": "semisynchronous",
    # Frozen / repeating-ground-track regimes.
    "molniya": "molniya", "tundra": "tundra",
}

# Tokens that are ordinary adjectives (could describe something other than an
# orbit) and therefore only fire when an orbit cue word is also in the text.
_NEEDS_CUE = {"equatorial", "polar", "retrograde", "prograde", "circular", "elliptical"}

# Words that mark the surrounding text as being about an orbit.
_ORBIT_CUES = ("orbit", "orbital", "yörünge")

# All recognised requirement tokens (for validation / round-tripping).
REQUIREMENTS = frozenset(_ALIASES.values())

# Orbital situations KSP reports for a craft on a real orbit. Anything else
# (LANDED, FLYING, SUB_ORBITAL, …) cannot satisfy an orbit requirement.
_ORBITAL_SITUATIONS = {"ORBITING", "DOCKED"}


# ── Normalisation ─────────────────────────────────────────────────────────────

def empty() -> dict:
    """A constraint dict with no orbit requirement."""
    return {"requirements": []}


def is_empty(constraint: dict | None) -> bool:
    """True when there is no orbit requirement to enforce."""
    if not constraint:
        return True
    return not constraint.get("requirements")


def normalize(raw: dict | None) -> dict:
    """Coerce a possibly-loose dict into the canonical schema: known requirement
    tokens only, deduped, order-preserved."""
    out = empty()
    raw = raw or {}
    seen: set[str] = set()
    reqs = raw.get("requirements")
    if isinstance(reqs, str):
        reqs = [reqs]
    for tok in reqs or []:
        t = str(tok).strip().lower()
        if t in REQUIREMENTS and t not in seen:
            seen.add(t)
            out["requirements"].append(t)
    notes = raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:200]
    return out


# ── Heuristic extraction ──────────────────────────────────────────────────────

def extract_heuristic(text: str) -> dict:
    """Keyword-based orbit-requirement extraction. Conservative: an ambiguous
    adjective (polar/equatorial/…) only counts when the text also reads as being
    about an orbit, so ordinary mission flavour produces no requirement.

    Returns an empty constraint when orbit checking is disabled in settings, so
    both the contract-listing merge and the submit gate naturally no-op."""
    out = empty()
    if not getattr(settings, "ORBIT_CHECK_ENABLED", True) or not text:
        return out

    low = text.lower()
    has_cue = any(cue in low for cue in _ORBIT_CUES)
    seen: set[str] = set()
    for phrase, token in _ALIASES.items():
        if token in seen:
            continue
        if token in _NEEDS_CUE and not has_cue:
            continue
        if _word_in(phrase, low):
            seen.add(token)
            out["requirements"].append(token)

    # "stationary" already implies an equatorial, circular, synchronous orbit, so
    # drop the redundant looser tokens it subsumes to keep messages clean.
    if "stationary" in seen:
        for sub in ("synchronous", "equatorial", "circular"):
            if sub in out["requirements"]:
                out["requirements"].remove(sub)
    return out


def _word_in(phrase: str, text: str) -> bool:
    """Whole-token containment so 'polar' doesn't match 'bipolar'."""
    return re.search(r"(?<![a-zçğıöşü0-9])" + re.escape(phrase) + r"(?![a-zçğıöşü0-9])",
                     text) is not None


# ── Verification (server-side authoritative check) ────────────────────────────

def _num(snap: dict, key: str) -> float | None:
    v = snap.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def verify_orbit(constraint: dict | None, snap: dict | None) -> list[str]:
    """Compare a vessel snapshot's orbital elements against the orbit requirement
    and return human-readable violation messages (empty == passes).

    Elements the client didn't report are skipped rather than failed (None => the
    check can't run, like a missing Δv), with one exception: every requirement
    needs the craft to actually be in orbit, so a non-orbital situation always
    fails when any requirement is present."""
    if is_empty(constraint) or not isinstance(snap, dict):
        return []

    reqs = constraint["requirements"]
    situation = (snap.get("situation") or "").upper()
    if situation not in _ORBITAL_SITUATIONS:
        names = ", ".join(_LABELS.get(r, r) for r in reqs)
        return [f"Craft must be in orbit ({names}); it is currently {situation or 'not orbiting'}."]

    incl = _num(snap, "inclination")
    ecc = _num(snap, "eccentricity")
    period = _num(snap, "period")
    rot = _num(snap, "rotation_period")

    out: list[str] = []
    for req in reqs:
        msg = _check_one(req, incl, ecc, period, rot)
        if msg:
            out.append(msg)
    return out


def _check_one(req: str, incl: float | None, ecc: float | None,
               period: float | None, rot: float | None) -> str | None:
    s = settings
    if req == "polar":
        if incl is not None and abs(incl - 90.0) > s.ORBIT_POLAR_INCL_TOL:
            return (f"Orbit must be polar (inclination ≈ 90°, ±{s.ORBIT_POLAR_INCL_TOL:.0f}°); "
                    f"current inclination is {incl:.1f}°.")
    elif req == "equatorial":
        if incl is not None and not _is_equatorial(incl):
            return (f"Orbit must be equatorial (inclination ≈ 0°, ±{s.ORBIT_EQUATORIAL_INCL_TOL:.0f}°); "
                    f"current inclination is {incl:.1f}°.")
    elif req == "retrograde":
        if incl is not None and incl <= 90.0 + s.ORBIT_INCLINED_MARGIN:
            return f"Orbit must be retrograde (inclination > 90°); current inclination is {incl:.1f}°."
    elif req == "prograde":
        if incl is not None and incl >= 90.0 - s.ORBIT_INCLINED_MARGIN:
            return f"Orbit must be prograde (inclination < 90°); current inclination is {incl:.1f}°."
    elif req == "circular":
        if ecc is not None and ecc > s.ORBIT_CIRCULAR_ECC_TOL:
            return (f"Orbit must be circular (eccentricity ≤ {s.ORBIT_CIRCULAR_ECC_TOL:.2f}); "
                    f"current eccentricity is {ecc:.3f}.")
    elif req == "elliptical":
        if ecc is not None and ecc < s.ORBIT_ELLIPTIC_ECC_MIN:
            return (f"Orbit must be elliptical (eccentricity ≥ {s.ORBIT_ELLIPTIC_ECC_MIN:.2f}); "
                    f"current eccentricity is {ecc:.3f}.")
    elif req == "synchronous":
        return _check_period(period, rot, 1.0, "synchronous")
    elif req == "semisynchronous":
        return _check_period(period, rot, 0.5, "semi-synchronous")
    elif req == "stationary":
        # Geostationary/keostationary == equatorial + circular + synchronous.
        for sub in ("equatorial", "circular", "synchronous"):
            m = _check_one(sub, incl, ecc, period, rot)
            if m:
                return ("Orbit must be geostationary (equatorial, circular and "
                        f"synchronous): {m}")
    elif req == "molniya":
        return _check_frozen(incl, ecc, period, rot, s.ORBIT_MOLNIYA_ECC_MIN, 0.5, "Molniya")
    elif req == "tundra":
        return _check_frozen(incl, ecc, period, rot, s.ORBIT_TUNDRA_ECC_MIN, 1.0, "Tundra")
    return None


def _is_equatorial(incl: float) -> bool:
    tol = settings.ORBIT_EQUATORIAL_INCL_TOL
    return incl <= tol or incl >= 180.0 - tol


def _check_period(period: float | None, rot: float | None, factor: float,
                  label: str) -> str | None:
    """Period must equal `factor`× the body's sidereal rotation period. The body's
    rotation period is reported by the client; if it's missing (old DLL) the check
    is skipped rather than failed."""
    if period is None or rot is None or rot <= 0:
        return None
    target = rot * factor
    if abs(period - target) / target > settings.ORBIT_SYNC_PERIOD_TOL:
        return (f"Orbit must be {label} (period ≈ {target/3600:.2f} h); "
                f"current period is {period/3600:.2f} h.")
    return None


def _check_frozen(incl: float | None, ecc: float | None, period: float | None,
                  rot: float | None, ecc_min: float, period_factor: float,
                  label: str) -> str | None:
    """Molniya/Tundra: a high-eccentricity orbit at the critical inclination
    (~63.4°) with a half-day (Molniya) or full-day (Tundra) period."""
    s = settings
    if incl is not None and abs(incl - s.ORBIT_FROZEN_INCL) > s.ORBIT_FROZEN_INCL_TOL:
        return (f"{label} orbit needs the critical inclination ≈ {s.ORBIT_FROZEN_INCL:.1f}° "
                f"(±{s.ORBIT_FROZEN_INCL_TOL:.0f}°); current inclination is {incl:.1f}°.")
    if ecc is not None and ecc < ecc_min:
        return (f"{label} orbit must be highly eccentric (eccentricity ≥ {ecc_min:.2f}); "
                f"current eccentricity is {ecc:.3f}.")
    return _check_period(period, rot, period_factor, f"{label} ({'half-day' if period_factor < 1 else 'one-day'})")


# Friendly labels for messages / summaries.
_LABELS = {
    "polar": "polar", "equatorial": "equatorial", "retrograde": "retrograde",
    "prograde": "prograde", "circular": "circular", "elliptical": "elliptical",
    "synchronous": "synchronous", "semisynchronous": "semi-synchronous",
    "stationary": "geostationary", "molniya": "Molniya", "tundra": "Tundra",
}


def summary_line(constraint: dict | None) -> str | None:
    """Short one-line description for logs / UI, or None if empty."""
    if is_empty(constraint):
        return None
    if constraint.get("notes"):
        return constraint["notes"]
    labels = [_LABELS.get(r, r) for r in constraint["requirements"]]
    return ("Required orbit: " + ", ".join(labels)) if labels else None


def label(req: str) -> str:
    """Friendly name for one requirement token."""
    return _LABELS.get(req, req)


# ── Explicit requirements (rescue targets) ────────────────────────────────────
#
# Everything above turns *mission text* into a requirement. A rescue target is
# different: the issuer picks the regime and the plane in the form, so the tokens
# arrive already canonical and there is nothing to parse. These helpers verify that
# kind of requirement against the same tolerances, so "polar" means one thing
# whether it was typed in a sentence or ticked in a box.

def normalize_types(raw) -> list[str]:
    """Coerce a client-supplied list (or comma-separated string) of regime tokens
    into canonical, deduped, order-preserved form. Unknown tokens are dropped."""
    if isinstance(raw, str):
        raw = raw.split(",")
    out: list[str] = []
    for tok in raw or []:
        t = str(tok).strip().lower()
        if t in REQUIREMENTS and t not in out:
            out.append(t)
    return out


def verify_types(types, snap: dict | None) -> list[str]:
    """Verify canonical regime tokens against a vessel snapshot. Thin wrapper over
    verify_orbit so explicit and text-derived requirements read identically."""
    types = normalize_types(types)
    if not types:
        return []
    return verify_orbit({"requirements": types}, snap)


def check_inclination(target: float | None, margin: float | None,
                      incl: float | None) -> str | None:
    """Plane match against an explicit target inclination, in degrees.

    Returns a violation message, or None when it passes / can't be checked. A
    missing target or a margin <= 0 means "any plane" — that is how every rescue
    issued before this field existed is read. Inclination runs 0..180° (>90° is
    retrograde), and 179° is not 1°: opposite directions in the same plane are
    opposite rendezvous problems, so the comparison deliberately doesn't wrap."""
    if target is None or incl is None:
        return None
    try:
        target = float(target)
        margin = float(margin or 0.0)
        incl = float(incl)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(target) and math.isfinite(margin) and math.isfinite(incl)):
        return None
    if margin <= 0:
        return None
    if abs(incl - target) > margin:
        return (f"Orbit must be inclined {target:.1f}° (±{margin:.1f}°); "
                f"current inclination is {incl:.1f}°.")
    return None


def describe_target(inc: float | None, margin_inc: float | None, types) -> str | None:
    """One-line summary of a rescue target's orbit requirement, or None."""
    bits: list[str] = []
    toks = normalize_types(types)
    if toks:
        bits.append(", ".join(label(t) for t in toks))
    if inc is not None and (margin_inc or 0) > 0:
        bits.append(f"inclination {float(inc):.1f}° (±{float(margin_inc):.1f}°)")
    return " · ".join(bits) if bits else None
