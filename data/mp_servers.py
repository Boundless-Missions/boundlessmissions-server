"""
data/mp_servers.py – the registry of game servers, and their service credentials.

Anyone can run a game server. That is the hosting model, not a concession, so
registration is open to **any linked account** rather than to the owner — gating
it on one person would make "anyone can run a server" false while appearing to
be a safety measure. What registration buys is not permission to exist; it is an
identity the master server can verify and, more importantly, one that is
**attributable to a person on the account layer**. The design's answer to a
hostile host is the audit trail, and this is where the trail starts.

Document shape:

    mp_servers/{server_id}
    {
        "server_id":        "srv_ab12…",
        "operator_account": "1234567890",
        "name":             "Kerbin Collective",
        "credential_jti":   "…",          # the live credential's id, for revocation
        "revoked":          false,
        "revoked_at":       null,
        "created_at":       "<iso8601>",
        "last_issued_at":   "<iso8601>"
    }

## Three things this deliberately does not do

**It never stores the credential.** Only its `jti`. A service credential is a
bearer token good for a year; keeping a copy would mean any read of this
collection hands out working credentials for every server on the network. It is
shown once, at issue, exactly like an API key — and re-issuing is how a lost one
is replaced, which also rotates the `jti` and retires the old one.

**It does not verify that the host is reachable, or real.** That is the master
server's job and it does it by observation: a server appears in the browser only
while it is heartbeating, from the address the announcement actually came from.
A registry entry with nothing behind it costs a row.

**It does not expire entries.** A credential expires on its own after a year;
the registry row outlives it on purpose, because the row is the audit record and
a host whose credential lapsed is still the account that ran it.
"""

import base64
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

#: How many servers one account may register. Bounds listing spam without
#: getting in the way of anyone genuinely running a few universes. A revoked
#: entry does not count against it — otherwise replacing a lost credential would
#: silently consume the allowance.
MAX_SERVERS_PER_ACCOUNT = 5

MAX_NAME_LENGTH = 64

#: The revoked list is polled by the master server, which verifies credentials
#: offline and so has no other way to learn a credential was withdrawn. Memoised
#: because it is read on a schedule by a service we run, and it changes rarely.
_REVOKED_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "ids": None}


class MpServerError(RuntimeError):
    pass


def _col():
    return _db.collection("mp_servers")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_name(name: str) -> str:
    """Bound and sanitise an operator-supplied name.

    Control characters are stripped rather than escaped: this string is rendered
    in a KSP IMGUI list and in a browser, and a name carrying newlines or
    terminal escapes is a display attack on everyone who opens the server list.
    The master does this too — belt and braces, because the two are separate
    services and neither should rely on the other having done it.
    """
    cleaned = "".join(ch for ch in str(name or "") if ch.isprintable())
    return cleaned.strip()[:MAX_NAME_LENGTH]


def list_for_account(account_id) -> list[dict]:
    """Every server this account has registered, revoked ones included."""
    account_id = str(account_id)
    out = []
    for snap in _col().where("operator_account", "==", account_id).stream():
        d = snap.to_dict() or {}
        d.pop("credential_jti", None)   # internal; not the caller's business
        out.append(d)
    out.sort(key=lambda d: d.get("created_at") or "")
    return out


def register(account_id, name: str) -> dict:
    """Register a new game server. Returns the stored record (no credential).

    The caller mints the credential itself and calls :func:`note_issued` with its
    `jti`; splitting it that way keeps the signing key in `data/mp_keys` and out
    of this module entirely, so nothing here can issue anything.
    """
    account_id = str(account_id)
    name = _clean_name(name)
    if not name:
        raise MpServerError("A server name is required.")

    live = [s for s in list_for_account(account_id) if not s.get("revoked")]
    if len(live) >= MAX_SERVERS_PER_ACCOUNT:
        raise MpServerError(
            f"You already have {len(live)} registered servers "
            f"({MAX_SERVERS_PER_ACCOUNT} is the limit). Revoke one first.")

    server_id = "srv_" + secrets.token_hex(8)
    rec = {
        "server_id": server_id,
        "operator_account": account_id,
        "name": name,
        "credential_jti": "",
        "revoked": False,
        "revoked_at": None,
        "created_at": _now_iso(),
        "last_issued_at": None,
    }
    _col().document(server_id).set(rec)
    log.info("registered game server %s for account %s", server_id, account_id)
    return rec


def get(server_id: str) -> dict | None:
    snap = _col().document(str(server_id)).get()
    return snap.to_dict() if snap.exists else None


def note_issued(server_id: str, jti: str) -> None:
    """Record which credential is the live one for this server.

    Overwriting the previous `jti` is what retires it: `revoked_jtis` reports
    every superseded id, so re-issuing a credential invalidates the old one
    without a separate revoke step. A host that lost its credential asks for a
    new one, and the one that may be in someone else's hands stops working.
    """
    doc = _col().document(str(server_id))
    snap = doc.get()
    if not snap.exists:
        raise MpServerError("No such server.")
    prev = (snap.to_dict() or {}).get("credential_jti") or ""

    updates = {"credential_jti": str(jti), "last_issued_at": _now_iso()}
    if prev and prev != str(jti):
        # Keep the superseded ids, so the master can refuse them. Bounded: a
        # server re-issued a thousand times would otherwise grow a document
        # without limit, and only recent ones can still be within their year.
        superseded = list((snap.to_dict() or {}).get("superseded_jtis") or [])
        superseded.append(prev)
        updates["superseded_jtis"] = superseded[-20:]
    doc.update(updates)
    invalidate()


def revoke(server_id: str, account_id) -> dict:
    """Withdraw a server's credential. Only its operator may do this."""
    server_id = str(server_id)
    rec = get(server_id)
    if not rec:
        raise MpServerError("No such server.")
    if str(rec.get("operator_account")) != str(account_id):
        # 404-shaped rather than 403-shaped at the endpoint, so a caller cannot
        # enumerate which server ids exist.
        raise MpServerError("No such server.")
    if rec.get("revoked"):
        return rec

    _col().document(server_id).update({"revoked": True, "revoked_at": _now_iso()})
    invalidate()
    log.info("revoked game server %s", server_id)
    rec["revoked"] = True
    return rec


def invalidate() -> None:
    with _cache_lock:
        _cache["ids"] = None
        _cache["at"] = 0.0
    invalidate_keys()     # a revoked server's transition key goes with it


def revoked_jtis(*, fresh: bool = False) -> list[str]:
    """Credential ids the master server must refuse.

    This exists because the master verifies credentials **offline** against the
    published key, which is what makes it cheap and outage-tolerant — and also
    what makes it unable to notice a withdrawal on its own. A 15-minute player
    token needs no such list because it simply expires; a one-year service
    credential does.

    Returned as ids rather than server ids so the master can check the `jti` it
    already parsed, without a second lookup, and so re-issuing a credential
    invalidates the old one by the same mechanism as revoking it.
    """
    if not fresh:
        with _cache_lock:
            ids, at = _cache["ids"], float(_cache["at"])
        if ids is not None and (time.time() - at) < _REVOKED_CACHE_TTL:
            return list(ids)

    out: list[str] = []
    try:
        for snap in _col().stream():
            d = snap.to_dict() or {}
            out.extend(str(j) for j in (d.get("superseded_jtis") or []) if j)
            if d.get("revoked") and d.get("credential_jti"):
                out.append(str(d["credential_jti"]))
    except Exception:
        # Fails CLOSED for the *caller* to decide: an empty list here would tell
        # the master "nothing is revoked", which is a stronger claim than "I do
        # not know". Raising lets it keep its previous list instead.
        log.exception("could not read the revoked credential list")
        raise

    with _cache_lock:
        _cache["ids"] = list(out)
        _cache["at"] = time.time()
    return out


# ── Public keys a server registers about itself ───────────────────────────────
#
# Two pieces of PUBLIC material, sent by the game server itself with its service
# credential (`PUT /api/v1/mp/servers/keys`, `game/production.py` in the
# Multiplayer-Server repo):
#
# * **The transition key** — the RS256 key the server signs sanctioned
#   transitions with. The base mod verifies those against keys fetched from ONE
#   compiled-in origin (this bot's `/api/v1/mp/jwks`), selected by the token's
#   `srv`, so a server's key is only ever trusted because this registry vouches
#   that the credential holder for that server id published it. Before this the
#   bot published none, every sanctioned transition failed to verify, and the
#   watchdog judged bubble form/dissolve and clock relabels as teleports.
# * **TLS pins** — SPKI SHA-256 of the server's (self-signed, raw-IP)
#   certificate. The client reads them back in the token-mint response, over
#   this bot's CA-validated HTTPS, and refuses any other certificate.
#
# Kept in sync by comment with `common/tlspin.py` (pin shape, MAX_PINS) and
# `common/transitions.py::TransitionSigner.public_jwk` (JWK shape).

MAX_TLS_PINS = 2
_PIN_RE = re.compile(r"^sha256/[A-Za-z0-9+/]{43}=$")
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")
#: RSA modulus bounds, in bytes: 2048..4096-bit keys. Smaller is forgeable,
#: larger only costs every base mod a slower verify.
_MIN_N, _MAX_N = 256, 512

_keys_lock = threading.Lock()
_keys_cache: dict = {"at": 0.0, "jwks": None}
_KEYS_CACHE_TTL = 60.0


def clean_tls_pins(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and _PIN_RE.match(v) and v not in out:
            out.append(v)
        if len(out) >= MAX_TLS_PINS:
            break
    return out


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def clean_transition_jwk(jwk, server_id: str) -> dict:
    """The stored (and published) form of a server's transition key.

    Strict, because the base mod trusts whatever this publishes under the
    server's id: RSA only, RS256, a 2048–4096-bit modulus, e = 65537, and
    `server_id` set from the CREDENTIAL — never from the body, so one server
    cannot publish a key under another's id. Unknown members are dropped.
    """
    if not isinstance(jwk, dict):
        raise MpServerError("transition_jwk must be an object")
    if jwk.get("kty") != "RSA" or jwk.get("alg", "RS256") != "RS256" \
            or jwk.get("use", "sig") != "sig":
        raise MpServerError("transition_jwk must be an RS256 signing key")
    n, e = jwk.get("n"), jwk.get("e")
    if not isinstance(n, str) or not isinstance(e, str) \
            or not _B64U_RE.match(n) or not _B64U_RE.match(e):
        raise MpServerError("transition_jwk needs base64url n and e")
    try:
        nb, eb = _b64u_decode(n), _b64u_decode(e)
    except Exception:
        raise MpServerError("transition_jwk n/e are not base64url")
    nb = nb.lstrip(b"\x00")
    if not (_MIN_N <= len(nb) <= _MAX_N):
        raise MpServerError("transition_jwk must be a 2048–4096-bit RSA key")
    if int.from_bytes(eb, "big") != 65537:
        raise MpServerError("transition_jwk exponent must be 65537")
    return {"kty": "RSA", "alg": "RS256", "use": "sig",
            "server_id": str(server_id), "n": n, "e": e}


def set_keys(server_id: str, jti: str, *, transition_jwk=None, tls_pins=None,
             universe_id: str = "") -> dict:
    """Record the public keys a server states about itself.

    ``server_id`` and ``jti`` come from the verified credential. The credential
    must be the server's LIVE one: a superseded or revoked credential (which may
    be in someone else's hands — that is why it was re-issued) cannot rewrite
    the keys the base mod and every client trust for this id.
    """
    server_id = str(server_id)
    rec = get(server_id)
    if not rec or rec.get("revoked") or str(rec.get("credential_jti") or "") != str(jti):
        raise MpServerError("Credential is not the server's live credential.")
    updates = {
        "tls_pins": clean_tls_pins(tls_pins),
        "keys_universe_id": str(universe_id or "")[:64],
        "keys_updated_at": _now_iso(),
    }
    if transition_jwk is not None:
        updates["transition_jwk"] = clean_transition_jwk(transition_jwk, server_id)
    _col().document(server_id).update(updates)
    invalidate_keys()
    log.info("game server %s registered keys (transition=%s, %d tls pin(s))",
             server_id, "yes" if transition_jwk is not None else "no",
             len(updates["tls_pins"]))
    rec.update(updates)
    return rec


def invalidate_keys() -> None:
    with _keys_lock:
        _keys_cache["jwks"] = None
        _keys_cache["at"] = 0.0


def transition_jwks(*, fresh: bool = False) -> list[dict]:
    """Every live server's transition key, as the base mod's `ParseJwks` reads them.

    A revoked server's key is withdrawn with it. Memoised like the revoked
    list: the JWKS route is anonymous and on every host's schedule.
    """
    if not fresh:
        with _keys_lock:
            keys, at = _keys_cache["jwks"], float(_keys_cache["at"])
        if keys is not None and (time.time() - at) < _KEYS_CACHE_TTL:
            return [dict(k) for k in keys]
    out: list[dict] = []
    for snap in _col().stream():
        d = snap.to_dict() or {}
        jwk = d.get("transition_jwk")
        if d.get("revoked") or not isinstance(jwk, dict):
            continue
        # Re-derive the id from the document, never trust a stored copy of it.
        out.append(dict(jwk, server_id=str(d.get("server_id") or snap.id)))
    with _keys_lock:
        _keys_cache["jwks"] = [dict(k) for k in out]
        _keys_cache["at"] = time.time()
    return out


def tls_pins_for(server_id: str) -> dict:
    """What the token-mint response tells the client about the server it is joining.

    ``registered`` is false for an unknown or revoked id; ``tls_pins`` is what the
    server registered (possibly none, from a server older than pinning).
    """
    sid = str(server_id or "").strip()
    rec = get(sid) if sid else None
    if not rec or rec.get("revoked"):
        return {"server_id": sid, "registered": False, "tls_pins": []}
    return {"server_id": sid, "registered": True,
            "tls_pins": clean_tls_pins(rec.get("tls_pins"))}
