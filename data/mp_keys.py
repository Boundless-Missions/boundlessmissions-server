"""
data/mp_keys.py – the multiplayer token signing key, and the tokens it mints.

This is the **account service's half** of the multiplayer trust model. Its
counterpart is `common/tokens.py` in the Multiplayer-Server repo, and the two are
deliberately *not* shared code: they are separate repositories, so they are
duplicated and kept in sync by comment — the same convention this codebase
already uses for `_TRAIT_MODS` ↔ `TraitMods` and `ENGINE_CATEGORIES` ↔
`GetEngineCategories`.

The duplication is small and asymmetric, which is what makes it affordable:

    this file MINTS and PUBLISHES.  The game server only VERIFIES.

Nothing here is ever shipped to a game server, and nothing there can produce a
token. That asymmetry is the entire point.

Why asymmetric signing at all, when `api_auth.py` signs its 30-day session
tokens with HMAC-SHA256 and that works fine: there, one party mints and the same
party verifies, so a shared secret is the simplest thing that works. Here the
verifier is *every self-hosted game server*, and an HMAC key that can verify is a
key that can mint. Ed25519 is what lets a host check a token is genuine while
being unable to forge one, so a compromised or malicious server can corrupt its
own universe and nothing else.

Document shape:

    config/mp_signing_keys
    {
        "current":  {"kid": "...", "pem": "-----BEGIN PRIVATE KEY-----...", "created_wc": 1788...},
        "previous": {"kid": "...", "pem": "...", "created_wc": 1788...},   # may be absent
        "rotated_at": "<iso8601>"
    }

**The private keys are at rest in Firestore**, and that is a deliberate choice
rather than an oversight. The alternatives are worse: a local file does not
survive a redeploy and breaks the moment there are two bot instances, and an
environment variable cannot rotate itself, which in practice means it never
rotates. Firestore is reachable only with the service-account credential the bot
already holds, so this is the same trust boundary as `.env` and the service
account JSON — not a new one. What must stay true is that no endpoint ever
returns the `pem` field; `jwks()` is the only publication path and it builds
public material from scratch.
"""

import base64
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from data.store import _db

log = logging.getLogger(__name__)

# ── Kept in sync with common/tokens.py in the Multiplayer-Server repo ────────
ALGORITHM = "EdDSA"
ISSUER = "boundlessmissions-accounts"
PLAYER_TOKEN_TTL_SECONDS = 15 * 60
SERVICE_CREDENTIAL_TTL_SECONDS = 365 * 24 * 3600
SIGNING_KEY_LIFETIME_SECONDS = 90 * 24 * 3600

#: Who a service credential is *for*. A game server presents it to the master
#: server; it is not a player token and must never be usable as one.
MASTER_AUDIENCE = "boundlessmissions-master"
SCOPE_GAME_SERVER = "game-server"

#: The allow-set token (presence visibility, `ksp-mp-presence-visibility.md` §6.4).
#: Minted here, carried by the client, verified OFFLINE by the game server against
#: the same JWKS as a player token. It shares the universe audience with a player
#: token, so — exactly like a service credential — the scope is what actually
#: keeps the two apart and the verifier checks it explicitly.
SCOPE_ALLOW_SET = "allow-set"
#: The three discovery tiers (§2). `public` admits everyone minus blocks,
#: `friends` admits only the hashed allow list, `hidden` admits no one.
ALLOW_TIERS = ("public", "friends", "hidden")
#: Backstop lifetime. Freshness is event-driven (the client re-fetches on a
#: tier/friend/block edit); this is only the worst-case latency for a block to
#: bite if that signal is missed, so it matches the player token's short life.
ALLOW_SET_TTL_SECONDS = 15 * 60
# ─────────────────────────────────────────────────────────────────────────────

# The JWKS is read by every game server on a schedule and by every joining
# client's server, so an uncached read here is a metered Firestore operation per
# join. Same reasoning as `data/policy.py`'s memoisation, and the same short TTL
# as a backstop for a rotation performed by another process.
_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "doc": None}


class MpKeyError(RuntimeError):
    pass


def _doc():
    return _db.collection("config").document("mp_signing_keys")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _allow_hash(salt: bytes, account: str) -> str:
    """``H(salt, account)`` — a 128-bit tag, base64url, for one allow/deny entry.

    Kept byte-for-byte identical to `common/tokens.py::_allow_hash` in the
    Multiplayer-Server repo: the game server tests membership by hashing a
    *connected* viewer's account with the subject's salt, so the two hashers must
    agree exactly or every membership test silently misses.
    """
    return _b64u(hashlib.sha256(salt + b"\x00" + account.encode("utf-8")).digest()[:16])


def key_id(public: Ed25519PublicKey) -> str:
    """A thumbprint, so the same key always has the same `kid`.

    Derived from the key rather than assigned, so the bot and a game server that
    has cached a JWKS agree about what to call a key without any shared naming
    scheme or coordination.
    """
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64u(hashlib.sha256(raw).digest())[:24]


def _new_key_entry(at_wc: float | None = None) -> dict:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "kid": key_id(priv.public_key()),
        "pem": pem,
        "created_wc": float(at_wc if at_wc is not None else time.time()),
    }


def _load_private(entry: dict) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(entry["pem"].encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise MpKeyError("stored multiplayer signing key is not Ed25519")
    return key


def _public_jwk(entry: dict) -> dict:
    pub = _load_private(entry).public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": entry["kid"],
        "x": _b64u(raw),
    }


def invalidate() -> None:
    with _cache_lock:
        _cache["doc"] = None
        _cache["at"] = 0.0


def _read(*, fresh: bool = False) -> dict:
    if not fresh:
        with _cache_lock:
            doc, at = _cache["doc"], float(_cache["at"])
        if doc is not None and (time.time() - at) < _CACHE_TTL:
            return doc
    snap = _doc().get()
    data = snap.to_dict() if snap.exists else {}
    with _cache_lock:
        _cache["doc"] = data
        _cache["at"] = time.time()
    return data


def get_keyset(*, at_wc: float | None = None) -> dict:
    """The live key set, creating or rotating it if due.

    Rotation is **lazy, on read**, rather than a scheduled job. That is the same
    shape `data/suspensions.py` uses for expiry and for the same reason: a
    sweeper is another moving part that can be down precisely when it matters,
    while resolving on read cannot drift from the thing it describes. The cost is
    that a bot which is never called never rotates — which is fine, because a key
    that signs nothing is a key nobody is verifying against.
    """
    now = float(at_wc if at_wc is not None else time.time())
    data = _read()
    current = data.get("current")

    if not current or "pem" not in current:
        current = _new_key_entry(now)
        data = {"current": current, "rotated_at": _now_iso()}
        _doc().set(data)
        invalidate()
        log.info("minted the first multiplayer signing key (kid=%s)", current["kid"])
        return data

    if now - float(current.get("created_wc", 0.0)) >= SIGNING_KEY_LIFETIME_SECONDS:
        # Promote current to previous and mint a new current. Both stay published
        # for the previous key's remaining life, so a token minted seconds before
        # this moment still verifies — which is the join outage the overlap
        # exists to prevent. The old `previous` is dropped here, which is what
        # actually retires it.
        data = {
            "current": _new_key_entry(now),
            "previous": current,
            "rotated_at": _now_iso(),
        }
        _doc().set(data)
        invalidate()
        log.info("rotated the multiplayer signing key (new kid=%s, previous=%s)",
                 data["current"]["kid"], current["kid"])
    return data


def jwks(*, at_wc: float | None = None) -> dict:
    """Public keys, in the shape a JWKS endpoint publishes.

    Public material only, built from the private keys rather than stored
    alongside them — so there is no path by which a stale or mistakenly-written
    private field could be served. Both keys appear whenever a previous one
    exists.
    """
    data = get_keyset(at_wc=at_wc)
    keys = [_public_jwk(data["current"])]
    prev = data.get("previous")
    if prev and "pem" in prev:
        keys.append(_public_jwk(prev))
    return {"keys": keys}


def mint_player_token(
    *,
    account_id: str,
    handle: str,
    universe_id: str,
    display_name: str = "",
    ttl_seconds: int = PLAYER_TOKEN_TTL_SECONDS,
    at_wc: float | None = None,
) -> tuple[str, int]:
    """Mint a token scoped to exactly one ``(account, universe)``.

    Returns ``(token, expires_wc)``.

    Three refusals, each closing something real:

    * **No handle, no token.** The handle is the immutable account name
      (`accounts.claim_username`, which refuses to change one already set).
      Contracts, disputes and combat reports all reference it, because display
      names are user-editable and renaming to match a trusted trader would
      otherwise be a trivial impersonation in a game where players sign binding
      agreements with strangers.
    * **No universe, no token.** Property is pinned to one universe, so a token
      naming only the account would let a host admit a player into a universe
      they hold nothing in — or let a hostile host replay a token it received
      into a different one.
    * **A 15-minute life.** Short TTL *is* the suspension mechanism: a suspended
      account stops being minted for, and every host learns within the TTL
      without any revocation push, list, or per-request round trip.
    """
    account_id = str(account_id or "").strip()
    handle = str(handle or "").strip()
    universe_id = str(universe_id or "").strip()
    if not account_id:
        raise MpKeyError("cannot mint a multiplayer token with no account id")
    if not handle:
        raise MpKeyError("cannot mint a multiplayer token with no immutable handle")
    if not universe_id:
        raise MpKeyError("cannot mint a multiplayer token with no universe")

    now = float(at_wc if at_wc is not None else time.time())
    data = get_keyset(at_wc=now)
    entry = data["current"]
    expires = int(now + ttl_seconds)

    claims = {
        "iss": ISSUER,
        "sub": account_id,
        "aud": universe_id,
        "iat": int(now),
        "nbf": int(now),
        "exp": expires,
        "jti": uuid.uuid4().hex,
        "handle": handle,
        "name": display_name or "",
    }
    token = jwt.encode(
        claims,
        entry["pem"],
        algorithm=ALGORITHM,
        headers={"kid": entry["kid"]},
    )
    return token, expires


def mint_service_credential(
    *,
    server_id: str,
    operator_account: str,
    ttl_seconds: int = SERVICE_CREDENTIAL_TTL_SECONDS,
    at_wc: float | None = None,
) -> tuple[str, str, int]:
    """Issue the long-lived credential a registered game server authenticates with.

    Returns ``(credential, jti, expires_wc)``. The `jti` is what the registry
    stores and what the revocation list names — never the credential itself,
    which is a bearer token good for a year and is shown to its operator exactly
    once.

    Signed with the **same key** as player tokens and separated from them by
    *audience* rather than by a second key. One key means one rotation to operate
    and one JWKS for everything to fetch, while the audience is what actually
    keeps the two apart: a player token names a *universe* as its audience and a
    credential names the master server, so neither verifier will accept the
    other's token however it is replayed.

    ``operator_account`` is carried so a host's behaviour is attributable to a
    person on the account layer. That is the design's whole answer to a hostile
    host — the audit trail, not a technical guarantee — so a credential with no
    operator is refused rather than issued anonymously.
    """
    server_id = str(server_id or "").strip()
    operator_account = str(operator_account or "").strip()
    if not server_id:
        raise MpKeyError("cannot mint a service credential with no server id")
    if not operator_account:
        raise MpKeyError("cannot mint a service credential with no operator account")

    now = float(at_wc if at_wc is not None else time.time())
    entry = get_keyset(at_wc=now)["current"]
    expires = int(now + ttl_seconds)
    jti = uuid.uuid4().hex

    claims = {
        "iss": ISSUER,
        "sub": server_id,
        "aud": MASTER_AUDIENCE,
        "scope": SCOPE_GAME_SERVER,
        "iat": int(now),
        "nbf": int(now),
        "exp": expires,
        "jti": jti,
        "operator": operator_account,
    }
    token = jwt.encode(claims, entry["pem"], algorithm=ALGORITHM,
                       headers={"kid": entry["kid"]})
    return token, jti, expires


def mint_allow_set_token(
    *,
    account_id: str,
    universe_id: str,
    tier: str,
    set_version: int,
    allow_accounts=(),
    block_accounts=(),
    ttl_seconds: int = ALLOW_SET_TTL_SECONDS,
    at_wc: float | None = None,
    salt: bytes | None = None,
) -> tuple[str, int]:
    """Mint an allow-set token for one ``(account, universe)``. Returns ``(token, expires_wc)``.

    The subject's exposure, signed. ``allow_accounts`` is the friends set the bot
    resolved for the friends tier (already block-excluded); ``block_accounts`` is
    an absolute deny applied at every tier, which is what lets ``public`` still
    exclude a blocker. Both are **hashed** with a fresh per-token ``salt`` before
    they leave the bot, so the raw account ids never reach an untrusted game-server
    operator, and one subject's set cannot be correlated with another's.

    Why a client courier is acceptable here when it would be a smell for a token
    that granted sight of *others*: this set governs exposure of *its own holder*.
    Withholding or replaying a stale one only makes the holder more invisible —
    there is no permissive direction to cheat. The lone exception, downgrade-replay
    (a stale set still listing a since-blocked account), is closed by ``set_version``
    plus the game server refusing a version lower than it has already seen.

    **Kept in sync by comment with `common/tokens.py::mint_allow_set_token`** in
    the Multiplayer-Server repo — the claim shape below is the wire contract its
    ``verify_allow_set_token`` reads, and the two are separate reimplementations
    on purpose (this mints, the game server only verifies).
    """
    account_id = str(account_id or "").strip()
    universe_id = str(universe_id or "").strip()
    if not account_id:
        raise MpKeyError("cannot mint an allow-set with no account id")
    if not universe_id:
        raise MpKeyError("cannot mint an allow-set with no universe")
    if tier not in ALLOW_TIERS:
        raise MpKeyError(f"unknown visibility tier {tier!r}")
    if int(set_version) < 0:
        raise MpKeyError("set_version must be non-negative")

    now = float(at_wc if at_wc is not None else time.time())
    entry = get_keyset(at_wc=now)["current"]
    expires = int(now + ttl_seconds)
    if salt is None:
        salt = uuid.uuid4().bytes

    claims = {
        "iss": ISSUER,
        "sub": account_id,
        "aud": universe_id,
        "iat": int(now),
        "nbf": int(now),
        "exp": expires,
        "jti": uuid.uuid4().hex,
        "scope": SCOPE_ALLOW_SET,
        "tier": tier,
        "sv": int(set_version),
        "salt": _b64u(salt),
        "allow": sorted({_allow_hash(salt, a) for a in allow_accounts if a}),
        "block": sorted({_allow_hash(salt, a) for a in block_accounts if a}),
    }
    token = jwt.encode(claims, entry["pem"], algorithm=ALGORITHM,
                       headers={"kid": entry["kid"]})
    return token, expires
