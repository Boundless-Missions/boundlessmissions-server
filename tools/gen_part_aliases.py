"""Generate data/part_aliases.py from the KSP mod's PartAliases.cs.

The alias table has exactly one source of truth — the C# file, because that is where
the substitution actually happens at install time. The bot needs the same pairs to
answer "can this buyer load this craft?" without over-warning about parts the mod
will silently swap for them. Two hand-maintained copies would drift, so this reads
the C# table and writes the Python one.

Run after editing PartAliases.cs:

    python tools/gen_part_aliases.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CS = Path(__file__).resolve().parents[2] / "KSP Mod Side" / "GeneKerman" / "PartAliases.cs"
OUT = Path(__file__).resolve().parents[1] / "data" / "part_aliases.py"

GROUP_RX = re.compile(r'G\(\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]+)",\s*(null|"[^"]*")\s*\)')


def main() -> int:
    if not CS.exists():
        print(f"error: {CS} not found", file=sys.stderr)
        return 1

    src = CS.read_text(encoding="utf-8")
    groups = [(label, a, b) for label, a, b, _note in GROUP_RX.findall(src)]
    if not groups:
        print("error: no G(...) entries parsed — did the table's shape change?", file=sys.stderr)
        return 1

    seen: dict[str, str] = {}
    for label, a, b in groups:
        for n in (a, b):
            if n in seen:
                print(f"error: {n} appears in both {seen[n]!r} and {label!r}", file=sys.stderr)
                return 1
            seen[n] = label

    lines = [
        '"""Interchangeable KSP part names — GENERATED, do not edit by hand.',
        "",
        "Regenerate with `python tools/gen_part_aliases.py` after editing the source of",
        'truth, "KSP Mod Side/GeneKerman/PartAliases.cs". See that file for how the pairs',
        "were derived and why some look-alikes are deliberately absent.",
        '"""',
        "",
        "# part name -> every other name that is the same part",
        "ALIASES: dict[str, tuple[str, ...]] = {",
    ]
    for label, a, b in groups:
        lines.append(f'    "{a}": ("{b}",),  # {label}')
        lines.append(f'    "{b}": ("{a}",),')
    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def equivalents(part_name: str) -> tuple[str, ...]:")
    lines.append('    """Names that can stand in for `part_name`; empty when it has none."""')
    lines.append("    return ALIASES.get(part_name, ())")
    lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} — {len(groups)} pairs, {len(seen)} names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
