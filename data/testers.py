"""
data/testers.py – accounts the owner has marked as testers.

A tester is an ordinary account given early access to surfaces that are not open
to everyone yet. Today that is the multiplayer **server management** page
(`/api/v1/web/mp/servers*`); the BM Launcher's tester-only install channel is
meant to reuse the same list (`bm-launcher.md`), which is why it is its own
module and not a flag inside the server code.

Distinct from `role_gate.py`, which restricts a *bot instance* to holders of a
Discord role. This list is per **account**, so a website-only player (no
Discord) can be a tester, and it is managed by the owner from the admin console
(`/api/v1/web/admin/testers`), not from `.env`, so adding one needs no restart.

Document shape:

    config/testers
    {
        "accounts": {"<account id>": {"added_at": "<iso>", "note": "…"}},
        "updated_at": "<iso>"
    }

Read on every gated request, so memoised (60 s, invalidated by this process's
own writes) — the same posture as `data/policy.py`. Fails CLOSED: a read error
means "not a tester", never "everyone is".
"""

import logging
import threading
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

MAX_TESTERS = 200
NOTE_MAX = 120
_CACHE_TTL = 60.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "accounts": None}


class TesterError(RuntimeError):
    pass


def _doc():
    return _db.collection("config").document("testers")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def invalidate() -> None:
    with _lock:
        _cache["accounts"] = None
        _cache["at"] = 0.0


def _read(*, fresh: bool = False) -> dict:
    if not fresh:
        with _lock:
            accts, at = _cache["accounts"], float(_cache["at"])
        if accts is not None and time.time() - at < _CACHE_TTL:
            return accts
    snap = _doc().get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    accts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    with _lock:
        _cache["accounts"] = dict(accts)
        _cache["at"] = time.time()
    return accts


def is_tester(account_id) -> bool:
    try:
        return str(account_id) in _read()
    except Exception:
        log.exception("tester list read failed; treating as not a tester")
        return False


def list_testers() -> list[dict]:
    rows = [{"account_id": k, **(v if isinstance(v, dict) else {})}
            for k, v in _read(fresh=True).items()]
    rows.sort(key=lambda r: r.get("added_at") or "")
    return rows


def add(account_id, *, note: str = "", by: str = "") -> dict:
    account_id = str(account_id).strip()
    if not account_id:
        raise TesterError("No such account.")
    accts = dict(_read(fresh=True))
    if account_id not in accts and len(accts) >= MAX_TESTERS:
        raise TesterError(f"The tester list is full ({MAX_TESTERS}).")
    clean = "".join(ch for ch in str(note or "") if ch.isprintable()).strip()[:NOTE_MAX]
    accts[account_id] = {"added_at": accts.get(account_id, {}).get("added_at") or _now_iso(),
                         "note": clean, "added_by": str(by)}
    _doc().set({"accounts": accts, "updated_at": _now_iso()})
    invalidate()
    log.warning("tester added: %s by %s", account_id, by)
    return {"account_id": account_id, **accts[account_id]}


def remove(account_id, *, by: str = "") -> bool:
    account_id = str(account_id).strip()
    accts = dict(_read(fresh=True))
    if account_id not in accts:
        return False
    accts.pop(account_id)
    _doc().set({"accounts": accts, "updated_at": _now_iso()})
    invalidate()
    log.warning("tester removed: %s by %s", account_id, by)
    return True
