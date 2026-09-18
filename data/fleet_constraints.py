"""
data/fleet_constraints.py — Constellation ("relay network") extraction & verification.

Some missions ask for a *network* rather than a craft: "deploy a communication
relay network around the Mun", "deploy three relay satellites into Kerbin orbit".
Until this module existed those were graded like any other flight mission — one
snapshot of whichever vessel the player happened to be flying, plus a screenshot
and the AI reviewer's goodwill. The mission text said "three", nothing counted to
three, and `data/mission_constraints.py` has no vessel-count bound to offer (its
counts are crew and parts).

This module turns that text into a structured constellation constraint and verifies
a *set* of vessels against it. The same canonical schema is enforced in two places,
mirroring data/orbit_constraints.py:
  • the KSP submit-button gate (client-side pre-check — see FleetConstraint.cs)
  • the bot's /submit endpoint (authoritative re-check — see api_server.py)

## Why this could be built at all

The obvious objection is physics range: KSP only *loads* vessels within ~2.5 km, so
a network spread around the Mun is mostly unloaded and, on the face of it, invisible
to the client. It isn't. `FlightGlobals.Vessels` lists every vessel in the save
whether loaded or not, unloaded orbits are kept live on rails, and part lists survive
as `protoPartSnapshots`. So the client can enumerate a whole sphere of influence
without loading anything — see `VesselDataCollector.CaptureBodyFleet`. What was
missing was never the scan; it was that the submit contract carried one snapshot.

## What a member has to be

A vessel counts toward the network when it is in the required body's SOI, ORBITING
on a bound orbit, not junk (Debris/Flag/SpaceObject/EVA), and — when the mission
says "relay" — carrying a relay-capable antenna. That last one is read from the
part prefab client-side (`ModuleDataTransmitter.antennaType == RELAY`) and arrives
here as a bare boolean, because the *set of parts that count as a relay* is a
question only the player's install can answer.

Deliberately NOT verified through CommNet, though the game maintains a real network
for unloaded vessels and it was the first thing tried. Two reasons: CommNet is off
entirely in some saves (`CommNetScenario.CommNetEnabled`), which would make the
mission unsubmittable rather than unmet; and RealAntennas replaces `CommNetVessel`
wholesale, which this codebase already knows hurts (see the note at
RescueImmunityGuardian.cs:960). Geometry — how many, how high, how spread — is
mod-agnostic and rescale-safe, and it is what the mission text actually describes.

Canonical constraint dict (omitted/empty == no constellation requirement):
    {
      "count":  int,      # how many vessels must qualify
      "relay":  bool,     # each member needs a relay-capable antenna
      "spread": float,    # degrees; minimum gap between neighbouring members in
                          # mean longitude. Written at extraction time from
                          # FLEET_SPREAD_FRAC so client and server cannot derive
                          # it apart — same rule as the orbit altitude margin.
      "notes":  str,      # human-readable summary (optional)
    }

Every number here is reported by the (untrusted) KSP client, exactly like Δv and the
used-parts list. `data/telemetry_check.check_fleet` is the backstop: it re-derives
mu = 4*pi^2*a^3/T^2 from each member's own claimed sma and period and rejects a set
that does not agree on one value, which is the fleet-sized version of the
over-determination argument that module already makes per snapshot.
"""
from __future__ import annotations

import math
import re

import settings

# ── Vocabulary ────────────────────────────────────────────────────────────────

# Phrases that mark a mission as being about a constellation rather than a craft.
# A bare "satellite" is not here on purpose: "deploy a satellite into Kerbin orbit"
# is one craft, and reading it as a network would make an easy mission unsubmittable.
_NETWORK_CUES = (
    "relay network", "comm network", "comms network", "communication network",
    "communications network", "satellite network", "constellation",
    "relay constellation", "network of satellites", "network of relays",
    "röle ağı", "uydu ağı", "iletişim ağı", "haberleşme ağı", "takımyıldız",
)

# Words that make the counted objects satellites rather than something else, so
# "three relay satellites" counts and "three kerbals" does not. Checked in a narrow
# window after the number (see _extract_count).
_SAT_WORDS = (
    "satellite", "satellites", "relay", "relays", "relay satellite",
    "relay satellites", "probe", "probes", "comsat", "comsats",
    "uydu", "uydular", "röle", "röleler", "sonda", "sondalar",
)

# Words that say the members must be relay-capable specifically.
_RELAY_WORDS = ("relay", "comm", "comms", "comsat", "communication", "communications",
                "röle", "iletişim", "haberleşme")

# Number words worth understanding. A constellation is never spelled out past a
# handful, and a digit covers everything above.
_NUM_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8,
}

# Situations a vessel can report while genuinely on a closed orbit. Mirrors
# telemetry_check / orbit_constraints — a LANDED relay is a ground station, not a
# member of an orbital network.
_ORBITAL_SITUATIONS = {"ORBITING", "DOCKED"}

# Vessel types that are never network members however they are orbiting. Mirrors
# VesselDataCollector.IsTransferable, plus EVA: a kerbal floating in a shell of
# satellites should not be counted as one of them.
_JUNK_TYPES = {"debris", "flag", "spaceobject", "unknown", "eva"}


# ── Shape ─────────────────────────────────────────────────────────────────────

def empty() -> dict:
    """A constraint dict with no constellation requirement."""
    return {}


def is_empty(constraint: dict | None) -> bool:
    """True when there is no constellation requirement to enforce."""
    if not constraint:
        return True
    return not _count_of(constraint)


def _count_of(constraint: dict | None) -> int:
    """The member count a constraint demands, or 0 when it demands none. The count
    is what makes a constellation constraint exist at all — "relay" and "spread"
    only ever qualify a count, they are never a requirement on their own."""
    try:
        n = int((constraint or {}).get("count") or 0)
    except (TypeError, ValueError):
        return 0
    return n if 2 <= n <= settings.FLEET_MAX_MEMBERS else 0


def normalize(raw: dict | None) -> dict:
    """Coerce a loose constraint dict (from Firestore, from a client, from the AI)
    into the canonical shape, dropping anything unreadable. Never raises."""
    if not isinstance(raw, dict):
        return empty()
    count = _count_of(raw)
    if not count:
        return empty()
    out: dict = {"count": count, "relay": bool(raw.get("relay"))}

    spread = raw.get("spread")
    try:
        s = float(spread)
    except (TypeError, ValueError):
        s = None
    if s is None or not math.isfinite(s) or s <= 0:
        s = default_spread(count)
    # A gap larger than the even spacing is unsatisfiable by construction: N members
    # on a circle cannot all be more than 360/N apart.
    out["spread"] = min(s, 360.0 / count)

    # How strong each member's relay antenna has to be, as antenna power. Optional —
    # absent means "any relay-capable antenna", which is what `relay` alone has always
    # meant. Expressed as a number rather than a part name because a name is answerable
    # only by the mod that ships it, while every install has a power figure for every
    # antenna, RealAntennas' rewrites included. The issuer picks a part they own and the
    # form sends what it is worth; the contractor satisfies it with whatever they have.
    power = raw.get("min_relay_power")
    try:
        p = float(power)
    except (TypeError, ValueError):
        p = None
    if p is not None and math.isfinite(p) and p > 0:
        out["min_relay_power"] = p
        # Which antenna model the number is written in. Recorded because there is no
        # conversion between models: a stock rating and a RealAntennas dBm figure are
        # different quantities, so a floor can only be judged on an install sharing the
        # model it was authored in. Naming it lets the other install SAY so instead of
        # silently passing or refusing the requirement.
        model = raw.get("relay_power_model")
        out["relay_power_model"] = (str(model).strip().lower()[:32]
                                    if isinstance(model, str) and model.strip() else "stock")

    notes = raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:200]
    return out


def default_spread(count: int) -> float:
    """The minimum neighbour gap a network of `count` members gets when the text
    names none: a fraction of the even spacing, so an imperfect network passes and
    a formation flying in one place does not."""
    if count < 2:
        return 0.0
    return (360.0 / count) * float(settings.FLEET_SPREAD_FRAC)


# ── Extraction ────────────────────────────────────────────────────────────────

_RX_COUNT = re.compile(
    r"\b(\d{1,2}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE)


def extract_heuristic(text: str) -> dict:
    """Keyword-based constellation extraction. Conservative in both directions: a
    mission only becomes a network mission when it says so, and a network with no
    stated number gets FLEET_DEFAULT_COUNT rather than a guess scaled to difficulty.

    Returns an empty constraint when constellation checking is disabled in settings,
    so both the contract-listing merge and the submit gate naturally no-op."""
    if not getattr(settings, "FLEET_CHECK_ENABLED", True) or not text:
        return empty()

    low = text.lower()
    has_network_cue = any(cue in low for cue in _NETWORK_CUES)
    count = _extract_count(low)

    # Two ways in. Either the text names the network ("a relay network around the
    # Mun"), or it counts the satellites ("three relay satellites into Kerbin
    # orbit") — the second is a network whether or not the word appears.
    if not has_network_cue and not count:
        return empty()

    if not count:
        count = int(settings.FLEET_DEFAULT_COUNT)
    count = max(2, min(count, settings.FLEET_MAX_MEMBERS))

    relay = any(w in low for w in _RELAY_WORDS)
    return normalize({"count": count, "relay": relay,
                      "spread": default_spread(count)})


def _extract_count(low: str) -> int:
    """How many satellites the text asks for, or 0. The number only counts when a
    satellite word follows it closely — "three relay satellites" is a count,
    "three kerbals aboard a relay network" is not, and the crew floor in
    data/mission_constraints.py is what should read the latter."""
    for m in _RX_COUNT.finditer(low):
        raw = m.group(1)
        n = _NUM_WORDS.get(raw)
        if n is None:
            try:
                n = int(raw)
            except ValueError:
                continue
        if not (2 <= n <= settings.FLEET_MAX_MEMBERS):
            continue
        # A short window, so the satellite word has to belong to this number:
        # enough for "three relay satellites", not enough to reach across a clause.
        tail = low[m.end():m.end() + 28]
        if any(re.match(r"\s*(?:\w+\s+){0,2}" + re.escape(w) + r"\b", tail)
               for w in _SAT_WORDS):
            return n
    return 0


# ── Verification ──────────────────────────────────────────────────────────────

def _num(snap: dict, key: str) -> float | None:
    """A finite float from a snapshot field, or None."""
    v = snap.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def mean_longitude(snap: dict) -> float | None:
    """Where a member sits around the body, in degrees 0..360.

    LAN + argument of periapsis + mean anomaly. For the near-circular, near-common
    plane a relay shell actually is, that is the phase angle you would draw on a
    map view, and unlike true anomaly it advances uniformly — so it can be compared
    across members captured at one instant without solving Kepler's equation.

    The three elements arrive separately rather than pre-summed on purpose: they are
    three more fields a forger has to keep mutually consistent, and summing them here
    means client and server cannot disagree about how the phase was derived.
    """
    lan, argpe, ma = (_num(snap, "lan"), _num(snap, "arg_pe"),
                      _num(snap, "mean_anomaly"))
    if lan is None or argpe is None or ma is None:
        return None
    return (lan + argpe + ma) % 360.0


def is_member(snap: dict, constraint: dict, body: str | None) -> bool:
    """Whether one snapshot counts toward the network. The autofilter, server side —
    the client applies the same test in FleetConstraint.IsMember before it offers
    the list, so the player sees the count they will be graded on."""
    if not isinstance(snap, dict):
        return False
    if body:
        if str(snap.get("body") or "").strip().lower() != body.strip().lower():
            return False
    if str(snap.get("situation") or "").strip().upper() not in _ORBITAL_SITUATIONS:
        return False
    if str(snap.get("vessel_type") or "").strip().lower() in _JUNK_TYPES:
        return False

    ecc = _num(snap, "eccentricity")
    if ecc is None or ecc >= 1.0:      # escaping / hyperbolic — passing through
        return False
    pe = _num(snap, "periapsis")
    if pe is None or pe <= 0:          # periapsis underground: a decaying orbit
        return False
    if _num(snap, "sma") is None or not (_num(snap, "period") or 0) > 0:
        return False
    if constraint.get("relay") and not snap.get("has_relay"):
        return False

    # Three states, because "weak", "unknown" and "unmeasurable" are three different
    # answers and only one of them should disqualify a satellite:
    #
    #   absent   an older client that predates the field. Fails closed, like the relay
    #            flag above: an antenna we cannot vouch for is not one to count.
    #   below 0  the install does not express antenna strength at all. RealAntennas
    #            deletes antennaPower from every part and rates an antenna in dBm and
    #            gain instead, which is not the same quantity in other units — there is
    #            nothing to compare. The floor is SKIPPED rather than failed: the
    #            contract still means what it says about the network, the spacing and
    #            the relay flag, and refusing every satellite on an RO install would
    #            make a stock-written contract unfillable there for a reason the player
    #            cannot act on. Same precedent as the spread test, which skips when a
    #            member reports no orbital angles rather than rejecting the client.
    #   0 or up  a real reading in the model the floor was written in. Compared.
    need_power = constraint.get("min_relay_power")
    if need_power:
        have = _num(snap, "relay_power")
        if have is None:
            return False
        if 0 <= have < float(need_power):
            return False
    return True


def _best_shell(members: list[dict], n: int) -> list[dict]:
    """The `n` members that most look like one shell — the window of `n` with the
    tightest spread in semi-major axis.

    A player's Mun may hold a relay network *and* a lander *and* a fuel depot, all
    of them orbiting and some of them carrying relay antennas. The mission asks
    whether a network exists, not whether every craft the player owns is part of
    one, so the check picks the best candidate subset rather than failing because
    an unrelated craft is in a different orbit.
    """
    if len(members) <= n:
        return list(members)
    ordered = sorted(members, key=lambda s: _num(s, "sma") or 0.0)
    best, best_spread = ordered[:n], float("inf")
    for i in range(len(ordered) - n + 1):
        window = ordered[i:i + n]
        lo = _num(window[0], "sma") or 0.0
        hi = _num(window[-1], "sma") or 0.0
        spread = (hi - lo) / hi if hi > 0 else float("inf")
        if spread < best_spread:
            best, best_spread = window, spread
    return best


def _min_gap(longitudes: list[float]) -> float:
    """The smallest gap between neighbours around the circle, in degrees. Wraps, so
    a member at 359° and one at 1° are 2° apart, not 358°."""
    if len(longitudes) < 2:
        return 360.0
    ordered = sorted(l % 360.0 for l in longitudes)
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    gaps.append(360.0 - ordered[-1] + ordered[0])
    return min(gaps)


def verify_fleet(constraint: dict | None, snapshots: list[dict],
                 body: str | None = None) -> list[str]:
    """Every way a submitted set of vessels fails a constellation requirement, as
    player-facing sentences. Empty list == satisfied.

    Never raises: a malformed payload yields "no members found", which is a refusal
    the player can act on, rather than an exception that reaches the endpoint."""
    constraint = normalize(constraint)
    if is_empty(constraint):
        return []
    if not getattr(settings, "FLEET_CHECK_ENABLED", True):
        return []

    want = constraint["count"]
    try:
        members = [s for s in (snapshots or []) if is_member(s, constraint, body)]
    except Exception:
        members = []

    where = f" around {body}" if body else ""
    if len(members) < want:
        kind = "relay satellites" if constraint.get("relay") else "satellites"
        return [f"This mission needs {want} {kind}{where}; "
                f"{len(members)} of your vessels there qualify."]

    shell = _best_shell(members, want)
    problems: list[str] = []

    # One shell, not a scattering: the members have to be at comparable altitudes.
    smas = [_num(s, "sma") or 0.0 for s in shell]
    lo, hi = min(smas), max(smas)
    if hi > 0 and (hi - lo) / hi > settings.FLEET_SMA_TOLERANCE:
        problems.append(
            f"The {want} qualifying vessels aren't in a common shell. Their orbits "
            f"range from {(lo - (_num(shell[0], 'body_radius') or 0.0)) / 1000:.0f} km "
            f"to {(hi - (_num(shell[0], 'body_radius') or 0.0)) / 1000:.0f} km "
            "semi-major axis. A relay network needs them at a comparable altitude.")

    # Spread around the body, not flying in formation.
    longs = [ml for ml in (mean_longitude(s) for s in shell) if ml is not None]
    if len(longs) == len(shell):
        gap = _min_gap(longs)
        need = float(constraint["spread"])
        if gap + 1e-9 < need:
            problems.append(
                f"The satellites are bunched together. The closest pair is {gap:.0f}° "
                f"apart around {body or 'the body'} and a network of {want} needs at "
                f"least {need:.0f}°. Spread them around the orbit.")
    # An older client that reports no orbital angles simply skips the spread test;
    # the same contract must not start rejecting clients that predate the field.

    return problems


# ── Presentation ──────────────────────────────────────────────────────────────

def describe(constraint: dict | None) -> str:
    """One line for a contract card / the submit panel, or "" when unconstrained."""
    constraint = normalize(constraint)
    if is_empty(constraint):
        return ""
    n = constraint["count"]
    kind = "relay satellites" if constraint.get("relay") else "satellites"
    line = f"{n} {kind} in orbit, at least {constraint['spread']:.0f}° apart"
    power = constraint.get("min_relay_power")
    if power:
        line += f", each with a {format_antenna_power(power)} relay antenna"
        model = constraint.get("relay_power_model") or "stock"
        if model != "stock":
            line += f" ({model} rating)"
    return line


def format_antenna_power(power: float) -> str:
    """Antenna power as a player reads it: KSP writes these as 2000000, the parts list
    shows 2M, and a requirement nobody can compare against their own antenna is not a
    requirement they can meet."""
    try:
        p = float(power)
    except (TypeError, ValueError):
        return "?"
    for cutoff, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if p >= cutoff:
            scaled = p / cutoff
            return (f"{scaled:.0f}{suffix}" if abs(scaled - round(scaled)) < 0.05
                    else f"{scaled:.1f}{suffix}")
    return f"{p:.0f}"
