"""
data/mp_social.py – the multiplayer overlay's social actions: message and report.

The presence overlay's owner cards carry Friend / Message / Block / Report
(ksp-mp-presence-visibility.md §7). Block and friend already had storage
(`data/blocks.py`, `data/friends.py`); this module holds the two that did not:

  * **Message** — a short note from one player to another, delivered as an
    ordinary notification in the recipient's feed (the same channel a friend
    request arrives on), so no new inbox, socket frame or client reader exists
    for it. The whole gate is *contactability*, decided by the same filter the
    overlay's identity resolve uses (`mp_profiles.resolve`): a block in either
    direction, or a visibility tier that excludes the sender, makes the target
    unreachable — and indistinguishably so, because a message refused for "they
    blocked you" must read exactly like one refused for "they're hidden", or
    the refusal itself leaks the block. A **mute** is the one-directional
    variant: the recipient asked not to hear from the sender, so the message is
    accepted and silently dropped (the sender learns nothing, which is what a
    mute is for).

  * **Report** — a moderation report against a player, optionally naming the
    craft it is about. Routes into the existing ticket stack (plan §"Report
    actions ... route into existing moderation, not a new pipeline"); this module
    is only its record, shaped like `contract_reports`: one keyed document per
    (subject, reporter) pair so a report is one read and cannot be filed twice to
    look louder, with a cool-down after which the same reporter may file again
    (a player who keeps misbehaving is a new incident, unlike a contract, which
    is one fixed moment).

Message text is stored and pushed **already neutralised** (`clean_message`). The
base mod's notification funnel (`TextSanitizer.CleanNotif`) strips emoji and
clamps length but deliberately leaves markup alone, and the feed renders into TMP
labels with rich text on — so a raw `<size=900>` or `<color=#0000>` from a peer
would draw. Server-authored notifications never contain angle brackets on purpose;
a peer-authored one must not be able to either.
"""

import logging
import re
import time

from data.store import _db

log = logging.getLogger(__name__)

MESSAGE_MAX = 500
REPORT_REASON_MAX = 1500
# A second report by the same reporter against the same player is accepted once
# this long has passed — a repeat is a new incident, not the same one twice.
REPORT_COOLDOWN = 24 * 3600.0

REPORT_CATEGORIES = ("harassment", "griefing", "cheating", "offensive_name", "other")

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_NEWLINES = re.compile(r"\n{3,}")


class SocialUnavailable(Exception):
    """A read or write needed to decide or record the action failed."""


def clean_message(text) -> str:
    """Neutralise a peer-authored message for storage and every renderer.

    Angle brackets become their single-guillemet look-alikes, so no rich-text tag
    can survive into a TMP label while the text still reads the same; control
    characters go (tab and newline kept), runs of blank lines collapse, and the
    result is clamped to `MESSAGE_MAX`.
    """
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _CONTROL.sub("", s)
    s = s.replace("<", "‹").replace(">", "›")
    s = _NEWLINES.sub("\n\n", s).strip()
    return s[:MESSAGE_MAX].rstrip()


def clean_reason(text) -> str:
    s = _CONTROL.sub("", str(text or "")).strip()
    return s[:REPORT_REASON_MAX]


def normalize_category(raw) -> str:
    c = str(raw or "").strip().lower()
    return c if c in REPORT_CATEGORIES else "other"


# ── message gate ─────────────────────────────────────────────────────────────

DELIVER = "deliver"
DROP = "drop"            # muted: accept, tell the sender it was sent, deliver nothing
UNREACHABLE = "unreachable"


def message_gate(sender_id, recipient_id, handle, *, resolve, is_muted) -> str:
    """Decide what happens to a message from `sender_id` to `recipient_id`.

    `resolve(sender, [handle]) -> {handle: profile|None}` is `mp_profiles.resolve`
    (uncached — a block placed a moment ago must bite now); `is_muted(a, b)` is
    `blocks.is_muted`. Injected so the decision is testable without Firestore.

    Fails **closed**: a mute check that cannot be read is treated as unreachable
    rather than delivered, because the one thing a mute promises is that nothing
    arrives.
    """
    sender, recipient = str(sender_id), str(recipient_id)
    if sender == recipient:
        return UNREACHABLE
    profiles = resolve(sender, [handle]) or {}
    prof = profiles.get(handle)
    if not prof or not prof.get("contactable"):
        return UNREACHABLE
    try:
        if is_muted(recipient, sender):
            return DROP
    except Exception:
        return UNREACHABLE
    return DELIVER


# ── report record ────────────────────────────────────────────────────────────

def _col():
    return _db.collection("mp_player_reports")


def _report_id(subject_id, reporter_id) -> str:
    return f"{subject_id}_{reporter_id}"


def get_report(subject_id, reporter_id) -> dict | None:
    try:
        snap = _col().document(_report_id(subject_id, reporter_id)).get()
    except Exception as exc:
        raise SocialUnavailable(str(exc)) from exc
    return snap.to_dict() if snap.exists else None


def report_blocked_by_cooldown(existing: dict | None, now: float | None = None) -> bool:
    """Whether an earlier report from this reporter still covers this subject."""
    if not existing:
        return False
    ts = now if now is not None else time.time()
    try:
        at = float(existing.get("at") or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    return (ts - at) < REPORT_COOLDOWN


def record_report(*, subject_id, subject_handle, subject_name, reporter_id,
                  reporter_name, category, reason, vessel_id="", vessel_name="",
                  universe_id="", guild_id="", ticket_channel_id="") -> None:
    """Store (or refresh) this reporter's report on this subject.

    The Discord ticket is where it is handled; this record keeps the count and the
    moment after the ticket is closed. A repeat after the cool-down overwrites the
    detail and bumps `count`, so a moderator sees "reported 3 times by this person"
    without the reporter being able to inflate it inside the window.
    """
    ref = _col().document(_report_id(subject_id, reporter_id))
    try:
        prev = ref.get()
        count = int((prev.to_dict() or {}).get("count") or 0) if prev.exists else 0
        ref.set({
            "subject_id": str(subject_id),
            "subject_handle": str(subject_handle or ""),
            "subject_name": str(subject_name or ""),
            "reporter_id": str(reporter_id),
            "reporter_name": str(reporter_name or ""),
            "category": normalize_category(category),
            "reason": clean_reason(reason),
            "vessel_id": str(vessel_id or "")[:64],
            "vessel_name": str(vessel_name or "")[:128],
            "universe_id": str(universe_id or "")[:64],
            "guild_id": str(guild_id or ""),
            "ticket_channel_id": str(ticket_channel_id or ""),
            "count": count + 1,
            "at": time.time(),
        })
    except Exception as exc:
        raise SocialUnavailable(str(exc)) from exc
