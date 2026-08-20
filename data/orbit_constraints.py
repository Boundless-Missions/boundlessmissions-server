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
      "alt": {                 # numeric altitude requirement (optional), metres ASL
        "ap":     float|None,  # target apoapsis  ("100x100 km", "apoapsis of 200 km")
        "pe":     float|None,  # target periapsis
        "margin": float|None,  # ± tolerance for the targets; written at extraction
                               # time so client and server can't derive it apart
        "min":    float|None,  # whole orbit at/above this (checked against periapsis)
        "max":    float|None,  # whole orbit at/below this (checked against apoapsis)
      },
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
    return not constraint.get("requirements") and not _alt_of(constraint)


_ALT_KEYS = ("ap", "pe", "margin", "min", "max")


def _alt_of(constraint: dict | None) -> dict | None:
    """The altitude sub-dict when it actually constrains something, else None."""
    alt = (constraint or {}).get("alt")
    if not isinstance(alt, dict):
        return None
    for k in ("ap", "pe", "min", "max"):  # a bare margin constrains nothing
        if alt.get(k) is not None:
            return alt
    return None


def _norm_alt(raw) -> dict | None:
    """Validate a loose altitude sub-dict: finite positive metres or absent."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for k in _ALT_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and f > 0:
            out[k] = f
    return out if any(out.get(k) is not None for k in ("ap", "pe", "min", "max")) else None


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
    alt = _norm_alt(raw.get("alt"))
    if alt:
        out["alt"] = alt
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

    # Numeric altitude requirement ("a 100x100 km orbit", "orbit at 250 km").
    # Cue-gated like the ambiguous adjectives: a bare number with a unit is
    # ordinary mission flavour unless the text is about an orbit.
    if has_cue:
        alt = _extract_altitude(low)
        if alt:
            out["alt"] = alt
    return out


# ── Numeric altitude extraction ───────────────────────────────────────────────

# A decimal number: "100", "71.5", "1,5" (comma-decimal locales write "1,5 km").
# "1,500 km" (thousands comma) is read as 1.5 km — the ambiguity is unresolvable
# from text alone and low-stakes here; authors writing thousands spell "1500".
_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(km|m)"

# "100x100 km", "80 × 120 km", "100km x 200km", "100 by 100 km". The first unit
# is optional and falls back to the second, which is how people actually write it.
_RX_APXPE = re.compile(_NUM + r"\s*" + _UNIT + r"?\s*(?:x|×|by)\s*" + _NUM + r"\s*" + _UNIT + r"\b")

# "orbit at/of/around 100 km" (cue before the number) and "100 km orbit",
# "100 km circular parking orbit", "100 km'lik yörünge" (number before the cue,
# at most a few words between). The number-before-cue form is the loosest match
# in this file, so it takes km only: with bare metres allowed, "500 m of cable to
# the orbital station" reads as a 500 m orbit.
_RX_CUE_NUM = re.compile(r"(?:orbit\w*|yörünge\w*)\s+(?:at|of|around|:)?\s*(?:an?\s+)?~?"
                         + _NUM + r"\s*" + _UNIT + r"\b")
_RX_NUM_CUE = re.compile(_NUM + r"\s*(km)\b[^\d.;!?]{0,40}?(?:orbit|yörünge)")

# Bounds: the whole orbit above/below an altitude.
_RX_ALT_MIN = re.compile(r"orbit\w*\s+(?:above|over|higher than)\s+" + _NUM + r"\s*" + _UNIT + r"\b")
_RX_ALT_MAX = re.compile(r"orbit\w*\s+(?:below|under|lower than)\s+" + _NUM + r"\s*" + _UNIT + r"\b")

# Explicit per-side targets: "apoapsis of 200 km", "periapsis at 80km".
_RX_AP = re.compile(r"apoapsis\s*(?:of|at|=|:)?\s*" + _NUM + r"\s*" + _UNIT + r"\b")
_RX_PE = re.compile(r"periapsis\s*(?:of|at|=|:)?\s*" + _NUM + r"\s*" + _UNIT + r"\b")

# An explicit tolerance: "within 5 km", "±10 km", "+/- 10km".
_RX_MARGIN = re.compile(r"(?:±|\+/-|within|plus or minus)\s*" + _NUM + r"\s*" + _UNIT + r"\b")


def _metres(num: str, unit: str) -> float | None:
    try:
        v = float(num.replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v * 1000.0 if unit == "km" else v


def default_alt_margin(ap: float | None, pe: float | None) -> float:
    """The ± tolerance an altitude target gets when the text names none: generous —
    the larger of a flat floor and a fraction of the bigger target, so "100 km"
    doesn't demand 100.0 km while "2000 km" isn't held to ±10."""
    target = max(ap or 0.0, pe or 0.0)
    return max(settings.ORBIT_ALT_MARGIN_MIN, target * settings.ORBIT_ALT_MARGIN_FRAC)


def _extract_altitude(low: str) -> dict | None:
    """Parse numeric altitude requirements out of (lowercased) mission text.
    Conservative: every form requires a unit, and the caller has already required
    an orbit cue word. Returns a canonical `alt` sub-dict or None. The margin is
    materialised here (explicit "within N km" wins, default otherwise) so the KSP
    client verifies against the same number the server does."""
    ap: float | None = None
    pe: float | None = None

    m = _RX_APXPE.search(low)
    if m:
        first = _metres(m.group(1), m.group(2) or m.group(4))
        second = _metres(m.group(3), m.group(4))
        if first is not None and second is not None:
            # Ap is the higher of the pair whichever way the author wrote it.
            ap, pe = max(first, second), min(first, second)

    # Named per-side targets override the pair form's split for that side.
    m = _RX_AP.search(low)
    if m:
        v = _metres(m.group(1), m.group(2))
        if v is not None:
            ap = v
    m = _RX_PE.search(low)
    if m:
        v = _metres(m.group(1), m.group(2))
        if v is not None:
            pe = v

    # A single "orbit at N km" only when nothing above matched: "100x100 km orbit"
    # must not additionally read as a 100 km single-altitude requirement.
    if ap is None and pe is None:
        m = _RX_CUE_NUM.search(low) or _RX_NUM_CUE.search(low)
        if m:
            v = _metres(m.group(1), m.group(2))
            if v is not None:
                ap = pe = v

    alt_min = alt_max = None
    m = _RX_ALT_MIN.search(low)
    if m:
        alt_min = _metres(m.group(1), m.group(2))
    m = _RX_ALT_MAX.search(low)
    if m:
        alt_max = _metres(m.group(1), m.group(2))

    if ap is None and pe is None and alt_min is None and alt_max is None:
        return None

    out: dict = {}
    if ap is not None:
        out["ap"] = ap
    if pe is not None:
        out["pe"] = pe
    if alt_min is not None:
        out["min"] = alt_min
    if alt_max is not None:
        out["max"] = alt_max

    if ap is not None or pe is not None:
        margin = None
        m = _RX_MARGIN.search(low)
        if m:
            margin = _metres(m.group(1), m.group(2))
            # Respect a precise ask, but never let it hit zero-width.
            if margin is not None:
                margin = max(margin, 1000.0)
        out["margin"] = margin if margin is not None else default_alt_margin(ap, pe)
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

    reqs = constraint.get("requirements") or []
    alt = _alt_of(constraint)
    situation = (snap.get("situation") or "").upper()
    if situation not in _ORBITAL_SITUATIONS:
        bits = [_LABELS.get(r, r) for r in reqs]
        if alt:
            bits.append(_alt_summary(alt))
        names = ", ".join(bits)
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
    if alt:
        out.extend(_check_alt(alt, _num(snap, "apoapsis"), _num(snap, "periapsis")))
    return out


def _check_alt(alt: dict, apo: float | None, peri: float | None) -> list[str]:
    """Verify a snapshot's Ap/Pe (metres above the surface) against an altitude
    requirement. Elements the client didn't report are skipped, like everywhere
    else in this file — the telemetry-consistency check is what keeps a snapshot
    honest, not this."""
    out: list[str] = []
    ap_t = alt.get("ap")
    pe_t = alt.get("pe")
    if ap_t is not None or pe_t is not None:
        margin = alt.get("margin")
        if margin is None or margin <= 0:
            margin = default_alt_margin(ap_t, pe_t)
        bad_ap = ap_t is not None and apo is not None and abs(apo - ap_t) > margin
        bad_pe = pe_t is not None and peri is not None and abs(peri - pe_t) > margin
        if bad_ap or bad_pe:
            need = " / ".join(
                bit for bit, want in (
                    (f"Ap {_fmt_km(ap_t)}", ap_t), (f"Pe {_fmt_km(pe_t)}", pe_t))
                if want is not None)
            have = " / ".join(
                bit for bit, t in (
                    (f"Ap {_fmt_km(apo)}", ap_t), (f"Pe {_fmt_km(peri)}", pe_t))
                if t is not None)
            out.append(f"Orbit off target: need {need} (±{_fmt_km(margin)}); "
                       f"current is {have}.")
    alt_min = alt.get("min")
    if alt_min is not None and peri is not None and peri < alt_min:
        out.append(f"The whole orbit must stay above {_fmt_km(alt_min)}; "
                   f"current periapsis is {_fmt_km(peri)}.")
    alt_max = alt.get("max")
    if alt_max is not None and apo is not None and apo > alt_max:
        out.append(f"The whole orbit must stay below {_fmt_km(alt_max)}; "
                   f"current apoapsis is {_fmt_km(apo)}.")
    return out


def _fmt_km(metres: float | None) -> str:
    if metres is None:
        return "?"
    km = metres / 1000.0
    return f"{km:,.1f} km" if abs(km) < 10 else f"{km:,.0f} km"


def _alt_summary(alt: dict) -> str:
    """Short human description of an altitude requirement: "100×100 km (±10 km)",
    "Ap 250 km", "above 400 km"."""
    bits: list[str] = []
    ap_t, pe_t = alt.get("ap"), alt.get("pe")
    if ap_t is not None or pe_t is not None:
        margin = alt.get("margin")
        if margin is None or margin <= 0:
            margin = default_alt_margin(ap_t, pe_t)
        if ap_t is not None and pe_t is not None:
            core = (_fmt_km(ap_t) if ap_t == pe_t
                    else f"{_fmt_km(ap_t)} × {_fmt_km(pe_t)}")
        elif ap_t is not None:
            core = f"Ap {_fmt_km(ap_t)}"
        else:
            core = f"Pe {_fmt_km(pe_t)}"
        bits.append(f"{core} (±{_fmt_km(margin)})")
    if alt.get("min") is not None:
        bits.append(f"above {_fmt_km(alt['min'])}")
    if alt.get("max") is not None:
        bits.append(f"below {_fmt_km(alt['max'])}")
    return ", ".join(bits)


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
    bits = [_LABELS.get(r, r) for r in (constraint.get("requirements") or [])]
    alt = _alt_of(constraint)
    if alt:
        bits.append(_alt_summary(alt))
    return ("Required orbit: " + ", ".join(bits)) if bits else None


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


# Regime pairs no single orbit can satisfy, derived from the check maths above
# (e.g. polar needs inclination ≈90°, equatorial ≈0°/180°; Molniya's half-day
# period contradicts a synchronous one). Used to refuse a rescue target at
# creation time — a contradictory set isn't "strict", it's unfillable, and the
# submit gate would list every violation forever without ever passing. Pairs that
# merely overlap ("stationary" implies "circular") are allowed: redundant is
# satisfiable. Mirrored in ContractCreation.cs::OrbitTypeConflictMap — keep the
# two in sync (same convention as REQUIREMENTS ↔ OrbitTypeTokens).
_CONFLICTS: dict[str, frozenset[str]] = {
    "prograde":        frozenset({"retrograde"}),
    "retrograde":      frozenset({"prograde", "molniya", "tundra"}),
    "polar":           frozenset({"equatorial", "stationary", "molniya", "tundra"}),
    "equatorial":      frozenset({"polar", "molniya", "tundra"}),
    "circular":        frozenset({"elliptical", "molniya", "tundra"}),
    "elliptical":      frozenset({"circular", "stationary"}),
    "stationary":      frozenset({"polar", "elliptical", "molniya", "tundra", "semisynchronous"}),
    "synchronous":     frozenset({"molniya", "semisynchronous"}),
    "semisynchronous": frozenset({"synchronous", "stationary", "tundra"}),
    "molniya":         frozenset({"retrograde", "polar", "equatorial", "circular",
                                  "stationary", "synchronous", "tundra"}),
    "tundra":          frozenset({"retrograde", "polar", "equatorial", "circular",
                                  "stationary", "semisynchronous", "molniya"}),
}


def conflicting_pair(types) -> tuple[str, str] | None:
    """First mutually-unsatisfiable pair in a set of regime tokens, or None.
    Accepts anything normalize_types does."""
    toks = normalize_types(types)
    for i, a in enumerate(toks):
        conflicts = _CONFLICTS.get(a)
        if not conflicts:
            continue
        for b in toks[i + 1:]:
            if b in conflicts:
                return (a, b)
    return None


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
