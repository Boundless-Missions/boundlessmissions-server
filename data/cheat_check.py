"""
cheat_check.py – Disqualify submissions whose vessels the KSP client flagged for
cheats (HyperEdit, VesselMover, the F12 cheat menu's Set Position / Set Orbit
and its cheat toggles).

The client is the only place this is knowable: a teleported vessel really *is*
at the target, so its telemetry is perfectly consistent and telemetry_check can
see nothing wrong — that check catches forged claims, this one catches cheated
truths. The mod's watchdog (KSP side: CheatDetection.cs) taints a vessel when
its flight state breaks physics continuity or a cheat tool/toggle is seen acting
on it, and the submission carries a `cheat_report` JSON covering exactly the
vessels submitted.

Three shapes of report, three meanings — the distinction is the point:
  - report says tainted → disqualify (when the env gate is on)
  - report says clean   → checked and found clean
  - no report at all    → an older client that never checked; treated as clean,
    because a new server-side gate must not start rejecting existing clients
    (the convention every other submission field here follows).

`tools_installed` (HyperEdit merely present) is context for the reviewer and is
deliberately never a reason to reject: presence is not usage, and plenty of
players keep HyperEdit installed for sandbox testing while flying missions
clean.

Gated by cfg.KSP_CHEAT_DISQUALIFY_ENABLED (.env, default on), passed in by the
caller so this module stays import-light and unit-testable. Like every client-
reported field this is one layer of defense-in-depth, not an anti-cheat — a
modified DLL can simply not report, exactly as it could forge telemetry.
"""

import json
from typing import NamedTuple

# A vessel can accumulate several reasons; cap what we echo back so a hand-
# crafted report can't turn the refusal message into a wall of text.
_MAX_REASONS_PER_VESSEL = 6
_MAX_VESSELS = 8


class Result(NamedTuple):
    reject: bool          # caller should refuse the submission
    reject_message: str   # player-facing reason (when reject is True)
    detail: str           # compact audit detail for the log


def evaluate(cheat_report: str | None, enabled: bool = True) -> Result:
    """Resolve a submission's cheat_report into a (reject?) decision. Never
    raises — a malformed or absent report yields a clean no-op result."""
    none = Result(False, "", "")
    if not enabled or not cheat_report:
        return none

    try:
        report = json.loads(cheat_report)
    except Exception:
        return none
    if not isinstance(report, dict) or not report.get("tainted"):
        return none

    lines: list[str] = []
    try:
        for v in (report.get("vessels") or [])[:_MAX_VESSELS]:
            if not isinstance(v, dict):
                continue
            name = str(v.get("name") or "vessel").strip() or "vessel"
            reasons = [str(r).strip() for r in (v.get("reasons") or []) if str(r).strip()]
            if reasons:
                lines.append(f"{name}: " + "; ".join(reasons[:_MAX_REASONS_PER_VESSEL]))
    except Exception:
        lines = []
    if not lines:
        # tainted=true with no usable per-vessel detail (shouldn't happen, but the
        # verdict field is what the client asserted — honor it with a generic line).
        lines = ["the submitted craft was flagged by the in-game cheat watchdog"]

    message = (
        "Submission disqualified — cheats were detected on the submitted craft:\n- "
        + "\n- ".join(lines)
        + "\n\nQuickload or revert to a save from before the cheat was used and "
        "fly the mission clean, then submit again."
    )
    return Result(reject=True, reject_message=message, detail=" | ".join(lines))
