"""
data/mp_host_commands.py – remote admin of a player's own game server.

`ksp-mp-remote-host-admin.md` (Multiplayer Implementation Plans). The owner
issues a command on the website; this module checks who may issue it, signs it
with the host-command key (`mp_keys.mint_host_command`) and queues it; the
server's agent (`bm-hostd`, Multiplayer-Server repo) long-polls for it with the
server's service credential, verifies the signature offline, runs it from a
closed allowlist, and posts the result back.

Nothing on the host listens for admin traffic, and the host holds only the
host-command PUBLIC key: a stolen service credential can read queued commands
and fake results, never issue a command.

Document shape (the audit trail):

    mp_host_commands/{cid}
    {
        "cid": "hc_…", "server_id": "srv_…", "actor": "<account id>",
        "cmd": "service.restart", "args_summary": {...}, "args_digest": "<sha256>",
        "seq": 12, "step_up": false,
        "issued_at": "<iso>", "expires_wc": 1800000090,
        "state": "pending" | "delivered" | "done" | "failed" | "expired",
        "error": "", "problems": [], "finished_at": "<iso>" | null
    }

**Full host.toml bodies and modsets are never stored.** ``args_summary`` keeps
sizes and hashes, and a result carrying the file itself (``settings.get``) is
held in memory for the owner to collect, not written.

## Cost (`docs/design/bot/cost-guard.md`)

Polling must not become Firestore reads. Pending commands live in an
in-process index keyed by server id, the long-poll waits on an
``asyncio.Event``, and the agent's reported state is kept in memory only (it is
refreshed every poll, ≤ 25 s, so a bot restart forgets it for one poll at most).
Firestore is written once per state change. A command is short-lived (90 s), so
the index is **not** rebuilt after a restart: the signed token is not stored,
and a command queued in the seconds before a restart reads as expired, which is
the honest answer.

Under FROZEN the guarded client refuses writes, so issuing fails (503) while
delivery of what is already queued continues — the delivery bookkeeping write is
best-effort.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import urllib.request
import threading
import time
from collections import deque
from datetime import datetime, timezone

from data import mp_keys
from data import mp_servers
from data.store import _db

log = logging.getLogger(__name__)

ACTIONS = mp_keys.HOSTCMD_ACTIONS
STEP_UP_ACTIONS = mp_keys.HOSTCMD_STEP_UP_ACTIONS
#: Actions that interrupt players. Rate-limited on their own, so a runaway UI
#: or a stolen session cannot turn a server into a restart loop.
DISRUPTIVE_ACTIONS = frozenset({"service.restart", "service.stop"})
#: Change nothing on the host. Counted against their own, larger budget, so a
#: page browsing graphs cannot use up the allowance that guards edits/restarts.
READ_ACTIONS = frozenset({"status", "settings.get", "metrics.get"})

#: Not written to Firestore at all: no audit row, no history entry. A usage
#: chart refreshing every 30 s would otherwise cost ~4 writes per refresh for a
#: read with nothing to audit. Kept in memory for the browser to collect.
EPHEMERAL_ACTIONS = frozenset({"metrics.get"})
#: The per-server seq is reserved in blocks and written once per block (the
#: agent only needs it to increase, so a restart skipping ahead is harmless).
SEQ_BLOCK = 1000

RATE_ALL = (30, 600.0)          # 30 changing commands / server / 10 min
RATE_READS = (240, 600.0)       # 240 reads / server / 10 min (a graph refresh is one)
RATE_DISRUPTIVE = (5, 3600.0)   # 5 restarts+stops / server / hour
#: An agent that has not polled for this long is reported offline.
AGENT_STALE_SECONDS = 90.0
#: How long a full result (e.g. the host.toml text) waits in memory for the
#: owner's browser to collect it.
RESULT_TTL_SECONDS = 600.0
MAX_ARG_BYTES = 300 * 1024     # a modset is at most 256 KiB on the host
WARN_MAX_SECONDS = 600
DEFAULT_WARN_SECONDS = 60
HISTORY_LIMIT = 50


class HostCmdError(RuntimeError):
    """Refused, with a message the owner can act on."""


class HostCmdNotFound(HostCmdError):
    """No such server for this caller. Answered as 404, exactly like a server
    that does not exist, so ids cannot be probed for ownership."""


def _col():
    return _db.collection("mp_host_commands")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── in-process state ─────────────────────────────────────────────────────────

_lock = threading.Lock()
_pending: dict[str, list[dict]] = {}        # server_id -> [{cid, token, expires_wc}]
_agents: dict[str, dict] = {}               # server_id -> {"state": {...}, "seen_wc": t}
_results: dict[str, dict] = {}              # cid -> {"server_id", "result", "at"}
_issued: dict[str, deque] = {}              # server_id -> deque[(t, cmd)]
_events: dict[str, asyncio.Event] = {}
_seq: dict[str, list[int]] = {}             # server_id -> [next to hand out, reserved up to]
_ephemeral: dict[str, dict] = {}            # cid -> in-memory row for EPHEMERAL_ACTIONS
_loop: asyncio.AbstractEventLoop | None = None


def _reset_for_tests() -> None:
    global _loop
    with _lock:
        for d in (_pending, _agents, _results, _issued, _events, _live, _seq, _ephemeral,
                  _listing_cache):
            d.clear()
    _loop = None


def _event(server_id: str) -> asyncio.Event:
    ev = _events.get(server_id)
    if ev is None:
        ev = _events[server_id] = asyncio.Event()
    return ev


def _wake(server_id: str) -> None:
    """Set the waiter's event on the loop that owns it; safe from a thread."""
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(lambda: _event(server_id).set())
    except RuntimeError:
        pass


# ── the agent's side ─────────────────────────────────────────────────────────

_AGENT_KEYS = ("remote_admin", "allow", "reason", "version", "active_state",
               "players", "max_players",
               # live figures for the website's server panel (2026-11-13)
               "vessels_active", "vessels_total", "bubbles_live", "server_type",
               "universe_id", "started_wc", "store_bytes", "cpu_pct", "mem_bytes",
               "host_mem_used", "host_mem_total", "host_load1", "cpu_count", "sample_t")


def note_agent(server_id: str, state: dict | None) -> None:
    """What the agent reported on this poll. Memory only (see the cost note)."""
    clean = {}
    for k in _AGENT_KEYS:
        v = (state or {}).get(k)
        if k == "allow":
            v = [a for a in (v or []) if a in ACTIONS] if isinstance(v, list) else []
        elif isinstance(v, str):
            v = v[:300]
        elif not isinstance(v, (int, float, bool, type(None))):
            v = None
        clean[k] = v
    if clean.get("remote_admin") not in ("enabled", "disabled", "absent"):
        clean["remote_admin"] = "disabled"
    with _lock:
        _agents[str(server_id)] = {"state": clean, "seen_wc": time.time()}


def agent_state(server_id: str) -> dict:
    with _lock:
        rec = _agents.get(str(server_id))
    if not rec:
        return {"online": False, "remote_admin": "unknown", "allow": []}
    age = time.time() - rec["seen_wc"]
    return {**rec["state"], "online": age < AGENT_STALE_SECONDS,
            "seen_seconds_ago": int(age)}


def _take(server_id: str) -> list[dict]:
    now = time.time()
    with _lock:
        items = _pending.pop(server_id, [])
    return [i for i in items if i["expires_wc"] > now]


async def wait_for_commands(server_id: str, timeout: float = 25.0) -> list[str]:
    """The long-poll: queued tokens now, or the first to arrive within ``timeout``."""
    global _loop
    _loop = asyncio.get_running_loop()
    server_id = str(server_id)
    items = _take(server_id)
    if not items:
        ev = _event(server_id)
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        items = _take(server_id)
    for i in items:
        if i.get("ephemeral"):
            with _lock:
                if i["cid"] in _ephemeral:
                    _ephemeral[i["cid"]]["state"] = "delivered"
            continue
        try:
            await asyncio.to_thread(
                lambda cid=i["cid"]: _col().document(cid).update({"state": "delivered"}))
        except Exception:
            # Best-effort bookkeeping: under FROZEN, or a blip, the command is
            # still delivered — the audit row just stays "pending" until the
            # result lands.
            log.warning("could not mark host command %s delivered", i["cid"])
    return [i["token"] for i in items]


def complete(server_id: str, cid: str, outcome: dict) -> bool:
    """Record the agent's result. Only for a cid issued TO this server."""
    server_id, cid = str(server_id), str(cid)
    with _lock:
        eph = _ephemeral.get(cid)
    if eph is not None:
        if eph["server_id"] != server_id or eph["state"] in ("done", "failed"):
            return False
        ok = outcome.get("ok") is True
        result = outcome.get("result") if isinstance(outcome.get("result"), dict) else None
        if result is not None and len(json.dumps(result, default=str)) > 512 * 1024:
            result = {"note": "result too large to relay"}
        with _lock:
            eph.update(state="done" if ok else "failed",
                       error=str(outcome.get("error") or "")[:500],
                       problems=[str(p)[:500] for p in (outcome.get("problems") or [])][:20],
                       finished_at=_now_iso())
            _results[cid] = {"server_id": server_id, "result": result, "at": time.time()}
        return True
    doc = _col().document(cid)
    snap = doc.get()
    rec = snap.to_dict() if snap.exists else None
    if not rec or rec.get("server_id") != server_id:
        return False
    if rec.get("state") in ("done", "failed"):
        return False        # first result wins; a replayed POST changes nothing
    ok = outcome.get("ok") is True
    problems = [str(p)[:500] for p in (outcome.get("problems") or [])
                if isinstance(p, (str, int, float))][:20]
    result = outcome.get("result") if isinstance(outcome.get("result"), dict) else None
    if result is not None and len(json.dumps(result, default=str)) > 512 * 1024:
        result = {"note": "result too large to relay"}
    with _lock:
        _results[cid] = {"server_id": server_id, "result": result, "at": time.time(),
                         "ok": ok, "error": str(outcome.get("error") or "")[:500],
                         "problems": problems}
        for k in [k for k, v in _results.items()
                  if time.time() - v["at"] > RESULT_TTL_SECONDS]:
            _results.pop(k, None)
    doc.update({
        "state": "done" if ok else "failed",
        "error": str(outcome.get("error") or "")[:500],
        "problems": problems,
        "result_summary": _summarise(result),
        "finished_at": _now_iso(),
    })
    return True


_LIVE_TTL = 60.0
_live: dict[str, tuple[str, float]] = {}    # server_id -> (live jti, read at)


def credential_is_live(server_id: str, jti: str) -> bool:
    """Is ``jti`` the server's live, unrevoked credential?

    Asked on every poll, so memoised for a minute: without it each agent would
    cost a Firestore read every 25 s forever. The price is that a revoke or
    re-issue cuts an agent off within a minute rather than instantly — and a
    cut-off agent can still only read commands, never issue one.
    """
    server_id = str(server_id)
    now = time.time()
    with _lock:
        hit = _live.get(server_id)
    if hit is None or now - hit[1] > _LIVE_TTL:
        rec = mp_servers.get(server_id) or {}
        live = "" if rec.get("revoked") else str(rec.get("credential_jti") or "")
        with _lock:
            _live[server_id] = (live, now)
        hit = (live, now)
    return bool(hit[0]) and hit[0] == str(jti)


def forget_credential(server_id: str) -> None:
    with _lock:
        _live.pop(str(server_id), None)


# ── the owner's side ─────────────────────────────────────────────────────────

def _owned(server_id: str, actor: str) -> dict:
    rec = mp_servers.get(server_id)
    if not rec or str(rec.get("operator_account")) != str(actor):
        raise HostCmdNotFound("No such server.")
    return rec


def _summarise(args: dict | None) -> dict:
    """What the audit row keeps: scalars as-is, bodies as size + hash."""
    out: dict = {}
    for k, v in (args or {}).items():
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, str) and len(v) <= 64 and k not in ("toml", "message"):
            out[k] = v
        else:
            body = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
            out[k] = {"bytes": len(body.encode("utf-8")),
                      "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]}
    return out


def _clean_args(cmd: str, args: dict) -> dict:
    """Shape-check what the website sent. The agent re-validates everything;
    this only keeps nonsense out of a signed token and the audit trail."""
    args = dict(args or {})
    if len(json.dumps(args, default=str)) > MAX_ARG_BYTES:
        raise HostCmdError("That is too large to send to a server.")
    if cmd == "settings.set":
        toml, base = args.get("toml"), args.get("base_hash")
        if not isinstance(toml, str) or not isinstance(base, str) or len(base) != 64:
            raise HostCmdError("Load the current settings before saving them.")
        return {"toml": toml, "base_hash": base}
    if cmd == "modset.set":
        modset = args.get("modset")
        if not isinstance(modset, (dict, str)):
            raise HostCmdError("Upload the modset.json exported from your install.")
        return {"modset": modset}
    if cmd == "listing.set":
        base, listing = args.get("base_hash"), args.get("listing")
        if not isinstance(base, str) or len(base) != 64 or not isinstance(listing, dict):
            raise HostCmdError("Load the current settings before saving them.")
        clean: dict = {}
        for k in ("name", "summary", "description", "website", "discord", "thumbnail"):
            v = listing.get(k)
            if v is None:
                continue
            if not isinstance(v, str) or len(v) > 4000:
                raise HostCmdError(f"{k} must be text.")
            clean[k] = v
        tags = listing.get("tags")
        if tags is not None:
            if not isinstance(tags, list) or len(tags) > 12 or \
                    not all(isinstance(t, str) and len(t) <= 32 for t in tags):
                raise HostCmdError("Tags must be a short list of names.")
            clean["tags"] = tags
        return {"base_hash": base, "listing": clean}
    if cmd == "metrics.get":
        out = {}
        for k in ("from", "to", "max_points"):
            v = args.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise HostCmdError("The time range must be numbers.")
            out[k] = v
        return out
    if cmd == "metrics.set":
        out = {}
        for k, kind in (("enabled", bool), ("clear", bool), ("retention_days", int)):
            v = args.get(k)
            if v is None:
                continue
            if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
                raise HostCmdError(f"{k} has the wrong type.")
            out[k] = v
        if not out:
            raise HostCmdError("Nothing to change.")
        return out
    if cmd in ("service.restart", "service.stop"):
        warn = args.get("warn_seconds", DEFAULT_WARN_SECONDS)
        if isinstance(warn, bool) or not isinstance(warn, int) \
                or not 0 <= warn <= WARN_MAX_SECONDS:
            raise HostCmdError(f"The warning must be 0–{WARN_MAX_SECONDS} seconds.")
        msg = "".join(ch for ch in str(args.get("message") or "") if ch.isprintable())
        return {"warn_seconds": warn, "message": msg.strip()[:200]}
    return {}


def _rate_check(server_id: str, cmd: str) -> None:
    now = time.time()
    with _lock:
        q = _issued.setdefault(server_id, deque())
        while q and now - q[0][0] > max(RATE_ALL[1], RATE_DISRUPTIVE[1]):
            q.popleft()
        reads = cmd in READ_ACTIONS
        limit, window = RATE_READS if reads else RATE_ALL
        recent = [c for t, c in q if now - t <= window and (c in READ_ACTIONS) == reads]
        if len(recent) >= limit:
            raise HostCmdError("Too many commands to this server. Wait a few minutes.")
        if cmd in DISRUPTIVE_ACTIONS:
            n = sum(1 for t, c in q if c in DISRUPTIVE_ACTIONS and now - t <= RATE_DISRUPTIVE[1])
            if n >= RATE_DISRUPTIVE[0]:
                raise HostCmdError("This server was restarted or stopped "
                                   f"{RATE_DISRUPTIVE[0]} times in the last hour. "
                                   "Wait before doing it again.")
        q.append((now, cmd))


def issue(server_id: str, actor: str, cmd: str, args: dict | None = None,
          *, step_up: bool = False) -> dict:
    """Check, sign, store and queue. Returns the audit row. Sync (Firestore);
    call :func:`_wake` after, from the loop, so a waiting poll returns at once."""
    server_id, actor = str(server_id), str(actor)
    rec = _owned(server_id, actor)
    if rec.get("revoked"):
        raise HostCmdError("This server's credential is revoked. Re-register it to manage it.")
    if cmd not in ACTIONS:
        raise HostCmdError("Unknown action.")
    agent = agent_state(server_id)
    if not agent.get("online"):
        raise HostCmdError("The server's admin agent is not connected. Is bm-hostd "
                           "installed and running on the host?")
    if agent.get("remote_admin") != "enabled":
        raise HostCmdError("Remote admin is off on this server. Turn it on in "
                           "/etc/boundless-mp/hostd.toml on the host.")
    if cmd not in (agent.get("allow") or []):
        raise HostCmdError("This server does not allow that action remotely.")
    if cmd in STEP_UP_ACTIONS and not step_up:
        raise HostCmdError("Confirm with your two-factor code to do this.")
    clean = _clean_args(cmd, args or {})
    _rate_check(server_id, cmd)

    with _lock:
        # The seq lives on the server row so it survives a bot restart; the
        # agent refuses anything not above the last one it ran. Reserved in
        # blocks so a command does not cost a write; after a restart the next
        # block starts above everything the old process could have issued.
        # One bot process, so the lock is the serialisation.
        cur = _seq.get(server_id)
        if cur is None or cur[0] > cur[1]:
            base = int(rec.get("hostcmd_seq") or 0) if cur is None else cur[1]
            reserved = base + SEQ_BLOCK
            mp_servers._col().document(server_id).update({"hostcmd_seq": reserved})
            rec["hostcmd_seq"] = reserved
            cur = _seq[server_id] = [base + 1, reserved]
        seq = cur[0]
        cur[0] += 1
    cid = "hc_" + secrets.token_hex(10)
    token, expires = mp_keys.mint_host_command(
        server_id=server_id, actor_account=actor, cid=cid, cmd=cmd, args=clean,
        seq=seq, step_up=step_up)
    row = {
        "cid": cid, "server_id": server_id, "actor": actor, "cmd": cmd,
        "args_summary": _summarise(clean),
        "args_digest": hashlib.sha256(
            json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
        "seq": seq, "step_up": bool(step_up), "issued_at": _now_iso(),
        "expires_wc": expires, "state": "pending", "error": "", "problems": [],
        "finished_at": None,
    }
    ephemeral = cmd in EPHEMERAL_ACTIONS
    if ephemeral:
        with _lock:
            _ephemeral[cid] = dict(row)
            for k in [k for k, v in _ephemeral.items()
                      if time.time() - float(v.get("expires_wc") or 0) > RESULT_TTL_SECONDS]:
                _ephemeral.pop(k, None)
    else:
        _col().document(cid).set(row)
    with _lock:
        _pending.setdefault(server_id, []).append(
            {"cid": cid, "token": token, "expires_wc": expires, "ephemeral": ephemeral})
    log.info("host command %s %s -> %s by %s (seq %d)", cid, cmd, server_id, actor, seq)
    return row


def _present(row: dict) -> dict:
    out = {k: row.get(k) for k in ("cid", "cmd", "actor", "args_summary", "issued_at",
                                   "state", "error", "problems", "finished_at",
                                   "result_summary")}
    if out["state"] in ("pending", "delivered") and \
            float(row.get("expires_wc") or 0) + 60 < time.time():
        out["state"] = "expired"
    return out


def get_result(server_id: str, actor: str, cid: str) -> dict:
    """One command, with the full result while it is still in memory."""
    _owned(server_id, actor)
    with _lock:
        eph = _ephemeral.get(str(cid))
    if eph is not None:
        row = dict(eph)
    else:
        snap = _col().document(str(cid)).get()
        row = snap.to_dict() if snap.exists else None
    if not row or row.get("server_id") != str(server_id):
        raise HostCmdNotFound("No such command.")
    out = _present(row)
    with _lock:
        mem = _results.get(str(cid))
    if mem and mem["server_id"] == str(server_id):
        out["result"] = mem["result"]
    return out


def history(server_id: str, actor: str, limit: int = 20) -> list[dict]:
    _owned(server_id, actor)
    rows = [s.to_dict() or {} for s in
            _col().where("server_id", "==", str(server_id)).stream()]
    rows.sort(key=lambda r: r.get("issued_at") or "", reverse=True)
    return [_present(r) for r in rows[:max(1, min(limit, HISTORY_LIMIT))]]


#: The master (server listing). On the main VPS it runs beside the bot, so the
#: default is loopback; MP_MASTER_URL points elsewhere (the public https URL works
#: too). A failed read just leaves `listing` out.
MASTER_URL = os.getenv("MP_MASTER_URL", "http://127.0.0.1:6700").rstrip("/")
_LISTING_TTL = 20.0
_listing_cache: dict[str, tuple[float, dict | None]] = {}
_LISTING_KEYS = ("address", "port", "reachable", "players", "max_players", "tags",
                 "description", "thumbnail", "blanked", "verified", "last_seen_wc",
                 "mod_count")


def master_listing(server_id: str) -> dict | None:
    """This server's public listing on the master, or None (not listed / no master)."""
    now = time.time()
    with _lock:
        hit = _listing_cache.get(server_id)
    if hit and now - hit[0] < _LISTING_TTL:
        return hit[1]
    data = None
    try:
        with urllib.request.urlopen(f"{MASTER_URL}/v1/servers/{server_id}", timeout=2) as r:
            raw = json.loads(r.read().decode("utf-8"))
        if isinstance(raw, dict):
            data = {k: raw.get(k) for k in _LISTING_KEYS}
            thumb = data.get("thumbnail")
            if isinstance(thumb, str) and thumb.startswith("/"):
                data["thumbnail"] = PUBLIC_MASTER_URL + thumb
    except Exception:
        data = None
    with _lock:
        _listing_cache[server_id] = (now, data)
    return data


PUBLIC_MASTER_URL = os.getenv("MP_PUBLIC_MASTER_URL", "https://master.boundlessmissions.com")


def servers_for(actor: str) -> list[dict]:
    """The owner's servers, each with the agent's live state and its listing."""
    out = []
    for s in mp_servers.list_for_account(actor):
        s = dict(s)
        s.pop("superseded_jtis", None)
        s.pop("transition_jwk", None)
        s["agent"] = agent_state(s.get("server_id", ""))
        s["listing"] = None if s.get("revoked") else master_listing(s.get("server_id", ""))
        out.append(s)
    return out
