"""
data/telemetry_check.py — Server-side plausibility check for KSP flight telemetry.

The KSP client is untrusted (see data/suspicion.py): a modified DLL can report any
vessel state it likes. The classic exploit is to claim a vessel is at the contract
target ("ORBITING Minmus") while it is really somewhere easier ("LANDED at Kerbin").
We cannot see the real game state, but the snapshot the client sends is internally
OVER-DETERMINED, and a forger who edits one field rarely keeps the rest consistent.

Two independent signals, both computed here on submit:

  HARD (physically impossible → can reject):
    • Kepler geometry. apoapsis/periapsis are altitudes above the surface, so for any
      bound orbit:
          sma          == body_radius + (apoapsis + periapsis) / 2
          eccentricity == (apoapsis - periapsis) / (apoapsis + periapsis + 2·body_radius)
      These are pure geometry — no GM/μ — so they hold identically on rescaled installs.
      A snapshot that violates them was assembled by hand, not produced by KSP.
    • Situation sanity. An ORBITING craft cannot have its periapsis below the surface,
      and apoapsis cannot sit below periapsis.

  SOFT (suspicious but legitimate on rescale → flag only, never reject):
    • Body-radius spoof. The radius the client reports for the claimed body should be
      near that body's catalogued radius (data/celestial_bodies.py). A large mismatch
      means either a rescale pack (fine) or a forged body name (not), so we only flag.

The geometry checks run only for bound orbits (ORBITING with eccentricity < 1); landed,
sub-orbital and hyperbolic states have no apoapsis to anchor the identity, so they are
left to the body-radius signal and the existing per-mission situation/body gates.

A constellation submission (mission_type "constellation" — see data/fleet_constraints.py)
carries many snapshots instead of one, and gets a third signal the single-vessel case
cannot have: every member's own claimed sma and period must imply the SAME standard
gravitational parameter, because they orbit the same body. See check_fleet.

Pure functions over the snapshot dict — no Firestore, no I/O — so the API layer can call
this synchronously and decide (per settings.TELEMETRY_CHECK_MODE) whether to reject, flag
via flag_suspicion(), or both.
"""
from __future__ import annotations

import math
from typing import Any, NamedTuple

import settings
from data import celestial_bodies as cb


class Violation(NamedTuple):
    hard: bool      # True = physically impossible (reject-worthy); False = advisory/flag
    code: str       # short machine tag for logs
    message: str    # human-readable, surfaced to the player on a hard reject


# Situations KSP reports for a craft on a closed, well-defined orbit.
_ORBITAL_SITUATIONS = {"ORBITING", "DOCKED"}


def _num(snap: dict, key: str) -> float | None:
    """Read a finite float from the snapshot, or None if absent/garbage."""
    v = snap.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def check_snapshot(snap: dict) -> list[Violation]:
    """All telemetry violations for a single vessel snapshot (see module docstring)."""
    if not isinstance(snap, dict):
        return []

    out: list[Violation] = []
    situation = (snap.get("situation") or "").upper()

    body_radius = _num(snap, "body_radius")
    apo = _num(snap, "apoapsis")
    peri = _num(snap, "periapsis")
    sma = _num(snap, "sma")
    ecc = _num(snap, "eccentricity")

    # ── Situation sanity (cheap, no body radius needed) ──────────────────────
    if situation in _ORBITAL_SITUATIONS:
        if apo is not None and peri is not None and apo + 1.0 < peri:
            out.append(Violation(
                True, "apo_below_peri",
                f"Reported apoapsis ({apo:,.0f} m) is below periapsis ({peri:,.0f} m), "
                "which is impossible for an orbit."))
        if peri is not None and peri < 0:
            out.append(Violation(
                True, "peri_subsurface",
                f"Reported as ORBITING but periapsis ({peri:,.0f} m) is below the surface, "
                "so that orbit intersects the ground."))

    # ── Kepler geometry (bound orbits only) ──────────────────────────────────
    geom_ok = (
        situation in _ORBITAL_SITUATIONS
        and body_radius is not None and body_radius > 0
        and apo is not None and peri is not None
        and (ecc is None or ecc < 1.0)
    )
    if geom_ok:
        # sma == body_radius + (apo + peri) / 2
        if sma is not None:
            sma_expected = body_radius + (apo + peri) / 2.0
            denom = max(abs(sma_expected), 1.0)
            if abs(sma - sma_expected) / denom > settings.TELEMETRY_SMA_TOLERANCE:
                out.append(Violation(
                    True, "sma_mismatch",
                    f"Semi-major axis ({sma:,.0f} m) doesn't match the reported "
                    f"apoapsis/periapsis (expected ~{sma_expected:,.0f} m); the orbital "
                    "numbers are inconsistent."))

        # eccentricity == (apo - peri) / (apo + peri + 2·body_radius)
        if ecc is not None:
            ecc_denom = apo + peri + 2.0 * body_radius
            if ecc_denom > 0:
                ecc_expected = (apo - peri) / ecc_denom
                if abs(ecc - ecc_expected) > settings.TELEMETRY_ECC_TOLERANCE:
                    out.append(Violation(
                        True, "ecc_mismatch",
                        f"Eccentricity ({ecc:.4f}) doesn't match the reported apoapsis/"
                        f"periapsis (expected ~{ecc_expected:.4f}); the orbital numbers "
                        "are inconsistent."))

    # ── Body-radius spoof (soft: legitimate on rescale installs) ─────────────
    body = snap.get("body")
    if body and body_radius is not None and body_radius > 0 and cb.is_known(body):
        catalog_radius = cb.get_body(body).get("radius")
        if catalog_radius:
            rel = abs(body_radius - catalog_radius) / catalog_radius
            if rel > settings.TELEMETRY_BODY_RADIUS_TOLERANCE:
                out.append(Violation(
                    False, "body_radius_mismatch",
                    f"Claimed body '{body}' (catalogued radius {catalog_radius:,.0f} m) but "
                    f"the client reported a body radius of {body_radius:,.0f} m "
                    f"({rel*100:.0f}% off); possible body spoof or a rescaled install."))

    return out


def check_fleet(snaps: list[dict]) -> list[Violation]:
    """Violations that only a *set* of snapshots can show.

    A constellation submission (see data/fleet_constraints.py) carries one snapshot
    per vessel in a body's sphere of influence, which multiplies the forgery surface:
    a fabricated network is many snapshots, and each of them can be made
    self-consistent on its own by anyone who read check_snapshot. What a forger has
    to keep consistent *across* them is physics they cannot choose.

    Every vessel orbiting one body obeys the same Kepler's third law, so each
    member's own claimed semi-major axis and period imply the same standard
    gravitational parameter:

        mu = 4 * pi^2 * a^3 / T^2

    The check never assumes a value for mu — it has none to assume, since a rescale
    pack changes it and the body may be modded — only that the set agrees on one.
    That is the fleet-sized version of the argument the per-snapshot geometry checks
    already make: the payload is over-determined, and a hand-edited member drifts off
    a constant the honest ones all sit on.

    Members are compared against the MEDIAN mu rather than the mean, so one forged
    entry cannot drag the reference far enough to accuse the real ones alongside it.
    """
    if not isinstance(snaps, list) or len(snaps) < 2:
        return []

    mus: list[tuple[float, dict]] = []
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        if (snap.get("situation") or "").upper() not in _ORBITAL_SITUATIONS:
            continue
        sma, period = _num(snap, "sma"), _num(snap, "period")
        ecc = _num(snap, "eccentricity")
        if sma is None or period is None or sma <= 0 or period <= 0:
            continue
        if ecc is not None and ecc >= 1.0:      # unbound: no period to speak of
            continue
        mus.append((4.0 * math.pi ** 2 * sma ** 3 / (period ** 2), snap))

    if len(mus) < 2:
        return []

    ordered = sorted(mu for mu, _ in mus)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return []

    out: list[Violation] = []
    tol = getattr(settings, "FLEET_MU_TOLERANCE", 0.02)
    for mu, snap in mus:
        rel = abs(mu - median) / median
        if rel > tol:
            name = snap.get("vessel_name") or "a vessel"
            out.append(Violation(
                True, "fleet_mu_mismatch",
                f"'{name}' reports a semi-major axis and orbital period that imply a "
                f"different {snap.get('body') or 'body'} than the other vessels "
                f"submitted with it ({rel*100:.0f}% off). Vessels orbiting one body "
                "cannot disagree about its gravity."))
    return out


# How many extra vessels one submission may be judged on. `sent_vessels` is a
# client-supplied list with no bound of its own, and every entry here becomes a
# full telemetry pass — the same list that, unbounded, made api_server render and
# upload an arbitrary number of PNGs on the shared event loop. The bound is
# repeated rather than imported because api_server imports this module, so the
# dependency only goes one way; the two numbers must stay equal, and 8 is what
# api_server's MAX_SUBMISSION_IMAGES uses. Truncating rather than refusing keeps
# it what it is — a check, not a gate — and the vessel a contract is actually
# about is the first entry.
MAX_SNAPSHOTS = 8


def _snapshots(vessel_data: dict) -> list[dict]:
    """Every per-vessel snapshot in a submission payload: the active/contract vessel
    plus any extras sent in a multi-vessel submission (capped at MAX_SNAPSHOTS).
    Mirrors the shape api_server already unpacks for orbit rendering."""
    snaps: list[dict] = []
    active = vessel_data.get("active_vessel") or vessel_data
    if isinstance(active, dict):
        snaps.append(active)
    for sv in (vessel_data.get("sent_vessels") or []):
        if isinstance(sv, dict):
            snaps.append(sv)
        if len(snaps) >= MAX_SNAPSHOTS:
            break
    return snaps


def _fleet_snapshots(vessel_data: dict) -> list[dict]:
    """The constellation members in a submission payload, capped at
    settings.FLEET_MAX_MEMBERS.

    Kept apart from _snapshots because the two lists are bounded for different
    reasons and by different numbers. `sent_vessels` is capped at MAX_SNAPSHOTS
    because every entry becomes an uploaded render; a constellation member is
    telemetry only — nothing is rendered, nothing is transferred — so it is capped
    where fleet_constraints caps it, which is high enough for a real network.
    """
    out: list[dict] = []
    limit = getattr(settings, "FLEET_MAX_MEMBERS", 24)
    for m in (vessel_data.get("constellation") or []):
        if isinstance(m, dict):
            out.append(m)
        if len(out) >= limit:
            break
    return out


class Result(NamedTuple):
    reject: bool                 # caller should refuse the submission
    flag: bool                   # caller should record a suspicion
    reject_message: str          # player-facing reason (when reject is True)
    detail: str                  # audit detail for the suspicion record


def evaluate(vessel_data: dict | None) -> Result:
    """Run the consistency checks over a submission and resolve them against
    settings.TELEMETRY_CHECK_MODE into a (reject?, flag?) decision. Never raises —
    a malformed payload yields a clean no-op result."""
    none = Result(False, False, "", "")
    mode = getattr(settings, "TELEMETRY_CHECK_MODE", "reject_and_flag")
    if not getattr(settings, "TELEMETRY_CHECK_ENABLED", True) or mode == "off":
        return none
    if not isinstance(vessel_data, dict):
        return none

    try:
        violations: list[Violation] = []
        for snap in _snapshots(vessel_data):
            violations.extend(check_snapshot(snap))
        # A constellation payload is checked per member and then across members —
        # the cross-check is the only one that can see a fabricated network whose
        # entries are each individually plausible.
        fleet = _fleet_snapshots(vessel_data)
        for snap in fleet:
            violations.extend(check_snapshot(snap))
        violations.extend(check_fleet(fleet))
    except Exception:
        # Defensive: a bug in the checker must never block a legitimate submission.
        return none

    if not violations:
        return none

    hard = [v for v in violations if v.hard]
    detail = "\n".join(f"- [{'HARD' if v.hard else 'soft'}] {v.message}" for v in violations)

    reject = bool(hard) and mode in ("reject_and_flag", "reject_only")
    flag = mode in ("reject_and_flag", "flag_only")

    reject_message = ""
    if reject:
        reject_message = (
            "This submission's flight telemetry is internally inconsistent:\n- "
            + "\n- ".join(v.message for v in hard)
        )

    return Result(reject=reject, flag=flag, reject_message=reject_message, detail=detail)
