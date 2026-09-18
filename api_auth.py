"""
api_auth.py – KSP ↔ Discord authentication layer.

Flow:
  1. User runs /b linkcode in Discord → 6-digit code stored in Firestore (10 min TTL)
  2. KSP client sends POST /api/v1/auth/link with the code
  3. Server validates, returns a signed session token (HMAC-SHA256)
  4. KSP stores the token locally, sends it with every request via Authorization header

No API keys, Firebase creds, or secrets ever touch the client.
"""

import hashlib
import hmac
import json
import logging
import secrets
import string
import time
from datetime import datetime, timezone

from firebase_admin import firestore

from data.store import _db, _db_unguarded

log = logging.getLogger(__name__)

# Token lifetime: 30 days
TOKEN_LIFETIME = 30 * 24 * 3600

# Which surface a token was minted for. A KSP token must not open the website's
# endpoints and vice versa: the two tiers carry different gates (device binding
# and the mod-hash gate exist only on the KSP tier), so a token that works on
# both lets a copied session.token bypass every gate the KSP tier still enforces.
# Tokens minted before this field existed carry no `aud`; verify_session_token
# reports None for them and the dependencies accept that for the rest of their
# 30-day life rather than logging everybody out at once.
AUD_KSP = "ksp"
AUD_WEB = "web"


def _mask(code: str) -> str:
    """A link code for the log: enough to correlate, not enough to use."""
    s = str(code or "")
    return s[:2] + "*" * max(0, len(s) - 2)

# Link code / login-approval challenge lifetime: 3 minutes
LINK_CODE_LIFETIME = 180
APPROVAL_LIFETIME = 180

# A code minted from the account panel gets longer, because the journey is longer.
# Three minutes is fine between two apps that are both already running — read the
# code in Discord, alt-tab to KSP. It is not enough when the next step is *launching*
# a modded KSP, which can spend minutes on the loading screen before the link window
# exists to type into. The wider window is cheap here because a panel-minted code
# does not authorize anything on its own: it says which account is being asked
# about, and the approval (which lands back in the panel) is the actual gate.
PANEL_LINK_CODE_LIFETIME = 600

# Where a link code was minted, which decides where its approval is answered.
# The rule: the approval must land on the surface that did NOT consume the code.
SOURCE_DISCORD = "discord"   # /linkcode in Discord  → approve in the Discord DM
SOURCE_PANEL = "panel"       # account panel on the website → approve in the panel

# Device-approval challenge lifetime: 1 hour. Longer than a login approval — a
# blocked device re-prompts the user if the DM is missed, so a short window would
# just nag. The device stays blocked until approved regardless.
DEVICE_CHALLENGE_LIFETIME = 3600


def _link_codes_col():
    return _db.collection("ksp_link_codes")


def _sessions_col():
    return _db.collection("ksp_sessions")


def _sessions_col_unguarded():
    """`ksp_sessions` off the UNMETERED-BRAKE handle, for the auth-path read only.

    `_get_token_version` runs on the auth dependency of every authenticated route.
    On the guarded handle it hits `guard.require_firebase()`, which raises at
    Level.FROZEN — and since the read then fails closed on a cold cache, a budget
    freeze turned into a 503 on EVERY authenticated route, the admin console
    included. The console is the documented way to lift a freeze, so the recovery
    path ran through the thing the freeze had disabled.

    Exempting one keyed read is the right trade: refusing it does not save the
    request, it only makes the request fail. The read is still reported to the
    meter by the caller, so it stays on the bill.
    """
    return _db_unguarded.collection("ksp_sessions")


def _twofa_col():
    return _db.collection("ksp_2fa_challenges")


def _digit_code(n: int = 6) -> str:
    """A cryptographically secure n-digit numeric code."""
    return "".join(secrets.choice(string.digits) for _ in range(n))


# ── Token Versioning (for "log out of all devices") ──────────────────────────
#
# Session tokens are stateless HMAC tokens, so there's nothing to "delete" to
# revoke them. Instead each user has a monotonically increasing token_version
# stored in their session doc; every token embeds the version it was minted at.
# verify_session_token rejects any token whose version is older than the user's
# current one, so bumping the version (logout_all_devices) instantly invalidates
# every token ever issued — across all devices — without touching the secret.
#
# The version is read on every request, so it's cached in-memory for a short TTL
# to keep verification cheap. logout_all_devices updates the cache in-process, so
# revocation takes effect immediately within the running bot (and within the TTL
# for the read-through path).

# TWO counters, not one. `token_version` means "log out EVERYWHERE" and every token
# checks it. `web_token_version` is the website's own sign-out: only an AUD_WEB token
# checks it, so signing out of a browser leaves the player's KSP installs linked.
#
# The split exists because the site's ordinary "Log out" used to bump the single
# global counter, so a browser sign-out 401'd every KSP client the account had. Each
# install only discovered that at its next launch, which made one sign-out read as
# the mod unlinking itself at random for days afterwards. Signing out of a browser is
# not a statement about the game and is now scoped like one; "log out everywhere" is
# still offered on both surfaces and still bumps the global counter.
_TOKEN_VERSION_TTL = 30  # seconds
# user_id -> (version, web_version, fetched_at). Both counters live in the same
# session document and are read and cached together: this sits on the auth dependency
# of every authenticated route, so fetching them separately would double the busiest
# read in the system.
_token_versions: dict[str, tuple[int, int, float]] = {}


class TokenVersionUnavailable(Exception):
    """The revocation version could not be read and nothing is cached.

    Raised rather than answered with 0, because 0 means "never revoked" — so a
    Firestore blip during a cold start (an empty cache is exactly the state after
    a restart) would accept every token this user has ever been issued, including
    ones revoked by a logout-all, a ban, or a device removal. `verify_session_token`
    turns this into a *retryable* refusal rather than an invalid-token one: the
    caller is told to try again shortly, not signed out.
    """


def _get_versions(user_id: str) -> tuple[int, int]:
    """`(token_version, web_token_version)` for a user — `(0, 0)` if never revoked.

    One Firestore read for both, cached together; see the note on `_token_versions`.

    Raises `TokenVersionUnavailable` when the read fails and there is nothing
    cached to fall back on — see the class docstring for why that is not 0.
    """
    cached = _token_versions.get(user_id)
    now = time.time()
    if cached is not None and now - cached[2] < _TOKEN_VERSION_TTL:
        return cached[0], cached[1]

    try:
        # Unguarded on purpose — see `_sessions_col_unguarded`. Metered by hand so
        # the exemption is from the brake, not from the bill.
        snap = _sessions_col_unguarded().document(user_id).get()
        try:
            from cost_guard import guard as _cost_guard
            _cost_guard.note_firestore(reads=1)
        except Exception:                       # metering must never fail a login
            pass
        data = (snap.to_dict() or {}) if snap.exists else {}
        version = int(data.get("token_version", 0) or 0)
        web_version = int(data.get("web_token_version", 0) or 0)
    except Exception as exc:
        log.warning("Could not read token version for %s: %s", user_id, exc)
        # Read failed: return the last known value (a revocation already cached still
        # applies), but do NOT cache this guess. Caching it would pin a possibly-stale
        # version for the whole TTL — so a Firestore blip at the wrong moment could
        # keep accepting a just-revoked token even after Firestore recovers. Not
        # caching means the very next request re-reads and picks up the real version.
        #
        # With nothing cached there is no last known value, and 0 is not a safe
        # stand-in for one: it is the specific value meaning "never revoked", so
        # answering it would honour every token this account has ever held. Refuse
        # instead, and let the caller make it a retry.
        if cached is None:
            raise TokenVersionUnavailable(str(user_id))
        return cached[0], cached[1]

    _token_versions[user_id] = (version, web_version, now)
    return version, web_version


def _get_token_version(user_id: str) -> int:
    """The global revocation version — the one EVERY token is checked against."""
    return _get_versions(user_id)[0]


def _get_web_token_version(user_id: str) -> int:
    """The website-only revocation version — checked by AUD_WEB tokens alone."""
    return _get_versions(user_id)[1]


# ── Link Codes ───────────────────────────────────────────────────────────────

def _claim_unused_code(payload: dict, *, attempts: int = 8) -> str:
    """Mint a 6-digit code and claim it with `create()`, retrying on a collision.

    The code IS the document id, and this used to be a `set()`, which overwrites
    unconditionally. Two live codes are drawn from a 10^6 space with a 3-10 minute
    life, so a collision while both are live silently re-pointed the earlier
    document at the later user — and `validate_link_code` then handed the first
    player's typed code back as the SECOND player's identity: a session token for
    an account that is not theirs, or (the likelier, non-adversarial case) an
    honest player linking their game to a stranger's wallet with no error anywhere.

    `create()` fails when the id already exists, which turns "improbable" into
    "impossible": a taken code is simply re-drawn. This is the primitive
    `weeklymissions._save_selection` already uses for exactly the same reason, and
    the inconsistency was the whole bug.

    A handful of attempts is ample — with even a thousand live codes the chance of
    eight consecutive collisions is about 10^-24 — and exhausting them raises
    rather than falling back to an overwrite.
    """
    from google.api_core import exceptions as _gexc
    for _ in range(attempts):
        code = _digit_code(6)
        try:
            _link_codes_col().document(code).create(payload)
            return code
        except _gexc.AlreadyExists:
            continue
    raise RuntimeError("Could not allocate an unused link code; try again.")


def generate_link_code(guild_id: int, user_id: int, username: str) -> str:
    """Create a 6-digit code, store in Firestore with 10-min expiry. Returns the code."""
    # Invalidate any existing codes for this user
    for doc in _link_codes_col().where("user_id", "==", str(user_id)).stream():
        doc.reference.delete()

    code = _claim_unused_code({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "source": SOURCE_DISCORD,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + LINK_CODE_LIFETIME,  # 3 minutes
    })
    log.info("Generated link code %s for user %s (%s)", _mask(code), user_id, username)
    return code


def generate_account_link_code(account_id, guild_id, display_name: str) -> tuple[str, float]:
    """Mint a link code from the account panel. Returns (code, expires_at).

    The one that makes a Discord-less account possible at all: `/linkcode` is a
    Discord slash command, so without this a player who signed up with Google has
    no way to link a KSP install. It does NOT replace `/linkcode` — an existing
    Discord player should never be made to create a website account first — so both
    mint into this same collection and only the `source` differs.
    """
    aid = str(account_id)
    for doc in _link_codes_col().where("user_id", "==", aid).stream():
        doc.reference.delete()

    expires_at = time.time() + PANEL_LINK_CODE_LIFETIME
    code = _claim_unused_code({
        "guild_id": str(guild_id),
        "user_id": aid,
        "username": display_name,
        "source": SOURCE_PANEL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    })
    log.info("Generated panel link code %s for account %s", _mask(code), aid)
    return code, expires_at


def validate_link_code(code: str) -> dict | None:
    """
    Check if a link code is valid and not expired.
    Returns {guild_id, user_id, username} or None.
    Deletes the code after successful validation (one-time use).
    """
    doc = _link_codes_col().document(code).get()
    if not doc.exists:
        return None

    data = doc.to_dict()
    if time.time() > data.get("expires_at", 0):
        # Expired — clean up
        doc.reference.delete()
        return None

    # One-time use — delete
    doc.reference.delete()
    log.info("Validated link code %s for user %s", _mask(code), data["user_id"])
    return {
        "guild_id": data["guild_id"],
        "user_id": data["user_id"],
        "username": data["username"],
        # Absent on codes minted before panel linking existed, which were all
        # Discord ones — so the default is what those actually were.
        "source": data.get("source", SOURCE_DISCORD),
    }


# ── Discord DM login approval ────────────────────────────────────────────────
#
# Second step of linking ("push approval"): a valid link code creates a pending
# challenge and the bot DMs the Discord user an "✅ Log in" / "🚫 Not me" button.
# The KSP client polls until the user approves (token issued) or denies. This is
# a genuine second check — completing the link requires an interaction inside the
# user's own Discord, which a stolen link code alone can't satisfy and which (unlike
# a numeric code) can't be read out to an attacker over social engineering.
#
# Nothing secret travels through Discord: the button only flips the challenge
# state, and the session token is handed solely to the polling client that holds
# the (144-bit, single-use, 3-minute) challenge_id. Gated by cfg.KSP_2FA_ENABLED.

def create_approval_challenge(guild_id: str, user_id: str, username: str,
                              client_ip: str = "", source: str = SOURCE_DISCORD,
                              device_id: str = "", aud: str = AUD_KSP) -> str:
    """Create a pending login-approval challenge. Returns the challenge_id.

    `source` decides which surface *answers* it — a DM for a code that came from
    Discord, the account panel for one that came from the panel. `aud` is the
    different question of which surface is *asking*, and it is recorded because the
    whole value of this step is that the player approves a named thing. The prompt
    said "a KSP client" unconditionally while the same challenge id could be
    redeemed on the website tier — which has neither device binding nor the
    mod-version gate — so the sentence they agreed to was not necessarily what
    happened. `poll_approval` refuses a poll from the other tier.

    The device id is kept only so the approving surface can show *what* is asking;
    it is not part of the decision, and the device is trusted by
    `_issue_link_token` as it always was.
    """
    challenge_id = secrets.token_urlsafe(18)
    _twofa_col().document(challenge_id).set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "status": "pending",
        "client_ip": client_ip,
        "source": source,
        "aud": aud,
        "device_id": device_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + APPROVAL_LIFETIME,
    })
    log.info("Created %s login-approval challenge for user %s", source, user_id)
    return challenge_id


def pending_panel_approval(account_id) -> dict | None:
    """The panel-sourced login approval this account is currently being asked for.

    Queried on `user_id` alone and filtered in Python rather than with a compound
    where(): a single-field equality needs no composite index, and this collection
    holds at most a handful of live rows per user. Returns the challenge (with its
    id) or None when there is nothing to answer.
    """
    aid = str(account_id)
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = _twofa_col().where(filter=FieldFilter("user_id", "==", aid)).limit(10)
        now = time.time()
        for doc in q.stream():
            d = doc.to_dict() or {}
            if d.get("source") != SOURCE_PANEL:
                continue
            if d.get("status") != "pending" or now > d.get("expires_at", 0):
                continue
            d["challenge_id"] = doc.id
            return d
    except Exception as exc:
        log.warning("Could not read pending approvals for %s: %s", aid, exc)
    return None


def _owns_challenge(stored_user_id, acting_user_id) -> bool:
    """Does `acting_user_id` own a challenge recorded against `stored_user_id`?

    The two are not always the same *kind* of id. A challenge stores an **account
    id** (`cogs/ksp_bridge` resolves `accounts.account_for_discord` before minting
    the code), while the Discord approval buttons hand back the clicker's raw
    **snowflake**. For almost everybody those are the same string — but not for an
    account that joined a website identity to a Discord one, where the account id
    is `a_…`. A bare `!=` therefore refused the real owner's own click, and since
    the device-binding gate has no other approval surface, a joined account was
    permanently locked out of the mod by any device change.

    So the comparison accepts either form: the stored id itself, or a snowflake
    that resolves to it. Still exact — it never widens ownership to a second
    account, it only stops one account being mistaken for two.
    """
    stored = str(stored_user_id or "")
    acting = str(acting_user_id or "")
    if not stored or not acting:
        return False
    if stored == acting:
        return True
    try:
        from data import accounts
        return str(accounts.account_for_discord(acting) or "") == stored
    except Exception as exc:  # a lookup failure must not approve anything
        log.warning("Challenge ownership lookup failed for %s: %s", acting, exc)
        return False


def resolve_approval(challenge_id: str, acting_user_id: str, approve: bool) -> bool:
    """Apply the Discord button's decision to a pending challenge.

    The acting Discord user MUST own the challenge — a click from anyone else (or
    on an expired/already-decided challenge) is ignored. Returns True if the new
    state ('approved'/'denied') was written, False otherwise. The challenge isn't
    consumed here; the polling KSP client consumes it via poll_approval.
    """
    doc = _twofa_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return False

    data = snap.to_dict()
    if not _owns_challenge(data.get("user_id"), acting_user_id):
        log.warning("Approval %s: acting user %s is not owner %s, ignored",
                    challenge_id, acting_user_id, data.get("user_id"))
        return False
    if data.get("status") != "pending" or time.time() > data.get("expires_at", 0):
        return False

    doc.update({"status": "approved" if approve else "denied"})
    log.info("Login-approval challenge %s %s by user %s",
             challenge_id, "approved" if approve else "denied", acting_user_id)
    return True


def poll_approval(challenge_id: str, aud: str = AUD_KSP) -> dict:
    """Poll a challenge on behalf of the waiting client.

    `aud` is the tier doing the polling. A challenge may only be redeemed by the
    surface it was created for: the approval prompt names one of them out loud, and
    a challenge answered as "a KSP client wants to sign in" must not turn into a
    website session. A mismatch reads as expired rather than as a distinct error —
    it is not a state any honest client reaches, and saying more only helps someone
    probing for one.

    Returns one of:
      {"state": "pending"}
      {"state": "approved", "guild_id", "user_id", "username"}  (consumes it)
      {"state": "denied"}    (consumes it)
      {"state": "expired"}   (unknown / timed-out / wrong tier)
    """
    doc = _twofa_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return {"state": "expired"}

    data = snap.to_dict()
    # Challenges minted before this field existed carry no `aud`; treat those as
    # KSP, which is what every one of them was.
    if str(data.get("aud") or AUD_KSP) != aud:
        log.warning("Approval challenge %s polled from the wrong tier (%s)", challenge_id, aud)
        return {"state": "expired"}
    if time.time() > data.get("expires_at", 0):
        doc.delete()
        return {"state": "expired"}

    status = data.get("status", "pending")
    if status == "pending":
        return {"state": "pending"}

    # Terminal state — consume the challenge (one-time use) and report it.
    doc.delete()
    if status == "approved":
        return {
            "state": "approved",
            "guild_id": data["guild_id"],
            "user_id": data["user_id"],
            "username": data["username"],
        }
    return {"state": "denied"}


# ── Device binding (anti account-sharing) ────────────────────────────────────
#
# Each KSP install writes a random device id once (PluginData/device.id) and sends
# it on every request as X-Device-Id. A device that completes the full link+login-
# approval flow is trusted automatically; any *other* device id appearing on the
# account later (e.g. a copied session token) is hard-blocked until the user
# approves it from a Discord DM ("✅ Yes, it's me") or rejects it ("🚫 No — report").
#
# The id is a random GUID read from no hardware property at all, so it stores no
# personal data and survives a NIC swap or a re-imaged network stack. IP and
# KSP.log are gathered only into a user-filed moderation report, never into the
# binding itself; no hardware identifier is collected anywhere.

_ALLOWED_DEV_TTL = 30  # seconds — cache trusted-device sets to keep checks cheap
_allowed_devices: dict[str, tuple[set, float]] = {}  # user_id -> (devices, fetched_at)


def _device_chal_col():
    return _db.collection("ksp_device_challenges")


def _get_allowed_devices(user_id: str) -> set | None:
    """The user's trusted-device set — or None when it is UNKNOWABLE right now
    (Firestore read failed with nothing cached).

    None is deliberately distinct from an empty set. Empty means "no device
    bound yet", which check_device now treats as an UNKNOWN device (it drives
    the DM-approval flow rather than silently adopting the first id seen). None
    means the read failed: check_device fails OPEN for that one request only, so
    an outage never locks everyone out. Keeping them distinct is what stops a
    Firestore blip from reading as "no devices" — same rule as _get_token_version
    above: on a failed read, return the last known value if there is one, and
    never cache the guess."""
    cached = _allowed_devices.get(user_id)
    now = time.time()
    if cached is not None and now - cached[1] < _ALLOWED_DEV_TTL:
        return cached[0]
    try:
        snap = _sessions_col().document(user_id).get()
        devices = (set(snap.to_dict().get("allowed_devices", []) or [])
                   if snap.exists else set())
    except Exception as exc:
        log.warning("Could not read allowed devices for %s: %s", user_id, exc)
        return cached[0] if cached is not None else None
    _allowed_devices[user_id] = (devices, now)
    return devices


def add_allowed_device(user_id: str, device_id: str) -> None:
    """Trust a device for this user (idempotent). Updates the in-process cache so
    the device's very next request passes without waiting for the cache TTL.
    Alongside the id, a device_meta entry records when trust was granted — that's
    what the "your devices" listing shows, and what remove_allowed_device drops."""
    if not device_id:
        return
    _sessions_col().document(user_id).set({
        "allowed_devices": firestore.ArrayUnion([device_id]),
        # set(merge=True) treats nested dict keys as literal map keys (no field-
        # path parsing), so a raw device id is safe here; sibling entries survive.
        "device_meta": {device_id: {
            "added_at": datetime.now(timezone.utc).isoformat(),
        }},
    }, merge=True)
    base = _get_allowed_devices(user_id)
    if base is not None:
        _allowed_devices[user_id] = (set(base) | {device_id}, time.time())
    log.info("Device %s… trusted for user %s", device_id[:8], user_id)


def remove_allowed_device(user_id: str, device_id: str) -> bool:
    """Un-trust a device — the reverse of add_allowed_device, and what makes the
    approval prompt a decision rather than a one-way door. The device's next
    request falls back into the device gate exactly like a brand-new PC (DM
    approval required); its session token is NOT revoked, because the gate in
    front of every gated endpoint is the block. Returns False when the id wasn't
    in the trust set. Removing the last device re-arms trust-on-first-use, which
    is the same state a fresh rollout account is in."""
    if not device_id:
        return False
    allowed = _get_allowed_devices(user_id)
    if allowed is not None and device_id not in allowed:
        return False
    _sessions_col().document(user_id).update({
        "allowed_devices": firestore.ArrayRemove([device_id]),
        # update() DOES parse field paths — device ids are hex and may start
        # with a digit, so the segment must be backtick-quoted.
        f"device_meta.`{device_id}`": firestore.DELETE_FIELD,
    })
    cached = _allowed_devices.get(user_id)
    if cached is not None:
        # Mutate the cached set in place, keeping its original timestamp: the
        # removal must be visible immediately without extending staleness.
        cached[0].discard(device_id)
    log.info("Device %s… un-trusted for user %s", device_id[:8], user_id)
    return True


def list_devices(user_id: str) -> list[dict]:
    """The user's trusted devices with their metadata, newest last. Reads the doc
    directly rather than the cache — a management screen should show the truth,
    and it is called far too rarely to need one. Devices trusted before metadata
    existed have added_at=None; the id itself is still listed and removable."""
    snap = _sessions_col().document(user_id).get()
    if not snap.exists:
        return []
    d = snap.to_dict() or {}
    meta = d.get("device_meta", {}) or {}
    return [
        {"device_id": did, "added_at": (meta.get(did) or {}).get("added_at")}
        for did in (d.get("allowed_devices", []) or [])
    ]


def check_device(user_id: str, device_id: str) -> str:
    """Return "ok" if the device is trusted for the user, else "unknown".

    An account with no bound device yet (a legacy pre-binding session) is
    treated as an unknown device: the caller triggers the DM-approval flow,
    so the owner confirms their device once rather than any holder of the
    token binding one silently. After that, only known ids pass.
    """
    allowed = _get_allowed_devices(user_id)
    if allowed is None:
        # Trust set unknowable (Firestore outage, nothing cached). Fail open for
        # THIS request only — the same call the suspension gate makes, for the
        # same reason: an outage that blocked every player would be worse than a
        # stolen token getting extra minutes. Crucially: adopt nothing and cache
        # nothing, so no permanent trust is ever granted on a failed read.
        return "ok"
    if not allowed:
        # Legacy account with no bound device yet (a session predating binding).
        # Do NOT silently adopt the first id seen: a stolen token would bind the
        # thief's device with no approval. Route it through the same DM-approval
        # flow as any new device — the real owner approves once, while an
        # attacker's attempt DMs the victim (who rejects it). Return "unknown"
        # WITHOUT caching, so nothing is trusted on the strength of the request.
        return "unknown"
    if not device_id:
        return "unknown"
    return "ok" if device_id in allowed else "unknown"


def _device_chal_id(user_id: str, device_id: str) -> str:
    """Deterministic challenge id per (user, device) so repeated requests from one
    blocked device reuse a single challenge instead of spamming DMs."""
    return "dev_" + hashlib.sha256(f"{user_id}:{device_id}".encode()).hexdigest()[:24]


def create_device_challenge(guild_id: str, user_id: str, username: str,
                            device_id: str, client_ip: str = "") -> tuple[str, bool]:
    """Get-or-create a pending challenge for an unrecognized (user, device).
    Returns (challenge_id, created); created=False means a live challenge already
    existed, so the caller must NOT send another DM."""
    cid = _device_chal_id(user_id, device_id)
    doc = _device_chal_col().document(cid)
    snap = doc.get()
    if snap.exists:
        data = snap.to_dict()
        if data.get("status") == "pending" and time.time() <= data.get("expires_at", 0):
            return cid, False
    doc.set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "device_id": device_id,
        "client_ip": client_ip,
        "status": "pending",
        "report_requested": False,
        "report_id": None,
        "report_done": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + DEVICE_CHALLENGE_LIFETIME,
    })
    log.info("Created device challenge for user %s (device %s…)", user_id, (device_id or "?")[:8])
    return cid, True


def resolve_device_challenge(challenge_id: str, acting_user_id: str,
                             approve: bool) -> dict | None:
    """Apply the Discord button decision (owner-checked). Approve → trust the
    device. Reject → mark denied and flag a moderation report. Returns the
    challenge data (so the caller can open the ticket on reject), or None."""
    doc = _device_chal_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    if not _owns_challenge(data.get("user_id"), acting_user_id):
        log.warning("Device challenge %s: acting user %s is not owner %s, ignored",
                    challenge_id, acting_user_id, data.get("user_id"))
        return None
    if data.get("status") != "pending":
        return None

    if approve:
        add_allowed_device(data["user_id"], data["device_id"])
        doc.update({"status": "approved"})
        data["status"] = "approved"
        log.info("Device challenge %s approved by user %s", challenge_id, acting_user_id)
        return data

    report_id = secrets.token_urlsafe(12)
    doc.update({"status": "denied", "report_requested": True, "report_id": report_id})
    data.update({"status": "denied", "report_requested": True, "report_id": report_id})
    log.info("Device challenge %s rejected by user %s (report %s)",
             challenge_id, acting_user_id, report_id)
    return data


def request_device_ping(challenge_id: str, acting_user_id: str) -> bool:
    """Owner presses '🔔 Ping this PC' in the DM. Flag the pending challenge so the
    blocked client surfaces an on-screen alert on its next poll — letting the owner
    confirm the machine is in front of them before reporting. Owner-checked."""
    doc = _device_chal_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return False
    data = snap.to_dict()
    if not _owns_challenge(data.get("user_id"), acting_user_id):
        return False
    if data.get("status") != "pending" or time.time() > data.get("expires_at", 0):
        return False
    doc.update({"ping_pending": True, "ping_requested_at": time.time()})
    log.info("Device challenge %s ping requested by user %s", challenge_id, acting_user_id)
    return True


def set_device_ticket_channel(challenge_id: str, channel_id: int | str) -> None:
    """Remember which ticket channel a device report opened, so the diagnostics
    follow-up (KSP.log) lands in the same ticket rather than a shared channel."""
    try:
        _device_chal_col().document(challenge_id).update(
            {"ticket_channel_id": str(channel_id)})
    except Exception as exc:
        log.warning("Could not set ticket channel for challenge %s: %s", challenge_id, exc)


def poll_device_challenge(challenge_id: str, owner_id: str | None = None) -> dict:
    """For the blocked KSP client. Returns:
       {"state":"pending", "ping"?} | {"state":"approved"} (consumes) |
       {"state":"denied", "report_id"?} | {"state":"expired"}.

    `owner_id` is the caller's account id. When given, a challenge belonging to
    somebody else is answered "expired" rather than served: every sibling endpoint
    checks this and this one did not, so any token holder who learned a challenge
    id could consume its `approved` state (spending the challenge and leaving the
    real owner's client polling forever), swallow the owner's "Ping this PC" —
    the one interactive check the approval DM offers — and read its `report_id`.
    Answering "expired" rather than 403 keeps the endpoint from confirming that
    an id someone guessed exists at all."""
    doc = _device_chal_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return {"state": "expired"}
    data = snap.to_dict()
    if owner_id is not None and str(data.get("user_id") or "") != str(owner_id):
        return {"state": "expired"}
    status = data.get("status", "pending")
    if status == "pending":
        if time.time() > data.get("expires_at", 0):
            # Delete rather than merely report. This branch used to return and leave the
            # document in place forever, and the document carries `client_ip` — which,
            # with uvicorn's access log off and Caddy configured without one, makes this
            # collection the only durable store of IP addresses in the whole system. The
            # privacy policy describes IP retention as being for "connection integrity,
            # rate limiting and abuse prevention", which does not describe keeping one
            # indefinitely against an account id and username. `poll_approval` already
            # deletes on expiry and on every terminal state; this is the same rule.
            doc.delete()
            return {"state": "expired"}
        out = {"state": "pending"}
        if data.get("ping_pending"):
            # Consume the ping so it fires once per button press.
            doc.update({"ping_pending": False})
            out["ping"] = True
        return out
    if status == "approved":
        doc.delete()  # device is trusted now; the challenge is spent
        return {"state": "approved"}
    out = {"state": "denied"}
    if data.get("report_requested") and not data.get("report_done"):
        out["report_id"] = data.get("report_id")
    else:
        # A denied challenge with nothing outstanding has served its purpose. The row is
        # kept (it is the record of a refusal a moderator may ask about) but the address
        # is not: once the diagnostics upload is done or was never asked for, `client_ip`
        # is evidence with no remaining use, and the ticket holds whatever was needed.
        if data.get("client_ip"):
            try:
                doc.update({"client_ip": firestore.DELETE_FIELD})
            except Exception as exc:  # noqa: BLE001 - best effort, never fail the poll
                log.debug("Could not clear client_ip on challenge %s: %s", challenge_id, exc)
    return out


def get_report_target(report_id: str) -> dict | None:
    """Find a denied challenge still awaiting its diagnostics upload."""
    if not report_id:
        return None
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = _device_chal_col().where(filter=FieldFilter("report_id", "==", report_id)).limit(1)
        for doc in q.stream():
            d = doc.to_dict()
            if d.get("report_requested") and not d.get("report_done"):
                d["_doc_id"] = doc.id
                return d
    except Exception as exc:
        log.warning("get_report_target(%s) failed: %s", report_id, exc)
    return None


def mark_report_done(challenge_doc_id: str) -> None:
    try:
        _device_chal_col().document(challenge_doc_id).update({"report_done": True})
    except Exception as exc:
        log.warning("Could not mark report done for %s: %s", challenge_doc_id, exc)


# ── Data erasure (user "delete my data") ─────────────────────────────────────

def purge_ksp_user_data(user_id: str) -> None:
    """Erase all KSP auth/security records tied to a user: session + device
    bindings, and any outstanding link codes and login/device challenges. Also
    clears the in-process caches so nothing lingers. Best-effort and idempotent."""
    uid = str(user_id)

    # Don't just delete the session doc: that resets token_version to 0, which
    # would leave existing tokens valid (and let a stray request re-bind the
    # device). Instead overwrite it with a minimal, non-identifying tombstone that
    # BUMPS the version — invalidating every issued token (each device drops to
    # unlinked on its next request) — while dropping username/guild/devices.
    try:
        snap = _sessions_col().document(uid).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        cur = int(data.get("token_version", 0) or 0)
        new_version = cur + 1
        # The web counter is carried across the tombstone rather than dropped. The
        # global bump already retires every web token, so this changes no decision —
        # but a counter that silently restarts at 0 is the one shape this whole
        # mechanism cannot afford, and `set()` with no merge would restart it.
        new_web = int(data.get("web_token_version", 0) or 0)
        _sessions_col().document(uid).set({   # no merge → strips all other fields
            "token_version": new_version,
            "web_token_version": new_web,
            "active": False,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        })
        _token_versions[uid] = (new_version, new_web, time.time())
    except Exception as exc:
        log.warning("purge: could not reset session for %s: %s", uid, exc)

    # Any docs across the auth collections that carry this user_id.
    for col in (_link_codes_col(), _twofa_col(), _device_chal_col()):
        try:
            for doc in col.where("user_id", "==", uid).stream():
                doc.reference.delete()
        except Exception as exc:
            log.warning("purge: cleanup in %s failed for %s: %s", col.id, uid, exc)

    # Drop cached trust so a subsequent request can't read stale allowed-devices.
    _allowed_devices.pop(uid, None)
    log.warning("Purged KSP auth/security data for user %s", uid)


# ── Session Tokens ───────────────────────────────────────────────────────────
#
# Key rotation: tokens are signed with the CURRENT secret but verified against an
# accept list (current + optionally the previous secret, see config's
# API_SECRET_KEY_PREVIOUS). That turns rotating the signing key from a mass
# force-unlink into a non-event: move the old key to _PREVIOUS, set a new current
# key, restart — existing tokens keep verifying for up to TOKEN_LIFETIME while
# every new token is minted under the new key; drop _PREVIOUS after 30 days.
# Each token carries a `kid` (short non-reversible hash of its signing key) for
# observability only — verification never trusts it, it just names which key a
# logged token was minted under.


def key_id(secret: str) -> str:
    """Short non-reversible identifier for a signing key: first 8 hex chars of
    its SHA-256. Safe to embed in tokens/logs — it identifies the key without
    revealing anything usable about it."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def _sign_token(payload: dict, secret: str) -> str:
    """Create an HMAC-SHA256 signed token from a payload dict."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    # Token format: base64(payload).signature
    import base64
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"{encoded}.{sig}"


def _verify_token(token: str, secret: str) -> dict | None:
    """Verify an HMAC-signed token. Returns payload dict or None."""
    import base64
    parts = token.split(".")
    if len(parts) != 2:
        return None

    encoded, sig = parts
    try:
        raw = base64.urlsafe_b64decode(encoded).decode()
    except Exception:
        return None

    expected_sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    payload = json.loads(raw)

    # Check expiry
    if time.time() > payload.get("exp", 0):
        return None

    return payload


def build_session_payload(guild_id: str, user_id: str, username: str, secret: str,
                          version: int, aud: str = AUD_KSP, now: float | None = None,
                          web_version: int = 0) -> dict:
    """The claims a session token carries. Pure, so it can be tested without
    Firestore; `create_session_token` is the only production caller."""
    now = time.time() if now is None else now
    payload = {
        "gid": guild_id,
        "uid": user_id,
        "usr": username,
        "iat": int(now),
        "exp": int(now + TOKEN_LIFETIME),
        "tv": version,
        "aud": aud,
        "kid": key_id(secret),  # which key minted this — observability only
    }
    # Only a web token carries `wtv`, because only a web token is ever checked
    # against the web counter. A KSP payload stays exactly what it was.
    if aud == AUD_WEB:
        payload["wtv"] = web_version
    return payload


def create_session_token(guild_id: str, user_id: str, username: str, secret: str,
                         aud: str = AUD_KSP) -> str:
    """Create a signed session token and store session in Firestore.

    `aud` names the surface the token is for (AUD_KSP / AUD_WEB); see the note
    above the constants."""
    # Mint the token at the user's current version. A fresh login does NOT bump
    # the version — only logout_all_devices does — so logging in on a new device
    # never invalidates the user's other devices.
    version, web_version = _get_versions(user_id)
    payload = build_session_payload(guild_id, user_id, username, secret, version, aud,
                                    web_version=web_version)
    token = _sign_token(payload, secret)

    # Store session reference in Firestore. merge=True preserves token_version if
    # a previous logout_all_devices already set it for this user.
    record = {
        "guild_id": guild_id,
        "user_id": user_id,
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": payload["exp"],
        "token_version": version,
        "active": True,
    }
    # The KSP guild, kept apart from `guild_id` and written only by a KSP mint.
    #
    # There is one session document per account, shared by both audiences, so the
    # *last* login wins `guild_id` — and a website sign-in always mints under the
    # home guild (`_account_guild_id`). A player who linked KSP in another guild
    # and later signed in on the site therefore left a record claiming the home
    # guild, while their game client goes on polling the guild it linked in. That
    # matters because the notification feed and the craft import queue are both
    # keyed by guild: anything written for them off this record would land where
    # they never look, and for a quicksent live vessel a stranded return entry
    # means the ship is in neither save. `merge=True` is what makes this survive
    # every later web login, so it stays the answer to "where does their game
    # read" rather than "where did they last sign in".
    if aud == AUD_KSP:
        record["ksp_guild_id"] = guild_id
    _sessions_col().document(user_id).set(record, merge=True)

    log.info("Created session token for user %s (guild %s)", user_id, guild_id)
    return token


def verify_session_token(token: str, secret: "str | list[str] | tuple") -> dict | None:
    """
    Verify a session token.
    Returns {guild_id, user_id, username} or None.

    `secret` may be a single key or an accept list (current key first, then the
    previous one during a rotation window — see the section comment above). The
    signature is tried against each; the embedded `kid` is never trusted to pick.
    """
    secrets_list = [secret] if isinstance(secret, str) else [s for s in secret if s]
    payload = None
    for s in secrets_list:
        payload = _verify_token(token, s)
        if payload is not None:
            break
    if payload is None:
        return None

    # Reject tokens minted before the user's last "log out of all devices".
    # Legacy tokens predating versioning carry no "tv" (treated as 0) and stay
    # valid until the first logout_all_devices bumps the version above 0.
    # `TokenVersionUnavailable` is deliberately NOT caught here: it means we cannot
    # tell whether this token was revoked, which is neither "valid" (None would be
    # wrong) nor "invalid" (the client would clear a good session on the 401). The
    # dependency turns it into a 503 the client retries.
    version, web_version = _get_versions(payload["uid"])
    if int(payload.get("tv", 0)) < version:
        return None

    # A website token is additionally checked against the web-only counter, which the
    # site's own "Log out" bumps. A KSP token never looks at it — that is the whole
    # point of the split (see the note above `_TOKEN_VERSION_TTL`). A web token minted
    # before this field existed carries no `wtv` and so reads as 0, which the first web
    # sign-out after the upgrade retires — exactly what a sign-out is for.
    if payload.get("aud") == AUD_WEB and int(payload.get("wtv", 0)) < web_version:
        return None

    return {
        "guild_id": payload["gid"],
        "user_id": payload["uid"],
        "username": payload["usr"],
        # None for tokens minted before audiences existed (accepted on both tiers
        # until they expire); AUD_KSP / AUD_WEB otherwise.
        "aud": payload.get("aud"),
    }


def logout_all_devices(user_id: str) -> int:
    """Log the user out of every device by bumping their token version.

    All session tokens ever issued to this user — including the one that made
    this request — fail verification on their next API call and the KSP client
    drops to its unlinked state (it clears the token on any 401). Returns the new
    token version. This is the user's own privacy control, not an admin action.
    """
    doc = _sessions_col().document(user_id)
    snap = doc.get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    current = int(data.get("token_version", 0) or 0)
    web_current = int(data.get("web_token_version", 0) or 0)
    new_version = current + 1

    doc.set({
        "token_version": new_version,
        "active": False,
        "logged_out_all_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    # Update the in-process cache so revocation is effective immediately. The web
    # counter is carried through unchanged: bumping the global one already retires
    # every web token, and moving both would make the two counters meaningless as a
    # record of which surface asked for what.
    _token_versions[user_id] = (new_version, web_current, time.time())

    log.info("User %s logged out of all devices (token version → %d)", user_id, new_version)
    return new_version


def logout_web_devices(user_id: str) -> int:
    """Sign the user out of the WEBSITE only, leaving KSP clients linked.

    Bumps the web counter, so every AUD_WEB token — this browser and any other —
    fails its next call, while the account's KSP `session.token`s are untouched and
    the game keeps playing. This is what the site's ordinary "Log out" calls.

    Returns the new web token version. Like `logout_all_devices` this is the user's
    own control, not an admin action.
    """
    doc = _sessions_col().document(user_id)
    snap = doc.get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    current = int(data.get("token_version", 0) or 0)
    web_current = int(data.get("web_token_version", 0) or 0)
    new_web = web_current + 1

    # `active` is deliberately NOT touched. It answers "has this account ever linked
    # and not logged out everywhere" (see `linked_user_ids`), and a browser sign-out
    # is not an answer to that — the KSP client it describes is still linked.
    doc.set({
        "web_token_version": new_web,
        "logged_out_web_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    _token_versions[user_id] = (current, new_web, time.time())

    log.info("User %s signed out of the website (web token version → %d)", user_id, new_web)
    return new_web


def linked_user_ids() -> set:
    """Every user id that has ever completed a link and not logged out everywhere.

    One stream of `ksp_sessions` rather than a read per user, because the caller
    (`/admin corpsgenerate`) asks the question once for a whole guild's member
    list. Expiry is deliberately not consulted: a session that lapsed still
    happened, and the fact this answers is "has this player accepted the in-mod
    terms", which does not expire with a token.
    """
    ids = set()
    try:
        for doc in _sessions_col().stream():
            d = doc.to_dict() or {}
            if d.get("active"):
                ids.add(str(d.get("user_id") or doc.id))
    except Exception as exc:
        log.warning("Could not list linked users: %s", exc)
    return ids


def get_linked_guild(user_id: str) -> str | None:
    """The guild this user's live session was linked through, or None.

    Feeds the ban→revoke hook (cogs/ksp_bridge.on_member_ban): a ban should end
    API access only when the banned guild is the one the session's authority
    came from — being banned from some unrelated server the bot also happens to
    be in must not log a player out of their own community."""
    try:
        snap = _sessions_col().document(str(user_id)).get()
        if snap.exists:
            d = snap.to_dict() or {}
            if d.get("active"):
                gid = str(d.get("guild_id") or "")
                return gid or None
    except Exception as exc:
        log.warning("Could not read linked guild for %s: %s", user_id, exc)
    return None
