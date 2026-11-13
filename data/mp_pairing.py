"""
data/mp_pairing.py – pairing a freshly installed game server to its owner's account.

The installer (`deploy/install.sh`, Multiplayer-Server repo) runs on the VPS as
root, asks this module for a **pairing code**, prints it, and waits. The owner
types the code on boundlessmissions.com → Servers, sees which machine is asking
(its hostname, OS and the public address it reported) and confirms with a name.
That registers the server under the owner's account exactly as
`POST /api/v1/mp/servers` would, and hands the one-time service credential
**straight to the waiting installer** — it never passes through the owner's
clipboard or browser.

Why the code goes host → website and not the other way round: the secret that
matters (the credential) then travels only over the installer's own HTTPS poll,
authenticated by a pairing secret only the installer holds. The code itself is
worthless on its own: it can only attach the machine that is *asking* to the
account that *types it*, and the owner sees that machine before confirming.

* Codes: 8 characters of Crockford base32 (40 bits), shown as ``XXXX-XXXX``,
  15 minutes, single use. Lookups are rate limited per account, so guessing is
  not a strategy.
* The installer's poll is authenticated by a 256-bit pairing secret returned only
  to it, compared in constant time.
* State is **in memory**: a pairing lives 15 minutes, and a bot restart in that
  window just means the installer asks for a new code (it does so on 404).
  Nothing here touches Firestore until the owner confirms, and then only via
  `mp_servers.register`.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time

from data import mp_keys
from data import mp_servers

log = logging.getLogger(__name__)

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford: no I L O U
CODE_LENGTH = 8
PAIR_TTL_SECONDS = 15 * 60
MAX_PENDING = 200
HOST_FIELD_MAX = 120


class PairingError(RuntimeError):
    pass


class PairingNotFound(PairingError):
    """Unknown, expired or already used. One answer for all three, on purpose."""


_lock = threading.Lock()
_by_id: dict[str, dict] = {}
_by_code: dict[str, str] = {}
_events: dict[str, asyncio.Event] = {}
_loop: asyncio.AbstractEventLoop | None = None


def _reset_for_tests() -> None:
    global _loop
    with _lock:
        _by_id.clear()
        _by_code.clear()
        _events.clear()
    _loop = None


def normalize_code(code: str) -> str:
    """Upper-case, drop separators, and read the look-alikes the way Crockford does."""
    c = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return c.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))


def display_code(code: str) -> str:
    return code[:4] + "-" + code[4:]


def _clean(value, limit: int = HOST_FIELD_MAX) -> str:
    return "".join(ch for ch in str(value or "") if ch.isprintable()).strip()[:limit]


def _sweep(now: float) -> None:
    for pid in [p for p, r in _by_id.items() if r["expires_wc"] < now]:
        rec = _by_id.pop(pid)
        _by_code.pop(rec["code"], None)
        _events.pop(pid, None)


def start(*, host: dict, observed_ip: str, now: float | None = None) -> dict:
    """A new pairing for an installer. Returns what the installer needs, once."""
    now = time.time() if now is None else now
    with _lock:
        _sweep(now)
        if len(_by_id) >= MAX_PENDING:
            raise PairingError("Too many servers are pairing right now. Try again in a few minutes.")
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in _by_code:
                break
        pair_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        rec = {
            "pair_id": pair_id,
            "code": code,
            "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
            "created_wc": now,
            "expires_wc": now + PAIR_TTL_SECONDS,
            "host": {
                "hostname": _clean(host.get("hostname")),
                "os": _clean(host.get("os")),
                "public_ip": _clean(host.get("public_ip"), 64),
                "observed_ip": _clean(observed_ip, 64),
                "server_type": _clean(host.get("server_type"), 32),
                "name_hint": _clean(host.get("name_hint"), mp_servers.MAX_NAME_LENGTH),
                "installer": _clean(host.get("installer"), 32),
            },
            "state": "pending",
            "result": None,
        }
        _by_id[pair_id] = rec
        _by_code[code] = pair_id
    log.info("server pairing %s started from %s (%s)", display_code(code),
             rec["host"]["public_ip"] or observed_ip, rec["host"]["hostname"])
    return {"pair_id": pair_id, "secret": secret, "code": display_code(code),
            "expires_wc": int(rec["expires_wc"])}


def _pending_by_code(code: str, now: float) -> dict:
    c = normalize_code(code)
    with _lock:
        _sweep(now)
        pid = _by_code.get(c)
        rec = _by_id.get(pid) if pid else None
        if not rec or rec["state"] != "pending":
            raise PairingNotFound("That code is not valid. Check it, or start the installer again.")
        return rec


def lookup(code: str, *, now: float | None = None) -> dict:
    """What the owner sees before confirming: which machine is asking."""
    now = time.time() if now is None else now
    rec = _pending_by_code(code, now)
    return {"code": display_code(rec["code"]), "host": dict(rec["host"]),
            "expires_wc": int(rec["expires_wc"])}


def claim(code: str, actor: str, name: str, *, now: float | None = None) -> dict:
    """Register the asking machine under ``actor`` and hand its credential over."""
    now = time.time() if now is None else now
    rec = _pending_by_code(code, now)
    with _lock:
        if rec["state"] != "pending":
            raise PairingNotFound("That code has just been used.")
        rec["state"] = "claiming"           # nobody else may claim it meanwhile
    try:
        server = mp_servers.register(actor, name or rec["host"]["name_hint"] or "My server")
        credential, jti, expires = mp_keys.mint_service_credential(
            server_id=server["server_id"], operator_account=str(actor))
        mp_servers.note_issued(server["server_id"], jti)
    except Exception:
        with _lock:
            rec["state"] = "pending"
        raise
    with _lock:
        rec["state"] = "claimed"
        rec["result"] = {"server_id": server["server_id"], "name": server["name"],
                         "credential": credential, "expires_wc": expires}
    log.info("server pairing %s claimed by %s as %s", display_code(rec["code"]), actor,
             server["server_id"])
    _wake(rec["pair_id"])
    return {"server_id": server["server_id"], "name": server["name"]}


def _event(pair_id: str) -> asyncio.Event:
    ev = _events.get(pair_id)
    if ev is None:
        ev = _events[pair_id] = asyncio.Event()
    return ev


def _wake(pair_id: str) -> None:
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(lambda: _event(pair_id).set())
    except RuntimeError:
        pass


def _take(pair_id: str, secret: str, now: float) -> dict:
    with _lock:
        _sweep(now)
        rec = _by_id.get(pair_id)
        if not rec or not hmac.compare_digest(
                rec["secret_hash"], hashlib.sha256(str(secret or "").encode()).hexdigest()):
            raise PairingNotFound("Unknown pairing.")
        if rec["state"] != "claimed":
            return {"state": "pending", "expires_wc": int(rec["expires_wc"])}
        # Handed over exactly once: the record goes, with the credential in it.
        _by_id.pop(pair_id, None)
        _by_code.pop(rec["code"], None)
        _events.pop(pair_id, None)
        return {"state": "paired", **rec["result"]}


async def wait(pair_id: str, secret: str, timeout: float = 25.0) -> dict:
    """The installer's long-poll. Raises PairingNotFound for a bad id/secret."""
    global _loop
    _loop = asyncio.get_running_loop()
    out = _take(pair_id, secret, time.time())
    if out["state"] == "paired":
        return out
    ev = _event(pair_id)
    ev.clear()
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return _take(pair_id, secret, time.time())
