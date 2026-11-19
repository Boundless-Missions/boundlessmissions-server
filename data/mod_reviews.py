"""
data/mod_reviews.py – the admin console's window onto the master's reviews of
third-party mod assets (server modpacks).

**What the master holds.** `Multiplayer Server/master/reviews.py`: one SQLite row
per asset sha256 — `verified`, `flagged` or `unreviewed` (the default for a hash
with no row) plus a note. A flagged asset is refused by every player's launcher
and turns the listing red; an unreviewed one needs the player's acknowledgement;
a server's `verified` badge is computed from these rows on every listing read.
The ruling is about exactly those bytes: a new release is a new hash and starts
unreviewed. Server packs (bm-launcher.md §34) put many modset entries behind one
zip, so one review often covers a whole pack.

**Why this module exists.** The master's admin routes take one shared operator
secret (`MASTER_ADMIN_TOKEN`) and have no idea who is holding it. So this is two
things at once:

1. a proxy that holds that secret **server-side only** — it is read from the
   environment here, attached to requests to the master here, and never appears
   in a response, a log line or an exception message;
2. the **audit log** (Firestore `mod_review_audit`), which is the only record of
   which person made which ruling. A row is written *before* the master is
   changed (`state: "pending"`), then marked `applied` or `failed`. If the row
   cannot be written the change is refused, so there is no review on the master
   that the log does not know was attempted; a row left `pending` means "tried,
   outcome not confirmed", never "nothing happened".

**The queue needs no master change.** Every listing already carries
`modset_hash`, and `GET /v1/modsets/{hash}` serves the canonical modset — with
each `github` / `link` entry's `url` and `sha256` — anonymously. Both are public
routes; only the two writes use the token.

Configuration, read once at import (rotating the token is an env change and a
restart, the same as on the master):

    MP_MASTER_URL               the master; the same variable and default as
                                data/mp_host_commands (loopback: on the main VPS
                                the master runs beside the bot)
    MP_MASTER_ADMIN_TOKEN       the master's MASTER_ADMIN_TOKEN, verbatim
    MP_MASTER_ADMIN_TOKEN_FILE  ...or a file holding it (0600; not both)

Without a token the queue still reads (it is public data) and the writes report
"not configured". The token is only ever sent over https or to a loopback
address — a plain-http MP_MASTER_URL naming another host would put it on the
wire in clear, so that combination is refused as a configuration problem.

Every function here is synchronous (urllib + the firebase-admin SDK); the API
calls them through `asyncio.to_thread`.
"""

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from data.store import _db

log = logging.getLogger(__name__)

#: The master's vocabulary (master/reviews.py STATUSES / NOTE_MAX). Kept in step
#: by hand, the workspace convention for a contract across a repo boundary.
STATUSES = ("verified", "flagged", "unreviewed")
NOTE_MAX = 500
_HASH_RE = re.compile(r"[0-9a-f]{64}")      # used with fullmatch: `$` would admit a trailing \n
_WS_RUN = re.compile(r"\s+")

#: The master answers at most 500 hashes per lookup (REVIEW_LOOKUP_MAX).
LOOKUP_CHUNK = 500
#: Distinct modsets one queue read will fetch. They are immutable and cached, so
#: this only bounds a cold start against a very large listing.
MAX_MODSETS = 300
#: Audit rows the console is shown.
AUDIT_LIMIT = 100

_TIMEOUT = 5.0
_LISTING_MAX_BYTES = 8 * 1024 * 1024
_MODSET_MAX_BYTES = 512 * 1024        # the master caps an upload at 256 KB
_JSON_MAX_BYTES = 1024 * 1024
_MODSET_CACHE_MAX = 512

MASTER_URL = os.getenv("MP_MASTER_URL", "http://127.0.0.1:6700").strip().rstrip("/")


class ReviewError(RuntimeError):
    """A refusal the console should show. `status` is the HTTP status to answer.
    The message is always safe to show an admin: it never carries the token."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ── configuration ────────────────────────────────────────────────────────────

def _load_token(env) -> tuple[str, str]:
    """(token, problem). The same rules as the master's own `read_secret`: the
    value directly or a `_FILE` only its owner can read, never both. The problem
    text names variables, never a value."""
    direct = (env.get("MP_MASTER_ADMIN_TOKEN") or "").strip()
    path = (env.get("MP_MASTER_ADMIN_TOKEN_FILE") or "").strip()
    if direct and path:
        return "", "Set MP_MASTER_ADMIN_TOKEN or MP_MASTER_ADMIN_TOKEN_FILE on the bot, not both."
    if not path:
        return direct, ("" if direct else
                        "The bot has no master admin token (MP_MASTER_ADMIN_TOKEN is not set).")
    try:
        if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return "", "MP_MASTER_ADMIN_TOKEN_FILE is readable by other users; chmod 600 it."
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return "", "MP_MASTER_ADMIN_TOKEN_FILE cannot be read by the bot."
    return token, ("" if token else "MP_MASTER_ADMIN_TOKEN_FILE is empty.")


ADMIN_TOKEN, _TOKEN_PROBLEM = _load_token(os.environ)


def _token_transport_ok(url: str) -> bool:
    """https anywhere, or plain http to this machine only."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def read_problem() -> str:
    """Why the queue cannot be read at all, or ""."""
    if not MASTER_URL:
        return "MP_MASTER_URL is empty on the bot, so it has no master to ask."
    return ""


def write_problem() -> str:
    """Why the console cannot change a review, or "" when it can."""
    if read_problem():
        return read_problem()
    if not ADMIN_TOKEN:
        return _TOKEN_PROBLEM or "The bot has no master admin token (MP_MASTER_ADMIN_TOKEN is not set)."
    if not _token_transport_ok(MASTER_URL):
        return ("MP_MASTER_URL is plain http to another machine; the admin token is only "
                "sent over https or to a loopback address.")
    return ""


def config_state() -> dict:
    """What an authorised viewer is told about the setup. Booleans and sentences;
    the URL is left out on purpose (it can name internal hosts)."""
    return {"read_configured": not read_problem(), "write_configured": not write_problem(),
            "problem": write_problem()}


# ── HTTP to the master ───────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib forwards `Authorization` on a redirect, to whatever host it names.
    The master never redirects, so a 3xx is an error rather than a hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


# No ProxyHandler({}) would mean the environment's HTTP(S)_PROXY is honoured — and
# the token handed to a proxy. The master is reached directly or not at all.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _call(method: str, path: str, *, body=None, admin: bool = False,
          limit: int = _JSON_MAX_BYTES) -> tuple[int, bytes]:
    problem = write_problem() if admin else read_problem()
    if problem:
        raise ReviewError(problem, 503)
    headers = {"Accept": "application/json", "User-Agent": "BoundlessMissions-bot/mod-reviews"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if admin:
        headers["Authorization"] = "Bearer " + ADMIN_TOKEN
    req = urllib.request.Request(MASTER_URL + path, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=_TIMEOUT) as r:
            status, raw = r.status, r.read(limit + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(limit + 1) or b""
        except Exception:  # noqa: BLE001
            raw = b""
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The class name only: a reason string is harmless today, but nothing
        # about this request belongs in a log line beyond its path.
        log.warning("master %s %s unreachable (%s)", method, path, type(exc).__name__)
        raise ReviewError("The master server could not be reached.", 503)
    if len(raw) > limit:
        raise ReviewError("The master's answer was larger than expected.", 502)
    return status, raw


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _master_detail(raw: bytes) -> str:
    data = _json(raw)
    detail = data.get("detail") if isinstance(data, dict) else None
    return clean_note(detail, 200) if isinstance(detail, str) else ""


# ── validation ───────────────────────────────────────────────────────────────

def valid_sha(value) -> bool:
    return isinstance(value, str) and bool(_HASH_RE.fullmatch(value))


def clean_note(value, limit: int = NOTE_MAX) -> str:
    """One line of text, as the master's `clean_line` stores it: whitespace
    collapsed first (so a newline becomes a space), then controls stripped."""
    if not isinstance(value, str):
        return ""
    text = _WS_RUN.sub(" ", value)
    return "".join(ch for ch in text if ch.isprintable()).strip()[:limit].rstrip()


def _https(url) -> str:
    """The master validated these (github_url_ok / link_url_ok); this is the
    belt to that brace, because the console renders them as links."""
    return url if isinstance(url, str) and url.startswith("https://") and len(url) <= 2048 else ""


# ── the queue ────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_modset_cache: dict[str, dict] = {}


def _modset(digest: str) -> dict | None:
    """A canonical modset by hash, or None. Immutable, so cached for the life of
    the process — after checking the bytes are what they were named by."""
    with _cache_lock:
        hit = _modset_cache.get(digest)
    if hit is not None:
        return hit
    status, raw = _call("GET", f"/v1/modsets/{digest}", limit=_MODSET_MAX_BYTES)
    if status != 200:
        return None
    if hashlib.sha256(raw).hexdigest() != digest:
        log.warning("master served modset %s with different bytes; ignored", digest[:12])
        return None
    data = _json(raw)
    if not isinstance(data, dict):
        return None
    with _cache_lock:
        while len(_modset_cache) >= _MODSET_CACHE_MAX:
            _modset_cache.pop(next(iter(_modset_cache)))
        _modset_cache[digest] = data
    return data


def lookup(shas) -> dict[str, tuple[str, str]]:
    """sha -> (status, note) from the master, unreviewed when it holds no row."""
    wanted = list(dict.fromkeys(s for s in shas if valid_sha(s)))
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(wanted), LOOKUP_CHUNK):
        chunk = wanted[i:i + LOOKUP_CHUNK]
        status, raw = _call("POST", "/v1/reviews/lookup", body={"sha256": chunk})
        data = _json(raw) if status == 200 else None
        reviews = data.get("reviews") if isinstance(data, dict) else None
        if not isinstance(reviews, dict):
            raise ReviewError(f"The master answered {status} to a review lookup.", 502)
        for sha in chunk:
            row = reviews.get(sha) if isinstance(reviews.get(sha), dict) else {}
            st = row.get("status") if row.get("status") in STATUSES else "unreviewed"
            out[sha] = (st, clean_note(row.get("note", "")))
    return out


_ORDER = {"unreviewed": 0, "flagged": 1, "verified": 2}


def queue() -> dict:
    """Every third-party asset on the master's live listings, one row per sha256.

    Unreviewed first (the work), then flagged (still being listed, which is worth
    seeing), then verified; within a status, the most widely used first.
    """
    status, raw = _call("GET", "/v1/servers", limit=_LISTING_MAX_BYTES)
    data = _json(raw) if status == 200 else None
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        raise ReviewError(f"The master answered {status} for its server list.", 502)
    servers = [s for s in servers if isinstance(s, dict)]

    wanted = list(dict.fromkeys(
        str(s.get("modset_hash") or "").lower() for s in servers
        if valid_sha(str(s.get("modset_hash") or "").lower())))
    truncated = len(wanted) > MAX_MODSETS
    modsets: dict[str, dict | None] = {}
    for digest in wanted[:MAX_MODSETS]:
        try:
            modsets[digest] = _modset(digest)
        except ReviewError as exc:
            if exc.status == 503:
                # Unreachable mid-read: say so now, rather than spend a full
                # timeout on every modset still to fetch.
                raise
            modsets[digest] = None

    items: dict[str, dict] = {}
    for srv in servers:
        ms = modsets.get(str(srv.get("modset_hash") or "").lower())
        if not ms:
            continue
        sid = str(srv.get("server_id") or "")
        for mod in ms.get("mods") or []:
            if not isinstance(mod, dict) or mod.get("source") not in ("github", "link"):
                continue
            sha = str(mod.get("sha256") or "").lower()
            if not valid_sha(sha):
                continue
            item = items.setdefault(sha, {"sha256": sha, "sources": [], "urls": [], "servers": []})
            if mod["source"] not in item["sources"]:
                item["sources"].append(mod["source"])
            url = _https(mod.get("url"))
            if url and url not in item["urls"]:
                item["urls"].append(url)
            entry = next((e for e in item["servers"] if e["server_id"] == sid), None)
            if entry is None:
                entry = {"server_id": sid, "name": clean_note(srv.get("name") or "", 120),
                         "blanked": bool(srv.get("blanked")), "mods": []}
                item["servers"].append(entry)
            entry["mods"].append({
                "id": clean_note(mod.get("id") or "", 120),
                "version": clean_note(mod.get("version") or "", 64),
                "class": clean_note(mod.get("class") or "", 32),
                "optional": bool(mod.get("optional")),
            })

    reviews = lookup(list(items)) if items else {}
    rows = []
    for sha, item in items.items():
        st, note = reviews.get(sha, ("unreviewed", ""))
        names = sorted({m["id"] for e in item["servers"] for m in e["mods"]}, key=str.lower)
        item.update(status=st, note=note, server_count=len(item["servers"]),
                    mod_count=len(names), label=names[0] if names else "")
        rows.append(item)
    rows.sort(key=lambda i: (_ORDER.get(i["status"], 0), -i["server_count"],
                             i["label"].lower(), i["sha256"]))
    return {
        "items": rows,
        "counts": {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES},
        "servers_listed": len(servers),
        "servers_with_modset": len([s for s in servers if s.get("modset_hash")]),
        "modsets_unavailable": sum(1 for v in modsets.values() if v is None),
        "truncated": truncated,
    }


# ── the audit log ────────────────────────────────────────────────────────────
#
#   mod_review_audit/{at_ms-random}
#   { sha256, action: "set"|"reset", status, note, previous_status, previous_note,
#     by_discord_id, by_account_id, by_name, tier: "owner"|"guild-admin", guild_ids,
#     at, at_ms, state: "pending"|"applied"|"failed", detail, finished_at, removed }
#
# Ordered by `at_ms` (a single-field index Firestore keeps automatically), so the
# console's read needs no composite index.

def _col():
    return _db.collection("mod_review_audit")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _begin(sha: str, action: str, status: str, note: str, actor: dict,
           previous: tuple[str, str]) -> str:
    now_ms = int(time.time() * 1000)
    doc_id = f"{now_ms:013d}-{secrets.token_hex(4)}"
    _col().document(doc_id).set({
        "sha256": sha, "action": action, "status": status, "note": note,
        "previous_status": previous[0], "previous_note": previous[1],
        "by_discord_id": str(actor.get("discord_id") or ""),
        "by_account_id": str(actor.get("account_id") or ""),
        "by_name": str(actor.get("name") or "")[:64],
        "tier": "owner" if actor.get("is_owner") else "guild-admin",
        "guild_ids": [str(g) for g in (actor.get("guild_ids") or [])][:50],
        "at": _now_iso(), "at_ms": now_ms, "state": "pending",
    })
    return doc_id


def _finish(doc_id: str, state: str, **fields) -> bool:
    try:
        _col().document(doc_id).update({"state": state, "finished_at": _now_iso(), **fields})
        return True
    except Exception:  # noqa: BLE001
        log.exception("mod review audit %s could not be marked %s", doc_id, state)
        return False


def recent(limit: int = AUDIT_LIMIT) -> list[dict]:
    limit = max(1, min(int(limit or AUDIT_LIMIT), 500))
    q = _col().order_by("at_ms", direction="DESCENDING").limit(limit)
    return [{"id": d.id, **(d.to_dict() or {})} for d in q.stream()]


def _refusal(status: int, raw: bytes) -> ReviewError:
    if status == 404:
        # The master's admin dependency answers 404 to everything it refuses,
        # including "admin routes not configured" — it cannot be told apart here.
        return ReviewError("The master did not accept the bot's admin token, or its admin "
                           "routes are off (MASTER_ADMIN_ROUTES / MASTER_ADMIN_TOKEN).", 502)
    if status == 400:
        return ReviewError(_master_detail(raw) or "The master refused that review.", 400)
    return ReviewError(f"The master answered {status}.", 502)


def _apply(sha: str, action: str, status: str, note: str, actor: dict, send) -> dict:
    """The shared shape of a write: check, look, log, change, mark."""
    problem = write_problem()
    if problem:
        raise ReviewError(problem, 503)
    previous = lookup([sha]).get(sha, ("unreviewed", ""))
    try:
        doc_id = _begin(sha, action, status, note, actor, previous)
    except Exception:  # noqa: BLE001
        log.exception("mod review audit could not be written; %s of %s refused", action, sha[:12])
        raise ReviewError("The audit log could not be written, so nothing was changed. "
                          "Try again in a moment.", 503)
    try:
        code, raw = send()
    except ReviewError as exc:
        _finish(doc_id, "failed", detail=str(exc)[:200])
        raise
    data = _json(raw) if code == 200 else None
    if not isinstance(data, dict) or not data.get("ok"):
        err = _refusal(code, raw)
        _finish(doc_id, "failed", detail=str(err)[:200])
        raise err
    extra = {"removed": bool(data.get("removed"))} if action == "reset" else {}
    stored_note = clean_note(data.get("note", note)) if action == "set" else ""
    logged = _finish(doc_id, "applied", **({"note": stored_note} if action == "set" else extra))
    log.warning("mod review: %s %s -> %s by %s (%s)", action, sha[:12], status or "unreviewed",
                actor.get("name") or actor.get("account_id"), doc_id)
    return {"sha256": sha, "status": status or "unreviewed", "note": stored_note,
            "previous_status": previous[0], "audit_id": doc_id, "audit_complete": logged,
            **extra}


def set_review(sha: str, status: str, note: str, actor: dict) -> dict:
    """Record a ruling on the master. `actor`: account_id, discord_id, name,
    is_owner, guild_ids — who is answerable for it."""
    sha = (sha or "").strip().lower()
    if not valid_sha(sha):
        raise ReviewError("That is not a 64-character sha256.", 422)
    if status not in STATUSES:
        raise ReviewError(f"status must be one of {', '.join(STATUSES)}.", 422)
    note = clean_note(note)
    return _apply(sha, "set", status, note, actor, lambda: _call(
        "PUT", f"/v1/admin/reviews/{sha}", body={"status": status, "note": note}, admin=True))


def reset_review(sha: str, actor: dict) -> dict:
    """Forget a ruling: the asset reads as unreviewed (with no note) again."""
    sha = (sha or "").strip().lower()
    if not valid_sha(sha):
        raise ReviewError("That is not a 64-character sha256.", 422)
    return _apply(sha, "reset", "", "", actor, lambda: _call(
        "DELETE", f"/v1/admin/reviews/{sha}", admin=True))
