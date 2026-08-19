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

from data.store import _db

log = logging.getLogger(__name__)

# Token lifetime: 30 days
TOKEN_LIFETIME = 30 * 24 * 3600

# Link code / login-approval challenge lifetime: 3 minutes
LINK_CODE_LIFETIME = 180
APPROVAL_LIFETIME = 180

# Device-approval challenge lifetime: 1 hour. Longer than a login approval — a
# blocked device re-prompts the user if the DM is missed, so a short window would
# just nag. The device stays blocked until approved regardless.
DEVICE_CHALLENGE_LIFETIME = 3600


def _link_codes_col():
    return _db.collection("ksp_link_codes")


def _sessions_col():
    return _db.collection("ksp_sessions")


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

_TOKEN_VERSION_TTL = 30  # seconds
_token_versions: dict[str, tuple[int, float]] = {}  # user_id -> (version, fetched_at)


def _get_token_version(user_id: str) -> int:
    """Current token-revocation version for a user (0 if never revoked)."""
    cached = _token_versions.get(user_id)
    now = time.time()
    if cached is not None and now - cached[1] < _TOKEN_VERSION_TTL:
        return cached[0]

    try:
        snap = _sessions_col().document(user_id).get()
        version = int(snap.to_dict().get("token_version", 0) or 0) if snap.exists else 0
    except Exception as exc:
        log.warning("Could not read token version for %s: %s", user_id, exc)
        # Read failed: return the last known value (a revocation already cached still
        # applies), but do NOT cache this guess. Caching it would pin a possibly-stale
        # version for the whole TTL — so a Firestore blip at the wrong moment could
        # keep accepting a just-revoked token even after Firestore recovers. Not
        # caching means the very next request re-reads and picks up the real version.
        return cached[0] if cached is not None else 0

    _token_versions[user_id] = (version, now)
    return version


# ── Link Codes ───────────────────────────────────────────────────────────────

def generate_link_code(guild_id: int, user_id: int, username: str) -> str:
    """Create a 6-digit code, store in Firestore with 10-min expiry. Returns the code."""
    # Invalidate any existing codes for this user
    for doc in _link_codes_col().where("user_id", "==", str(user_id)).stream():
        doc.reference.delete()

    code = _digit_code(6)
    _link_codes_col().document(code).set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + LINK_CODE_LIFETIME,  # 3 minutes
    })
    log.info("Generated link code %s for user %s (%s)", code, user_id, username)
    return code


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
    log.info("Validated link code %s for user %s", code, data["user_id"])
    return {
        "guild_id": data["guild_id"],
        "user_id": data["user_id"],
        "username": data["username"],
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
                              client_ip: str = "") -> str:
    """Create a pending login-approval challenge. Returns the challenge_id."""
    challenge_id = secrets.token_urlsafe(18)
    _twofa_col().document(challenge_id).set({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "username": username,
        "status": "pending",
        "client_ip": client_ip,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + APPROVAL_LIFETIME,
    })
    log.info("Created login-approval challenge for user %s", user_id)
    return challenge_id


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
    if str(data.get("user_id")) != str(acting_user_id):
        log.warning("Approval %s: acting user %s is not owner %s — ignored",
                    challenge_id, acting_user_id, data.get("user_id"))
        return False
    if data.get("status") != "pending" or time.time() > data.get("expires_at", 0):
        return False

    doc.update({"status": "approved" if approve else "denied"})
    log.info("Login-approval challenge %s %s by user %s",
             challenge_id, "approved" if approve else "denied", acting_user_id)
    return True


def poll_approval(challenge_id: str) -> dict:
    """Poll a challenge on behalf of the waiting KSP client.

    Returns one of:
      {"state": "pending"}
      {"state": "approved", "guild_id", "user_id", "username"}  (consumes it)
      {"state": "denied"}    (consumes it)
      {"state": "expired"}   (unknown / timed-out)
    """
    doc = _twofa_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return {"state": "expired"}

    data = snap.to_dict()
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
# The id is a random GUID — not a MAC — so it stores no personal data and survives
# MAC rotation. The real MAC / IP / KSP.log are gathered only into a user-filed
# moderation report, never into the binding itself.

_ALLOWED_DEV_TTL = 30  # seconds — cache trusted-device sets to keep checks cheap
_allowed_devices: dict[str, tuple[set, float]] = {}  # user_id -> (devices, fetched_at)


def _device_chal_col():
    return _db.collection("ksp_device_challenges")


def _get_allowed_devices(user_id: str) -> set:
    cached = _allowed_devices.get(user_id)
    now = time.time()
    if cached is not None and now - cached[1] < _ALLOWED_DEV_TTL:
        return cached[0]
    devices: set = set()
    try:
        snap = _sessions_col().document(user_id).get()
        if snap.exists:
            devices = set(snap.to_dict().get("allowed_devices", []) or [])
    except Exception as exc:
        log.warning("Could not read allowed devices for %s: %s", user_id, exc)
        if cached is not None:
            return cached[0]
    _allowed_devices[user_id] = (devices, now)
    return devices


def add_allowed_device(user_id: str, device_id: str) -> None:
    """Trust a device for this user (idempotent). Updates the in-process cache so
    the device's very next request passes without waiting for the cache TTL."""
    if not device_id:
        return
    _sessions_col().document(user_id).set(
        {"allowed_devices": firestore.ArrayUnion([device_id])}, merge=True)
    self_set = set(_get_allowed_devices(user_id)) | {device_id}
    _allowed_devices[user_id] = (self_set, time.time())
    log.info("Device %s… trusted for user %s", device_id[:8], user_id)


def check_device(user_id: str, device_id: str) -> str:
    """Return "ok" if the device is trusted for the user, else "unknown".

    Trust-on-first-use: if the account has no bound device yet (a session created
    before binding existed), the first device id seen is adopted so existing users
    aren't locked out by the rollout. After that, only known ids pass.
    """
    allowed = _get_allowed_devices(user_id)
    if not allowed:
        if device_id:
            add_allowed_device(user_id, device_id)
        return "ok"
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
    if str(data.get("user_id")) != str(acting_user_id):
        log.warning("Device challenge %s: acting user %s is not owner %s — ignored",
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
    if str(data.get("user_id")) != str(acting_user_id):
        return False
    if data.get("status") != "pending" or time.time() > data.get("expires_at", 0):
        return False
    doc.update({"ping_pending": True, "ping_requested_at": time.time()})
    log.info("Device challenge %s ping requested by user %s", challenge_id, acting_user_id)
    return True


def set_device_ticket_channel(challenge_id: str, channel_id: int | str) -> None:
    """Remember which ticket channel a device report opened, so the diagnostics
    follow-up (MAC + KSP.log) lands in the same ticket rather than a shared channel."""
    try:
        _device_chal_col().document(challenge_id).update(
            {"ticket_channel_id": str(channel_id)})
    except Exception as exc:
        log.warning("Could not set ticket channel for challenge %s: %s", challenge_id, exc)


def poll_device_challenge(challenge_id: str) -> dict:
    """For the blocked KSP client. Returns:
       {"state":"pending", "ping"?} | {"state":"approved"} (consumes) |
       {"state":"denied", "report_id"?} | {"state":"expired"}."""
    doc = _device_chal_col().document(challenge_id)
    snap = doc.get()
    if not snap.exists:
        return {"state": "expired"}
    data = snap.to_dict()
    status = data.get("status", "pending")
    if status == "pending":
        if time.time() > data.get("expires_at", 0):
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
        cur = int(snap.to_dict().get("token_version", 0) or 0) if snap.exists else 0
        new_version = cur + 1
        _sessions_col().document(uid).set({   # no merge → strips all other fields
            "token_version": new_version,
            "active": False,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        })
        _token_versions[uid] = (new_version, time.time())
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


def create_session_token(guild_id: str, user_id: str, username: str, secret: str) -> str:
    """Create a signed session token and store session in Firestore."""
    now = time.time()
    # Mint the token at the user's current version. A fresh login does NOT bump
    # the version — only logout_all_devices does — so logging in on a new device
    # never invalidates the user's other devices.
    version = _get_token_version(user_id)
    payload = {
        "gid": guild_id,
        "uid": user_id,
        "usr": username,
        "iat": int(now),
        "exp": int(now + TOKEN_LIFETIME),
        "tv": version,
    }
    token = _sign_token(payload, secret)

    # Store session reference in Firestore. merge=True preserves token_version if
    # a previous logout_all_devices already set it for this user.
    _sessions_col().document(user_id).set({
        "guild_id": guild_id,
        "user_id": user_id,
        "username": username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": payload["exp"],
        "token_version": version,
        "active": True,
    }, merge=True)

    log.info("Created session token for user %s (guild %s)", user_id, guild_id)
    return token


def verify_session_token(token: str, secret: str) -> dict | None:
    """
    Verify a session token.
    Returns {guild_id, user_id, username} or None.
    """
    payload = _verify_token(token, secret)
    if payload is None:
        return None

    # Reject tokens minted before the user's last "log out of all devices".
    # Legacy tokens predating versioning carry no "tv" (treated as 0) and stay
    # valid until the first logout_all_devices bumps the version above 0.
    if int(payload.get("tv", 0)) < _get_token_version(payload["uid"]):
        return None

    return {
        "guild_id": payload["gid"],
        "user_id": payload["uid"],
        "username": payload["usr"],
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
    current = 0
    if snap.exists:
        current = int(snap.to_dict().get("token_version", 0) or 0)
    new_version = current + 1

    doc.set({
        "token_version": new_version,
        "active": False,
        "logged_out_all_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    # Update the in-process cache so revocation is effective immediately.
    _token_versions[user_id] = (new_version, time.time())

    log.info("User %s logged out of all devices (token version → %d)", user_id, new_version)
    return new_version
