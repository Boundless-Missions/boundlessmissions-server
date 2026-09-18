#!/usr/bin/env python
"""
tools/weekly_board_preview.py — print the weekly board an install would be offered.

The point of per-install boards is that you cannot see them by reading the template
pool: what a player gets is the pool filtered by their install tags and resolved
against their system's bodies. This prints that, for a real dev instance or for a
made-up profile, without launching KSP and without touching Firestore.

    # every built-in scenario, side by side
    cd "GK Discord Bot" && .venv/bin/python tools/weekly_board_preview.py

    # a real dev instance (reads its GameData/ directory names)
    .venv/bin/python tools/weekly_board_preview.py \
        --instance "/home/ayd/Documents/KSP DEV Instances/KR-KSP" --preset stock

    # a profile by hand
    .venv/bin/python tools/weekly_board_preview.py \
        --mods NearFuturePropulsion,Kerbalism --preset rss --week 2026-W40

## The one caveat

`--instance` reads the **directory names under GameData/**, which is not quite what the
mod sends. `ContractCreation.BuildActiveModlist()` lists only the folders that
contributed a *loaded part*, so the disk listing is an upper bound: a folder that ships
no parts (Real Solar System is the important one — it is configs over the stock system)
appears here and not at runtime. That is exactly why the catalog upload carries the body
list as well, and why `--preset` exists: bodies come from `FlightGlobals.Bodies` and
nothing on disk can tell you them. Treat `--instance` as "which tags could fire" and the
game as the authority.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import settings                                    # noqa: E402
from data import install_profile as ip             # noqa: E402

# Body lists as the two systems report them. Only enough of each to be recognised —
# `tags_for` tests for a subset, not for the whole system.
PRESETS = {
    "stock": ["Kerbol", "Moho", "Eve", "Gilly", "Kerbin", "Mun", "Minmus", "Duna",
              "Ike", "Dres", "Jool", "Laythe", "Vall", "Tylo", "Bop", "Pol", "Eeloo"],
    "rss": ["Sun", "Mercury", "Venus", "Earth", "Moon", "Mars", "Phobos", "Deimos",
            "Ceres", "Jupiter", "Io", "Europa", "Ganymede", "Callisto", "Saturn",
            "Titan", "Enceladus", "Uranus", "Neptune", "Triton", "Pluto"],
    "none": [],
}
# Outer Planets is additive — it adds bodies to whatever system is already there.
PRESETS["opm"] = PRESETS["stock"] + ["Sarnus", "Hale", "Ovok", "Slate", "Tekto",
                                     "Urlum", "Polta", "Priax", "Neidon", "Thatmo",
                                     "Plock", "Karen"]

# The scenarios printed when no profile is given. Named after what they represent
# rather than after an instance, since the instances change.
SCENARIOS = [
    ("stock, nothing installed", "stock", []),
    ("stock + Near Future", "stock", ["NearFuturePropulsion", "NearFutureElectrical"]),
    ("stock + Far Future", "stock", ["FarFutureTechnologies"]),
    ("stock + TAC Life Support", "stock", ["ThunderAerospace"]),
    ("stock + Outer Planets", "opm", []),
    ("RSS + Realism Overhaul", "rss", ["RealismOverhaul", "RealFuels"]),
    ("RSS + RO + Kerbalism (RP-1 shape)", "rss", ["RealismOverhaul", "RealFuels", "Kerbalism"]),
]


def board_for(week: str, bodies: list[str], mods: list[str], count: int):
    """The tags, the bucket and the board, for one profile."""
    from cogs.weeklymissions import _generate_missions
    tags = ip.tags_for({"bodies": bodies, "mods": mods})
    return tags, ip.bucket_id(tags), _generate_missions(week, count, tags)


def show(label: str, week: str, bodies: list[str], mods: list[str], count: int,
         verbose: bool) -> None:
    tags, bucket, board = board_for(week, bodies, mods, count)
    coins = sum(m["coins"] for m in board)
    xp = sum(m["xp"] for m in board)
    avg = sum(m["difficulty"] for m in board) / max(1, len(board))
    print(f"\n\033[1m{label}\033[0m")
    print(f"  tags   {' '.join(sorted(tags))}")
    print(f"  bucket {bucket}   missions {len(board)}   "
          f"avg difficulty {avg:.1f}   week total {coins} coins / {xp} XP")
    if not verbose:
        return
    for m in board:
        gate = m["required_body"] or "editor"
        sit = m["required_situation"] or "-"
        print(f"    #{m['n']:>2}  d{m['difficulty']:<2} {m['coins']:>4}c  "
              f"{gate:<9} {sit:<11} {m['desc_en']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", default="2026-W38", help="week key, e.g. 2026-W38")
    ap.add_argument("--instance", help="a KSP install; its GameData/ names become the mod list")
    ap.add_argument("--mods", default="", help="comma-separated GameData folder names")
    ap.add_argument("--bodies", default="", help="comma-separated body names")
    ap.add_argument("--preset", choices=sorted(PRESETS), help="a canned body list")
    ap.add_argument("--count", type=int, default=settings.WEEKLY_MISSIONS_COUNT)
    ap.add_argument("-q", "--quiet", action="store_true", help="summary lines only")
    args = ap.parse_args()

    mods = [m.strip() for m in args.mods.split(",") if m.strip()]
    bodies = [b.strip() for b in args.bodies.split(",") if b.strip()]

    if args.instance:
        gd = os.path.join(args.instance, "GameData")
        if not os.path.isdir(gd):
            print(f"no GameData under {args.instance}", file=sys.stderr)
            return 2
        mods += sorted(d for d in os.listdir(gd) if os.path.isdir(os.path.join(gd, d)))
    if args.preset:
        bodies += PRESETS[args.preset]

    if not mods and not bodies and not args.instance and not args.preset:
        print(f"week {args.week}, every scenario, {args.count} missions each")
        for label, preset, ms in SCENARIOS:
            show(label, args.week, PRESETS[preset], ms, args.count, not args.quiet)
        return 0

    label = args.instance or "profile"
    if args.instance and not args.preset:
        print("note: no --preset, so no body list, this reads as the stock system.\n"
              "      pass --preset rss for an RSS install.", file=sys.stderr)
    show(os.path.basename(label.rstrip("/")) or label,
         args.week, bodies, mods, args.count, not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
