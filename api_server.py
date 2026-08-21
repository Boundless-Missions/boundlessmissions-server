"""
api_server.py – FastAPI REST API for KSP mod ↔ Discord bot bridge.

Runs inside the bot process via uvicorn. All endpoints require a valid
session token (Authorization: Bearer <token>) except /auth/link.

No API keys, Firebase creds, or secrets are exposed to clients.
"""

import asyncio
import hashlib
import io
import logging
import math
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import (
    FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Query, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from firebase_admin import firestore
from pydantic import BaseModel

import settings
from config import cfg
from cost_guard import FirebaseBudgetExceeded, guard as cost_guard
from api_auth import (
    validate_link_code, create_session_token, verify_session_token,
    logout_all_devices, create_approval_challenge, resolve_approval, poll_approval,
    add_allowed_device, check_device, create_device_challenge,
    poll_device_challenge, get_report_target, mark_report_done,
    remove_allowed_device, list_devices, purge_all_link_codes,
)
from api_models import (
    LinkRequest, LinkResponse, PollRequest, DeviceStatusResponse,
    UserProfile,
    WeeklyMissionsResponse, Mission, MissionSelectRequest, MissionSelectResponse,
    ContractSummary, ContractListResponse, ContractAcceptResponse, PendingRequest,
    PartCatalogUpload, PartCatalogResponse,
    CorpInfo, CorpListResponse, ContractCreateRequest, AuctionCreateRequest, ContractReviewRequest,
    ContractDisputeRequest, ContractRequestResponse, RescueTarget,
    GameCommandRequest, GameCommandResult,
    SubmissionResult, FlightSubmission, VesselSnapshot,
    Notification, NotificationsResponse,
    MarketplaceListResult, MarketplaceListing, MarketplaceListingsResponse,
    MarketplaceListingsPage, WebBuyResult, CraftCompatibility,
    VoteRequest, VoteResult, MyVotesResponse, ReportRequest, ReportResult,
    WebAuction, WebAuctionListResponse, WebAuctionBidRequest,
    VersionCheckResponse,
    AttestChallenge, AttestRespondRequest, AttestResult,
)
from data.store import store, _db, _storage_bucket, sign_stored, SIGNED_URL_MAX_TTL
from data import contracts as cdb
from data import guild_config
from data import mod_version as mver
from data import policy as policy
from data import suspensions
from data import suspicion as susp
from data import telemetry_check as tcheck
from data import mission_constraints as mc
from data import orbit_constraints as oc
from data import part_resolver as pr
from data import marketplace as mkt
from data import imports as imp
from data import auctions as aucdb
# The contract state machine, shared with the Discord buttons. `contract_actions`
# late-imports this module back (for the notification hub and the rescue helpers), so
# the cycle is resolved at call time, not import time.
import contract_actions as ca

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=3))

# ── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Boundless Missions KSP Bridge API",
    version="1.0.0",
    # Docs + schema are off unless explicitly enabled (dev only) — otherwise the
    # whole API surface is enumerable by anyone.
    docs_url="/api/docs" if cfg.API_DOCS_ENABLED else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if cfg.API_DOCS_ENABLED else None,
)

# CORS only when an explicit browser origin list is configured. The KSP client is
# not a browser, so by default no cross-origin headers are served (no wildcard).
if cfg.API_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.API_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(FirebaseBudgetExceeded)
async def _budget_exceeded_handler(request: Request, exc: FirebaseBudgetExceeded):
    """A spending stop is a service state, not a bug.

    Without this every Firestore call made while the cost guard is frozen
    surfaces to the KSP client and the website as an opaque 500, which reads as
    "the server is broken" and invites exactly the retry storm that makes the
    spend worse. 503 + Retry-After says the right thing to both: the mod's error
    handling treats it as a transient outage, and the message explains itself.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "reason": "budget_exceeded"},
        # The freeze lifts on the 1st, but a client should not be told to sleep
        # for days — an hour is long enough to stop hammering and short enough
        # to pick up a budget the owner raises by hand.
        headers={"Retry-After": "3600"},
    )


# ── Upload limits (DoS / decompression-bomb defense) ─────────────────────────
#
# The API runs in-process with the Discord bot, so an unbounded upload or a gzip
# bomb takes the whole bot down via memory exhaustion. Every uploaded file is read
# through _read_upload (hard byte cap) and every gzip payload through _safe_gunzip
# (hard *decompressed* cap), and a cheap Content-Length check rejects oversized
# request bodies before Starlette buffers/spools them.

MAX_UPLOAD_BYTES = 25 * 1024 * 1024          # per uploaded file (craft, screenshot, …)
MAX_LOG_BYTES = 60 * 1024 * 1024             # KSP.log diagnostics can be large
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024    # cap on any single gzip expansion
MAX_REQUEST_BYTES = 80 * 1024 * 1024         # whole-request guard (a submission carries several files)
_LOG_HEAD_BYTES = 2_000_000                  # KSP.log: keep the first 2 MB (mod list, system specs)
_LOG_TAIL_BYTES = 7_000_000                  # KSP.log: keep the last 7 MB (most recent events)

# ── Blueprint/screenshot cap, derived from the mod's render scale ─────────────
# A blueprint is a deterministic 2048×1100 px image multiplied by BLUEPRINT_SCALE
# (see settings.py / VesselRenderer.cs). Its byte size therefore grows with the
# scale *squared*. We budget a generous per-pixel allowance — far above what a
# real (mostly-flat) blueprint or even a noisy full-res game screenshot encodes
# to — and cap there, plus a small fixed floor for headers. This is much tighter
# than the generic 25 MB cap, so a tampered client can't pad renders huge and
# spray oversized uploads at the API. Auto-tracks the scale: bump BLUEPRINT_SCALE
# and the ceiling rises with it, no separate constant to edit.
_BLUEPRINT_BASE_W = 2048
_BLUEPRINT_BASE_H = 1100
_BLUEPRINT_BYTES_PER_PX = 1.5                # worst-case legitimate PNG budget
MAX_BLUEPRINT_BYTES = 512 * 1024 + int(
    _BLUEPRINT_BASE_W * _BLUEPRINT_BASE_H
    * (settings.BLUEPRINT_SCALE ** 2) * _BLUEPRINT_BYTES_PER_PX
)


@app.middleware("http")
async def _limit_request_size(request: Request, call_next):
    """Reject an over-large request body up front (when Content-Length is present)
    so a huge multipart upload isn't buffered/spooled before a handler runs."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_REQUEST_BYTES:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=413, content={"detail": "Request body too large."})
    return await call_next(request)


async def _read_upload(f: UploadFile, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an UploadFile fully but abort past `limit` bytes (413), so one client
    can't exhaust memory with a giant upload. Reads in 1 MiB chunks."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await f.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="Uploaded file is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _trim_log(log_bytes: bytes | None) -> bytes | None:
    """Cap a KSP.log to a Discord-friendly attachment size. Keeps both ends: the
    head carries the loaded-assembly/mod list and system specs, the tail carries the
    most recent events. Only the middle is dropped, and the cut is marked.

    The KSP client already trims to the same head/tail before uploading, so this is
    normally a no-op — it stays as the backstop for a client that didn't (an older
    one, or a device report, which uploads the whole file)."""
    if not log_bytes or len(log_bytes) <= _LOG_HEAD_BYTES + _LOG_TAIL_BYTES:
        return log_bytes
    head, tail = log_bytes[:_LOG_HEAD_BYTES], log_bytes[-_LOG_TAIL_BYTES:]
    dropped = len(log_bytes) - _LOG_HEAD_BYTES - _LOG_TAIL_BYTES
    marker = (f"\n\n... [GeneKerman: {dropped:,} bytes of log omitted "
              f"between the first {_LOG_HEAD_BYTES // 1_000_000} MB and "
              f"last {_LOG_TAIL_BYTES // 1_000_000} MB] ...\n\n").encode("utf-8")
    return head + marker + tail


def _safe_gunzip(raw: bytes, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """gzip-decompress with a hard cap on the *decompressed* size, defusing a
    decompression bomb (a few KB that expands to gigabytes). Reads at most
    `limit`+1 bytes of output, so memory stays bounded regardless of the input.

    Raises HTTPException(413) past the cap. Propagates (OSError, EOFError) for
    non-gzip input so callers can keep their existing 'fall back to raw bytes'
    behavior for payloads that weren't actually compressed."""
    import gzip
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        out = gz.read(limit + 1)
    if len(out) > limit:
        raise HTTPException(status_code=413, detail="Decompressed payload is too large.")
    return out


# ── WebSocket Notification Hub ───────────────────────────────────────────────

class NotificationHub:
    """Tracks live WebSocket connections per (guild_id, user_id) and pushes
    notifications to them. All public methods are coroutines and must run on
    the server event loop."""

    def __init__(self):
        self._conns: dict[tuple[int, int], set[WebSocket]] = {}

    async def connect(self, gid: int, uid: int, ws: WebSocket):
        await ws.accept()
        self._conns.setdefault((gid, uid), set()).add(ws)
        log.info("WS: user %d (guild %d) connected (%d live)", uid, gid, len(self._conns[(gid, uid)]))

    def disconnect(self, gid: int, uid: int, ws: WebSocket):
        conns = self._conns.get((gid, uid))
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            self._conns.pop((gid, uid), None)

    async def push(self, gid: int, uid: int, payload: dict):
        conns = self._conns.get((gid, uid))
        if not conns:
            return
        dead = []
        for ws in list(conns):
            try:
                await ws.send_json({"type": "notification", "notification": payload})
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)

    async def push_frame(self, gid: int, uid: int, payload: dict) -> int:
        """Send a raw typed frame to one user's live clients. Returns how many
        received it.

        Deliberately separate from broadcast(), which is fleet-wide: a command is
        addressed to one account's running games and must never reach anyone else.
        Two KSP installs linked to the same account both get it, which is why the
        count is returned — the caller reports where it went.
        """
        conns = self._conns.get((gid, uid))
        if not conns:
            return 0
        delivered = 0
        dead = []
        for ws in list(conns):
            try:
                await ws.send_json(payload)
                delivered += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)
        return delivered

    async def broadcast(self, payload: dict):
        """Send a raw frame to every live connection (not per-user). Used for
        fleet-wide pokes like a published-version notice."""
        for conns in list(self._conns.values()):
            for ws in list(conns):
                try:
                    await ws.send_json(payload)
                except Exception:
                    conns.discard(ws)


_hub = NotificationHub()
_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_loop():
    """Capture the running event loop so the sync _create_notification helper can
    schedule pushes onto it from any context."""
    global _loop
    _loop = asyncio.get_running_loop()


def _push_notification(gid: int, uid: int, payload: dict):
    """Thread-safe fire-and-forget push of a notification to live sockets."""
    if _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_hub.push(gid, uid, payload), _loop)
    except Exception as exc:
        log.warning("WS: failed to schedule push for user %d: %s", uid, exc)


def broadcast_version_update():
    """Poke every connected KSP client to re-run its version check. Called after a
    new mod version is published so already-running clients gate live instead of
    only on their next restart. Safe no-op if the API server isn't up yet."""
    if _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_hub.broadcast({"type": "version"}), _loop)
        log.info("WS: broadcast version-update poke to live clients")
    except Exception as exc:
        log.warning("WS: failed to broadcast version update: %s", exc)


def broadcast_policy_update():
    """Poke every connected KSP client to re-fetch the policy version. Called after
    the Privacy Policy / Terms version is bumped so already-running clients raise
    the re-consent gate live instead of only on their next restart. Clients learn
    the new version from /version/check, so this just nudges them to re-check.
    Safe no-op if the API server isn't up yet."""
    if _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_hub.broadcast({"type": "policy"}), _loop)
        log.info("WS: broadcast policy-update poke to live clients")
    except Exception as exc:
        log.warning("WS: failed to broadcast policy update: %s", exc)


# ── Auth Dependency ──────────────────────────────────────────────────────────

def _get_api_secret() -> str:
    # config.py refuses to start with a blank/default secret when the KSP API is
    # enabled, so this is always a real key here. This is the SIGNING key — new
    # tokens are always minted under it.
    return cfg.API_SECRET_KEY


def _accept_secrets() -> list[str]:
    """Keys accepted when VERIFYING a token: the current signing key first, then
    the previous one while a rotation window is open (API_SECRET_KEY_PREVIOUS,
    already validated by config.py — never blank-as-key, never a placeholder,
    never a duplicate of the current key). Every verify call site uses this list
    so rotation can't be half-applied."""
    out = [cfg.API_SECRET_KEY]
    if cfg.API_SECRET_KEY_PREVIOUS:
        out.append(cfg.API_SECRET_KEY_PREVIOUS)
    return out


# ── Rate limiting (link / 2FA brute-force defense) ───────────────────────────
#
# The link and 2FA endpoints accept short numeric codes, so they're the only
# guessable attack surface. A simple in-memory sliding window (per-IP and a
# global cap) keeps an attacker from sweeping the code space within a code's
# 3-minute life. In-process is sufficient: the bot is a single process.

_RATE_BUCKETS: dict[str, list[float]] = {}
_last_bucket_sweep: float = 0.0


def _sweep_rate_buckets(now: float):
    """Drop buckets with no recent hits so the dict can't grow unboundedly with
    one entry per IP ever seen. Cheap: runs at most every 5 minutes."""
    global _last_bucket_sweep
    if now - _last_bucket_sweep < 300:
        return
    _last_bucket_sweep = now
    for k in list(_RATE_BUCKETS.keys()):
        recent = [t for t in _RATE_BUCKETS[k] if now - t < 120]
        if recent:
            _RATE_BUCKETS[k] = recent
        else:
            del _RATE_BUCKETS[k]
    # Same treatment for the per-user flood windows (defined later in the module).
    for k in list(_USER_FLOOD.keys()):
        recent = [t for t in _USER_FLOOD[k] if now - t < 120]
        if recent:
            _USER_FLOOD[k] = recent
        else:
            del _USER_FLOOD[k]
    for k in list(_USER_FLOOD_FLAGGED.keys()):
        if now - _USER_FLOOD_FLAGGED[k] > 3600:
            del _USER_FLOOD_FLAGGED[k]


def _rate_limit(key: str, max_hits: int, window: float):
    """Record a hit for `key`; raise 429 if it exceeds max_hits within window."""
    now = time.time()
    _sweep_rate_buckets(now)
    hits = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window]
    if len(hits) >= max_hits:
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a moment and try again.")
    hits.append(now)
    _RATE_BUCKETS[key] = hits


def _client_ip(request: Request) -> str:
    """The real client IP for rate limiting.

    X-Forwarded-For is honored ONLY when the request's direct peer is a
    configured trusted proxy — otherwise the header is attacker-controlled (each
    forged value would mint a fresh bucket and defeat per-IP limiting), so we use
    the raw socket peer. With trusted proxies set, walk the XFF chain from the
    right past any trusted hops; the first untrusted address is the client.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = cfg.API_TRUSTED_PROXIES
    if trusted and peer in trusted:
        chain = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for hop in reversed(chain):
            if hop not in trusted:
                return hop
    return peer


def _guard_link_attempt(request: Request):
    """Throttle link/2FA attempts: per-IP and globally."""
    ip = _client_ip(request)
    # Per-IP is the brute-force defense. The global cap is only a coarse backstop,
    # kept high (settings) so an attacker flooding the endpoint can't trip it and
    # lock every legitimate player out of linking (self-DoS). Both are configurable.
    _rate_limit(f"link:{ip}", max_hits=settings.KSP_LINK_RATELIMIT_PER_IP, window=60.0)
    _rate_limit("link:global", max_hits=settings.KSP_LINK_RATELIMIT_GLOBAL, window=60.0)


# ── Link-code sweep defense ──────────────────────────────────────────────────
#
# The per-IP limit caps one machine, but a distributed sweep of the 6-digit code
# space fits under the global cap — and with KSP_2FA_ENABLED=false a hit is a
# session token, not just an approval DM. Codes are free to regenerate (one
# /linkcode), so the cheap counter-move is to make sweeping self-defeating: past
# a failure threshold no honest community reaches, burn every outstanding code,
# leaving the sweep nothing to hit. Failed guesses are the one clean signal —
# legitimate users nearly always paste a code that exists.

_FAILED_LINK_GUESSES: list[float] = []
_LINK_SWEEP_WINDOW = 180.0          # one code lifetime (LINK_CODE_LIFETIME)
_LINK_SWEEP_MAX_FAILURES = 40       # far past typo volume, far below sweep volume


def _note_failed_link_guess() -> None:
    """Record a failed link-code guess; purge all outstanding codes when the
    volume says sweep rather than typo. In-process state, like _RATE_BUCKETS."""
    now = time.time()
    _FAILED_LINK_GUESSES[:] = [t for t in _FAILED_LINK_GUESSES
                               if now - t < _LINK_SWEEP_WINDOW]
    _FAILED_LINK_GUESSES.append(now)
    if len(_FAILED_LINK_GUESSES) >= _LINK_SWEEP_MAX_FAILURES:
        _FAILED_LINK_GUESSES.clear()
        n = purge_all_link_codes()
        log.warning("Link-code sweep suspected (%d failed guesses in %.0fs) — "
                    "purged %d outstanding link codes",
                    _LINK_SWEEP_MAX_FAILURES, _LINK_SWEEP_WINDOW, n)


async def get_user_allow_suspended(authorization: str = Header(default="")) -> dict:
    """Validate just the session token — no device gate, and no suspension gate.

    Two things must keep working while a player is suspended: reading their own
    suspension (or they are stuck at a wall with no text on it), and logging out
    of every device (their own privacy control, which a punishment must not take
    away). Nothing else uses this.

    The header is optional-with-default so a MISSING header is our 401, not
    FastAPI's 422 — the mod's session handling treats 401 as the single "this
    session is finished" signal, and every no-token shape must speak it."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = verify_session_token(authorization[7:], _accept_secrets())
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _suspension_detail(rec: dict) -> dict:
    """The 403 body a suspended caller gets. Structured rather than a sentence:
    the KSP client draws a gate window from it and the website renders `message`,
    so the expiry has to arrive as a number that can be counted down, not as prose
    that has to be parsed."""
    s = suspensions.summary(rec) or {}
    reason = s.get("reason") or ""
    return {
        "code": "suspended",
        "reason": reason,
        "until": s.get("until", 0),
        "until_iso": s.get("until_iso", ""),
        "message": ("Your access to Boundless Missions services is temporarily "
                    "suspended" + (f": {reason}" if reason else ".")),
    }


def enforce_not_suspended(user_id: str) -> None:
    """403 `suspended` while a suspension is running. Everything token-gated goes
    through here — see get_user_token_only."""
    rec = suspensions.get_active(user_id)
    if rec is not None:
        raise HTTPException(status_code=403, detail=_suspension_detail(rec))


async def get_user_token_only(authorization: str = Header(default="")) -> dict:
    """Validate just the session token (no device gate). Used by the device-
    approval poll / report endpoints, which a blocked device must still reach.

    The suspension gate lives here rather than in get_current_user so that it
    covers the website's token-only endpoints too — a suspension the marketplace
    ignored would not be one."""
    user = await get_user_allow_suspended(authorization)
    enforce_not_suspended(user["user_id"])
    return user


def enforce_mod_version(x_mod_hash: str) -> None:
    """Hard-block outdated / modified clients on gated endpoints by comparing the
    client's reported DLL hash (X-Mod-Hash) against the published latest.

    Fail-open to match /version/check: a no-op when the gate is disabled or nothing
    has been published yet. Otherwise a mismatch raises 426 `update_required`, which
    the client turns into its blocking "update required" window.
    """
    if not cfg.KSP_VERSION_CHECK_ENABLED:
        return
    cfg_doc = mver.get_config()
    latest_hash = (cfg_doc.get("latest_hash") or "").lower()
    if not latest_hash:
        return
    if (x_mod_hash or "").strip().lower() != latest_hash:
        raise HTTPException(status_code=426, detail={
            "code": "update_required",
            "latest_version": cfg_doc.get("latest_version"),
            "latest_hash": latest_hash,
            "download_url": cfg_doc.get("download_url"),
            "message": "Your Boundless Missions mod is out of date. Update to keep playing.",
        })


async def get_current_user(request: Request,
                           authorization: str = Header(default=""),
                           x_device_id: str = Header(default="", alias="X-Device-Id"),
                           x_mod_hash: str = Header(default="", alias="X-Mod-Hash")) -> dict:
    """Validate the session token, enforce the suspension and version gates, then
    device binding.

    A suspended account is refused with 403 `suspended` before anything else: it is
    the one refusal that no client-side action can clear, so telling someone to
    update a DLL that will still be refused afterwards would only waste their time.
    An outdated/modified DLL is refused with 426 `update_required`. An unrecognized
    device id is refused with 403 `device_unverified` and a challenge_id; the user is
    DMed an approve/reject prompt and the client polls /auth/device/poll until trusted.
    """
    user = await get_user_token_only(authorization)

    # Version gate next: an old client should be told to update before anything else.
    enforce_mod_version(x_mod_hash)

    if cfg.KSP_DEVICE_BINDING_ENABLED and check_device(user["user_id"], x_device_id) != "ok":
        client_ip = _client_ip(request)
        challenge_id, created = create_device_challenge(
            user["guild_id"], user["user_id"], user["username"], x_device_id, client_ip)
        if created:
            await _dm_device_approval(int(user["user_id"]), challenge_id, x_device_id, client_ip)
        raise HTTPException(status_code=403, detail={
            "code": "device_unverified",
            "challenge_id": challenge_id,
            "message": "A new device is using your account. Approve it from your Discord DM.",
        })

    return user


# ── Auth Endpoints ───────────────────────────────────────────────────────────

def _issue_link_token(result: dict, device_id: str = "") -> LinkResponse:
    """Mint a session token for a validated identity and return the linked response.
    The linking device (if it sent one) is trusted automatically, since it just
    completed the full link + login-approval flow."""
    token = create_session_token(
        result["guild_id"], result["user_id"], result["username"],
        _get_api_secret(),
    )
    if device_id:
        add_allowed_device(result["user_id"], device_id)
    return LinkResponse(
        status="ok",
        token=token,
        username=result["username"],
        guild_id=result["guild_id"],
        user_id=result["user_id"],
    )


async def _dm_login_approval(user_id: int, challenge_id: str, client_ip: str) -> bool:
    """DM the user a login-approval prompt with Log-in / Not-me buttons.
    Returns False if it couldn't be sent."""
    if not _bot_instance:
        return False
    try:
        import discord
        from cogs.ksp_bridge import LinkApprovalView
        u = await _bot_instance.fetch_user(user_id)
        when = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        where = client_ip or "unknown"
        e = discord.Embed(
            title="🔐 Approve KSP Login",
            description=(
                "A KSP client just entered your link code and wants to sign in as you.\n\n"
                f"**When:** {when}\n**From IP:** `{where}`\n\n"
                "If this is you, press **✅ Log in**. If you didn't try to link KSP, "
                "press **🚫 Not me**, since someone may have your link code.\n"
                "This request expires in 3 minutes."
            ),
            color=discord.Color.orange(),
        )
        await u.send(embed=e, view=LinkApprovalView(challenge_id))
        return True
    except Exception as exc:
        log.warning("Could not DM login approval to user %s: %s", user_id, exc)
        return False


@app.post("/api/v1/auth/link", response_model=LinkResponse)
async def auth_link(req: LinkRequest, request: Request,
                    x_device_id: str = Header(default="", alias="X-Device-Id"),
                    x_mod_hash: str = Header(default="", alias="X-Mod-Hash")):
    """Exchange a 6-digit link code for a session token (or a login-approval challenge)."""
    _guard_link_attempt(request)

    # Block outdated/modified clients up front so they can't even start a session.
    enforce_mod_version(x_mod_hash)

    result = validate_link_code(req.code)
    if result is None:
        _note_failed_link_guess()
        raise HTTPException(status_code=400, detail="Invalid or expired link code")

    # Approval off → link immediately (trusting the linking device).
    if not cfg.KSP_2FA_ENABLED:
        return _issue_link_token(result, x_device_id)

    # Approval on → DM the user a Log-in button and wait for them to poll. The link
    # code is already consumed, so a failed DM means this attempt can't proceed.
    client_ip = _client_ip(request)
    challenge_id = create_approval_challenge(
        result["guild_id"], result["user_id"], result["username"], client_ip)
    sent = await _dm_login_approval(int(result["user_id"]), challenge_id, client_ip)
    if not sent:
        raise HTTPException(
            status_code=502,
            detail="Couldn't DM your login approval. Enable DMs from server "
                   "members in Discord, then request a new link code.",
        )

    log.info("KSP: login-approval challenge issued for %s", result["username"])
    return LinkResponse(status="approval_required", challenge_id=challenge_id)


@app.post("/api/v1/auth/link/poll", response_model=LinkResponse)
async def auth_link_poll(req: PollRequest, request: Request,
                         x_device_id: str = Header(default="", alias="X-Device-Id")):
    """Poll a login-approval challenge. Returns the token once the user has pressed
    Log-in in Discord; tells the client to keep waiting until then."""
    # Polling is frequent and the challenge_id is unguessable (144-bit), so the
    # brute-force link guard doesn't apply — just a generous anti-abuse cap.
    _rate_limit(f"poll:{_client_ip(request)}", max_hits=120, window=60.0)

    state = poll_approval(req.challenge_id)
    if state["state"] == "pending":
        return LinkResponse(status="pending")
    if state["state"] == "approved":
        log.info("KSP: login approved, linking %s", state["username"])
        return _issue_link_token(state, x_device_id)
    if state["state"] == "denied":
        raise HTTPException(status_code=403, detail="Login request was denied.")
    raise HTTPException(status_code=400, detail="Login request expired. Request a new link code.")


# ── Device binding ───────────────────────────────────────────────────────────

async def _dm_device_approval(user_id: int, challenge_id: str,
                              device_id: str, client_ip: str) -> bool:
    """DM the user a 'new device' approval prompt with Yes-it's-me / report buttons."""
    if not _bot_instance:
        return False
    try:
        import discord
        from cogs.ksp_bridge import DeviceApprovalView
        u = await _bot_instance.fetch_user(user_id)
        when = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        e = discord.Embed(
            title="🖥️ New device on your account",
            description=(
                "A KSP client on a device we don't recognize is trying to use your "
                "account. It's blocked until you decide.\n\n"
                f"**When:** {when}\n**From IP:** `{client_ip or 'unknown'}`\n\n"
                "**Did you just switch PCs, reinstall, or clear the mod's files?**\n"
                "→ Press **✅ Yes, it's me** to trust this device.\n\n"
                "**Not sure which PC this is?**\n"
                "→ Press **🔔 Ping this PC** and the blocked PC will flash an "
                "*“Is this you?”* alert on its screen, so you can check before reporting.\n\n"
                "If this wasn't you, someone may be using your account. Press "
                "**🚫 No, report it** to alert the moderators.\n\n"
                "⚠️ Try **✅ Yes, it's me** first if you changed anything yourself, "
                "and only report if you're sure it wasn't you."
            ),
            color=discord.Color.orange(),
        )
        await u.send(embed=e, view=DeviceApprovalView(challenge_id))
        return True
    except Exception as exc:
        log.warning("Could not DM device approval to user %s: %s", user_id, exc)
        return False


async def _post_device_report(target: dict, mac: str, log_bytes: bytes | None):
    """Post the enriched moderation ticket (MAC + KSP.log) for a reported device.
    The base ticket was already posted when the user pressed 'report'; this adds
    the diagnostics the offending client uploaded."""
    if not _bot_instance:
        return
    # Prefer the ticket the base report opened, so all the diagnostics land there;
    # fall back to the guild's contract-mod channel only if no ticket was created.
    ch_id = target.get("ticket_channel_id")
    t_gid = target.get("guild_id")
    if not ch_id and not t_gid:
        return
    try:
        import discord
        if ch_id:
            ch = _bot_instance.get_channel(int(ch_id)) or await _bot_instance.fetch_channel(int(ch_id))
        else:
            ch = guild_config.resolve_channel(_bot_instance, int(t_gid), "contract_mod")
        if ch is None:
            return
        e = discord.Embed(
            title="📎 Device report: client diagnostics",
            description=(
                f"**User:** {target.get('username')} (`{target.get('user_id')}`)\n"
                f"**Device id:** `{target.get('device_id')}`\n"
                f"**IP:** `{target.get('client_ip') or 'unknown'}`\n"
                f"**MAC:** `{mac or 'not provided'}`"
            ),
            color=discord.Color.red(),
        )
        files = []
        if log_bytes:
            files.append(discord.File(io.BytesIO(log_bytes), filename="KSP.log"))
        else:
            e.add_field(name="KSP.log", value="⚠️ not provided by the client", inline=False)
        await ch.send(embed=e, files=files)
        log.info("Posted device-report diagnostics for user %s (log=%d bytes)",
                 target.get("user_id"), len(log_bytes) if log_bytes else 0)
    except Exception as exc:
        log.warning("Could not post device-report diagnostics: %s", exc)


@app.post("/api/v1/auth/device/poll", response_model=DeviceStatusResponse)
async def auth_device_poll(req: PollRequest, request: Request,
                           user: dict = Depends(get_user_token_only)):
    """Poll a device-approval challenge from a blocked client (token-only auth so
    the block itself doesn't deadlock the poll)."""
    _rate_limit(f"devpoll:{_client_ip(request)}", max_hits=120, window=60.0)
    state = poll_device_challenge(req.challenge_id)
    return DeviceStatusResponse(status=state["state"], report_id=state.get("report_id"),
                                ping=bool(state.get("ping")))


@app.post("/api/v1/device/report/{report_id}")
async def device_report(report_id: str,
                        mac: str = Form(default=""),
                        ksp_log: UploadFile = File(default=None),
                        user: dict = Depends(get_user_token_only)):
    """Receive the offending device's diagnostics (MAC + KSP.log) for a report the
    user opened, and append them to the moderation ticket."""
    target = get_report_target(report_id)
    if not target:
        log.warning("Device report %s: no pending report target found", report_id)
        raise HTTPException(status_code=404, detail="No pending report for this id")
    # The uploader must be the account the report is about — the offending client
    # holds a (copied) token for exactly that account, so this costs the real flow
    # nothing while keeping an unrelated token from writing into someone else's
    # moderation ticket. 404, not 403: don't confirm the report exists.
    if str(target.get("user_id")) != str(user.get("user_id")):
        log.warning("Device report %s: uploader %s is not the reported account %s",
                    report_id, user.get("user_id"), target.get("user_id"))
        raise HTTPException(status_code=404, detail="No pending report for this id")
    log_bytes = await _read_upload(ksp_log, MAX_LOG_BYTES) if ksp_log is not None else None
    log.info("Device report %s received from user %s (mac=%s, log=%d bytes)",
             report_id, user.get("user_id"), "yes" if mac else "no",
             len(log_bytes) if log_bytes else 0)
    await _post_device_report(target, mac, _trim_log(log_bytes))
    mark_report_done(target["_doc_id"])
    return {"success": True}


# ── Trusted-device management ────────────────────────────────────────────────
#
# What makes the device-approval prompt reversible: the approved-devices list can
# be read and pruned, so a device trusted at 2am (a friend's PC, a mistaken
# approval) is not trusted forever. Both endpoints sit behind get_current_user —
# the full gate, device binding included — on purpose: the trust list may only be
# edited FROM a trusted device. A copied token on an unapproved machine (the
# exact adversary device binding exists for) must not be able to strip the
# owner's real devices; token-only auth would allow that. The website therefore
# can't manage devices (it carries no device id) — its security lever remains
# logout_all, which is deliberately available even to a stolen token because
# using it burns that token too.


class DeviceRemoveRequest(BaseModel):
    device_id: str


@app.get("/api/v1/auth/devices")
async def auth_devices_list(x_device_id: str = Header(default="", alias="X-Device-Id"),
                            user: dict = Depends(get_current_user)):
    """The account's trusted devices, flagging which one is asking. Ids are the
    caller's own opaque install GUIDs — nothing personal to withhold."""
    devices = await asyncio.to_thread(list_devices, user["user_id"])
    for d in devices:
        d["current"] = bool(x_device_id) and d["device_id"] == x_device_id
    return {"devices": devices}


@app.post("/api/v1/auth/devices/remove")
async def auth_devices_remove(req: DeviceRemoveRequest,
                              x_device_id: str = Header(default="", alias="X-Device-Id"),
                              user: dict = Depends(get_current_user)):
    """Un-trust one device. Removing the CURRENT device is allowed — it is how a
    player retires the PC they're sitting at — and takes effect on its next
    request, when the device gate challenges it like any new machine. Removing
    the last device re-arms trust-on-first-use (the fresh-account state)."""
    removed = await asyncio.to_thread(
        remove_allowed_device, user["user_id"], req.device_id)
    if not removed:
        raise HTTPException(status_code=404, detail="That device is not on your account.")
    log.info("User %s removed trusted device %s…", user["user_id"], req.device_id[:8])
    return {"success": True, "removed_current": req.device_id == x_device_id}


# ── Bug reports ───────────────────────────────────────────────────────────────
#
# Filed from the mod's Tools tab, with the player's KSP.log optionally attached —
# which is the whole point of doing this in-game rather than asking for a Discord
# message: the one artefact that makes a KSP bug diagnosable is the log, and no
# player is going to find, trim and upload a 40 MB file by hand.
#
# It opens a normal ticket (so the reporter can be replied to in a private channel
# they can see) but pings the `bug_report` role rather than the mods — see
# cogs/tickets.create_ticket's `notify_role_key`.

_BUG_SUMMARY_MAX = 200
_BUG_DETAILS_MAX = 1500


@app.post("/api/v1/bugreport")
async def bug_report(summary: str = Form(...),
                     details: str = Form(default=""),
                     mod_version: str = Form(default=""),
                     ksp_log: UploadFile = File(default=None),
                     user: dict = Depends(get_current_user)):
    """File an in-game bug report as a ticket, with the client's KSP.log attached."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    # Deliberately tight: a bug report is a human writing prose, and each one costs
    # a channel plus a log upload. Three an hour is more than anyone reports in
    # good faith, and the cap is per user rather than per IP because a ticket is
    # attributable to an account.
    _rate_limit(f"bugreport:{uid}", max_hits=3, window=3600.0)

    summary = (summary or "").strip()[:_BUG_SUMMARY_MAX]
    details = (details or "").strip()[:_BUG_DETAILS_MAX]
    if not summary:
        raise HTTPException(status_code=400, detail="A summary is required.")

    log_bytes = _trim_log(await _read_upload(ksp_log, MAX_LOG_BYTES)) if ksp_log is not None else None
    log.info("Bug report from user %s (%s): %r (log=%d bytes)",
             uid, user.get("username"), summary[:80], len(log_bytes) if log_bytes else 0)

    if not _bot_instance:
        return {"success": False, "message": "The bot is not available right now."}
    guild = _bot_instance.get_guild(gid)
    if guild is None:
        return {"success": False, "message": "Your Discord server is not reachable right now."}

    import discord
    from cogs.tickets import create_ticket

    desc = f"**Summary**\n{summary}\n\n**Details**\n{details or '_none given_'}"
    e = discord.Embed(
        title="🖥️ Client",
        description=(f"**Mod version:** `{mod_version or 'unknown'}`\n"
                     f"**KSP.log:** {'attached below' if log_bytes else '⚠️ not attached'}"),
        color=discord.Color.blurple(),
    )
    files = [discord.File(io.BytesIO(log_bytes), filename="KSP.log")] if log_bytes else []

    channel = await create_ticket(
        _bot_instance, guild,
        opener_id=uid,
        kind="bug",
        title="Bug report (in-game)",
        description=desc,
        color=discord.Color.orange(),
        extra_embeds=[e],
        files=files,
        ping_mods=False,             # bug reports are not a moderation matter
        notify_role_key="bug_report",
    )
    if channel is None:
        return {"success": False,
                "message": "Couldn't open a ticket — the server's ticket system isn't set up."}
    return {"success": True,
            "message": f"Reported. A private ticket (#{channel.name}) is open in Discord."}


@app.get("/api/v1/auth/verify", response_model=UserProfile)
async def auth_verify(user: dict = Depends(get_current_user)):
    """Validate session token and return user profile."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    u = store.get_user(gid, uid)

    return UserProfile(
        user_id=user["user_id"],
        username=user["username"],
        guild_id=user["guild_id"],
        xp=u.get("xp", 0),
        level=u.get("level", 0),
        balance=u.get("balance", 0),
        messages=u.get("messages", 0),
        unlocked_levels=u.get("unlocked_levels", []),
        currency_name=settings.CURRENCY_NAME,
    )


@app.get("/api/v1/auth/suspension")
async def auth_suspension(user: dict = Depends(get_user_allow_suspended)):
    """This account's suspension state — the one endpoint a suspended client can
    still call, so its "check again" button has something to ask.

    `by` is stripped: who issued a suspension is a moderation fact, not something
    the suspended player is owed, and naming a moderator to someone angry at the
    decision is how a moderator gets harassed."""
    rec = suspensions.get_active(user["user_id"])
    if rec is None:
        return {"suspended": False}
    detail = _suspension_detail(rec)
    detail.pop("code", None)
    return {"suspended": True, **detail}


@app.post("/api/v1/auth/logout_all")
async def auth_logout_all(user: dict = Depends(get_user_allow_suspended)):
    """Log the current user out of every device.

    The user's own privacy control for an account left linked somewhere else.
    Bumps their token version so every session token — including this caller's —
    is rejected from here on; each device drops to its unlinked state on its next
    request. Not an admin action: a user can only log out their own sessions.

    Deliberately behind the bare token check rather than the full gate: cutting a
    session loose is exactly what someone does when their account is linked on a
    machine they no longer control, and a suspension, an outdated DLL or an
    unapproved device must not be what stops them. The worst a stolen token buys
    here is logging the thief out along with everyone else.
    """
    new_version = logout_all_devices(user["user_id"])
    log.info("KSP: %s logged out of all devices", user["username"])
    return {"success": True, "token_version": new_version}


# ── Version gate ─────────────────────────────────────────────────────────────

@app.get("/api/v1/version/check", response_model=VersionCheckResponse)
async def version_check(hash: str = "", version: str = ""):
    """Report whether the calling KSP client's DLL is the published latest.

    Unauthenticated on purpose: the client must be able to learn it's outdated
    before (and independent of) linking, and the response carries only public
    version info. `hash` is the SHA256 of the client's GeneKerman.dll; `version`
    is its self-reported version label (display only).
    """
    # Advertised independently of the DLL version gate: a policy bump must be able
    # to force re-consent even when the update gate is off or nothing is published.
    pver = policy.get_version()
    if not cfg.KSP_VERSION_CHECK_ENABLED:
        # Gate disabled — never tell a client to update, but still advertise the
        # published DLL hash so the client can always confirm the expected build.
        cfg_doc = mver.get_config()
        return VersionCheckResponse(
            enabled=False, up_to_date=True,
            latest_version=cfg_doc.get("latest_version"),
            latest_hash=(cfg_doc.get("latest_hash") or None),
            your_version=version or None,
            policy_version=pver)
    resp = mver.check(hash, version)
    resp["policy_version"] = pver
    return VersionCheckResponse(**resp)


# ── Anti-cheat: suspicion flagging → moderator ticket ─────────────────────────
#
# The client is untrusted; reward endpoints validate what they can server-side and
# call flag_suspicion() when something still looks forged. Each distinct signal
# opens at most one mods-only ticket per user per cooldown so mods aren't spammed.

# reason → (ticket title, cooldown seconds, min count before a ticket is opened)
_SUSPICION_RULES = {
    "dll_tamper":           ("🛡️ Possible modified mod (failed attestation)", 6 * 3600, 1),
    "illegal_mods":         ("⚠️ Repeated disallowed-mod submissions",        24 * 3600, 3),
    "impossible_telemetry": ("⚠️ Implausible flight telemetry",               12 * 3600, 1),
    "request_flood":        ("⚠️ Extreme request rate (possible automation)", 6 * 3600, 1),
}


async def flag_suspicion(gid: int, uid: int, username: str, reason: str,
                         details: str, severity: str = "high") -> None:
    """Record an anti-cheat suspicion and, when it clears its threshold/cooldown,
    open a mods-only ticket about the user. Awaitable from handlers; never raises."""
    try:
        import discord
        count = await asyncio.to_thread(
            susp.record, gid, uid, username, reason, severity, details)
        title, cooldown, min_count = _SUSPICION_RULES.get(
            reason, (f"⚠️ Suspicious activity: {reason}", 12 * 3600, 1))
        if count < min_count:
            return
        if not await asyncio.to_thread(susp.claim_ticket, gid, uid, reason, cooldown):
            return
        if not _bot_instance:
            return
        guild = _bot_instance.get_guild(gid)
        if guild is None:
            return
        from cogs.tickets import create_ticket
        desc = (f"**User:** {username} (`{uid}`)\n"
                f"**Signal:** `{reason}` · severity **{severity}**\n"
                f"**Times seen (all-time):** {count}\n\n{details}")
        await create_ticket(
            _bot_instance, guild,
            opener_id=None,            # mods-only — the suspect must NOT see this
            subject_user_id=uid,
            kind="user",
            title=title,
            description=desc,
            color=discord.Color.dark_red(),
        )
        log.warning("Anti-cheat: opened ticket for user %s (%s, count=%d)", uid, reason, count)
    except Exception as exc:
        log.warning("flag_suspicion failed (%s/%s): %s", uid, reason, exc)


# ── Anti-cheat: per-user extreme-rate flood detection ─────────────────────────
#
# Distinct from _rate_limit(): that BLOCKS guessable auth endpoints by IP. This
# only OBSERVES authenticated, cost/reward-bearing endpoints by user and, when a
# user's rate is far past any human play pattern, raises a (deduped) suspicion
# ticket. It never blocks — that's the per-endpoint limit's job — and it stays
# in-memory so detection itself costs no Firestore: flag_suspicion (which does
# write) is reached only once a threshold trips, at most once per window.
_USER_FLOOD: dict[tuple[int, str], list[float]] = {}    # (uid, bucket) -> hit times
_USER_FLOOD_FLAGGED: dict[tuple[int, str], float] = {}  # (uid, bucket) -> last flag time


def _note_user_action(gid: int, uid: int, username: str, bucket: str,
                      threshold: int, window: float) -> None:
    """Record one cost/reward action by `uid`. Fire a 'request_flood' suspicion
    when they exceed `threshold` actions within `window` seconds. Cheap + safe to
    call on the hot path; never raises and never blocks the request."""
    now = time.time()
    key = (uid, bucket)
    hits = [t for t in _USER_FLOOD.get(key, []) if now - t < window]
    hits.append(now)
    _USER_FLOOD[key] = hits
    if len(hits) < threshold:
        return
    # Threshold tripped: don't re-flag (and re-write Firestore) on every further
    # hit — at most once per window per (user, bucket).
    if now - _USER_FLOOD_FLAGGED.get(key, 0.0) < window:
        return
    _USER_FLOOD_FLAGGED[key] = now
    asyncio.create_task(flag_suspicion(
        gid, uid, username, reason="request_flood",
        details=(f"User sent **{len(hits)}** `{bucket}` requests in under "
                 f"{int(window)}s, far above normal play. Likely a scripted "
                 f"client hammering reward/AI endpoints."),
        severity="medium"))


# ── Attestation (challenge-response anti-tamper) ──────────────────────────────
#
# A static self-reported DLL hash (X-Mod-Hash) is trivially spoofed by hardcoding
# the expected value. Attestation instead asks for SHA256(server-nonce + a random
# window of the DLL): the answer changes every time, so it can't be precomputed —
# a tamperer must keep the entire pristine DLL on disk to pass. Not unbreakable
# (acknowledged), but it defeats casual patches and a failure is a strong tamper
# signal that we route to moderators. Fail-open when no pristine DLL is stored.

_attest_challenges: dict[str, dict] = {}   # attest_id -> {user_id, expected, expires}
_ATTEST_TTL = 60.0


def _prune_attest() -> None:
    now = time.time()
    for k in [k for k, v in _attest_challenges.items() if v["expires"] < now]:
        _attest_challenges.pop(k, None)


@app.get("/api/v1/attest/challenge", response_model=AttestChallenge)
async def attest_challenge(user: dict = Depends(get_user_token_only)):
    """Issue a one-time nonce + byte-window for the client to hash against its DLL."""
    # Attestation asks the same question the version gate asks — "is this the published
    # build?" — just cryptographically instead of on the client's word. So it honours
    # the same switch. With the gate off for development, every local build is by
    # definition not the published one, and each attestation round would open a
    # mods-only "possible modified mod" ticket about the developer.
    if not cfg.KSP_VERSION_CHECK_ENABLED:
        return AttestChallenge(enabled=False)
    info = await asyncio.to_thread(mver.get_latest_dll_bytes)
    if not info:
        return AttestChallenge(enabled=False)   # nothing stored → can't verify, skip
    h, data = info
    n = len(data)
    # Hash a random ~10% slice (min 16 KB) of the DLL: keeping it a random window
    # defeats precomputed answers, while ~10% coverage roughly doubles the chance a
    # single check lands on modified bytes vs. the old fixed 16 KB (~5% of a 323 KB
    # DLL). The client already reads the whole DLL to hash any window, so it's free.
    length = min(n, max(16384, n // 10))
    offset = secrets.randbelow(n - length + 1) if n > length else 0
    nonce = secrets.token_hex(16)
    attest_id = secrets.token_urlsafe(18)
    expected = hashlib.sha256(nonce.encode() + data[offset:offset + length]).hexdigest()
    _prune_attest()
    _attest_challenges[attest_id] = {
        "user_id": str(user["user_id"]),
        "expected": expected,
        "expires": time.time() + _ATTEST_TTL,
    }
    return AttestChallenge(enabled=True, attest_id=attest_id, nonce=nonce,
                           offset=offset, length=length)


@app.post("/api/v1/attest/respond", response_model=AttestResult)
async def attest_respond(req: AttestRespondRequest, request: Request,
                         user: dict = Depends(get_user_token_only)):
    """Verify the client's attestation digest. A mismatch on a valid, fresh,
    owner-matched challenge is a strong tamper signal → flag for moderators."""
    _rate_limit(f"attest:{_client_ip(request)}", max_hits=30, window=60.0)
    # Checked here too, not only when issuing: a challenge handed out before the switch
    # was flipped can still arrive, and nothing stops a client POSTing here unprompted.
    # An accusation is the expensive half of this endpoint, so it gets its own guard.
    if not cfg.KSP_VERSION_CHECK_ENABLED:
        return AttestResult(ok=True)
    ch = _attest_challenges.pop(req.attest_id, None)
    # Unknown/expired/foreign challenge: inconclusive (e.g. server restart) — don't
    # accuse, just report not-ok so the client retries on its next cycle.
    if not ch or ch["expires"] < time.time() or str(ch["user_id"]) != str(user["user_id"]):
        return AttestResult(ok=False)
    if secrets.compare_digest(ch["expected"], (req.digest or "").strip().lower()):
        return AttestResult(ok=True)
    # Server-observed forensics: the client IP (seen at the socket, not self-reported)
    # and the bound device id. Both are meaningful here precisely because they don't
    # come from the tampered client's payload, unlike MAC/KSP.log (those are only
    # collected via the consented device-report flow, where the uploader is trusted).
    client_ip = _client_ip(request)
    device_id = request.headers.get("x-device-id", "") or "unknown"
    await flag_suspicion(
        int(user["guild_id"]), int(user["user_id"]), user.get("username", ""),
        reason="dll_tamper",
        details=("The client failed challenge-response attestation: SHA256 of its "
                 "DLL window + server nonce did not match the published build. This "
                 "usually means the GeneKerman.dll on this account has been modified "
                 "or replaced from the official release.\n\n"
                 f"**Client IP:** `{client_ip}`\n"
                 f"**Device id:** `{device_id}`"),
        severity="high")
    return AttestResult(ok=False)


# ── Debug/test-only endpoints ────────────────────────────────────────────────
#
# Gated by cfg.DEBUG_ENDPOINTS_ENABLED: 404 for everyone when off (invisible in
# production, like the owner console), so this ships harmlessly. Used by the KSP
# debug test panel to prove the signed-URL invariant end to end: a private object
# is reachable through its signed URL but NOT through its bare public URL.

@app.get("/api/v1/debug/signtest")
async def debug_signtest(user: dict = Depends(get_user_token_only)):
    """Mint a throwaway PRIVATE object and return both a signed URL (should 200) and
    its bare public URL (should 403). Dev-server only; 404 when the gate is off."""
    if not cfg.DEBUG_ENDPOINTS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    from data.store import upload_private, signed_url as _signed
    uid = str(user["user_id"])
    path = f"debug/signtest/{uid}/{secrets.token_hex(8)}.txt"
    payload = f"gk-signtest {datetime.now(timezone.utc).isoformat()}".encode()
    await asyncio.to_thread(upload_private, path, payload, "text/plain")
    # public_url is the unsigned object URL; on a private object it must 403.
    public_url = _storage_bucket.blob(path).public_url
    return {
        "signed_url": await asyncio.to_thread(_signed, path),
        "public_url": public_url,
        "expected": {"signed_status": 200, "public_status": 403},
        "note": "Signed URL should download; the bare public URL should be forbidden.",
    }


# ── User Profile ─────────────────────────────────────────────────────────────

@app.get("/api/v1/user/profile", response_model=UserProfile)
async def user_profile(user: dict = Depends(get_current_user)):
    """Get the current user's profile (balance, XP, level)."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    u = store.get_user(gid, uid)

    return UserProfile(
        user_id=user["user_id"],
        username=user["username"],
        guild_id=user["guild_id"],
        xp=u.get("xp", 0),
        level=u.get("level", 0),
        balance=u.get("balance", 0),
        messages=u.get("messages", 0),
        unlocked_levels=u.get("unlocked_levels", []),
        currency_name=settings.CURRENCY_NAME,
    )


# ── Weekly Missions ──────────────────────────────────────────────────────────

def _classification_ref(week_key: str):
    """Firestore ref for cached mission classifications."""
    return _db.collection("mission_classifications").document(week_key)


async def _classify_missions(missions: list[dict], week_key: str) -> list[dict]:
    """
    AI-classify each mission as 'craft_build' or 'active_vessel' with
    required_situation and required_body. Results are cached in Firestore
    so AI is only called once per week.
    """
    # Check cache first
    ref = _classification_ref(week_key)
    snap = ref.get()
    if snap.exists:
        cached = snap.to_dict().get("classifications", {})
        if cached:
            for m in missions:
                key = str(m["id"])
                if key in cached:
                    m["mission_type"] = cached[key].get("mission_type", "active_vessel")
                    m["required_situation"] = cached[key].get("required_situation")
                    m["required_body"] = cached[key].get("required_body")
            return missions

    # No cache — run AI classification
    try:
        from cogs.screenshots import active_client, record_gemini, _MODEL
        gemini_client = active_client()
    except Exception:
        gemini_client = None
        record_gemini = lambda *_: None

    if not gemini_client:
        # No AI (key missing OR monthly budget reached) — heuristic fallback.
        return _classify_heuristic(missions)

    # Build AI prompt
    mission_list = "\n".join(
        f'{m["id"]}. {m["desc_en"]} (category: {m["category"]}, difficulty: {m["difficulty"]})'
        for m in missions
    )

    prompt = (
        "You are classifying KSP (Kerbal Space Program) missions for a mod.\n\n"
        "For each mission, determine:\n"
        "1. mission_type: 'craft_build' (vessel must be shown in VAB/SPH editor) "
        "or 'active_vessel' (vessel must be in flight, at the right place)\n"
        "2. required_situation: The KSP vessel situation needed. "
        "Options: ORBITING, LANDED, SPLASHED, FLYING, SUB_ORBITAL, ESCAPING, DOCKED, null\n"
        "3. required_body: The celestial body name (Kerbin, Mun, Minmus, Duna, Eve, Jool, etc.) or null\n\n"
        "Rules:\n"
        "- 'construction' category missions that say 'build' are 'craft_build'\n"
        "- Missions about orbiting, landing, flying, returning are 'active_vessel'\n"
        "- If a mission says 'orbit X', required_situation = 'ORBITING', required_body = X\n"
        "- If a mission says 'land on X', required_situation = 'LANDED', required_body = X\n"
        "- 'flyby' missions: required_situation = 'ESCAPING' or 'SUB_ORBITAL', required_body = target\n"
        "- 'dock' missions: required_situation = 'ORBITING' (docking happens in orbit)\n"
        "- If no specific body/situation, set to null\n\n"
        f"Missions:\n{mission_list}\n\n"
        "Return ONLY valid JSON — an array of objects:\n"
        '[{"id": 1, "mission_type": "...", "required_situation": "...", "required_body": "..."}]'
    )

    from google.genai import types
    import json

    try:
        response = gemini_client.models.generate_content(
            model=_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=2048),
        )
        record_gemini(response)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        classifications = json.loads(raw.strip())
    except Exception as exc:
        log.error("AI mission classification failed: %s", exc)
        return _classify_heuristic(missions)

    # Apply classifications and build cache
    cache = {}
    cls_map = {c["id"]: c for c in classifications}

    for m in missions:
        cls = cls_map.get(m["id"], {})
        m["mission_type"] = cls.get("mission_type", "active_vessel")
        m["required_situation"] = cls.get("required_situation")
        m["required_body"] = cls.get("required_body")
        cache[str(m["id"])] = {
            "mission_type": m["mission_type"],
            "required_situation": m.get("required_situation"),
            "required_body": m.get("required_body"),
        }

    # Save to Firestore cache
    ref.set({"classifications": cache, "classified_at": datetime.now(timezone.utc).isoformat()})
    log.info("AI classified %d missions for week %s", len(cache), week_key)

    return missions


def _classify_heuristic(missions: list[dict]) -> list[dict]:
    """Fallback heuristic when AI is unavailable."""
    build_keywords = ["build", "construct", "assemble", "deploy a relay", "deploy a communication"]
    for m in missions:
        desc_lower = m["desc_en"].lower()
        if m["category"] == "construction" or any(kw in desc_lower for kw in build_keywords):
            m["mission_type"] = "craft_build"
        else:
            m["mission_type"] = "active_vessel"

        m["required_body"] = None
        for body in settings.KNOWN_CELESTIAL_BODIES:
            if body.lower() in desc_lower:
                m["required_body"] = body
                break

        m["required_situation"] = None
        if "orbit" in desc_lower:
            m["required_situation"] = "ORBITING"
        elif "land" in desc_lower:
            m["required_situation"] = "LANDED"
        elif "flyby" in desc_lower:
            m["required_situation"] = "SUB_ORBITAL"
        elif "dock" in desc_lower:
            m["required_situation"] = "ORBITING"

    return missions


# ── Single-contract classification (for human-issued contracts) ──────────

_BUILD_KEYWORDS = [
    "build", "construct", "assemble", "design", "create", "make",
    "deploy a relay", "deploy a communication", "station with",
]
_FLIGHT_KEYWORDS = [
    "orbit", "land on", "fly to", "reach", "dock", "rendezvous",
    "return", "flyby", "intercept", "capture", "eva", "splashdown",
]


def _classify_text_heuristic(mission_text: str) -> dict:
    """Classify a single mission description using keyword heuristics."""
    text_lower = mission_text.lower()

    is_build = any(kw in text_lower for kw in _BUILD_KEYWORDS)
    is_flight = any(kw in text_lower for kw in _FLIGHT_KEYWORDS)

    # Build keywords without flight keywords = craft_build.
    # Both present = flight takes priority ("build and fly to orbit" = active_vessel).
    if is_build and not is_flight:
        mission_type = "craft_build"
    else:
        mission_type = "active_vessel"

    required_body = None
    for body in settings.KNOWN_CELESTIAL_BODIES:
        if body.lower() in text_lower:
            required_body = body
            break

    required_situation = None
    if mission_type == "active_vessel":
        if "orbit" in text_lower:
            required_situation = "ORBITING"
        elif "land" in text_lower:
            required_situation = "LANDED"
        elif "flyby" in text_lower:
            required_situation = "SUB_ORBITAL"
        elif "dock" in text_lower:
            required_situation = "ORBITING"

    return {
        "mission_type": mission_type,
        "required_situation": required_situation,
        "required_body": required_body,
    }


# ── KSP part catalog + part-name resolution ──────────────────────────────────
#
# Resolving a mission's loose part mention ("the Thud engine", a typo'd "thudd")
# to a real installed part needs the player's actual part list. The KSP client
# uploads that catalog (hash-gated); we cache it in memory and persist it to
# Firestore so it survives a bot restart. Resolutions (and the AI tie-breaks they
# sometimes need) are cached per (catalog-hash, mention) so a fetch costs nothing
# after the first.

_PART_CATALOGS: dict[str, dict] = {}        # "gid:uid" -> {"hash":..., "parts":[...]}
_RESOLVE_CACHE: dict[tuple, str | None] = {}  # (catalog_hash, loose_lower) -> name|None

# Names that look like parts in a listing's `parts` but never are. A .craft PART node
# carries a `partName` field holding the Unity component class, not a part: "Part" on
# every part there is, "CompoundPart" on struts and fuel lines, and legacy class names
# in pre-1.0 files. Clients before the CkanGenerator fix scanned that field flat and
# shipped those with every listing, so every craft looked like it needed a part nobody
# on earth has installed. Fixed at the source in the mod; this keeps listings made by
# an older client (and any still in Firestore) from raising the same false alarm.
# Part/CompoundPart are the whole Part class hierarchy in KSP 1.12; the other three are
# what pre-1.0 craft files still on disk write in that field.
_NOT_PART_NAMES = frozenset({"Part", "CompoundPart", "Strut", "Winglet", "ControlSurface"})


def _catalog_key(gid: int, uid: int) -> str:
    return f"{gid}:{uid}"


def _catalog_doc(gid: int, uid: int):
    return _db.collection("guilds").document(str(gid)).collection("part_catalogs").document(str(uid))


def _get_user_catalog(gid: int, uid: int) -> dict | None:
    """The requesting user's uploaded catalog, loading from Firestore on a cold cache."""
    key = _catalog_key(gid, uid)
    cat = _PART_CATALOGS.get(key)
    if cat is not None:
        return cat
    try:
        snap = _catalog_doc(gid, uid).get()
        if snap.exists:
            cat = snap.to_dict()
            _PART_CATALOGS[key] = cat
            return cat
    except Exception as exc:
        log.warning("Failed to load part catalog for %s: %s", key, exc)
    return None


def _craft_compatibility(gid: int, uid: int, listing: dict) -> CraftCompatibility:
    """Can this user actually load this craft? Checked against the part catalog their
    KSP client uploaded (see /api/v1/parts/catalog).

    Advisory, never a gate: a craft nobody can load yet is still worth owning, and the
    mod substitutes what it can at install time. Which is exactly why substitutable
    parts are separated out — warning someone about a part PartAliases will silently
    swap for them on install would be a false alarm about a problem that fixes itself.

    Returns known=False rather than guessing when the answer isn't knowable: the user
    has never uploaded a catalog, or the listing predates part tagging. "Unknown" must
    never render as a green light.
    """
    from data.part_aliases import equivalents

    craft_parts = [str(p) for p in (listing.get("parts") or []) if p
                   and str(p) not in _NOT_PART_NAMES]
    if not craft_parts:
        return CraftCompatibility(
            known=False, reason="This listing was made before crafts recorded their parts."
        )

    cat = _get_user_catalog(gid, uid)
    if not cat or not cat.get("parts"):
        return CraftCompatibility(
            known=False,
            reason="Start KSP with the mod linked once and we can check this craft "
                   "against your installed parts.",
        )

    installed = {str(p.get("name", "")) for p in cat["parts"] if p.get("name")}

    missing, substitutable = [], []
    for name in craft_parts:
        if name in installed:
            continue
        missing.append(name)
        if any(alt in installed for alt in equivalents(name)):
            substitutable.append(name)

    blocking = [p for p in missing if p not in substitutable]

    if not missing:
        reason = "You have every part this craft uses."
    elif not blocking:
        reason = (f"{len(substitutable)} part(s) aren't installed under that name, but you "
                  "have an equivalent — the mod swaps them in when the craft arrives.")
    else:
        reason = (f"You're missing {len(blocking)} part(s) this craft needs; it won't load "
                  "until you install what provides them.")

    return CraftCompatibility(
        known=True,
        compatible=not blocking,
        missing_parts=missing,
        substitutable_parts=substitutable,
        reason=reason,
    )


def _ai_resolve_part(mission_text: str):
    """Build an ai_resolver(loose, candidates)->name|None bound to this mission's
    text, or None when no AI is configured."""
    try:
        from cogs.screenshots import active_client, record_gemini, _MODEL
        gemini_client = active_client()
    except Exception:
        gemini_client = None
        record_gemini = lambda *_: None
    if not gemini_client:
        return None

    from google.genai import types
    import json

    def _resolver(loose: str, candidates: list[dict]) -> str | None:
        if not candidates:
            return None
        listing = "\n".join(f"- {c.get('name')} | {c.get('title')}" for c in candidates[:12])
        prompt = (
            "A KSP mission has a part restriction mentioning a part by an informal "
            "or possibly mistyped name. Pick which installed part it refers to.\n\n"
            f"Mission: \"{mission_text}\"\n"
            f"Mentioned part: \"{loose}\"\n\n"
            "Installed candidates (internal_name | display_title):\n"
            f"{listing}\n\n"
            "Reply with ONLY the exact internal_name of the best match, or NONE if "
            "none clearly fits."
        )
        try:
            resp = gemini_client.models.generate_content(
                model=_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=64),
            )
            record_gemini(resp)
            ans = (resp.text or "").strip().strip("`").splitlines()[0].strip()
            if not ans or ans.upper() == "NONE":
                return None
            return ans
        except Exception as exc:
            log.warning("Gemini part resolution failed for %r: %s", loose, exc)
            return None

    return _resolver


def _resolve_constraints(constraints: dict | None, gid: int, uid: int,
                         mission_text: str) -> dict | None:
    """Add resolved internal part names to a constraints dict using the user's
    catalog (deterministic fuzzy + AI tie-break, both cached). Returns the
    constraints unchanged when there's nothing to resolve or no catalog."""
    if mc.is_empty(constraints):
        return constraints
    if not (constraints.get("forbidden_parts") or constraints.get("required_parts")):
        return constraints
    cat = _get_user_catalog(gid, uid)
    if not cat or not cat.get("parts"):
        return constraints  # no catalog yet → loose matching only

    chash = cat.get("hash") or pr.catalog_hash(cat["parts"])
    ai = _ai_resolve_part(mission_text)

    def _cached_resolver(loose: str) -> str | None:
        ck = (chash, loose.lower())
        if ck in _RESOLVE_CACHE:
            return _RESOLVE_CACHE[ck]
        name = pr.resolve_part(loose, cat["parts"], ai)
        _RESOLVE_CACHE[ck] = name
        return name

    return mc.resolve_parts(constraints, _cached_resolver)


async def _classify_single_contract(gid: int, contract_id: str, mission_text: str) -> dict:
    """
    Classify a single contract's mission text. Uses AI if available,
    falls back to heuristic. Caches result back to the contract doc.
    """
    # Try AI first
    try:
        from cogs.screenshots import active_client, record_gemini, _MODEL
        gemini_client = active_client()
    except Exception:
        gemini_client = None
        record_gemini = lambda *_: None

    if gemini_client:
        from google.genai import types
        import json

        prompt = (
            "Classify this KSP mission for a mod. The mission text may be in English or Turkish.\n\n"
            f"Mission: \"{mission_text}\"\n\n"
            "Determine:\n"
            "1. mission_type: 'craft_build' (design/build a vessel in VAB/SPH) "
            "or 'active_vessel' (fly vessel to specific place/situation)\n"
            "2. required_situation: ORBITING, LANDED, SPLASHED, FLYING, SUB_ORBITAL, ESCAPING, or null\n"
            "3. required_body: celestial body name or null\n"
            "4. constraints: part-usage restrictions stated in the text (a 'mission limit'). "
            "Leave every list empty if the text states no restriction. Use these keys, each a list of strings:\n"
            "   - forbidden_parts / required_parts: specific part names, e.g. \"Thud\", \"Mainsail\"\n"
            "   - forbidden_propellants / required_propellants: fuel/resource names, "
            "e.g. \"LqdHe3\", \"LiquidFuel\", \"XenonGas\", \"MonoPropellant\", \"SolidFuel\"\n"
            "   - forbidden_engine_categories / required_engine_categories: one or more of "
            "[nuclear, ion, solid, chemical, electric, monoprop, rcs]\n"
            "   - forbidden_part_categories / required_part_categories: one or more of "
            "[heatshield, parachute, solarpanel, wheel, ladder, reactionwheel, rtg]\n"
            "   - max_parts / min_parts: integer part-count limits, or null. "
            "'at most/up to N parts' => max_parts N; 'fewer than N' => max_parts N-1; "
            "'at least N' => min_parts N; 'more than N' => min_parts N+1.\n"
            "   - max_dv / min_dv: vacuum delta-v (Δv) limits in m/s, or null. "
            "Convert km/s to m/s (3.5 km/s => 3500). 'at least 3000 m/s of delta-v' => "
            "min_dv 3000; 'no more than 5000 m/s dv' => max_dv 5000.\n"
            "   - max_crew / min_crew: crew-aboard limits, or null. 'crew of 3' => "
            "min_crew 3 and max_crew 3; 'at least 2 kerbals' => min_crew 2; "
            "'2-4 crew' => min_crew 2, max_crew 4; 'send one kerbal' => min_crew 1 and "
            "max_crew 1. An uncrewed mission ('unmanned/uncrewed probe', 'no crew aboard') "
            "=> max_crew 0 — 0 is a real limit here, not 'no limit', so use null (never 0) "
            "when the text says nothing about crew. A crewed mission with no number "
            "('a crewed flyby') => min_crew 1. Kerbals to be *rescued or returned* are not "
            "crew limits: 'rescue 2 stranded kerbals' sets neither bound.\n"
            "   - crew_traits: per-profession crew requirements, or {}. An object keyed by "
            "KSP profession name with {\"min\": N} and/or {\"max\": N}: 'send two pilots and "
            "a scientist' => {\"Pilot\": {\"min\": 2}, \"Scientist\": {\"min\": 1}}; "
            "'at most one tourist' => {\"Tourist\": {\"max\": 1}}; 'no tourists' => "
            "{\"Tourist\": {\"max\": 0}}; 'exactly 2 engineers' => "
            "{\"Engineer\": {\"min\": 2, \"max\": 2}}. Stock professions are Pilot, Engineer, "
            "Scientist, Tourist; modded ones (Kolonist, Miner, Medic, Scout, Biologist, "
            "Quartermaster, Technician, Mechanic, Geologist, Botanist, Chemist, Farmer) are "
            "allowed, spelled exactly like that. A bare count is a floor, not an exact "
            "count. Kerbals to be rescued are not crew_traits either.\n\n"
            "Constraint rules:\n"
            "- 'must use / only / powered by X' => required_*. "
            "'can't use / doesn't use / does not use / no / without / X-less' => forbidden_*.\n"
            "- Negation flips intent: 'doesn't use deuterium-powered engines' => "
            "forbidden_propellants ['LqdDeuterium'] (NOT required). Never put the same item "
            "in both a forbidden and a required list.\n"
            "- 'nuclear/atomic/NTR/NERV engine' => engine category 'nuclear'. 'ion' => 'ion'. "
            "'SRB/solid booster' => 'solid'.\n"
            "- 'heatshield-less / no heat shield' => forbidden_part_categories ['heatshield'].\n"
            "- 'Lqd He3 / helium-3 powered' => required_propellants ['LqdHe3'].\n"
            "- When the text names a specific part (e.g. 'Vector', 'Mainsail', 'Thud'), copy that "
            "name VERBATIM into required_parts/forbidden_parts — never translate it to a real-world "
            "or 'equivalent' name (do NOT turn 'Vector' into 'SSME', or 'Mainsail' into 'RS-68'), and "
            "do NOT also add an engine category for that named part. Only set an engine category when "
            "the text names a general *kind* of engine (e.g. 'nuclear engine', 'any ion thruster'). "
            "Map a fuel to a propellant.\n\n"
            "Return ONLY valid JSON:\n"
            '{"mission_type": "...", "required_situation": "...", "required_body": "...", '
            '"constraints": {"forbidden_parts": [], "required_parts": [], '
            '"forbidden_propellants": [], "required_propellants": [], '
            '"forbidden_engine_categories": [], "required_engine_categories": [], '
            '"forbidden_part_categories": [], "required_part_categories": [], '
            '"max_parts": null, "min_parts": null, "max_dv": null, "min_dv": null, '
            '"max_crew": null, "min_crew": null, "crew_traits": {}}}'
        )

        try:
            response = gemini_client.models.generate_content(
                model=_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=512),
            )
            record_gemini(response)
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            result = json.loads(raw.strip())
            # AI is authoritative for constraints: trust its decision, including an
            # all-empty result ("no limits"). The heuristic only steps in when the
            # AI is unavailable or errors (the fallback branches below).
            result["constraints"] = mc.normalize(result.get("constraints"))
            log.info("AI classified contract %s: %s%s", contract_id, result.get("mission_type"),
                     "" if mc.is_empty(result["constraints"]) else f" + limits ({mc.summary_line(result['constraints'])})")
        except Exception as exc:
            log.error("AI single-contract classification failed: %s", exc)
            result = _classify_text_heuristic(mission_text)
            result["constraints"] = mc.extract_heuristic(mission_text)
    else:
        result = _classify_text_heuristic(mission_text)
        result["constraints"] = mc.extract_heuristic(mission_text)

    # Crew-aboard limits ("crew of 3", "2–4 kerbals", "unmanned probe") are the one
    # rule derived both ways: the heuristic runs even when the AI answered, and fills
    # only the bounds the AI left unset. Presence is tested with `is not None`, since
    # max_crew 0 (uncrewed) is a limit and would otherwise read as "the AI said
    # nothing" — and then be overwritten by nothing, or overwrite a real AI answer.
    _crew = mc.extract_heuristic(mission_text)
    for _k in ("min_crew", "max_crew"):
        if _crew.get(_k) is not None and result["constraints"].get(_k) is None:
            result["constraints"][_k] = _crew[_k]
    # Per-profession crew is filled the same way, but per profession rather than
    # wholesale: the AI naming one ("a scientist") is no reason to drop another the
    # text also named, and whichever source spoke first about a given profession is
    # the one that keeps it.
    _ai_traits = result["constraints"].setdefault("crew_traits", {})
    for _trait, _bounds in (_crew.get("crew_traits") or {}).items():
        _ai_traits.setdefault(_trait, _bounds)
    if not _ai_traits:
        result["constraints"].pop("crew_traits", None)
    mc.resolve_conflicts(result["constraints"])

    # Cache result back to the contract document
    try:
        cdb.update_contract(gid, contract_id,
            mission_type=result.get("mission_type", "active_vessel"),
            required_situation=result.get("required_situation"),
            required_body=result.get("required_body"),
            constraints=result.get("constraints"),
        )
    except Exception as exc:
        log.error("Failed to cache classification for %s: %s", contract_id, exc)

    return result


@app.get("/api/v1/missions/weekly", response_model=WeeklyMissionsResponse)
async def get_weekly_missions(user: dict = Depends(get_current_user)):
    """Get the current week's 20 missions with AI classification."""
    from cogs.weeklymissions import _week_key, _week_bounds, _is_locked, _load_missions, _generate_missions

    gid = int(user["guild_id"])
    now = datetime.now(TZ)
    wk = _week_key(now)

    missions, _ = _load_missions(gid, wk)
    if not missions:
        missions = _generate_missions(wk, settings.WEEKLY_MISSIONS_COUNT)

    # Classify missions (cached — AI runs at most once per week)
    missions = await _classify_missions(missions, wk)

    _, week_end = _week_bounds(now)
    closes_at = (week_end - timedelta(days=1)).isoformat()

    return WeeklyMissionsResponse(
        week_key=wk,
        missions=[Mission(**m) for m in missions],
        is_locked=_is_locked(now),
        closes_at=closes_at,
    )


@app.post("/api/v1/missions/select", response_model=MissionSelectResponse)
async def select_mission(req: MissionSelectRequest, user: dict = Depends(get_current_user)):
    """Accept a weekly mission — creates a contract in Firestore."""
    from cogs.weeklymissions import (
        _week_key, _week_bounds, _is_locked, _load_missions, _generate_missions,
        _has_selected, _save_selection,
    )
    from cogs.corps import _get_corp

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    now = datetime.now(TZ)
    wk = _week_key(now)

    # Locked?
    if _is_locked(now):
        return MissionSelectResponse(success=False, message="Mission selection is locked (Sunday).")

    # Has corp?
    corp = _get_corp(gid, uid)
    if not corp:
        return MissionSelectResponse(success=False, message="You need a corporation first! Use /g corpsetup in Discord.")

    # Already selected?
    if _has_selected(gid, wk, uid, req.mission_id):
        return MissionSelectResponse(success=False, message="You already selected this mission.")

    # Find the mission
    missions, _ = _load_missions(gid, wk)
    if not missions:
        missions = _generate_missions(wk, settings.WEEKLY_MISSIONS_COUNT)

    # Ensure classification is loaded
    missions = await _classify_missions(missions, wk)

    mission = next((m for m in missions if m["id"] == req.mission_id), None)
    if mission is None:
        return MissionSelectResponse(success=False, message="Mission not found.")

    # Create contract
    _, week_end = _week_bounds(now)
    due = (week_end - timedelta(days=1)).strftime("%Y-%m-%d")

    # Use bot user ID as issuer — we need to get it from somewhere
    # The bot instance is stored globally after startup
    bot_user_id = _get_bot_user_id()

    c = cdb.create_contract(
        guild_id=gid,
        issuer_id=bot_user_id,
        issuer_name="Boundless Missions",
        contractor_id=uid,
        contractor_name=user["username"],
        mission=mission["desc_en"],
        payment=mission["coins"],
        fine=mission["fine"],
        due_date=due,
    )
    # Store classification on the contract so KSP can enforce rules. Part-limit
    # constraints are derived from the mission text (heuristic — no extra AI call).
    weekly_constraints = mission.get("constraints") or mc.extract_heuristic(mission.get("desc_en", ""))
    cdb.update_contract(gid, c["contract_id"],
        status=cdb.ACTIVE,
        mission_type=mission.get("mission_type", "active_vessel"),
        required_situation=mission.get("required_situation"),
        required_body=mission.get("required_body"),
        constraints=weekly_constraints,
    )

    # Save selection
    _save_selection(gid, wk, uid, req.mission_id)

    # Create a notification for the user
    _create_notification(gid, uid, "mission_accepted",
                         f"Mission #{req.mission_id} Accepted",
                         f"Weekly mission accepted. Due: {due}. Reward: +{mission['coins']} KCoins, +{mission['xp']} XP.",
                         {"contract_id": c["contract_id"], "mission_id": req.mission_id})

    log.info("KSP: %s accepted weekly mission #%d", user["username"], req.mission_id)

    return MissionSelectResponse(
        success=True,
        contract_id=c["contract_id"],
        message=f"Mission #{req.mission_id} accepted!",
    )


# ── Contracts ────────────────────────────────────────────────────────────────

@app.post("/api/v1/parts/catalog", response_model=PartCatalogResponse)
async def upload_part_catalog(req: PartCatalogUpload, user: dict = Depends(get_current_user)):
    """Receive the KSP client's installed part list so the bot can resolve loosely
    typed part mentions in mission limits. Hash-gated: an unchanged catalog is a
    no-op. Stored in memory and persisted to Firestore to survive restarts."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    key = _catalog_key(gid, uid)

    existing = _get_user_catalog(gid, uid)
    if existing and existing.get("hash") == req.hash and existing.get("parts"):
        return PartCatalogResponse(success=True, stored=False, parts=len(existing["parts"]))

    # Keep only the two fields we use, capped to a sane size.
    parts = [
        {"name": str(p.get("name", "")), "title": str(p.get("title", ""))}
        for p in (req.parts or []) if p.get("name") or p.get("title")
    ][:8000]
    cat = {"hash": req.hash, "parts": parts}
    _PART_CATALOGS[key] = cat
    # Invalidate cached resolutions for any previous catalog of this user.
    for ck in [k for k in _RESOLVE_CACHE if k[0] != req.hash]:
        _RESOLVE_CACHE.pop(ck, None)
    try:
        _catalog_doc(gid, uid).set(cat)
    except Exception as exc:
        log.warning("Could not persist part catalog for %s (memory only): %s", key, exc)

    log.info("Stored part catalog for %s: %d parts (hash %s)", key, len(parts), req.hash[:8])
    return PartCatalogResponse(success=True, stored=True, parts=len(parts))


def _summary_constraints(c: dict, gid: int, uid: str, constraints: dict | None) -> dict | None:
    """The `constraints` object a contract summary ships to the KSP client: the part
    limits (already decided by the caller — stored, AI-classified or heuristic),
    resolved against this user's installed catalog, plus the orbit-regime requirement
    parsed from the mission text. None when there is nothing to enforce.

    Deliberately AI-free, so it can run over a whole list — and so a contract shows
    the same requirements while it is still pending as it does once it is active. The
    orbit requirement is re-derived from the text every time rather than stored, which
    is also what the /submit re-check does.
    """
    if not mc.is_empty(constraints):
        # Resolve loose part mentions against this user's installed catalog so the
        # client filters/checks the exact part, not a fragile substring.
        constraints = _resolve_constraints(constraints, gid, uid, c.get("mission", ""))
    orbit_c = oc.extract_heuristic(c.get("mission", ""))
    # Don't ship an all-empty constraints object to the client — but keep it when
    # there's an orbit requirement even if there are no part limits.
    if mc.is_empty(constraints) and oc.is_empty(orbit_c):
        return None
    out = dict(constraints) if constraints else {}
    if not oc.is_empty(orbit_c):
        out["orbit"] = orbit_c
    return out


def _summary_rescue_target(c: dict, uid: str, include_wreck_parts: bool = True) -> dict | None:
    """A rescue's target as a summary should carry it, or None.

    wreck_parts is only useful to the rescuer's client once they have accepted (to
    tell them live whether the wreck is aboard) and is the biggest field on a rescue
    — one entry per part. Everyone else, and every still-pending offer, gets the
    target without it.
    """
    rt = c.get("rescue_target") or {}
    if not rt:
        return None
    if not rt.get("wreck_parts"):
        return rt
    if not include_wreck_parts or c.get("contractor_id") != uid:
        rt = {**rt, "wreck_parts": []}
    return rt


@app.get("/api/v1/contracts/active", response_model=ContractListResponse)
async def get_active_contracts(user: dict = Depends(get_current_user)):
    """Get all active contracts for the current user."""
    gid = int(user["guild_id"])
    uid = str(user["user_id"])
    bot_uid = str(_get_bot_user_id())

    active_statuses = {cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED, cdb.COMPLETED}
    contracts = []

    for c in cdb.iter_user_contracts(gid, uid):
        if c.get("status") in active_statuses:
            # Auto-classify if missing (human-issued or old contracts)
            mission_type = c.get("mission_type")
            req_sit = c.get("required_situation")
            req_body = c.get("required_body")
            constraints = c.get("constraints")

            # Rescue contracts carry an explicit type — never AI-classify them.
            if not mission_type and c.get("mission_type") != cdb.RESCUE:
                cls = await _classify_single_contract(gid, c["contract_id"], c["mission"])
                mission_type = cls.get("mission_type", "active_vessel")
                req_sit = cls.get("required_situation")
                req_body = cls.get("required_body")
                constraints = cls.get("constraints")
            elif "constraints" not in c and c.get("mission_type") != cdb.RESCUE:
                # Legacy contract from before constraint extraction existed (no
                # constraints field at all) — derive cheaply so it still gets
                # editor/submit enforcement. A contract the AI already classified
                # keeps its stored decision, including a deliberate "no limits"
                # (empty dict) — we must not re-derive over the AI's call here.
                constraints = mc.extract_heuristic(c.get("mission", ""))
            # Part limits (resolved against this user's catalog) plus the orbit-regime
            # requirement parsed from the mission text — see _summary_constraints.
            constraints = _summary_constraints(c, gid, uid, constraints)

            rescue_target = None
            rescue_kerbals = []
            is_modded_target = False
            rescue_vessel_node_url = None
            rescue_pid = None
            if c.get("mission_type") == cdb.RESCUE:
                rt = _summary_rescue_target(c, uid) or {}
                rescue_target = RescueTarget(**rt) if rt else None
                rescue_kerbals = c.get("rescue_kerbals", [])
                is_modded_target = bool(rt.get("is_modded"))
                # Only the rescuer (contractor) gets the wreck node, so their client
                # can spawn/respawn the stranded vessel on demand after accepting.
                if c.get("contractor_id") == uid:
                    rescue_vessel_node_url = c.get("rescue_vessel_node_url")
                # Mirror of the above for the issuer: the id of the craft they gave
                # away, so their client can confirm it really left their save.
                if c.get("issuer_id") == uid:
                    rescue_pid = c.get("rescue_pid")

            contracts.append(ContractSummary(
                contract_id=c["contract_id"],
                mission=c["mission"],
                issuer_name=c.get("issuer_name", "Unknown"),
                contractor_name=c.get("contractor_name", "Unknown"),
                payment=c["payment"],
                fine=c["fine"],
                due_date=c["due_date"],
                status=c["status"],
                created_at=c.get("created_at"),
                is_bot_issued=(c.get("issuer_id") == bot_uid),
                is_outgoing=(c.get("issuer_id") == uid),
                modlist=c.get("modlist"),
                mission_type=mission_type,
                required_situation=req_sit,
                required_body=req_body,
                constraints=constraints,
                rescue_target=rescue_target,
                rescue_kerbals=rescue_kerbals,
                is_modded_target=is_modded_target,
                rescue_vessel_node_url=sign_stored(rescue_vessel_node_url),
                rescue_pid=rescue_pid,
                flag_preview_url=c.get("flag_preview_url"),
                life_support=c.get("life_support", "none") or "none",
                ls_endurance_days=float(c.get("ls_endurance_days") or 0.0),
                ls_crew_capacity=int(c.get("ls_crew_capacity") or 0),
                # Only meaningful while disputed; carried for both parties so the
                # contractor sees "waiting on the issuer" and the issuer sees the ask.
                pending_request=(PendingRequest(**c["pending_request"])
                                 if c.get("pending_request") else None),
                auto_fine_at=(_dt.isoformat()
                              if (_dt := ca.auto_fine_at(c)) else None),
                more_time_used=(int(c.get("more_time_requests") or 0)
                                >= settings.DISPUTE_MAX_MORE_TIME_REQUESTS),
            ))

    # Sort newest first
    contracts.sort(key=lambda c: c.created_at or "", reverse=True)

    return ContractListResponse(contracts=contracts)


@app.get("/api/v1/contracts/incoming", response_model=ContractListResponse)
async def get_incoming_contracts(user: dict = Depends(get_current_user)):
    """Get pending contracts where this user is the contractor.

    These carry the same requirement fields as /contracts/active — what the craft may
    be built from, where it has to get to, and which orbit it has to be in. "No ion
    engines" and "polar orbit" are things to know *before* accepting, and an offer
    that only revealed its terms once accepted was showing them one decision too late.

    What it deliberately doesn't carry is anything only the accepted rescuer can use:
    the wreck's part list and its vessel node stay behind the accept.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])
    bot_uid = str(_get_bot_user_id())

    col = cdb._col(gid)
    contracts = []

    for doc in col.where("contractor_id", "==", uid).stream():
        c = doc.to_dict()
        if c.get("status") != cdb.PENDING:
            continue

        # Never AI-classify here: an inbox is a list, the classifier is a per-contract
        # AI call, and /contracts/active runs it on accept anyway. A contract that has
        # already been classified (weekly missions, anything previously listed) shows
        # its stored terms; one that hasn't shows the part/orbit limits its text
        # implies, which is the cheap half and the half that gates the build.
        constraints = c.get("constraints")
        if "constraints" not in c and c.get("mission_type") != cdb.RESCUE:
            constraints = mc.extract_heuristic(c.get("mission", ""))

        rt = (_summary_rescue_target(c, uid, include_wreck_parts=False)
              if c.get("mission_type") == cdb.RESCUE else None)

        contracts.append(ContractSummary(
            contract_id=c["contract_id"],
            mission=c["mission"],
            issuer_name=c.get("issuer_name", "Unknown"),
            contractor_name=c.get("contractor_name", "Unknown"),
            payment=c["payment"],
            fine=c["fine"],
            due_date=c["due_date"],
            status=c["status"],
            created_at=c.get("created_at"),
            is_bot_issued=(c.get("issuer_id") == bot_uid),
            modlist=c.get("modlist"),
            mission_type=c.get("mission_type", "active_vessel"),
            required_situation=c.get("required_situation"),
            required_body=c.get("required_body"),
            constraints=_summary_constraints(c, gid, uid, constraints),
            rescue_target=RescueTarget(**rt) if rt else None,
            rescue_kerbals=c.get("rescue_kerbals", []),
            is_modded_target=bool((c.get("rescue_target") or {}).get("is_modded")),
            flag_preview_url=c.get("flag_preview_url"),
            life_support=c.get("life_support", "none") or "none",
            ls_endurance_days=float(c.get("ls_endurance_days") or 0.0),
            ls_crew_capacity=int(c.get("ls_crew_capacity") or 0),
        ))

    return ContractListResponse(contracts=contracts)


# ── Contract state transitions ───────────────────────────────────────────────
#
# The transitions themselves live in `contract_actions`, not here. They are driven by
# three front ends — these endpoints, the Discord buttons in `cogs/contract_views.py`,
# and (Phase 6a) the website — and when each front end owned its own copy they drifted
# apart on what a transition *does*, not just on how it reports failure. See that
# module's docstring for the specific divergences that motivated the split.
#
# What stays here is HTTP shape: which failures are a status code and which are a
# 200 with `success: false`. The KSP client distinguishes the two, so the existing
# mapping is preserved exactly.

def _raise_for(result: ca.Result) -> None:
    """Turn the codes the KSP client expects as HTTP failures into HTTP failures.

    Everything else (a wrong-state contract, an unaffordable fine) stays a 200 with
    `success: false`, because the mod renders that as an in-window message rather than
    a transport error.
    """
    if result.code == ca.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Contract not found")
    if result.code == ca.FORBIDDEN:
        raise HTTPException(status_code=403, detail=result.message)
    # USE_GIVE_UP deliberately does *not* land here. It is a business rule with a
    # sentence the player needs to read ("Give Up instead — it costs the fine"), and
    # both clients render a 403 as a generic transport error. `cancel` returns it as a
    # 200 with the message intact.


@app.post("/api/v1/contracts/{contract_id}/accept", response_model=ContractAcceptResponse)
async def accept_contract(contract_id: str, user: dict = Depends(get_current_user)):
    """Accept a pending contract."""
    r = await ca.accept(int(user["guild_id"]), contract_id,
                        actor_id=int(user["user_id"]), actor_name=user["username"])
    _raise_for(r)
    if not r.ok:
        return ContractAcceptResponse(success=False, message=r.message)

    rt = r.data.get("rescue_target") or {}
    return ContractAcceptResponse(
        success=True, message=r.message,
        rescue_vessel_node_url=sign_stored(r.data.get("rescue_vessel_node_url")),
        rescue_target=RescueTarget(**rt) if rt else None,
        rescue_kerbals=r.data.get("rescue_kerbals", []),
    )


@app.post("/api/v1/contracts/{contract_id}/review", response_model=ContractAcceptResponse)
async def review_submission(contract_id: str, req: ContractReviewRequest,
                            user: dict = Depends(get_current_user)):
    """Issuer reviews a submitted contract: approve (→ completed, pay contractor) or
    refuse (→ disputed). Mirrors the Discord ContractReviewView buttons so the review
    can be done from the KSP mod without switching to Discord."""
    r = await ca.review(int(user["guild_id"]), contract_id,
                        actor_id=int(user["user_id"]), actor_name=user["username"],
                        approve=bool(req.approve))
    _raise_for(r)
    return ContractAcceptResponse(success=r.ok, message=r.message)


@app.post("/api/v1/contracts/{contract_id}/dispute", response_model=ContractAcceptResponse)
async def resolve_dispute(contract_id: str, req: ContractDisputeRequest,
                          user: dict = Depends(get_current_user)):
    """Contractor resolves a refused (disputed) submission from the KSP mod, mirroring
    the Discord DisputeView buttons: settle / more_time / pay_fine / sue.

    Actions needing the other party's approval (settle, more_time on human contracts)
    hand off to the existing Discord approval views, exactly like review_submission
    does for the dispute itself."""
    r = await ca.dispute(int(user["guild_id"]), contract_id,
                         actor_id=int(user["user_id"]), actor_name=user["username"],
                         action=req.action or "", new_date=req.new_date or "")
    _raise_for(r)
    return ContractAcceptResponse(success=r.ok, message=r.message)


@app.post("/api/v1/contracts/{contract_id}/settle_response", response_model=ContractAcceptResponse)
async def settle_response_from_ksp(contract_id: str, req: ContractRequestResponse,
                                   user: dict = Depends(get_current_user)):
    """Issuer answers a settlement request from in game.

    Until this existed the answer lived only in a Discord DM, which meant the whole
    dispute flow stalled for anyone playing without Discord open."""
    r = await ca.settle_response(int(user["guild_id"]), contract_id,
                                 actor_id=int(user["user_id"]), actor_name=user["username"],
                                 approve=bool(req.approve))
    _raise_for(r)
    return ContractAcceptResponse(success=r.ok, message=r.message)


@app.post("/api/v1/contracts/{contract_id}/more_time_response", response_model=ContractAcceptResponse)
async def more_time_response_from_ksp(contract_id: str, req: ContractRequestResponse,
                                      user: dict = Depends(get_current_user)):
    """Issuer answers a deadline-extension request from in game.

    No date is accepted here on purpose — the granted date is the one stored on the
    contract when the contractor asked, so approving cannot quietly grant a different
    extension than the one requested."""
    r = await ca.more_time_response(int(user["guild_id"]), contract_id,
                                    actor_id=int(user["user_id"]), actor_name=user["username"],
                                    approve=bool(req.approve))
    _raise_for(r)
    return ContractAcceptResponse(success=r.ok, message=r.message)


@app.post("/api/v1/contracts/{contract_id}/reimport_submission", response_model=ContractAcceptResponse)
async def reimport_submission(contract_id: str, user: dict = Depends(get_current_user)):
    """Give the contractor back the craft they submitted, from the server's own copy.

    The hole this fills: land at the target, submit, hit KSP's Recover button — the
    craft (and its crew) leave the save through stock recovery. If the issuer then
    refuses and the dispute ends in more time, the contract is active again but the
    delivery craft no longer exists to re-fly. The submission already uploaded the
    full vessel node (it is how an approval delivers the craft to the issuer), so
    restoring is just queueing that same node back to its *builder* as a live-vessel
    import — the exact mechanism _restore_issuer_vessel already uses for the issuer's
    wreck on a failed rescue.

    Client-initiated rather than automatic on the more-time approval, because only
    the client knows whether the craft is actually gone — most contractors still have
    it, and spawning a duplicate would be worse than the gap. Idempotent: the import
    queue de-dupes on (source, ref_id), so a second press while one is pending
    returns the queued entry rather than a second craft.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        return ContractAcceptResponse(success=False, message="Contract not found.")
    if c.get("contractor_id") != str(uid):
        return ContractAcceptResponse(success=False, message="Not your contract.")
    if c.get("mission_type") != cdb.RESCUE:
        return ContractAcceptResponse(
            success=False, message="Only a rescue delivery has a stored craft to restore.")
    # Active (a dispute granted more time) or still disputed. Never completed — the
    # craft belongs to the issuer then — and never pending/submitted, where the local
    # copy is still the player's to keep or the review hasn't happened yet.
    if c.get("status") not in (cdb.ACTIVE, cdb.DISPUTED):
        return ContractAcceptResponse(
            success=False, message="This contract isn't in a state where the craft can be restored.")
    url = c.get("delivered_vessel_node_url")
    if not url:
        return ContractAcceptResponse(
            success=False, message="No submitted craft is stored for this contract.")

    craft_name = (c.get("vessel_data") or {}).get("vessel_name") or "Submitted craft"
    # owner_name is the contractor themselves: on import their own crew's names come
    # back untagged while the rescued kerbals keep the issuer's tag — exactly the
    # state the save was in before the recovery.
    imp.enqueue(gid, uid, "submission_restore", contract_id, craft_name,
                vessel_node_url=url, owner_name=user["username"])
    log.info("Rescue %s: queued submitted-craft restore for contractor %d", contract_id, uid)
    return ContractAcceptResponse(
        success=True,
        message="Craft restore queued — it spawns where it was when you submitted, "
                "on your next Space Center visit.")


@app.post("/api/v1/contracts/{contract_id}/cancel", response_model=ContractAcceptResponse)
async def cancel_contract(contract_id: str, user: dict = Depends(get_current_user)):
    """Withdraw (issuer) or decline (contractor) a contract that has not finished.

    A contractor may only cancel while the offer is still pending — backing out after
    accepting is `give_up`, which costs the agreed fine. Cancelling here used to be a
    free alternative to that, which made the fine optional.
    """
    r = await ca.cancel(int(user["guild_id"]), contract_id,
                        actor_id=int(user["user_id"]), actor_name=user["username"])
    return ContractAcceptResponse(success=r.ok, message=r.message)


@app.post("/api/v1/contracts/{contract_id}/give_up", response_model=ContractAcceptResponse)
async def give_up_contract(contract_id: str, user: dict = Depends(get_current_user)):
    """Contractor gives up on an active contract they accepted, paying the agreed fine
    to the issuer (who also gets their escrowed payment back)."""
    r = await ca.give_up(int(user["guild_id"]), contract_id,
                         actor_id=int(user["user_id"]), actor_name=user["username"])
    return ContractAcceptResponse(success=r.ok, message=r.message)


# ── Corporations ─────────────────────────────────────────────────────────────

@app.get("/api/v1/corps/list", response_model=CorpListResponse)
async def list_corps(user: dict = Depends(get_current_user)):
    """
    List all corporations in the guild.

    avatar_url and level are extras for the KSP mod's player picker. Both come from
    caches only — guild.get_member and the in-memory store — never fetch_member: this
    endpoint is one round trip per picker open, and N members * one Discord fetch each
    would turn that into a multi-second stall on a large guild. A member missing from
    the cache simply comes back without an avatar, which the client renders as initials.
    """
    gid = int(user["guild_id"])
    guild = _bot_instance.get_guild(gid) if _bot_instance else None

    corps_col = _db.collection("guilds").document(str(gid)).collection("corps")
    corps = []
    for doc in corps_col.stream():
        d = doc.to_dict()
        if not d:
            continue

        avatar_url = None
        level = 0
        try:
            uid = int(doc.id)
            member = guild.get_member(uid) if guild else None
            if member is not None:
                asset = member.display_avatar
                # 64px because these draw at ~40px in a list and every one is proxied
                # through the mod's image route, and PNG because Nitro members have
                # animated avatars whose default URL is a .gif — which the mod's proxy
                # sniffs and rejects, since it only re-serves PNG/JPEG/WebP. Some assets
                # reject replace() arguments, so fall back to the plain URL.
                try:
                    avatar_url = asset.replace(size=64, format="png").url
                except Exception:
                    avatar_url = asset.url
            level = store.get_user(gid, uid).get("level", 0)
        except (ValueError, TypeError):
            pass  # non-numeric doc id — still listable, just without the extras

        corps.append(CorpInfo(
            owner_id=doc.id,
            owner_name=d.get("owner_name", d.get("name", "Unknown")),
            corp_name=d.get("name", "Unknown Corp"),
            avatar_url=avatar_url,
            level=level,
        ))

    return CorpListResponse(corps=corps)


@app.post("/api/v1/contracts/create", response_model=ContractAcceptResponse)
async def create_contract_from_ksp(req: ContractCreateRequest, user: dict = Depends(get_current_user)):
    """Create a new contract from the KSP mod (issuer = current user, contractor = corp owner)."""
    from datetime import date
    from cogs.corps import _get_corp

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    contractor_id = int(req.contractor_id)

    # Self-contract check
    if contractor_id == uid and not settings.CONTRACT_ALLOW_SELF:
        return ContractAcceptResponse(success=False, message="You can't contract yourself.")

    # Validate date
    try:
        dt = datetime.strptime(req.due_date, "%Y-%m-%d").date()
        if dt <= date.today():
            return ContractAcceptResponse(success=False, message="Due date must be in the future.")
    except ValueError:
        return ContractAcceptResponse(success=False, message="Invalid date format. Use YYYY-MM-DD.")

    # Check contract limit
    count = cdb.count_active(gid, uid)
    if count >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
        return ContractAcceptResponse(
            success=False,
            message=f"Active contract limit reached ({settings.MAX_ACTIVE_CONTRACTS_PER_USER}).",
        )

    # Resolve contractor name from corp data
    corp = _get_corp(gid, contractor_id)
    contractor_name = corp.get("owner_name", "Unknown") if corp else "Unknown"

    # Escrow: lock the payment. Atomic check-and-deduct so concurrent requests
    # can't both escrow from the same balance (double-spend).
    if not await store.try_debit(gid, uid, req.payment):
        bal = store.get_user(gid, uid).get("balance", 0)
        return ContractAcceptResponse(
            success=False,
            message=f"Insufficient balance ({req.payment} needed, you have {bal}).",
        )

    # Create contract
    c = cdb.create_contract(
        guild_id=gid,
        issuer_id=uid,
        issuer_name=user["username"],
        contractor_id=contractor_id,
        contractor_name=contractor_name,
        mission=req.mission,
        payment=req.payment,
        fine=req.fine,
        due_date=req.due_date,
        modlist=req.modlist,
    )

    # Always let the AI read the mission text and decide the constraints (and
    # situation/body), even when the caller pins the contract type — otherwise
    # craft-build contracts, which are exactly the ones that carry part limits,
    # would never get AI-extracted limits. An explicit craft_build/active_vessel
    # then overrides only the *type* the AI guessed. flag_design isn't a vessel,
    # so it skips extraction entirely.
    ctype = (req.contract_type or "auto").lower()
    if ctype == "flag_design":
        cdb.update_contract(gid, c["contract_id"], mission_type=ctype)
    else:
        await _classify_single_contract(gid, c["contract_id"], req.mission)
        if ctype in ("craft_build", "active_vessel"):
            cdb.update_contract(gid, c["contract_id"], mission_type=ctype)

    # Tell the contractor on Discord: their corp channel, falling back to a DM.
    if _bot_instance:
        try:
            from cogs.contract_views import ContractOfferView, _embed

            e = _embed(c, gid)
            e.description = f"📜 You received a new contract offer from **{user['username']}** (via KSP)!"
            dm_msg = await ca.deliver_to_player(
                gid, contractor_id, embed=e, view=ContractOfferView(c["contract_id"], gid))
            if dm_msg is not None:
                cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm_msg.id))
        except Exception as exc:
            log.error("Failed to notify contractor %d on Discord: %s", contractor_id, exc)
            # Don't fail the contract creation — they'll see it in notifications

    # Also create a notification
    _create_notification(
        gid, contractor_id, "contract_incoming",
        "📜 New Contract Offer",
        f"{user['username']} sent you a contract: {req.mission[:100]}",
        {"contract_id": c["contract_id"]},
    )

    log.info("KSP: %s created contract %s for user %d (%d coins)",
             user["username"], c["contract_id"], contractor_id, req.payment)

    return ContractAcceptResponse(success=True, message=f"Contract sent! ID: {c['contract_id']}")


async def _open_auction_checked(gid: int, uid: int, username: str,
                                req: AuctionCreateRequest) -> ContractAcceptResponse:
    """Validate and open a reverse auction — the flow behind the KSP mod's
    /auctions/create, the only place an auction can start. Escrows start_value
    and posts to the Discord auction channels; bidding/closing then happen in
    Discord or on the website."""
    from datetime import date

    if _bot_instance is None or not guild_config.any_channel_configured(_bot_instance, "auction"):
        return ContractAcceptResponse(success=False, message="Auctions are not available right now.")
    if not (settings.AUCTION_MIN_DURATION_HOURS <= req.duration_hours <= settings.AUCTION_MAX_DURATION_HOURS):
        return ContractAcceptResponse(
            success=False,
            message=f"Duration must be {settings.AUCTION_MIN_DURATION_HOURS}–{settings.AUCTION_MAX_DURATION_HOURS} hours.",
        )
    # Checked here rather than on the model so a client that gets it wrong reads a
    # sentence explaining the floor instead of a 422 field error.
    if req.start_value < settings.AUCTION_MIN_START_VALUE:
        return ContractAcceptResponse(
            success=False,
            message=(f"Starting price must be at least {settings.AUCTION_MIN_START_VALUE} KCoins — "
                     f"a bid has to undercut it by {settings.AUCTION_MIN_DECREMENT} and stay above "
                     "zero, so anything lower leaves no bid anyone could place."),
        )
    try:
        dt = datetime.strptime(req.due_date, "%Y-%m-%d").date()
        if dt <= date.today():
            return ContractAcceptResponse(success=False, message="Due date must be in the future.")
    except ValueError:
        return ContractAcceptResponse(success=False, message="Invalid date format. Use YYYY-MM-DD.")

    bal = store.get_user(gid, uid).get("balance", 0)
    if bal < req.start_value:
        return ContractAcceptResponse(
            success=False,
            message=f"Insufficient balance ({req.start_value} needed, you have {bal}).",
        )
    if cdb.count_active(gid, uid) >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
        return ContractAcceptResponse(
            success=False,
            message=f"Active contract limit reached ({settings.MAX_ACTIVE_CONTRACTS_PER_USER}).",
        )
    if not _bot_instance:
        return ContractAcceptResponse(success=False, message="Bot is offline. Try again shortly.")

    # Build / active / flag types carry through to the winner's contract; anything
    # else (including "auto" and "rescue", which is a different endpoint) leaves it
    # untyped. A flag design has no in-game build step, so a part restriction on one
    # would mean nothing — dropped here as well as in the clients that send it.
    mission_type = req.contract_type if req.contract_type in (
        "craft_build", "active_vessel", cdb.FLAG_DESIGN) else None
    modlist = None if mission_type == cdb.FLAG_DESIGN else req.modlist
    try:
        from cogs.auctions import open_auction
        a = await open_auction(
            _bot_instance, gid, uid, username, req.mission,
            req.start_value, req.fine, req.due_date, req.duration_hours, modlist,
            mission_type=mission_type,
        )
    except ValueError:
        # Atomic escrow lost the race against another spend — funds no longer cover it.
        bal = store.get_user(gid, uid).get("balance", 0)
        return ContractAcceptResponse(
            success=False,
            message=f"Insufficient balance ({req.start_value} needed, you have {bal}).",
        )
    except Exception as exc:
        log.error("Auction create failed for user %d: %s", uid, exc)
        return ContractAcceptResponse(success=False, message="Could not post the auction. Try again.")

    log.info("%s opened auction %s (start %d, %dh)",
             username, a["auction_id"], req.start_value, req.duration_hours)
    return ContractAcceptResponse(success=True, message="Auction posted to Discord!")


@app.post("/api/v1/auctions/create", response_model=ContractAcceptResponse)
async def create_auction_from_ksp(req: AuctionCreateRequest, user: dict = Depends(get_current_user)):
    """Open a reverse auction from the KSP mod. Escrows start_value, posts it to the
    Discord auction channel; bidding/closing happen in Discord or on the website."""
    return await _open_auction_checked(int(user["guild_id"]), int(user["user_id"]),
                                       user["username"], req)


# Stock Kerbol-system bodies — used as a server-side fallback to flag a rescue
# target as "modded" when the client didn't send is_modded. (KNOWN_CELESTIAL_BODIES
# deliberately also lists popular modded bodies, so it can't be used for this.)
_STOCK_BODIES = {
    "kerbol", "sun", "moho", "eve", "gilly", "kerbin", "mun", "minmus",
    "duna", "ike", "dres", "jool", "laythe", "vall", "tylo", "bop", "pol", "eeloo",
}


@app.post("/api/v1/contracts/create_rescue", response_model=ContractAcceptResponse)
async def create_rescue_contract(
    contractor_id: str = Form(...),
    mission: str = Form(...),
    payment: int = Form(...),
    fine: int = Form(0),
    due_date: str = Form(...),
    modlist: Optional[str] = Form(None),
    body: str = Form(...),
    mode: str = Form("orbit"),
    ap: Optional[float] = Form(None),
    pe: Optional[float] = Form(None),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
    margin_alt: float = Form(0.0),
    margin_pos: float = Form(0.0),
    # Orbit-mode only: the plane (inclination ± margin) and any named orbital regimes
    # the delivery orbit has to be. Both absent == any orbit with the right Ap/Pe,
    # which is what every rescue issued before these fields existed asked for.
    inc: Optional[float] = Form(None),
    margin_inc: float = Form(0.0),
    orbit_types: str = Form(""),       # comma-separated canonical tokens
    is_modded: bool = Form(False),
    rescue_pid: Optional[str] = Form(None),
    kerbals: str = Form("[]"),         # JSON list of tagged names: ["{issuer}'s Jeb Kerman", ...]
    vessel_node: UploadFile = File(...),  # gzipped issuer vessel snapshot (the wreck)
    # Life-support provisioning of the wreck, scanned on the issuer's client. The
    # rescuer's client compares it with their own install: a wreck built for another LS
    # mod carries nothing they can use, so its crew stay in emergency freeze and the
    # wreck is stocked with a ration kit of the rescuer's own life support.
    life_support: str = Form("none"),
    ls_endurance_days: float = Form(0.0),
    ls_crew_capacity: int = Form(0),
    # What the rescuer has to bring back: "crew" (the kerbals, wreck optional) or
    # "vessel" (the wreck too). min_dv is a floor on the delivering craft's remaining
    # vacuum delta-v so the crew aren't stranded a second time. 0 = no requirement.
    recovery: str = Form("crew"),
    min_dv: float = Form(0.0),
    user: dict = Depends(get_current_user),
):
    """Create a rescue contract from the KSP mod.

    The issuer is in flight on a crewed vessel; their client snapshots that vessel
    (crew kept as-is — they're tagged "{issuer}'s {kerbal}" when the rescuer imports
    the wreck), captures the delivery target, and uploads it here. The wreck node is
    stored so the rescuer's client can spawn it on accept. The issuer's client removes
    its own copy of the vessel locally once this returns success.
    """
    import json

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    try:
        contractor_uid = int(contractor_id)
    except (TypeError, ValueError):
        return ContractAcceptResponse(success=False, message="Invalid contractor.")

    if contractor_uid == uid and not settings.CONTRACT_ALLOW_SELF:
        return ContractAcceptResponse(success=False, message="You can't contract yourself.")

    # Validate date
    from datetime import date
    try:
        dt = datetime.strptime(due_date, "%Y-%m-%d").date()
        if dt <= date.today():
            return ContractAcceptResponse(success=False, message="Due date must be in the future.")
    except ValueError:
        return ContractAcceptResponse(success=False, message="Invalid date format. Use YYYY-MM-DD.")

    # The tagged kerbal names the rescuer must recover ("{issuer}'s {kerbal}").
    try:
        rescue_kerbals = json.loads(kerbals) if kerbals else []
        if not isinstance(rescue_kerbals, list):
            rescue_kerbals = []
    except Exception:
        rescue_kerbals = []
    rescue_kerbals = [str(k) for k in rescue_kerbals if k]
    if not rescue_kerbals:
        return ContractAcceptResponse(success=False, message="No crew aboard to rescue.")

    # Balance + active-contract limit (same gates as a regular contract).
    u = store.get_user(gid, uid)
    if u.get("balance", 0) < payment:
        return ContractAcceptResponse(
            success=False, message=f"Insufficient balance ({payment} needed).")
    if cdb.count_active(gid, uid) >= settings.MAX_ACTIVE_CONTRACTS_PER_USER:
        return ContractAcceptResponse(
            success=False,
            message=f"Active contract limit reached ({settings.MAX_ACTIVE_CONTRACTS_PER_USER}).")

    from cogs.corps import _get_corp
    corp = _get_corp(gid, contractor_uid)
    contractor_name = corp.get("owner_name", "Unknown") if corp else "Unknown"

    if not is_modded and body.strip().lower() not in _STOCK_BODIES:
        is_modded = True

    recovery = (recovery or "crew").strip().lower()
    if recovery not in ("crew", "vessel"):
        return ContractAcceptResponse(
            success=False, message="Recovery must be 'crew' or 'vessel'.")
    if min_dv < 0 or min_dv != min_dv or min_dv in (float("inf"), float("-inf")):
        return ContractAcceptResponse(
            success=False, message="Invalid delta-v requirement.")

    # Orbital plane + regime, both orbit-mode only. A surface target has no orbit to
    # constrain, so anything sent with one is dropped rather than stored unreachable.
    mode_l = (mode or "orbit").lower()
    req_inc: float | None = None
    req_margin_inc = 0.0
    req_types: list[str] = []
    if mode_l == "orbit":
        req_types = oc.normalize_types(orbit_types)
        # A contradictory pair (polar + equatorial, circular + elliptical, …) is not
        # a strict contract, it is one no orbit can ever satisfy — refuse it here
        # rather than escrow money against an unfillable target. The client's form
        # de-selects conflicts as they are picked, so this only fires for an old or
        # hand-rolled client.
        _bad = oc.conflicting_pair(req_types)
        if _bad:
            return ContractAcceptResponse(
                success=False,
                message=f"Orbit types '{oc.label(_bad[0])}' and '{oc.label(_bad[1])}' "
                        "contradict each other — no orbit can be both.")
        if inc is not None:
            if not math.isfinite(inc) or not 0.0 <= inc <= 180.0:
                return ContractAcceptResponse(
                    success=False, message="Inclination must be between 0° and 180°.")
            if not math.isfinite(margin_inc):
                return ContractAcceptResponse(
                    success=False, message="Invalid inclination margin.")
            req_inc = float(inc)
            # A margin is a floor, not an error: too tight a tolerance makes the
            # contract impossible rather than malformed (same as the Ap/Pe margin).
            req_margin_inc = max(float(margin_inc) if margin_inc > 0
                                 else settings.RESCUE_INCL_MARGIN_DEFAULT,
                                 settings.RESCUE_INCL_MARGIN_MIN)

    rescue_target = {
        "body": body, "mode": mode_l,
        "ap": ap, "pe": pe, "lat": lat, "lon": lon,
        "margin_alt": margin_alt, "margin_pos": margin_pos, "is_modded": is_modded,
        "recovery": recovery, "min_dv": float(min_dv),
        "inc": req_inc, "margin_inc": req_margin_inc, "orbit_types": req_types,
    }

    # Escrow the payment (atomic check-and-deduct — no double-spend across requests).
    if not await store.try_debit(gid, uid, payment):
        return ContractAcceptResponse(
            success=False, message=f"Insufficient balance ({payment} needed).")

    c = cdb.create_contract(
        guild_id=gid, issuer_id=uid, issuer_name=user["username"],
        contractor_id=contractor_uid, contractor_name=contractor_name,
        mission=mission, payment=payment, fine=fine, due_date=due_date,
        modlist=modlist,
        mission_type=cdb.RESCUE,
        rescue_target=rescue_target,
        rescue_kerbals=rescue_kerbals,
        rescue_pid=rescue_pid,
        life_support=life_support,
        ls_endurance_days=ls_endurance_days,
        ls_crew_capacity=ls_crew_capacity,
    )

    # Store the wreck snapshot (gzipped ConfigNode) in Firebase Storage.
    try:
        node_bytes = await _read_upload(vessel_node)
        # Private, like the submitted vessel node: the wreck is transferred only to
        # the rescuer, served via a signed URL (get_incoming/active_contracts and the
        # import-queue serve point).
        node_url = await cdb.upload_private_to_storage(
            c["contract_id"], "rescue_vessel.cfg", node_bytes, "application/gzip")
        updates: dict = {"rescue_vessel_node_url": node_url}
        # On a "vessel" recovery the wreck's own parts are the evidence it came home,
        # so pin them now. Taken from the node rather than trusted from the client:
        # the same bytes the rescuer will spawn are the ones we check against.
        if recovery == "vessel":
            rescue_target["wreck_parts"] = _extract_part_uids(node_bytes)
            updates["rescue_target"] = rescue_target
        cdb.update_contract(gid, c["contract_id"], **updates)
    except Exception as exc:
        # Roll the contract back — without the wreck node the rescue can't happen.
        log.error("Rescue vessel upload failed for %s: %s", c["contract_id"], exc)
        cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
        await store.add_balance(gid, uid, payment)
        return ContractAcceptResponse(success=False, message="Failed to store the rescue vessel.")

    # Notify the contractor on Discord, exactly like create_contract_from_ksp:
    # corp channel first, DM fallback.
    if _bot_instance:
        try:
            from cogs.contract_views import ContractOfferView, _embed
            e = _embed(c, gid)
            e.description = (f"🛟 **{user['username']}** needs a rescue at **{body}** "
                             f"({len(rescue_kerbals)} kerbal(s)), via KSP!")
            dm_msg = await ca.deliver_to_player(
                gid, contractor_uid, embed=e, view=ContractOfferView(c["contract_id"], gid))
            if dm_msg is not None:
                cdb.update_contract(gid, c["contract_id"], dm_message_id=str(dm_msg.id))
        except Exception as exc:
            log.error("Failed to notify rescue contractor %d: %s", contractor_uid, exc)

    _create_notification(
        gid, contractor_uid, "contract_incoming",
        "🛟 New Rescue Mission",
        f"{user['username']} needs {len(rescue_kerbals)} kerbal(s) rescued at {body}.",
        {"contract_id": c["contract_id"]},
    )

    log.info("KSP: %s created RESCUE contract %s for user %d (%d coins, %d kerbals)",
             user["username"], c["contract_id"], contractor_uid, payment, len(rescue_kerbals))
    return ContractAcceptResponse(success=True, message=f"Rescue contract sent! ID: {c['contract_id']}")


def _extract_crew_names(vn_data: bytes | None) -> set[str]:
    """Pull assigned crew names out of a (gzipped) vessel ConfigNode. KSP stores
    assigned crew as `crew = <Name>` lines on PART nodes; rescued kerbals keep
    their renamed "{issuer}'s {kerbal} Kerman" names in the rescue craft."""
    import gzip
    import re
    if not vn_data:
        return set()
    try:
        text = _safe_gunzip(vn_data).decode("utf-8", "ignore")
    except (OSError, EOFError):
        text = vn_data.decode("utf-8", "ignore")
    except Exception:
        return set()
    names: set[str] = set()
    for m in re.finditer(r"^\s*crew\s*=\s*(.+?)\s*$", text, re.MULTILINE):
        val = m.group(1).strip()
        if val and not val.isdigit():
            names.add(val)
    return names


# Share of the wreck's original parts that must arrive on a "vessel" recovery. Not
# 100%: a tow that loses a solar panel to a docking bump is still the wreck brought
# home. Must match ContractCreation.WreckCoverageRequired in the KSP mod, or the
# client would pass a submission the server then rejects.
_WRECK_COVERAGE_REQUIRED = 0.5


def _extract_part_uids(vn_data: bytes | None) -> list[str]:
    """Pull part flightIDs out of a (gzipped) vessel ConfigNode. KSP writes them as
    `uid = <n>` on each PART node and preserves them across export, import, docking
    and undocking — which is what lets a towed wreck still be recognised as itself."""
    import re
    if not vn_data:
        return []
    try:
        text = _safe_gunzip(vn_data).decode("utf-8", "ignore")
    except (OSError, EOFError):
        text = vn_data.decode("utf-8", "ignore")
    except Exception:
        return []
    seen: set[str] = set()
    uids: list[str] = []
    for m in re.finditer(r"^\s*uid\s*=\s*(\d+)\s*$", text, re.MULTILINE):
        val = m.group(1)
        # uid 0 is KSP's "unset" — every editor part carries it, so it identifies
        # nothing and would make coverage meaningless.
        if val == "0" or val in seen:
            continue
        seen.add(val)
        uids.append(val)
    return uids


def _validate_rescue_submission(c: dict, vessel_data: str | None, vn_data: bytes | None,
                                delta_v_vac: str | None = None):
    """Defense-in-depth recheck of a rescue submission: right body + situation
    (from telemetry), the target orbit's plane and regime when the issuer named one
    (from telemetry), every stranded kerbal aboard (from the node), the wreck itself
    aboard on a "vessel" recovery (part flightIDs, from the node), and any delta-v
    floor the issuer set (from telemetry). Returns (ok, reason). The Ap/Pe and
    lat/lon margins are enforced authoritatively client-side."""
    rt = c.get("rescue_target") or {}
    body = (rt.get("body") or "").strip().lower()
    mode = (rt.get("mode") or "orbit").lower()

    vd: dict = {}
    if vessel_data:
        import json
        try:
            payload = json.loads(vessel_data)
        except Exception:
            payload = None
        # The submission wraps the snapshot: {"contract_id": …, "active_vessel": {…}}.
        # Fall back to the bare snapshot so a hand-rolled payload still gets checked.
        if isinstance(payload, dict):
            vd = payload.get("active_vessel") or payload
        if not isinstance(vd, dict):
            vd = {}
    if vd:
        vbody = (vd.get("body") or "").strip().lower()
        if body and vbody and vbody != body:
            return False, f"Rescue craft is at {vd.get('body')}, must be at {rt.get('body')}."
        sit = (vd.get("situation") or "").upper()
        if mode == "orbit" and sit and sit != "ORBITING":
            return False, "Rescue craft must be ORBITING the target."
        if mode == "surface" and sit and sit not in ("LANDED", "SPLASHED"):
            return False, "Rescue craft must be LANDED/SPLASHED at the target."

        # The plane and the regime of the delivery orbit. Ap/Pe say nothing about
        # either, so without these a craft can sit in an equatorial orbit and satisfy
        # a rescue from a polar one. Elements the client didn't report are skipped
        # rather than failed, like every other telemetry-derived check here.
        if mode == "orbit":
            incl = vd.get("inclination")
            msg = oc.check_inclination(rt.get("inc"), rt.get("margin_inc"), incl)
            if msg:
                return False, f"Rescue craft is in the wrong orbital plane. {msg}"
            violations = oc.verify_types(rt.get("orbit_types"), vd)
            if violations:
                return False, ("Rescue craft isn't in the orbit this contract requires:\n- "
                               + "\n- ".join(violations))

    names = _extract_crew_names(vn_data)
    if names:
        missing = [k for k in c.get("rescue_kerbals", []) if k not in names]
        if missing:
            return False, f"Rescue craft is missing kerbals: {', '.join(missing)}."

    # "vessel" recovery: the wreck itself has to be in what was submitted. wreck_parts
    # was pinned from the uploaded node at creation, so this compares the handed-over
    # craft against the returned one by part flightID.
    if (rt.get("recovery") or "crew").lower() == "vessel":
        wreck = {str(u) for u in (rt.get("wreck_parts") or [])}
        if wreck:
            here = set(_extract_part_uids(vn_data))
            found = len(wreck & here)
            needed = max(1, math.ceil(len(wreck) * _WRECK_COVERAGE_REQUIRED))
            if found < needed:
                return False, (
                    f"This rescue requires the stranded vessel itself: only {found} of "
                    f"{len(wreck)} of its parts came back (at least {needed} needed).")

    # Delta-v floor, so the crew are delivered somewhere they can leave from. The
    # client sends -1/blank when the stock readout can't give it a number; a value we
    # can't read is not a violation we can prove, and the client already warned.
    min_dv = float(rt.get("min_dv") or 0.0)
    if min_dv > 0 and delta_v_vac not in (None, ""):
        try:
            dv = float(delta_v_vac)
        except (TypeError, ValueError):
            dv = -1.0
        # Same 0.5% slack as the mission-limit delta-v check, so a craft sitting right
        # on the number isn't failed by rounding between client and server.
        if dv >= 0 and dv < min_dv * 0.995:
            return False, (f"Rescue craft has {dv:.0f} m/s delta-v left, "
                           f"this contract requires at least {min_dv:.0f} m/s.")

    return True, ""


async def _deliver_rescue_craft(gid: int, contract_id: str, c: dict):
    """On approval: deliver the rescue craft (now carrying the kerbals home) to the
    issuer as a live-vessel import. Crew names are tagged/stripped by the issuer's
    client on import (their own kerbals strip back to original), so no server-side
    rename is needed. owner_name = the contractor who handed the craft over."""
    url = c.get("delivered_vessel_node_url") or c.get("vessel_node_url")
    if not url:
        log.warning("Rescue %s approved but has no delivered vessel node.", contract_id)
        return
    issuer_id = int(c["issuer_id"])
    craft_name = (c.get("vessel_data") or {}).get("vessel_name") or "Rescue Craft"
    imp.enqueue(gid, issuer_id, "rescue_delivery", contract_id, craft_name,
                vessel_node_url=url, owner_name=c.get("contractor_name", ""))
    # Credit the rescuer with a completed rescue for the leaderboard/stats.
    try:
        await store.add_rescue(gid, int(c["contractor_id"]))
    except Exception as exc:
        log.warning("Could not record rescue stat for contract %s: %s", contract_id, exc)
    _create_notification(
        gid, issuer_id, "rescue_delivered", "🛟 Kerbals Returned!",
        "Your rescued kerbals are home. The rescue craft will appear in your save.",
        {"contract_id": contract_id},
    )
    log.info("Rescue %s: delivered craft to issuer %d", contract_id, issuer_id)


async def _restore_issuer_vessel(gid: int, contract_id: str, c: dict):
    """On failure (cancel / rescuer paid fine / etc.): give the issuer their original
    vessel back at its original spot. The stored wreck node holds the original orbit
    and crew; owner_name = the issuer, so their client strips any tag back to the
    original kerbal names on import."""
    if not c.get("issuer_vessel_removed"):
        return  # never removed (e.g. failed before acceptance) → nothing to do
    url = c.get("rescue_vessel_node_url")
    if not url:
        log.warning("Rescue %s failed but has no stored wreck node to restore.", contract_id)
        return
    issuer_id = int(c["issuer_id"])
    imp.enqueue(gid, issuer_id, "rescue_delivery", contract_id, "Stranded Vessel",
                vessel_node_url=url, owner_name=c.get("issuer_name", ""))
    cdb.update_contract(gid, contract_id, issuer_vessel_removed=False)
    _create_notification(
        gid, issuer_id, "rescue_failed", "↩️ Rescue Cancelled",
        "The rescue didn't go through. Your vessel is being returned to its place.",
        {"contract_id": contract_id},
    )
    log.info("Rescue %s: restored issuer %d vessel", contract_id, issuer_id)


# ── Submissions ──────────────────────────────────────────────────────────────

@app.post("/api/v1/contracts/{contract_id}/submit", response_model=SubmissionResult)
async def submit_contract(
    contract_id: str,
    craft_file: Optional[UploadFile] = File(None),
    vessel_node: Optional[UploadFile] = File(None),
    loadmeta: Optional[str] = Form(None),
    vessel_data: Optional[str] = Form(None),
    screenshot1: Optional[UploadFile] = File(None),
    screenshot2: Optional[UploadFile] = File(None),
    screenshot3: Optional[UploadFile] = File(None),
    # Multi-vessel submissions send one render per craft under this repeated field
    # (uncapped). The numbered fields above are kept for older clients.
    screenshots: List[UploadFile] = File(default=[]),
    modlist: Optional[str] = Form(None),
    used_modlist: Optional[str] = Form(None),
    # Per-part summary of the submitted craft (JSON array) used to verify the
    # contract's part-limit constraints. See data/mission_constraints.py.
    used_parts: Optional[str] = Form(None),
    # Craft's stock-calculated vacuum Δv (m/s) — the bot can't recompute it, so a
    # min/max-Δv mission limit is verified against this client-reported value.
    delta_v_vac: Optional[str] = Form(None),
    # Life-support flag of the submitted craft (which LS mod it's provisioned for, its
    # per-kerbal endurance and crew capacity) — stored on the contract for the review embed.
    life_support: Optional[str] = Form(None),
    ls_endurance_days: float = Form(0.0),
    ls_crew_capacity: int = Form(0),
    user: dict = Depends(get_current_user),
):
    """
    Submit a contract completion with craft file, loadmeta, vessel data, and screenshots.
    Files are uploaded to Firebase Storage. AI review is triggered for bot-issued contracts.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    bot_uid = _get_bot_user_id()
    _note_user_action(gid, uid, user.get("username", ""), "submit", *settings.FLOOD_SUBMIT)

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if c.get("contractor_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your contract")

    if c.get("status") != cdb.ACTIVE:
        return SubmissionResult(success=False, message="Contract is not active.")

    # Read the vessel node once (used by both the rescue check below and the
    # Storage upload further down). UploadFile.read() can only be consumed once.
    vn_data = await _read_upload(vessel_node) if vessel_node else None

    # Rescue: server-side defense-in-depth before accepting the submission — the
    # rescue craft must be at the target body/situation and carry every stranded
    # kerbal. The client gates this too, but a modified DLL must not bypass it.
    if c.get("mission_type") == cdb.RESCUE:
        ok, reason = _validate_rescue_submission(c, vessel_data, vn_data, delta_v_vac)
        if not ok:
            return SubmissionResult(success=False, message=reason)

    # Server-side part-restriction check (defense-in-depth — the client also gates this,
    # but an old/modified DLL must not bypass it). The contract's modlist is an allow-list
    # of top-level mod folders (tokens prefixed with "-" are client-only exclude paths we
    # can't evaluate at folder granularity, so they're ignored here). used_modlist is the
    # set of folders the submitted craft actually uses. Skipped if either side is absent.
    required_modlist = c.get("modlist")
    if required_modlist and used_modlist:
        allowed = {
            tok.strip().lower()
            for tok in required_modlist.split(",")
            if tok.strip() and not tok.strip().startswith("-")
        }
        if allowed:
            illegal = sorted(
                f.strip()
                for f in used_modlist.split(",")
                if f.strip() and f.strip().lower() not in allowed
            )
            if illegal:
                log.info("Submission rejected for contract %s: craft uses disallowed mods %s",
                         contract_id, illegal)
                await flag_suspicion(
                    gid, uid, user.get("username", ""), reason="illegal_mods",
                    details=(f"Craft submitted to contract `{contract_id}` used mods "
                             f"outside the allowed list: {', '.join(illegal)}. "
                             "(Auto-rejected server-side; flagged only after repeats, "
                             "may be an honest mod-loadout mistake.)"),
                    severity="medium")
                return SubmissionResult(
                    success=False,
                    message=f"Craft uses parts outside this contract's allowed mods: {', '.join(illegal)}.",
                )

    # Server-side part-limit ("mission limit") check — authoritative re-check of
    # the constraints the editor/submit gate already enforce client-side. Skipped
    # when the contract has no constraints or the client reported no part summary.
    constraints = c.get("constraints")
    # Crew aboard, read from the submitted telemetry (active-vessel snapshot). Used by
    # the min/max-crew limit; None when no telemetry was sent (e.g. craft-build).
    crew_count = None
    # Who was aboard by profession ({"Pilot": 2, ...}), for the per-trait rule. None
    # when the client is too old to report it, which skips that check rather than
    # failing it — the same contract must not start rejecting existing clients.
    crew_traits = None
    if vessel_data:
        import json
        try:
            _vd_crew = json.loads(vessel_data)
            _snap = _vd_crew.get("active_vessel") or _vd_crew
            _cc = _snap.get("crew_count")
            crew_count = int(_cc) if _cc is not None else None
            _ct = _snap.get("crew_traits")
            crew_traits = _ct if isinstance(_ct, dict) else None
        except Exception:
            crew_count = None
            crew_traits = None

    constraint_checked = False
    if not mc.is_empty(constraints) and (used_parts or delta_v_vac):
        import json
        # Resolve loose part mentions against the submitter's catalog so the
        # authoritative check matches the exact part, with loose fallback.
        constraints = _resolve_constraints(constraints, gid, uid, c.get("mission", ""))
        try:
            parsed_parts = json.loads(used_parts) if used_parts else []
        except Exception as exc:
            log.warning("Bad used_parts payload for contract %s: %s", contract_id, exc)
            parsed_parts = None
        # Client-reported vacuum Δv (None if absent/unparseable => Δv limit skipped).
        try:
            dv = float(delta_v_vac) if delta_v_vac not in (None, "") else None
        except (TypeError, ValueError):
            dv = None
        if isinstance(parsed_parts, list):
            constraint_checked = True
            violations = mc.verify_used_parts(constraints, parsed_parts, delta_v=dv,
                                              crew_count=crew_count,
                                              crew_traits=crew_traits)
            if violations:
                log.info("Submission rejected for contract %s: constraint violations %s",
                         contract_id, violations)
                return SubmissionResult(
                    success=False,
                    message="Craft breaks this contract's mission limits:\n- " + "\n- ".join(violations),
                )

    # Active-vessel missions may report telemetry but no parts list; still enforce the
    # crew limits in that case (the parts check above already covers crew when it ran).
    if not constraint_checked and (crew_count is not None or crew_traits is not None):
        crew_violations = mc.verify_crew(constraints, crew_count, crew_traits)
        if crew_violations:
            log.info("Submission rejected for contract %s: crew violations %s",
                     contract_id, crew_violations)
            return SubmissionResult(
                success=False,
                message="Craft breaks this contract's mission limits:\n- " + "\n- ".join(crew_violations),
            )

    # Server-side flight-telemetry consistency check (defense-in-depth, like the
    # rescue/part-limit gates above). The client's orbital snapshot is over-determined,
    # so a forged "I'm at the target" claim usually leaves the apoapsis/periapsis/sma/
    # eccentricity numbers mutually inconsistent — physically impossible, not a tolerance
    # issue. Mode (reject / flag / both) is settings.TELEMETRY_CHECK_MODE. Skipped when
    # the submission carries no telemetry (e.g. craft-build missions).
    if vessel_data:
        import json
        try:
            _vd_for_check = json.loads(vessel_data)
        except Exception:
            _vd_for_check = None
        verdict = tcheck.evaluate(_vd_for_check)
        if verdict.flag:
            await flag_suspicion(
                gid, uid, user.get("username", ""), reason="impossible_telemetry",
                details=(f"Flight telemetry submitted to contract `{contract_id}` failed "
                         f"the consistency check:\n{verdict.detail}"),
                severity="high")
        if verdict.reject:
            log.info("Submission rejected for contract %s: implausible telemetry", contract_id)
            return SubmissionResult(success=False, message=verdict.reject_message)

    # Server-side orbit-regime check — authoritative re-check of the orbit-type
    # requirement the submit gate enforces client-side. Parsed fresh from the
    # mission text (so it works regardless of what's stored on the contract) and
    # verified against the active-vessel snapshot. Skipped when the mission names
    # no specific orbit or the submission carried no telemetry (e.g. craft-build).
    orbit_c = oc.extract_heuristic(c.get("mission", ""))
    if not oc.is_empty(orbit_c) and vessel_data:
        import json
        try:
            _vd_orbit = json.loads(vessel_data)
            _snap_orbit = _vd_orbit.get("active_vessel") or _vd_orbit
        except Exception:
            _snap_orbit = None
        if isinstance(_snap_orbit, dict):
            orbit_violations = oc.verify_orbit(orbit_c, _snap_orbit)
            if orbit_violations:
                log.info("Submission rejected for contract %s: orbit violations %s",
                         contract_id, orbit_violations)
                return SubmissionResult(
                    success=False,
                    message="Craft isn't in the orbit this contract requires:\n- "
                            + "\n- ".join(orbit_violations),
                )

    # Upload files to Firebase Storage
    stored_files = []

    # Craft file. Stored PRIVATE (a bare bucket path, not a public URL): a contract
    # craft is private to the two parties, so it's reachable only through a signed
    # URL minted at download time (see download_craft / sign_stored). Screenshots
    # below stay public — they're shown in Discord embeds and the web review.
    if craft_file:
        data = await _read_upload(craft_file)
        try:
            url = await cdb.upload_private_to_storage(
                contract_id, craft_file.filename, data,
                craft_file.content_type or "application/octet-stream"
            )
            stored_files.append({"filename": craft_file.filename, "url": url,
                                 "content_type": craft_file.content_type or "application/octet-stream"})
        except Exception as exc:
            log.error("Craft upload failed: %s", exc)

    # Screenshots — legacy numbered fields plus the uncapped repeated field, so a
    # multi-craft submission stores a render for every selected vessel.
    all_screenshots = [s for s in (screenshot1, screenshot2, screenshot3) if s]
    all_screenshots += [s for s in (screenshots or []) if s]
    for ss in all_screenshots:
        # Blueprints/renders are a fixed, scale-derived size — cap them well below
        # the generic 25 MB limit so a tampered client can't spray padded uploads.
        data = await _read_upload(ss, MAX_BLUEPRINT_BYTES)
        try:
            url = await cdb.upload_to_storage(
                contract_id, ss.filename, data,
                ss.content_type or "image/png"
            )
            stored_files.append({"filename": ss.filename, "url": url,
                                 "content_type": ss.content_type or "image/png"})
        except Exception as exc:
            log.error("Screenshot upload failed: %s", exc)

    if not stored_files:
        return SubmissionResult(success=False, message="No files uploaded successfully.")

    has_image = any(f["content_type"].startswith("image/") for f in stored_files)
    if not has_image:
        return SubmissionResult(success=False, message="At least one screenshot is required.")

    # Update contract status
    now = datetime.utcnow().isoformat()
    update_fields = {
        "status": cdb.SUBMITTED,
        "submitted_files": stored_files,
        "submitted_at": now,
        "contractor_modlist": modlist,
    }

    # Life-support flag of the delivered craft — drives the "min–max LS for N kerbals"
    # line on the contract embed. Only stored when the craft is actually provisioned.
    if life_support and life_support.strip().lower() != "none":
        update_fields["life_support"] = life_support.strip().lower()
        update_fields["ls_endurance_days"] = float(ls_endurance_days or 0.0)
        update_fields["ls_crew_capacity"] = int(ls_crew_capacity or 0)

    # Store vessel data and loadmeta if provided
    parsed_vessel_data: dict | None = None
    if vessel_data:
        import json
        try:
            parsed_vessel_data = json.loads(vessel_data)
            update_fields["vessel_data"] = parsed_vessel_data
        except Exception:
            update_fields["vessel_data_raw"] = vessel_data

    if loadmeta:
        update_fields["loadmeta"] = loadmeta

    # Orbital telemetry diagram — rendered once from the captured vessel state and
    # persisted to Storage so it can be surfaced both in the Discord review embed
    # and the in-game review window. Only present when the client sent vessel data
    # (active-vessel / rescue submissions); other submission types skip it.
    if parsed_vessel_data:
        try:
            from orbit_render import render_orbit

            # One orbit diagram per submitted craft: the active (contract) vessel
            # first, then any extras sent in a multi-vessel submission.
            snaps = []
            active_snap = parsed_vessel_data.get("active_vessel") or parsed_vessel_data
            if isinstance(active_snap, dict):
                snaps.append(active_snap)
            for sv in (parsed_vessel_data.get("sent_vessels") or []):
                if isinstance(sv, dict):
                    snaps.append(sv)

            telemetry_urls = []
            for idx, snap in enumerate(snaps):
                try:
                    orbit_png = render_orbit(snap)
                except Exception as exc:
                    log.warning("orbit render failed for vessel %d on %s: %s", idx, contract_id, exc)
                    continue
                if not orbit_png:
                    continue

                fname = "orbit_telemetry.png" if idx == 0 else f"orbit_telemetry_{idx}.png"
                url = await cdb.upload_to_storage(contract_id, fname, orbit_png, "image/png")
                telemetry_urls.append(url)

            # Telemetry diagrams stay OUT of the blueprint image list — they're surfaced
            # in the dedicated in-game telemetry window (one per craft) and the Discord
            # embed. telemetry_image_url keeps the active craft's diagram for back-compat.
            if telemetry_urls:
                update_fields["telemetry_image_url"] = telemetry_urls[0]
                update_fields["telemetry_image_urls"] = telemetry_urls
        except Exception as exc:
            log.warning("Failed to render/store orbit telemetry for %s: %s", contract_id, exc)

    # Upload vessel node (full vessel state for transfer) to Storage. Private, like
    # the craft file above — it's the transferred vessel, private to the parties, and
    # is served only via a signed URL (download_craft / the import-queue serve points).
    if vn_data is not None:
        try:
            vn_url = await cdb.upload_private_to_storage(
                contract_id, "vessel_node.cfg", vn_data, "application/gzip"
            )
            update_fields["vessel_node_url"] = vn_url
            # For rescue, this is the craft that carries the kerbals home — delivered
            # to the issuer (with restored names) once they approve.
            if c.get("mission_type") == cdb.RESCUE:
                update_fields["delivered_vessel_node_url"] = vn_url
            log.info("Vessel node uploaded: %d bytes (gzipped)", len(vn_data))
        except Exception as exc:
            log.error("Vessel node upload failed: %s", exc)

    cdb.update_contract(gid, contract_id, **update_fields)

    # AI Review for bot-issued contracts
    is_bot_issued = str(c.get("issuer_id")) == str(bot_uid)

    if is_bot_issued:
        result = await _ai_review_submission(gid, uid, contract_id, c, stored_files, vessel_data, loadmeta)
        return result

    # Human-issued: notify issuer via Discord notification system AND Discord channel
    _create_notification(
        gid, int(c["issuer_id"]), "submission_received",
        "Contract Submission",
        f"{user['username']} submitted work for: {c['mission'][:50]}",
        {"contract_id": contract_id},
    )

    # Also post to the issuer's corp channel in Discord
    await _discord_notify_issuer(
        gid, int(c["issuer_id"]), contract_id, c, user["username"], stored_files,
        parsed_vessel_data,
    )

    log.info("KSP: %s submitted contract %s (human-issued)", user["username"], contract_id)
    return SubmissionResult(
        success=True,
        message="Submitted! Waiting for issuer review.",
        review_status="pending",
    )


async def _ai_review_submission(
    gid: int, uid: int, contract_id: str, c: dict,
    stored_files: list[dict], vessel_data: str | None, loadmeta: str | None,
) -> SubmissionResult:
    """Run Gemini AI review on a bot-issued contract submission."""
    import json
    from cogs.screenshots import active_client, record_gemini, _MODEL
    gemini_client = active_client()

    if not gemini_client:
        # No AI (key missing OR monthly budget reached) — auto-accept
        return await _auto_accept_contract(gid, uid, contract_id, c)

    # Download screenshots for AI
    screenshots = [f for f in stored_files if f.get("content_type", "").startswith("image/")]
    if not screenshots:
        return SubmissionResult(success=False, message="No screenshots for AI review.")

    img_bytes_list = []
    for s in screenshots:
        try:
            raw = await cdb.download_url(s["url"])
            img_bytes_list.append(raw)
        except Exception:
            pass

    if not img_bytes_list:
        return SubmissionResult(success=False, message="Could not download screenshots for review.")

    # Build prompt including loadmeta context
    mission_desc = c.get("mission", "")
    extra_context = ""
    if loadmeta:
        extra_context += f"\n\nCraft loadmeta data:\n{loadmeta}"
    if vessel_data:
        extra_context += f"\n\nVessel telemetry data:\n{vessel_data}"

    review_prompt = (
        f"You are reviewing a KSP contract submission from the in-game mod client.\n"
        f"The mission was: \"{mission_desc}\"\n"
        f"{extra_context}\n\n"
        f"Analyze the screenshot(s) and any provided telemetry/loadmeta data.\n"
        f"Determine if the mission was completed successfully.\n"
        f"Additionally, assign the highest applicable KSP achievement level (1-15):\n"
        f"1. Kerbin Orbit | 2. Mun Landing | 3. Docking | 4. Duna Landing | 5. RSS Earth Orbit\n"
        f"6. Eve Landing | 7. Asteroid Redirect | 8. RSS Moon Landing | 9. Jool 5 | 10. Interstellar\n"
        f"11. RSS Mars | 12. RSS Venus Landing | 13. RSS Gas Giant | 14. Kerbol Grand Tour | 15. RSS Interstellar\n"
        f"If none clearly apply, set ksp_level to 0.\n\n"
        f"Return ONLY valid JSON:\n"
        f'{{\n  "approved": true/false,\n  "reason": "brief explanation",\n  "ksp_level": integer\n}}'
    )

    from google.genai import types

    parts = [types.Part.from_text(text=review_prompt)]
    for img in img_bytes_list:
        parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))

    try:
        response = gemini_client.models.generate_content(
            model=_MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=512),
        )
        record_gemini(response)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        result = json.loads(raw.strip())
    except Exception as exc:
        log.error("AI review failed for KSP submission: %s", exc)
        return await _auto_accept_contract(gid, uid, contract_id, c)

    if result.get("approved", False):
        return await _auto_accept_contract(
            gid, uid, contract_id, c,
            result.get("reason", ""), result.get("ksp_level", 0)
        )
    else:
        reason = result.get("reason", "AI review did not approve this submission.")
        # Persist the reason so it can be shown to mods if the player sues. The dispute
        # fields are shared with the other two entry points so the auto-fine clock
        # starts here too (see contract_actions.open_dispute_fields).
        cdb.update_contract(gid, contract_id, review_reason=reason,
                            **ca.open_dispute_fields())
        _create_notification(gid, uid, "review_result",
                             "❌ Submission Refused",
                             reason,
                             {"contract_id": contract_id})
        return SubmissionResult(
            success=True, message="Submission reviewed.",
            review_status="refused", reason=reason,
        )


async def _auto_accept_contract(
    gid: int, uid: int, contract_id: str, c: dict,
    reason: str = "", ksp_level: int = 0,
) -> SubmissionResult:
    """Accept a contract, grant rewards."""
    now = datetime.utcnow().isoformat()
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED, completed_at=now)

    # Grant payment
    await store.add_balance(gid, uid, c["payment"])

    # Grant XP
    diff = c["payment"] // settings.WEEKLY_COINS_PER_DIFFICULTY if settings.WEEKLY_COINS_PER_DIFFICULTY else 0
    xp = diff * settings.WEEKLY_XP_PER_DIFFICULTY
    if xp > 0:
        u = store.get_user(gid, uid)
        await store.set_xp(gid, uid, u["xp"] + xp)

    # KSP level award — record globally (cross-server) and keep the per-guild
    # mirror for backward compatibility.
    if ksp_level > 0:
        await store.add_unlocked_level(gid, uid, ksp_level)
        from data import achievements
        achievements.add_unlocked(uid, ksp_level)

    _create_notification(gid, uid, "review_result",
                         "✅ Mission Approved!",
                         f"{reason}\n+{c['payment']} KCoins, +{xp} XP" if reason else f"+{c['payment']} KCoins, +{xp} XP",
                         {"contract_id": contract_id, "ksp_level": ksp_level})

    log.info("KSP: Auto-accepted contract %s for user %d (+%d coins, +%d XP, level %d)",
             contract_id, uid, c["payment"], xp, ksp_level)

    # The finished craft goes to the builder's corporation channel.
    await _deliver_craft_to_corp(gid, uid, contract_id)

    return SubmissionResult(
        success=True, message="Mission approved!",
        review_status="approved", reason=reason,
        xp_awarded=xp, coins_awarded=c["payment"],
    )


# ── Notifications ────────────────────────────────────────────────────────────

def _notifications_col(guild_id: int, user_id: int):
    return (_db.collection("guilds").document(str(guild_id))
            .collection("ksp_notifications").document(str(user_id))
            .collection("items"))


def _create_notification(
    guild_id: int, user_id: int, notif_type: str,
    title: str, message: str, data: dict | None = None,
):
    """Create a notification in Firestore for a user and push it to any live
    WebSocket connections."""
    doc_id = uuid.uuid4().hex[:12]
    payload = {
        "id": doc_id,
        "type": notif_type,
        "title": title,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "read": False,
        "data": data or {},
    }
    _notifications_col(guild_id, user_id).document(doc_id).set(payload)
    _push_notification(guild_id, user_id, payload)


# ── WebSocket connect tickets ────────────────────────────────────────────────
# UnityWebRequest can't set an Authorization header on a WS handshake, so the
# original design put the 30-day session token in the WS URL (?token=) — where it
# leaks into access/proxy logs and stays replayable for a month. Instead the client
# exchanges its token (over a normal authenticated request) for a short-lived,
# single-use ticket and connects with ?ticket=. A logged ticket URL exposes nothing
# reusable: the ticket dies on first use or after 30 seconds.
_ws_tickets: dict[str, dict] = {}     # ticket -> {guild_id, user_id, username, expires}
_WS_TICKET_TTL = 30.0


def _prune_ws_tickets() -> None:
    now = time.time()
    for k in [k for k, v in _ws_tickets.items() if v["expires"] < now]:
        _ws_tickets.pop(k, None)


@app.post("/api/v1/auth/ws-ticket")
async def issue_ws_ticket(user: dict = Depends(get_user_token_only)):
    """Exchange a valid session token for a short-lived, single-use WebSocket ticket,
    so the long-lived token never has to travel in the WS URL (see _ws_tickets)."""
    _prune_ws_tickets()
    ticket = secrets.token_urlsafe(24)
    _ws_tickets[ticket] = {
        "guild_id": user["guild_id"],
        "user_id": user["user_id"],
        "username": user["username"],
        "expires": time.time() + _WS_TICKET_TTL,
    }
    return {"ticket": ticket, "expires_in": int(_WS_TICKET_TTL)}


@app.websocket("/ws/v1/notifications")
async def notifications_ws(websocket: WebSocket):
    """Live notification stream. The client connects with a short-lived single-use
    ticket (?ticket=, from POST /auth/ws-ticket). A legacy ?token= is still accepted
    for older clients but is deprecated — it leaks the 30-day token into URL/logs."""
    user = None
    ticket = websocket.query_params.get("ticket", "")
    if ticket:
        _prune_ws_tickets()
        info = _ws_tickets.pop(ticket, None)   # single-use: consumed on connect
        if info and info["expires"] >= time.time():
            user = {"guild_id": info["guild_id"], "user_id": info["user_id"],
                    "username": info["username"]}

    if user is None:
        # Deprecated fallback for clients that still pass the token directly.
        # It must honor the suspension gate itself: the ticket path inherits it
        # from get_user_token_only at issue time, but a raw token skips that, and
        # a suspension the notification stream ignored would not be one.
        token = websocket.query_params.get("token", "")
        user = verify_session_token(token, _accept_secrets()) if token else None
        if user is not None and suspensions.get_active(user["user_id"]) is not None:
            user = None

    if user is None:
        await websocket.close(code=1008)  # policy violation
        return

    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    await _hub.connect(gid, uid, websocket)
    try:
        # Keep the socket open; client may send keepalive pings we just discard.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("WS: receive loop ended for user %d: %s", uid, exc)
    finally:
        _hub.disconnect(gid, uid, websocket)


@app.get("/api/v1/user/notifications", response_model=NotificationsResponse)
async def get_notifications(user: dict = Depends(get_current_user)):
    """Get recent notifications (read + unread) for the current user, newest first."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    col = _notifications_col(gid, uid)
    notifs = []

    # Single-field order_by is auto-indexed — no composite index needed.
    for doc in col.order_by(
        "timestamp", direction=firestore.Query.DESCENDING
    ).limit(50).stream():
        notifs.append(Notification(**doc.to_dict()))

    return NotificationsResponse(
        notifications=notifs,
        unread_count=sum(1 for n in notifs if not n.read),
    )


@app.post("/api/v1/user/notifications/mark_read")
async def mark_notifications_read(user: dict = Depends(get_current_user)):
    """Mark all notifications as read."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    col = _notifications_col(gid, uid)
    for doc in col.where("read", "==", False).stream():
        doc.reference.update({"read": True})

    return {"success": True}


@app.post("/api/v1/user/notifications/{notif_id}/mark_read")
async def mark_notification_read(notif_id: str, user: dict = Depends(get_current_user)):
    """Mark a single notification as read."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    _notifications_col(gid, uid).document(notif_id).update({"read": True})
    return {"success": True}


@app.delete("/api/v1/user/notifications/read")
async def dismiss_read_notifications(user: dict = Depends(get_current_user)):
    """Delete every notification this player has already read.

    Declared *before* the `{notif_id}` route below on purpose: FastAPI matches in
    declaration order, so the other way round this path would arrive as a dismiss
    of a notification whose id is the literal string "read".

    Batched rather than deleted one at a time — clearing a feed is the one place
    where the count is unbounded (the fetch caps at 50, the collection does not),
    and one round trip per document is what makes that expensive.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    col = _notifications_col(gid, uid)
    batch = _db.batch()
    count = 0
    deleted = 0

    for doc in col.where("read", "==", True).stream():
        batch.delete(doc.reference)
        count += 1
        deleted += 1
        # Firestore batches max out at 500 operations.
        if count >= 450:
            batch.commit()
            batch = _db.batch()
            count = 0

    if count > 0:
        batch.commit()

    return {"success": True, "deleted": deleted}


@app.delete("/api/v1/user/notifications/{notif_id}")
async def dismiss_notification(notif_id: str, user: dict = Depends(get_current_user)):
    """Dismiss (delete) a single notification."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    _notifications_col(gid, uid).document(notif_id).delete()
    return {"success": True}


# ── Craft Download ───────────────────────────────────────────────────────────

@app.get("/api/v1/craft/download/{contract_id}")
async def download_craft(contract_id: str, user: dict = Depends(get_current_user)):
    """Get craft file download URL from a completed contract.

    Player-to-player contract crafts stay private to the two parties (issuer +
    contractor), who import them in the mod as usual. Bot-contract crafts are NOT
    served here — they're delivered to the builder's corp channel instead.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if c.get("status") != cdb.COMPLETED:
        raise HTTPException(status_code=400, detail="Contract not completed yet")

    is_bot_issued = str(c.get("issuer_id")) == str(_get_bot_user_id())
    if is_bot_issued:
        # Bot-contract crafts are delivered to the builder's corp channel, never
        # imported — this also blocks re-importing the live vessel into a save.
        raise HTTPException(
            status_code=403,
            detail="Bot-contract crafts are delivered to your corporation channel, not imported.",
        )
    if uid not in (str(c.get("issuer_id")), str(c.get("contractor_id"))):
        raise HTTPException(status_code=403, detail="This craft is private to the contract parties.")

    files = c.get("submitted_files", [])
    craft_files = [f for f in files if f.get("filename", "").endswith(".craft")]
    vessel_node_url = c.get("vessel_node_url")

    if not craft_files and not vessel_node_url:
        raise HTTPException(status_code=404, detail="No craft file or vessel data in submission")

    # The craft/vessel objects are private; mint a short-lived signed URL for each
    # reference the client is about to download. sign_stored passes legacy public
    # URLs through unchanged, so pre-migration submissions still resolve.
    signed_craft_files = [
        {**f, "url": sign_stored(f.get("url"))} for f in craft_files
    ]
    return {
        "craft_files": signed_craft_files,
        "loadmeta": c.get("loadmeta"),
        "vessel_node_url": sign_stored(vessel_node_url),
    }


@app.get("/api/v1/contracts/{contract_id}/submission")
async def get_submission_preview(contract_id: str, user: dict = Depends(get_current_user)):
    """Return the contractor's submitted images (vessel render / blueprint) so the
    issuer can preview the work in-game before approving or refusing it.

    Restricted to the contract's two parties. Only the image files are returned —
    the craft file itself stays gated behind the existing /craft endpoint, which
    only opens up once the contract is completed.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])

    c = cdb.get_contract(gid, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")

    if uid not in (str(c.get("issuer_id")), str(c.get("contractor_id"))):
        raise HTTPException(status_code=403, detail="This submission is private to the contract parties.")

    # Flag-design: watermarked preview while pending review; the clean full-res
    # flag is only exposed once the contract is completed (i.e. paid for).
    if c.get("mission_type") == cdb.FLAG_DESIGN:
        if c.get("status") == cdb.COMPLETED and c.get("flag_fullres_url"):
            # Private full-res once completed → sign it; the watermarked preview is public.
            url, fname = sign_stored(c["flag_fullres_url"]), (c.get("flag_filename") or "flag.png")
        else:
            url, fname = c.get("flag_preview_url"), "flag_preview.png"
        images = [{"filename": fname, "url": url}] if url else []
        return {"images": images, "vessel_name": "", "telemetry_url": ""}

    images = [
        {"filename": f.get("filename"), "url": f.get("url")}
        for f in c.get("submitted_files", [])
        if (f.get("content_type") or "").startswith("image/") and f.get("url")
    ]

    vessel_name = (c.get("vessel_data") or {}).get("vessel_name") or ""
    # The orbital telemetry diagrams are stored separately (not in submitted_files) so
    # the mod shows them in their own window rather than mixed with blueprints. A
    # multi-craft submission has one per craft; telemetry_url stays for old clients.
    telemetry_urls = c.get("telemetry_image_urls") or (
        [c["telemetry_image_url"]] if c.get("telemetry_image_url") else []
    )
    telemetry_url = telemetry_urls[0] if telemetry_urls else ""
    return {
        "images": images,
        "vessel_name": vessel_name,
        "telemetry_url": telemetry_url,
        "telemetry_urls": telemetry_urls,
    }


def _sign_import_entry(e: dict) -> dict:
    """Return a copy of an import/gift queue entry with its craft/vessel file
    references resolved to signed URLs for the client to download. Only the file
    fields are touched; image fields (blueprint_url) and legacy public URLs pass
    through unchanged via sign_stored. The queue stores a private object as a bare
    path, so the URL is minted fresh on each poll rather than baked in at enqueue."""
    out = dict(e)
    for k in ("craft_url", "vessel_node_url", "flag_url"):
        if out.get(k):
            out[k] = sign_stored(out[k])
    return out


@app.get("/api/v1/craft/imports/pending")
async def craft_imports_pending(user: dict = Depends(get_current_user)):
    """Crafts the player queued (in Discord) for auto-import into their save.

    The mod polls this at the Space Center, imports each entry, then acks it via
    POST /api/v1/craft/imports/{import_id}/done so it isn't imported twice.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    imports = []
    for e in imp.list_pending(gid, uid):
        # A friend quicksend awaiting the recipient's accept/decline is not in the
        # auto-import queue yet — it only joins once accepted (status → "queued").
        # Entries written before the status field existed have none and auto-import
        # as they always did.
        if (e.get("status") or "queued") == "offered":
            continue
        # dedup_key lets the mod skip an entry it already processed into this save.
        dedup = e["ref_id"] if e.get("source") == "contract" else f"{e.get('source')}:{e['ref_id']}"
        imports.append({**_sign_import_entry(e), "dedup_key": dedup})

    imports.sort(key=lambda x: x.get("created_at") or "")
    return {"imports": imports}


@app.post("/api/v1/craft/imports/{import_id}/done")
async def craft_import_done(import_id: str, user: dict = Depends(get_current_user)):
    """Ack a completed import — removes it from the player's queue."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    entry = imp.get(gid, uid, import_id)
    deleted = imp.delete(gid, uid, import_id)
    # A gift's Storage files serve exactly one download — the accepted import, or
    # the decline-return to the sender. Once that import is acked nothing will
    # ever fetch them again, so clean them up here rather than leaking a payload
    # per quicksend forever.
    if deleted and entry and entry.get("source") in ("gift_craft", "gift_vessel"):
        await asyncio.to_thread(imp.delete_gift_files, entry["ref_id"])
    return {"success": deleted}


# ── Friend-quicksend offers ───────────────────────────────────────────────────
# A quicksent craft is an unsolicited push into someone's save, so unlike the
# imports the player queued themselves (contract/market/library — already an
# explicit request in Discord) it waits here for an in-game accept or decline.


@app.get("/api/v1/craft/gifts/pending")
async def craft_gifts_pending(user: dict = Depends(get_current_user)):
    """Quicksent crafts awaiting this player's accept/decline decision."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    gifts = [_sign_import_entry(e) for e in imp.list_pending(gid, uid)
             if (e.get("status") or "queued") == "offered"]
    gifts.sort(key=lambda x: x.get("created_at") or "")
    return {"gifts": gifts}


@app.post("/api/v1/craft/gifts/{import_id}/accept")
async def craft_gift_accept(import_id: str, user: dict = Depends(get_current_user)):
    """Accept an offered gift: it joins the normal auto-import queue.

    The client usually imports it on the spot from the returned entry; the queue
    is the fallback for the scenes where a live vessel can't spawn (the accept
    happened in the VAB, say) — the next poll in a safe scene delivers it.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    entry = imp.get(gid, uid, import_id)
    if entry is None or (entry.get("status") or "queued") != "offered":
        return {"success": False, "message": "That offer is no longer there."}

    imp.set_status(gid, uid, import_id, "queued")
    entry["status"] = "queued"
    entry["dedup_key"] = f"{entry.get('source')}:{entry['ref_id']}"

    # Tell the sender. For a live vessel the notification also carries the pid the
    # sender's client reported at send time: the vessel left their save then, but a
    # quickload can roll the removal back while the offer lives on — the client
    # re-queues the removal off this echo, so the accepted hand-over cannot leave a
    # copy behind.
    sender_id = entry.get("sender_id")
    if sender_id:
        _create_notification(
            gid, int(sender_id), "craft_gift_accepted",
            "🎁 Craft Accepted",
            f"{user['username']} accepted the craft you sent: "
            f"{entry.get('craft_name') or 'Craft'}.",
            {"craft_name": entry.get("craft_name") or "",
             "vessel_pid": entry.get("vessel_pid") or ""},
        )

    # The client imports on the spot from this entry, so sign its file references now.
    signed_entry = _sign_import_entry(entry)
    signed_entry["dedup_key"] = entry["dedup_key"]
    return {"success": True, "entry": signed_entry}


@app.post("/api/v1/craft/gifts/{import_id}/reject")
async def craft_gift_reject(import_id: str, user: dict = Depends(get_current_user)):
    """Decline an offered gift, and the sender hears.

    A blueprint (gift_craft) was only ever a copy — the entry and its files go
    away. A live vessel (gift_vessel) left the sender's save at send time, so
    declining it must give it BACK: the same stored vessel node is re-queued to
    the sender as a normal auto-import (no accept step — it is their own ship
    coming home), and the files stay for that one download; the sender's ack of
    the return import cleans them up (see craft_import_done).
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    entry = imp.get(gid, uid, import_id)
    if entry is None or (entry.get("status") or "queued") != "offered":
        return {"success": False, "message": "That offer is no longer there."}

    imp.delete(gid, uid, import_id)

    sender_id = entry.get("sender_id")
    # vessel_pid doubles as the marker that the sender's client removed the
    # vessel at send time (older clients sent a copy and kept theirs) — without
    # it a "return" would hand the sender a duplicate of a ship they never lost.
    returning = (entry.get("source") == "gift_vessel" and sender_id
                 and entry.get("vessel_pid"))
    if returning:
        imp.enqueue(
            gid, int(sender_id), source="gift_vessel", ref_id=entry["ref_id"],
            craft_name=entry.get("craft_name") or "Craft",
            vessel_node_url=entry.get("vessel_node_url"),
            owner_name=entry.get("owner_name"),
            vessel_pid=entry.get("vessel_pid"),
        )
    else:
        await asyncio.to_thread(imp.delete_gift_files, entry["ref_id"])

    if sender_id:
        _create_notification(
            gid, int(sender_id), "craft_gift_declined",
            "📪 Craft Declined",
            f"{user['username']} declined the craft you sent: "
            f"{entry.get('craft_name') or 'Craft'}."
            + (" It is being returned to your save." if returning else ""),
            {"craft_name": entry.get("craft_name") or ""},
        )
    return {"success": True, "message": "Declined."}


@app.post("/api/v1/craft/send")
async def craft_send_to_friend(
    file: UploadFile = File(...),
    blueprint: Optional[UploadFile] = File(None),
    recipient_id: str = Form(...),
    kind: str = Form("craft"),
    craft_name: str = Form("Craft"),
    vessel_pid: str = Form(None),
    user: dict = Depends(get_current_user),
):
    """Quicksend a craft/vessel from the KSP mod's Tools tab to another player.

    kind="vessel" delivers a LIVE vessel (the recipient's client spawns it in their
    save); kind="craft" delivers a .craft blueprint into their Ships folder. The
    entry is created as an OFFER (status "offered"): the recipient's client shows
    it — with the rendered `blueprint` preview when the sender's client managed
    one — and only an explicit accept moves it into the auto-import queue; a
    decline deletes a blueprint but RETURNS a live vessel to the sender's import
    queue, because a live vessel is a hand-over: the sender's client removes it
    from their save once this endpoint confirms, and `vessel_pid` (its pid in the
    sender's save) is what lets their client cancel or re-assert that removal
    when the decision comes back. The payload arrives gzip-compressed (like
    submissions/listings); we store it decompressed.
    """
    import gzip
    from cogs.corps import _get_corp

    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    try:
        rid = int(recipient_id)
    except (TypeError, ValueError):
        return {"success": False, "message": "Invalid recipient."}

    if rid == uid:
        return {"success": False, "message": "You can't send a craft to yourself."}

    # Resolve the recipient — they must be a known player (have a corp, as the
    # in-game friend picker lists) or a current member of the guild.
    corp = _get_corp(gid, rid)
    recipient_name = corp.get("owner_name") if corp else None
    if recipient_name is None and _bot_instance:
        guild = _bot_instance.get_guild(gid)
        member = guild.get_member(rid) if guild else None
        if member:
            recipient_name = member.display_name
    if recipient_name is None:
        return {"success": False, "message": "That player isn't in this server."}

    kind = (kind or "craft").lower()
    if kind not in ("craft", "vessel"):
        return {"success": False, "message": "Unknown send type."}

    raw = await _read_upload(file)
    try:
        payload = _safe_gunzip(raw)
    except (OSError, EOFError):
        payload = raw  # fall back if it wasn't compressed

    iid = uuid.uuid4().hex[:12]
    if kind == "vessel":
        filename = "vessel.cfg"
    else:
        filename = file.filename or f"{craft_name}.craft"
        if not filename.lower().endswith(".craft"):
            filename += ".craft"

    try:
        url = await asyncio.to_thread(imp.upload_gift, iid, filename, payload)
    except Exception as exc:
        log.error("Quicksend upload failed: %s", exc)
        return {"success": False, "message": "Failed to upload the craft."}

    # Rendered blueprint preview — what the recipient judges the offer by.
    # Optional: a failed render client-side still sends, just without a picture.
    bp_url = None
    if blueprint is not None:
        try:
            bp_data = await _read_upload(blueprint)
            bp_url = await asyncio.to_thread(imp.upload_gift_blueprint, iid, bp_data)
        except Exception as exc:
            log.error("Quicksend blueprint upload failed: %s", exc)

    if kind == "vessel":
        imp.enqueue(
            gid, rid, source="gift_vessel", ref_id=iid, craft_name=craft_name,
            vessel_node_url=url, owner_name=user["username"],
            blueprint_url=bp_url, sender_id=uid, status="offered",
            vessel_pid=(vessel_pid or "").strip() or None,
        )
        kind_label = "a live vessel"
    else:
        imp.enqueue(
            gid, rid, source="gift_craft", ref_id=iid, craft_name=craft_name,
            craft_url=url, craft_filename=filename, owner_name=user["username"],
            blueprint_url=bp_url, sender_id=uid, status="offered",
        )
        kind_label = "a craft"

    _create_notification(
        gid, rid, "craft_gift",
        "🎁 Craft Offered",
        f"{user['username']} sent you {kind_label}: {craft_name}. "
        f"Accept or decline it in KSP (any scene with the sidebar — Space Center, "
        f"VAB/SPH or flight).",
        {"craft_name": craft_name},
    )

    log.info("KSP: %s quicksent %s '%s' to %d", user["username"], kind, craft_name, rid)
    # vessel_returnable tells the client this server stored the pid and will
    # return the vessel on a decline — the client only removes it from the
    # sender's save on that promise. An older server never says it, and the
    # client falls back to the old send-a-copy behavior rather than deleting a
    # ship nothing would ever give back.
    return {
        "success": True,
        "message": f"Sent to {recipient_name}!",
        "vessel_returnable": kind == "vessel" and bool((vessel_pid or "").strip()),
    }


# ── Marketplace ────────────────────────────────────────────────────────────────

def _fmt_wait(seconds: float) -> str:
    """A cooldown remainder as '5h 12m' / '12m' / 'under a minute', for a one-line
    status in the game (nothing here is precise enough to be worth showing seconds)."""
    total = max(0, int(seconds))
    hours, minutes = total // 3600, (total % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "under a minute"


@app.post("/api/v1/marketplace/list", response_model=MarketplaceListResult)
async def marketplace_list_craft(
    craft_file: UploadFile = File(...),
    blueprint: Optional[UploadFile] = File(None),
    thumbnail: Optional[UploadFile] = File(None),
    craft_name: str = Form(...),
    craft_type: str = Form("VAB"),
    part_count: int = Form(0),
    mass: float = Form(0.0),
    cost: float = Form(0.0),
    price: int = Form(...),
    mods: str = Form(""),
    parts: str = Form(""),
    life_support: str = Form("none"),
    ls_endurance_days: float = Form(0.0),
    ls_crew_capacity: int = Form(0),
    custom_textures: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """List a craft (.craft blueprint) for sale on the marketplace.

    The mod uploads the craft gzip-compressed (like contract submissions). We
    decompress and store the raw .craft so a buyer's download is a straight one.
    The listing appears on the website; Discord no longer mirrors it (see
    cogs/marketplace.py).
    """
    import gzip

    if price < settings.MARKETPLACE_MIN_PRICE or price > settings.MARKETPLACE_MAX_PRICE:
        return MarketplaceListResult(
            success=False,
            message=f"Price must be between {settings.MARKETPLACE_MIN_PRICE} and "
                    f"{settings.MARKETPLACE_MAX_PRICE} KCoins.",
        )

    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    raw = await _read_upload(craft_file)
    # The mod gzips the craft; fall back to raw bytes if it wasn't compressed.
    try:
        craft_bytes = _safe_gunzip(raw)
    except (OSError, EOFError):
        craft_bytes = raw

    filename = craft_file.filename or "craft.craft"
    if not filename.lower().endswith(".craft"):
        filename += ".craft"

    # mods: client sends a comma-separated list of distinct GameData folders the
    # craft's parts come from. Dedup + drop blanks; stock-only crafts send nothing.
    mod_list = sorted({m.strip() for m in mods.split(",") if m.strip()})
    # parts: the craft's exact part names, for the pre-purchase compatibility check.
    # Capped — a very large craft is still only a few hundred distinct parts, and this
    # goes into a Firestore document with a 1 MiB ceiling.
    part_list = sorted({p.strip() for p in parts.split(",") if p.strip()})[:2000]
    # custom_textures: the client sends "1" only when the craft carries a Textures
    # Unlimited paint job, and nothing at all otherwise — which is also what an older
    # client sends, so an absent field has to mean False rather than an error.
    has_custom_textures = custom_textures.strip().lower() in ("1", "true", "yes")

    listing = mkt.create_listing(
        gid, uid, user["username"],
        craft_name=craft_name, craft_type=craft_type, part_count=part_count,
        mass=mass, cost=cost, price=price,
        craft_url="", craft_filename=filename,
        mods=mod_list, parts=part_list,
        life_support=(life_support or "none").strip().lower(),
        ls_endurance_days=ls_endurance_days,
        ls_crew_capacity=ls_crew_capacity,
        custom_textures=has_custom_textures,
    )

    try:
        url = await mkt.upload_craft(listing["listing_id"], filename, craft_bytes)
    except Exception as exc:
        log.error("Marketplace craft upload failed: %s", exc)
        return MarketplaceListResult(success=False, message="Failed to upload craft file.")

    mkt.update_listing(gid, listing["listing_id"], craft_url=url)
    listing["craft_url"] = url

    # Rendered blueprint image — shown publicly on the listing. Optional: if the
    # render failed client-side, the listing still posts without an image.
    if blueprint is not None:
        try:
            bp_data = await _read_upload(blueprint)
            bp_url = await mkt.upload_blueprint(
                listing["listing_id"], bp_data, blueprint.content_type or "image/png"
            )
            mkt.update_listing(gid, listing["listing_id"], blueprint_url=bp_url)
            listing["blueprint_url"] = bp_url
        except Exception as exc:
            log.error("Marketplace blueprint upload failed: %s", exc)

    # Square NW-view thumbnail — the website's listing-card image. Optional; the
    # site falls back to the full blueprint when a listing has none.
    if thumbnail is not None:
        try:
            thumb_data = await _read_upload(thumbnail)
            thumb_url = await mkt.upload_thumbnail(
                listing["listing_id"], thumb_data, thumbnail.content_type or "image/png"
            )
            mkt.update_listing(gid, listing["listing_id"], thumbnail_url=thumb_url)
            listing["thumbnail_url"] = thumb_url
        except Exception as exc:
            log.error("Marketplace thumbnail upload failed: %s", exc)

    # Complexity bonus. Counted in distinct part types (part_list), not part_count —
    # 300 copies of one girder is not a design. Claimed once per cooldown window;
    # the listing itself is never gated by it, so a second qualifying craft today
    # still lists, it just doesn't pay. A client too old to send `parts` sends an
    # empty list and so never qualifies: there is nothing here to judge complexity by.
    reward = 0
    reward_note = ""
    if len(part_list) > settings.MARKETPLACE_UPLOAD_REWARD_MIN_PARTS:
        granted, wait = await store.try_claim_timed_reward(
            gid, uid, "marketplace_upload",
            settings.MARKETPLACE_UPLOAD_REWARD,
            settings.MARKETPLACE_UPLOAD_REWARD_COOLDOWN,
        )
        if granted:
            reward = settings.MARKETPLACE_UPLOAD_REWARD
            reward_note = (f"+{reward:,} KCoins for a {len(part_list)}-part design.")
            log.info("KSP: %s earned %d KCoins for listing '%s' (%d distinct parts)",
                     user["username"], reward, craft_name, len(part_list))
        else:
            reward_note = ("Complexity bonus already claimed today — "
                           f"the next one is in {_fmt_wait(wait)}.")

    log.info("KSP: %s listed craft '%s' for %d (listing %s)",
             user["username"], craft_name, price, listing["listing_id"])
    return MarketplaceListResult(
        success=True,
        message=("Your craft is now for sale!" + (f" {reward_note}" if reward_note else "")),
        listing_id=listing["listing_id"],
        reward=reward,
        reward_note=reward_note,
    )


@app.get("/api/v1/marketplace/listings", response_model=MarketplaceListingsResponse)
async def marketplace_listings(user: dict = Depends(get_current_user)):
    """Return all active marketplace listings."""
    gid = int(user["guild_id"])
    listings = [
        MarketplaceListing(
            listing_id=l["listing_id"],
            seller_id=l["seller_id"],
            seller_name=l.get("seller_name", ""),
            craft_name=l.get("craft_name", ""),
            craft_type=l.get("craft_type", ""),
            part_count=l.get("part_count", 0),
            mass=l.get("mass", 0.0),
            cost=l.get("cost", 0.0),
            price=l.get("price", 0),
            sales_count=l.get("sales_count", 0),
            created_at=l.get("created_at"),
        )
        for l in mkt.list_active(gid)
    ]
    return MarketplaceListingsResponse(listings=listings)


@app.post("/api/v1/marketplace/{listing_id}/delist", response_model=MarketplaceListResult)
async def marketplace_delist(listing_id: str, user: dict = Depends(get_current_user)):
    """Delist a craft the caller owns."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    listing = mkt.get_listing(gid, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your listing")

    if listing.get("status") == mkt.ACTIVE:
        mkt.update_listing(gid, listing_id, status=mkt.DELISTED)
        listing["status"] = mkt.DELISTED

    return MarketplaceListResult(success=True, message="Craft delisted.", listing_id=listing_id)


# ══════════════════════════════════════════════════════════════════════════════
#  Website API (/api/v1/web/...)
#
#  A second client of this same API: the public marketplace website. It is a
#  browser, so it has no GeneKerman.dll and no device id — the mod-version gate
#  and device binding don't apply. These endpoints therefore use token-only auth
#  (get_user_token_only) and a link flow that skips enforce_mod_version. The
#  website never talks to Firestore directly; everything (balance, buy, delist)
#  goes through here so the bot stays the single source of truth.
# ══════════════════════════════════════════════════════════════════════════════

def _has_custom_textures(l: dict) -> bool:
    """Whether a listing's craft carries a Textures Unlimited paint job.

    The KSP client sends this as its own flag at list-time, which is the answer to
    trust. A listing made before that flag existed carries no field, and for those the
    mod row is a sound stand-in: TU adds zero parts, so its folder can only have got
    into `mods` through the paint-job scan that put it there (TexturePackFoldersForCraft),
    never through the part walk. Matched on a normalized substring because TU is
    installed under more than one folder name ("TexturesUnlimited", "000_TexturesUnlimited");
    TUFX — scene-wide post-processing, nothing per-craft — does not match it and must not.
    """
    if "custom_textures" in l:
        return bool(l.get("custom_textures"))
    for m in l.get("mods", []) or []:
        if "texturesunlimited" in "".join(c for c in str(m).lower() if c.isalnum()):
            return True
    return False


def _listing_to_model(l: dict, include_download: bool = False) -> MarketplaceListing:
    """Map a raw Firestore listing dict to the API model.

    craft_url is the paywalled download link and is withheld by default: the public
    grid must not hand a non-buyer the craft. It's included (as a fresh signed URL,
    7-day max TTL so a page left open still works) only for owner/buyer surfaces —
    My Uploads and My Purchases — where the caller is entitled to the file."""
    craft_url = (sign_stored(l.get("craft_url"), ttl=SIGNED_URL_MAX_TTL)
                 if include_download else None)
    return MarketplaceListing(
        listing_id=l["listing_id"],
        seller_id=l["seller_id"],
        seller_name=l.get("seller_name", ""),
        craft_name=l.get("craft_name", ""),
        craft_type=l.get("craft_type", ""),
        part_count=l.get("part_count", 0),
        mass=l.get("mass", 0.0),
        cost=l.get("cost", 0.0),
        price=l.get("price", 0),
        sales_count=l.get("sales_count", 0),
        created_at=l.get("created_at"),
        mods=l.get("mods", []) or [],
        thumbnail_url=l.get("thumbnail_url") or None,
        blueprint_url=l.get("blueprint_url") or None,
        craft_url=craft_url,
        craft_filename=l.get("craft_filename") or None,
        status=l.get("status", mkt.ACTIVE),
        life_support=l.get("life_support", "none") or "none",
        ls_endurance_days=l.get("ls_endurance_days", 0.0) or 0.0,
        ls_crew_capacity=l.get("ls_crew_capacity", 0) or 0,
        custom_textures=_has_custom_textures(l),
        likes=int(l.get("likes", 0) or 0),
        dislikes=int(l.get("dislikes", 0) or 0),
    )


WEB_PAGE_SIZE = 25

# "Recommended" is a *discovery* sort: it is there so a craft uploaded this week can
# be found at all, which sorting by likes alone never allows — a listing that has been
# up for a year outvotes a good one from Tuesday no matter how well the new one is
# received. So the window is what makes it work, not a detail of it.
RECOMMENDED_WINDOW_DAYS = 15


def _listing_age_days(l: dict, now: datetime) -> float:
    """Age of a listing in days. A listing with no/unparseable created_at is treated
    as ancient rather than brand new, so a broken timestamp can't win a rate sort."""
    raw = l.get("created_at") or ""
    try:
        # created_at is written by data/marketplace.py as a naive UTC isoformat.
        return max(0.0, (now - datetime.fromisoformat(raw)).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return float(RECOMMENDED_WINDOW_DAYS * 100)


def _net_likes(l: dict) -> int:
    return int(l.get("likes", 0) or 0) - int(l.get("dislikes", 0) or 0)


def _recommend_rate(l: dict, now: datetime) -> float:
    """Net likes per day of existence, damped by a day.

    The +1 is what stops an hours-old listing with a single like from sitting at the
    top of the page forever: without it, dividing by a near-zero age makes the first
    vote worth more than every later one put together.
    """
    return _net_likes(l) / (_listing_age_days(l, now) + 1.0)


@app.post("/api/v1/web/auth/link", response_model=LinkResponse)
async def web_auth_link(req: LinkRequest, request: Request,
                        x_device_id: str = Header(default="", alias="X-Device-Id")):
    """Website link: exchange a 6-digit code for a session token (or a login-
    approval challenge). Identical to /auth/link but WITHOUT the mod-version gate,
    since a browser has no DLL to hash."""
    _guard_link_attempt(request)

    result = validate_link_code(req.code)
    if result is None:
        _note_failed_link_guess()
        raise HTTPException(status_code=400, detail="Invalid or expired link code")

    if not cfg.KSP_2FA_ENABLED:
        return _issue_link_token(result, x_device_id)

    client_ip = _client_ip(request)
    challenge_id = create_approval_challenge(
        result["guild_id"], result["user_id"], result["username"], client_ip)
    sent = await _dm_login_approval(int(result["user_id"]), challenge_id, client_ip)
    if not sent:
        raise HTTPException(
            status_code=502,
            detail="Couldn't DM your login approval. Enable DMs from server "
                   "members in Discord, then request a new link code.",
        )
    log.info("WEB: login-approval challenge issued for %s", result["username"])
    return LinkResponse(status="approval_required", challenge_id=challenge_id)


@app.post("/api/v1/web/auth/link/poll", response_model=LinkResponse)
async def web_auth_link_poll(req: PollRequest, request: Request,
                             x_device_id: str = Header(default="", alias="X-Device-Id")):
    """Poll a website login-approval challenge until the user presses Log-in."""
    _rate_limit(f"webpoll:{_client_ip(request)}", max_hits=120, window=60.0)
    state = poll_approval(req.challenge_id)
    if state["state"] == "pending":
        return LinkResponse(status="pending")
    if state["state"] == "approved":
        log.info("WEB: login approved, linking %s", state["username"])
        return _issue_link_token(state, x_device_id)
    if state["state"] == "denied":
        raise HTTPException(status_code=403, detail="Login request was denied.")
    raise HTTPException(status_code=400, detail="Login request expired. Request a new link code.")


@app.get("/api/v1/web/profile", response_model=UserProfile)
async def web_profile(user: dict = Depends(get_user_token_only)):
    """The logged-in website user's profile (balance, XP, level). Token-only auth."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    u = store.get_user(gid, uid)
    return UserProfile(
        user_id=user["user_id"],
        username=user["username"],
        guild_id=user["guild_id"],
        xp=u.get("xp", 0),
        level=u.get("level", 0),
        balance=u.get("balance", 0),
        messages=u.get("messages", 0),
        unlocked_levels=u.get("unlocked_levels", []),
        currency_name=settings.CURRENCY_NAME,
        is_owner=_is_owner_id(user["user_id"]),
    )


@app.get("/api/v1/web/marketplace/listings", response_model=MarketplaceListingsPage)
async def web_marketplace_listings(
    page: int = 1,
    sort: str = "new",
    price_min: int | None = None,
    price_max: int | None = None,
    craft_type: str | None = None,
    parts_max: int | None = None,
    mass_max: float | None = None,
    mods: list[str] = Query(default=[]),
    mod_mode: str = "required",
    q: str | None = None,
):
    """Paginated, filterable, sortable marketplace grid for the website (25/page).

    Public (no auth): the catalog is already mirrored to public Discord channels,
    so browsing needs no login — only buying / managing uploads does.

    Filtering/sorting is done in Python over all active listings: the shared market
    is small enough that this is simpler and cheaper than maintaining Firestore
    composite indexes for every filter combination. Revisit if it ever grows large.
    """
    items = mkt.list_active(0)

    # available_mods is computed from the *unfiltered* active set so the facet shows
    # every mod a user could filter by, not just those left after the current filter.
    available_mods = sorted({m for l in items for m in (l.get("mods") or [])})

    def _keep(l: dict) -> bool:
        if price_min is not None and l.get("price", 0) < price_min:
            return False
        if price_max is not None and l.get("price", 0) > price_max:
            return False
        if craft_type and l.get("craft_type", "").upper() != craft_type.upper():
            return False
        if parts_max is not None and l.get("part_count", 0) > parts_max:
            return False
        if mass_max is not None and l.get("mass", 0.0) > mass_max:
            return False
        if mods:
            lm = set(l.get("mods") or [])
            sel = set(mods)
            if mod_mode == "allowed":
                # craft may only use mods from the selection (nothing outside it)
                if not lm.issubset(sel):
                    return False
            elif mod_mode == "restricted":
                # craft must not use any of the selected mods
                if not sel.isdisjoint(lm):
                    return False
            else:  # "required" — craft must include every selected mod
                if not sel.issubset(lm):
                    return False
        if q:
            ql = q.lower()
            if ql not in l.get("craft_name", "").lower() and ql not in l.get("seller_name", "").lower():
                return False
        return True

    items = [l for l in items if _keep(l)]

    if sort == "price_asc":
        items.sort(key=lambda l: l.get("price", 0))
    elif sort == "price_desc":
        items.sort(key=lambda l: l.get("price", 0), reverse=True)
    elif sort == "sales":
        items.sort(key=lambda l: l.get("sales_count", 0), reverse=True)
    elif sort == "likes":
        items.sort(key=lambda l: (_net_likes(l), int(l.get("likes", 0) or 0),
                                  l.get("created_at") or ""), reverse=True)
    elif sort == "recommended":
        # Fresh crafts (< RECOMMENDED_WINDOW_DAYS old) ranked by how fast they are
        # collecting likes, then everything else by net likes. The older half is a
        # *tail*, not a second ranking: without it a quiet fortnight would leave the
        # tab all but empty, which is worse than showing well-liked older crafts
        # below the new ones.
        now = datetime.utcnow()
        fresh = [l for l in items if _listing_age_days(l, now) <= RECOMMENDED_WINDOW_DAYS]
        rest = [l for l in items if _listing_age_days(l, now) > RECOMMENDED_WINDOW_DAYS]
        fresh.sort(key=lambda l: (_recommend_rate(l, now), l.get("created_at") or ""),
                   reverse=True)
        rest.sort(key=lambda l: (_net_likes(l), l.get("created_at") or ""), reverse=True)
        items = fresh + rest
    else:  # "new"
        items.sort(key=lambda l: l.get("created_at") or "", reverse=True)

    total = len(items)
    pages = max(1, (total + WEB_PAGE_SIZE - 1) // WEB_PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * WEB_PAGE_SIZE
    window = items[start:start + WEB_PAGE_SIZE]

    return MarketplaceListingsPage(
        listings=[_listing_to_model(l) for l in window],
        total=total,
        page=page,
        pages=pages,
        available_mods=available_mods,
    )


@app.post("/api/v1/web/marketplace/{listing_id}/buy", response_model=WebBuyResult)
async def web_marketplace_buy(listing_id: str, user: dict = Depends(get_user_token_only)):
    """Buy a craft from the website — the only place a craft is bought now that
    Discord's Buy button is retired. Atomically debits the buyer, credits the seller,
    queues the craft for KSP auto-import AND returns a direct .craft download URL.
    Re-buying a craft you already own is a free re-delivery (no charge)."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    listing = mkt.get_listing(gid, listing_id)
    if not listing or listing.get("status") != mkt.ACTIVE:
        raise HTTPException(status_code=404, detail="This craft is no longer for sale.")

    seller_id = int(listing["seller_id"])
    if uid == seller_id:
        raise HTTPException(status_code=400, detail="You can't buy your own listing.")

    price = int(listing["price"])
    already_owned = str(uid) in (listing.get("buyers") or [])
    new_purchase = False

    if not already_owned:
        # Atomic check-and-deduct so a double-submit can't overdraw.
        if not await store.try_debit(gid, uid, price):
            bal = store.get_user(gid, uid).get("balance", 0)
            return WebBuyResult(
                success=False, balance=bal,
                message=f"You need {price:,} {settings.CURRENCY_NAME} but only have {bal:,}.",
            )
        # Debit succeeded — now claim ownership atomically. The claim is a Firestore
        # transaction, so of two concurrent double-submits exactly one adds the buyer
        # and gets True; the loser is refunded below. Without this, both requests pass
        # the `already_owned` read above and the buyer pays twice for one craft.
        claimed = await asyncio.to_thread(mkt.try_claim_purchase, gid, listing_id, uid)
        if claimed:
            new_purchase = True
            await store.add_balance(gid, seller_id, price)
        else:
            # Listing vanished mid-buy, or a concurrent request already recorded this
            # buyer: the debit was redundant → refund it and don't pay the seller.
            await store.add_balance(gid, uid, price)
            already_owned = True

    # Queue for KSP auto-import (idempotent on source+ref_id) and offer direct download.
    imp.enqueue(
        gid, uid, "market", listing_id, listing.get("craft_name", "Craft"),
        craft_url=listing.get("craft_url"), craft_filename=listing.get("craft_filename"),
    )

    if new_purchase:
        if _bot_instance is not None:
            # Notify the seller, best-effort.
            try:
                seller = await _bot_instance.fetch_user(seller_id)
                await seller.send(
                    f"💰 **{user['username']}** bought your craft **{listing.get('craft_name')}** "
                    f"for **{price:,}** {settings.CURRENCY_SYMBOL} on the website."
                )
            except Exception:
                pass

    bal = store.get_user(gid, uid).get("balance", 0)
    log.info("WEB: %s bought listing %s for %d%s", user["username"], listing_id, price,
             " (already owned, free)" if already_owned else "")
    return WebBuyResult(
        success=True,
        message=("Re-downloaded (you already own this craft)." if already_owned
                 else f"Purchased for {price:,} {settings.CURRENCY_NAME}. Queued for KSP import."),
        balance=bal,
        # Buyer just paid (or already owns) → mint a signed download link.
        craft_url=sign_stored(listing.get("craft_url")) or None,
        craft_filename=listing.get("craft_filename") or None,
        already_owned=already_owned,
        compatibility=_craft_compatibility(gid, uid, listing),
    )


@app.get("/api/v1/web/marketplace/{listing_id}/compatibility",
         response_model=CraftCompatibility)
async def web_marketplace_compatibility(listing_id: str,
                                        user: dict = Depends(get_user_token_only)):
    """Whether the caller can load this craft, checked against the part catalog their
    KSP client uploaded. For the listing detail view, so a buyer finds out BEFORE
    paying rather than when the craft won't open in the VAB."""
    listing = mkt.get_listing(int(user["guild_id"]), listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="No such listing.")
    return _craft_compatibility(int(user["guild_id"]), int(user["user_id"]), listing)


# ── Website: votes & reports ─────────────────────────────────────────────────
#
# All three endpoints below are token-only authed, and that is the point: a vote or
# a report is attributable to a linked Discord account or it is worthless. Browsing
# stays public (the catalog is mirrored to public Discord channels anyway), so an
# anonymous visitor sees the tallies and is asked to sign in to move them.

_REPORT_REASON_MAX = 1500


@app.get("/api/v1/web/marketplace/votes", response_model=MyVotesResponse)
async def web_marketplace_my_votes(user: dict = Depends(get_user_token_only)):
    """Every vote the caller has cast, so the grid can show which crafts they've
    already voted on. One document read for the whole marketplace."""
    return MyVotesResponse(votes=mkt.get_user_votes(int(user["user_id"])))


@app.post("/api/v1/web/marketplace/{listing_id}/vote", response_model=VoteResult)
async def web_marketplace_vote(listing_id: str, req: VoteRequest,
                               user: dict = Depends(get_user_token_only)):
    """Like (1), dislike (-1) or clear (0) the caller's vote on a listing."""
    uid = int(user["user_id"])
    # Generous, because changing your mind is normal and toggling costs one small
    # write; tight enough that a script can't inflate the "recommended" sort by
    # racing a listing's counter with a rented account.
    _rate_limit(f"mkvote:{uid}", max_hits=120, window=3600.0)

    listing = mkt.get_listing(int(user["guild_id"]), listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="No such listing.")
    if listing.get("seller_id") == str(uid):
        raise HTTPException(status_code=400, detail="You can't vote on your own craft.")

    result = await asyncio.to_thread(mkt.set_vote, listing_id, uid, req.vote)
    if result is None:
        raise HTTPException(status_code=404, detail="No such listing.")
    likes, dislikes = result
    my_vote = mkt.VOTE_UP if req.vote > 0 else (mkt.VOTE_DOWN if req.vote < 0 else mkt.VOTE_NONE)
    return VoteResult(success=True, likes=likes, dislikes=dislikes, my_vote=my_vote)


@app.post("/api/v1/web/marketplace/{listing_id}/report", response_model=ReportResult)
async def web_marketplace_report(listing_id: str, req: ReportRequest,
                                 user: dict = Depends(get_user_token_only)):
    """Report a listing to the moderators as a private Discord ticket.

    The ticket is opened in the *reporter's* server, because a ticket they cannot
    see is no use to them — they'd have nowhere to answer a follow-up question. The
    listing's origin server is named in the embed instead, since the marketplace is
    global and the two are often different.

    The seller is `subject_user_id`: shown to the mods for context, deliberately NOT
    granted access to the channel — the same rule anti-cheat tickets follow.
    """
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    # A report costs a channel and a moderator's attention. Three an hour is far
    # more than anyone files in good faith; per user rather than per IP because a
    # ticket is attributable to an account.
    _rate_limit(f"mkreport:{uid}", max_hits=3, window=3600.0)

    reason = (req.reason or "").strip()[:_REPORT_REASON_MAX]
    if not reason:
        raise HTTPException(status_code=400, detail="Say what's wrong with this craft.")

    listing = mkt.get_listing(gid, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="No such listing.")
    if listing.get("seller_id") == str(uid):
        raise HTTPException(status_code=400, detail="That's your own craft.")
    if await asyncio.to_thread(mkt.get_report, listing_id, uid):
        raise HTTPException(status_code=409,
                            detail="You've already reported this craft — the mods have it.")

    if not _bot_instance:
        raise HTTPException(status_code=503, detail="The bot is not available right now.")
    guild = _bot_instance.get_guild(gid)
    if guild is None:
        raise HTTPException(status_code=503,
                            detail="Your Discord server is not reachable right now.")

    import discord
    from cogs.tickets import create_ticket

    seller_id = str(listing.get("seller_id", ""))
    origin_gid = str(listing.get("guild_id", "") or "")
    origin = _bot_instance.get_guild(int(origin_gid)) if origin_gid.isdigit() else None
    origin_name = origin.name if origin else f"server `{origin_gid or 'unknown'}`"
    e = discord.Embed(
        title="🛒 Reported listing",
        description=(
            f"**Craft:** {listing.get('craft_name', 'Unknown')}\n"
            f"**Listing ID:** `{listing_id}`\n"
            f"**Price:** {int(listing.get('price', 0)):,} {settings.CURRENCY_SYMBOL}\n"
            f"**Status:** {listing.get('status', mkt.ACTIVE)} · "
            f"{int(listing.get('sales_count', 0))} sold\n"
            f"**Seller:** {listing.get('seller_name', 'Unknown')} (<@{seller_id}>, `{seller_id}`)\n"
            f"**Listed from:** {origin_name}\n"
            f"**Reported by:** {user.get('username', 'Unknown')} (<@{uid}>, `{uid}`)"
        ),
        color=discord.Color.orange(),
    )
    if listing.get("thumbnail_url") or listing.get("blueprint_url"):
        e.set_image(url=listing.get("thumbnail_url") or listing["blueprint_url"])

    channel = await create_ticket(
        _bot_instance, guild,
        opener_id=uid,
        subject_user_id=int(seller_id) if seller_id.isdigit() else None,
        kind="user",
        title="Marketplace report",
        description=f"**Why this craft was reported**\n{reason}",
        color=discord.Color.orange(),
        extra_embeds=[e],
    )
    if channel is None:
        raise HTTPException(
            status_code=503,
            detail="Couldn't open a ticket — this server's ticket system isn't set up.")

    await asyncio.to_thread(
        mkt.record_report, listing, uid, user.get("username", ""), reason,
        gid, channel.id,
    )
    log.info("WEB: %s reported listing %s (seller %s)", user.get("username"), listing_id, seller_id)
    return ReportResult(
        success=True,
        message=f"Reported. A private ticket (#{channel.name}) is open in Discord.")


# ── Website: contracts ───────────────────────────────────────────────────────
#
# The same contracts the KSP mod shows, on a phone. Every action below is a thin
# wrapper over `contract_actions`, which is also what the mod endpoints and the
# Discord buttons call — adding this surface adds no new copy of any transition,
# which was the whole point of extracting that module first.
#
# What is deliberately NOT here: **submitting**. A submission carries live telemetry
# and a screenshot from a running game, so it cannot originate in a browser at all —
# and it is where cheating actually bites, which is why the gated tier (device
# binding + mod hash) exists on the mod endpoints and is not needed on these.
#
# Each action gets its own path. Never a `/contracts/{id}/{action}` wildcard: that
# would silently enrol every verb `contract_actions` grows later, and this tier has
# the weakest auth of the three.


def _web_contract_summary(c: dict, uid: str, bot_uid: str) -> ContractSummary:
    """A contract as the website needs it.

    Classification is deliberately skipped, unlike /api/v1/contracts/active. That path
    calls `_classify_single_contract` for anything unclassified — a *Gemini request* —
    and resolves part constraints against the user's installed-part catalog. Both exist
    to drive in-editor enforcement in KSP. A browser enforces nothing, so paying an AI
    call to open a list on a phone would be pure waste; stored values are used as-is.
    """
    return ContractSummary(
        contract_id=c["contract_id"],
        mission=c["mission"],
        issuer_name=c.get("issuer_name", "Unknown"),
        contractor_name=c.get("contractor_name", "Unknown"),
        payment=c["payment"],
        fine=c["fine"],
        due_date=c["due_date"],
        status=c["status"],
        created_at=c.get("created_at"),
        is_bot_issued=(str(c.get("issuer_id")) == bot_uid),
        is_outgoing=(str(c.get("issuer_id")) == uid),
        modlist=c.get("modlist"),
        mission_type=c.get("mission_type") or "active_vessel",
        required_situation=c.get("required_situation"),
        required_body=c.get("required_body"),
        flag_preview_url=c.get("flag_preview_url"),
        rescue_kerbals=c.get("rescue_kerbals", []) or [],
        life_support=c.get("life_support", "none") or "none",
        ls_endurance_days=float(c.get("ls_endurance_days") or 0.0),
        ls_crew_capacity=int(c.get("ls_crew_capacity") or 0),
        pending_request=(PendingRequest(**c["pending_request"])
                         if c.get("pending_request") else None),
        auto_fine_at=(_dt.isoformat() if (_dt := ca.auto_fine_at(c)) else None),
        more_time_used=(int(c.get("more_time_requests") or 0)
                        >= settings.DISPUTE_MAX_MORE_TIME_REQUESTS),
    )


@app.get("/api/v1/web/contracts", response_model=ContractListResponse)
async def web_contracts(user: dict = Depends(get_user_token_only)):
    """Every contract this user is party to that is still going somewhere.

    One call for both directions and both open states, because the client is a single
    page that filters in memory — the mod splits active/incoming only because its two
    IMGUI tabs fetch independently.
    """
    gid = int(user["guild_id"])
    uid = str(user["user_id"])
    bot_uid = str(_get_bot_user_id())

    open_statuses = {cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED,
                     cdb.MOD_REVIEW, cdb.COMPLETED}
    contracts = [_web_contract_summary(c, uid, bot_uid)
                 for c in cdb.iter_user_contracts(gid, uid)
                 if c.get("status") in open_statuses]
    contracts.sort(key=lambda x: x.created_at or "", reverse=True)
    return ContractListResponse(contracts=contracts)


def _web_actor(user: dict, request: Request) -> tuple[int, int, str]:
    """(guild_id, user_id, username) after a per-user rate limit.

    Keyed on the account rather than the IP: these actions move money, and the thing
    worth bounding is one account hammering its own contracts, not a shared NAT.
    """
    _rate_limit(f"webct:{user['user_id']}", max_hits=30, window=60.0)
    return int(user["guild_id"]), int(user["user_id"]), user["username"]


def _web_result(r: ca.Result) -> ContractAcceptResponse:
    """Business failures come back as 200 + success:false so the page can print the
    sentence; only "not yours" and "no such contract" are HTTP failures."""
    _raise_for(r)
    return ContractAcceptResponse(success=r.ok, message=r.message)


@app.post("/api/v1/web/contracts/{contract_id}/accept", response_model=ContractAcceptResponse)
async def web_contract_accept(contract_id: str, request: Request,
                              user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.accept(gid, contract_id, actor_id=uid, actor_name=name))


@app.post("/api/v1/web/contracts/{contract_id}/cancel", response_model=ContractAcceptResponse)
async def web_contract_cancel(contract_id: str, request: Request,
                              user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.cancel(gid, contract_id, actor_id=uid, actor_name=name))


@app.post("/api/v1/web/contracts/{contract_id}/give_up", response_model=ContractAcceptResponse)
async def web_contract_give_up(contract_id: str, request: Request,
                               user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.give_up(gid, contract_id, actor_id=uid, actor_name=name))


@app.post("/api/v1/web/contracts/{contract_id}/review", response_model=ContractAcceptResponse)
async def web_contract_review(contract_id: str, req: ContractReviewRequest, request: Request,
                              user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.review(gid, contract_id, actor_id=uid, actor_name=name,
                                       approve=bool(req.approve)))


@app.post("/api/v1/web/contracts/{contract_id}/dispute", response_model=ContractAcceptResponse)
async def web_contract_dispute(contract_id: str, req: ContractDisputeRequest, request: Request,
                               user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.dispute(gid, contract_id, actor_id=uid, actor_name=name,
                                        action=req.action or "", new_date=req.new_date or ""))


@app.post("/api/v1/web/contracts/{contract_id}/settle_response", response_model=ContractAcceptResponse)
async def web_contract_settle_response(contract_id: str, req: ContractRequestResponse,
                                       request: Request,
                                       user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.settle_response(gid, contract_id, actor_id=uid,
                                                actor_name=name, approve=bool(req.approve)))


@app.post("/api/v1/web/contracts/{contract_id}/more_time_response", response_model=ContractAcceptResponse)
async def web_contract_more_time_response(contract_id: str, req: ContractRequestResponse,
                                          request: Request,
                                          user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    return _web_result(await ca.more_time_response(gid, contract_id, actor_id=uid,
                                                   actor_name=name, approve=bool(req.approve)))


# ── Web auctions ─────────────────────────────────────────────────────────────
#
# The website's window onto the same global reverse auctions that run in Discord.
# One shared document per auction (auctions/{id} in Firestore), so a bid placed
# here and a press of the Discord "Bid Lower" button are the same write — the
# rules (undercut by min_decrement, anti-snipe, issuer can't bid) are mirrored
# from cogs/auctions.BidModal, and every mutation re-renders the Discord mirrors
# so the channel embeds never lag the site. Only OPEN auctions are listed: a
# closed auction becomes a contract, which the Contracts tab already shows.

def _web_auction_model(a: dict, uid: str) -> WebAuction:
    return WebAuction(
        auction_id=a["auction_id"],
        mission=a.get("mission", ""),
        issuer_name=a.get("issuer_name", ""),
        start_value=int(a.get("start_value", 0)),
        current_bid=int(a.get("current_bid") or a.get("start_value", 0)),
        current_bidder_name=a.get("current_bidder_name"),
        bid_count=int(a.get("bid_count", 0)),
        min_decrement=int(a.get("min_decrement", 1)),
        fine=int(a.get("fine", 0)),
        due_date=a.get("due_date", ""),
        ends_at=a.get("ends_at", ""),
        created_at=a.get("created_at"),
        mission_type=a.get("mission_type"),
        modlist=a.get("modlist"),
        is_yours=str(a.get("issuer_id")) == uid,
        is_leading=(a.get("current_bidder_id") is not None
                    and str(a.get("current_bidder_id")) == uid),
    )


@app.get("/api/v1/web/auctions", response_model=WebAuctionListResponse)
async def web_auctions(user: dict = Depends(get_user_token_only)):
    """Every open auction, soonest-ending first. Auctions are global (mirrored
    into every server), so there is no guild filter — same view Discord gets."""
    uid = str(user["user_id"])
    auctions = [_web_auction_model(a, uid) for a in aucdb.list_open(0)]
    auctions.sort(key=lambda x: x.ends_at or "")
    return WebAuctionListResponse(auctions=auctions)


@app.post("/api/v1/web/auctions/{auction_id}/bid", response_model=ContractAcceptResponse)
async def web_auction_bid(auction_id: str, req: WebAuctionBidRequest, request: Request,
                          user: dict = Depends(get_user_token_only)):
    gid, uid, name = _web_actor(user, request)
    if _bot_instance is None:
        return ContractAcceptResponse(success=False, message="Auctions are not available right now.")

    # The whole bid — re-read, re-validate the ceiling, and write — runs in one
    # Firestore transaction (see aucdb.try_place_bid), so a concurrent lower bid can't
    # be clobbered by a higher one landing microseconds later. Same checks and order
    # as the Discord modal, just made atomic.
    res = await asyncio.to_thread(
        aucdb.try_place_bid, gid, auction_id, uid, name, req.amount,
        settings.AUCTION_ANTISNIPE_SECONDS)

    if not res["ok"]:
        reason = res["reason"]
        if reason == "missing":
            raise HTTPException(status_code=404, detail="No such auction")
        if reason == "closed":
            return ContractAcceptResponse(success=False, message="This auction has already ended.")
        if reason == "own":
            return ContractAcceptResponse(success=False, message="You can't bid on your own auction.")
        # too_high
        ceiling, step = res["ceiling"], res["step"]
        return ContractAcceptResponse(
            success=False,
            message=f"Bid must be at most {ceiling} — undercut the current lowest by at least {step}.",
        )

    a = res["auction"]
    from cogs.auctions import _edit_auction_message
    await _edit_auction_message(_bot_instance, a, int(a.get("guild_id") or gid), live=True)
    log.info("WEB: %s bid %d on auction %s", name, req.amount, auction_id)
    return ContractAcceptResponse(
        success=True, message=f"Bid placed: {req.amount}. You're the lowest bidder!")


@app.post("/api/v1/web/auctions/{auction_id}/end", response_model=ContractAcceptResponse)
async def web_auction_end(auction_id: str, request: Request,
                          user: dict = Depends(get_user_token_only)):
    """End the caller's own auction early — the website's "End now" button."""
    gid, uid, name = _web_actor(user, request)
    if _bot_instance is None:
        return ContractAcceptResponse(success=False, message="Auctions are not available right now.")
    a = aucdb.get_auction(gid, auction_id)
    if a is None:
        raise HTTPException(status_code=404, detail="No such auction")
    if str(uid) != str(a["issuer_id"]):
        raise HTTPException(status_code=403, detail="Only the issuer can end this auction.")
    if a["status"] != aucdb.OPEN:
        return ContractAcceptResponse(success=False, message="This auction has already ended.")
    from cogs.auctions import close_auction
    await close_auction(_bot_instance, gid, auction_id, ended_by="issuer")
    log.info("WEB: %s ended auction %s early", name, auction_id)
    return ContractAcceptResponse(success=True, message="Auction ended.")


# ── Web → game commands ──────────────────────────────────────────────────────
#
# The website can ask the caller's own running KSP to raise a window. It travels
# down the notification socket the client already holds, addressed to one account.
#
# Two rules hold this surface in place, and both are deliberate:
#
#   1. **Commands may only raise UI.** Contract actions go over HTTP and are
#      authorized against the account like any other request. A command is
#      different: it reaches into a running game process, where no per-action
#      authorization is possible on the receiving end. So the allow-list stays
#      UI-only. Something irreversible needs a stronger auth tier, not a bigger
#      allow-list — `/api/v1/web/*` is token-only, with no device binding and no
#      mod-version gate, because a browser has no DLL to hash.
#   2. **The frame carries an id, never terms.** The mod re-reads the mission
#      type, situation, body, mod list and constraints from /contracts/active,
#      so a caller here cannot quietly relax a contract's own requirements.
#
# Free by construction: the socket is closed while data sharing is off, so no
# command can arrive at an opted-out client.

_GAME_COMMANDS = {"open_submit"}
_SAFE_CONTRACT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@app.post("/api/v1/web/game/command", response_model=GameCommandResult)
async def web_game_command(req: GameCommandRequest, request: Request,
                           user: dict = Depends(get_user_token_only)):
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    _rate_limit(f"cmd:{uid}", max_hits=20, window=60.0)

    command = (req.command or "").strip()
    if command not in _GAME_COMMANDS:
        raise HTTPException(status_code=400, detail="Unknown command.")

    # An enumerated arm per command, matching the mod's switch. Each new command is
    # a decision made here and there — this is not a dispatcher.
    frame = None
    if command == "open_submit":
        cid = (req.contract_id or "").strip()
        if not _SAFE_CONTRACT_ID.match(cid):
            raise HTTPException(status_code=400, detail="Bad contract id.")

        # Checked here only so the page can say why nothing happened. The mod
        # re-validates independently against its own token, which is what actually
        # guards this — the check below can only ever see the caller's contracts.
        c = cdb.get_contract(gid, cid)
        if not c or str(c.get("contractor_id")) != str(uid):
            raise HTTPException(status_code=404, detail="Contract not found.")
        if c.get("status") != cdb.ACTIVE:
            return GameCommandResult(
                success=False,
                message="That contract is not active, so there is nothing to submit.")

        # `mission` is the server's own lookup, not something the page supplied, so
        # naming it in the prompt is safe — and a prompt that says which contract it
        # is about is the difference between an answerable question and a jump scare.
        frame = {"type": "command", "command": "open_submit",
                 "contract_id": cid, "mission": c.get("mission") or ""}

    if frame is None:
        # A name in the allow-list with no arm above. Refuse rather than send a frame
        # the mod's switch has never seen.
        log.error("Game command %s is allow-listed but has no handler", command)
        raise HTTPException(status_code=500, detail="Command not implemented.")

    clients = await _hub.push_frame(gid, uid, frame)
    if clients == 0:
        return GameCommandResult(
            success=False, clients=0,
            message="KSP isn't running, or notifications are off in the mod's settings.")

    log.info("WS: command %s sent to user %d (%d client%s)",
             command, uid, clients, "" if clients == 1 else "s")
    return GameCommandResult(
        success=True, clients=clients,
        message="Sent to KSP. Accept the prompt in game." if clients == 1
                else f"Sent to {clients} running KSP clients. Accept the prompt in game.")


@app.get("/api/v1/web/marketplace/mine", response_model=MarketplaceListingsResponse)
async def web_marketplace_mine(user: dict = Depends(get_user_token_only)):
    """The caller's own listings (active + delisted) — the "My Uploads" view."""
    uid = int(user["user_id"])
    items = sorted(mkt.list_by_seller(uid), key=lambda l: l.get("created_at") or "", reverse=True)
    # Owner of these listings → entitled to the download link.
    return MarketplaceListingsResponse(
        listings=[_listing_to_model(l, include_download=True) for l in items])


@app.get("/api/v1/web/marketplace/purchases", response_model=MarketplaceListingsResponse)
async def web_marketplace_purchases(user: dict = Depends(get_user_token_only)):
    """Crafts the caller has bought — the "My Purchases" view (free re-download)."""
    uid = int(user["user_id"])
    items = sorted(mkt.list_by_buyer(uid), key=lambda l: l.get("created_at") or "", reverse=True)
    # Buyer of these crafts → entitled to re-download.
    return MarketplaceListingsResponse(
        listings=[_listing_to_model(l, include_download=True) for l in items])


@app.post("/api/v1/web/marketplace/{listing_id}/delist", response_model=MarketplaceListResult)
async def web_marketplace_delist(listing_id: str, user: dict = Depends(get_user_token_only)):
    """Delist a craft the caller owns (the website "My Uploads" delete action).
    Uploads stay mod-only; this only flips an existing listing to delisted."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    listing = mkt.get_listing(gid, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your listing")

    if listing.get("status") == mkt.ACTIVE:
        mkt.update_listing(gid, listing_id, status=mkt.DELISTED)
        listing["status"] = mkt.DELISTED

    return MarketplaceListResult(success=True, message="Craft delisted.", listing_id=listing_id)


@app.post("/api/v1/web/marketplace/{listing_id}/relist", response_model=MarketplaceListResult)
async def web_marketplace_relist(listing_id: str, user: dict = Depends(get_user_token_only)):
    """Re-activate a delisted craft the caller owns (puts it back up for sale)."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    listing = mkt.get_listing(gid, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your listing")

    if listing.get("status") != mkt.ACTIVE:
        mkt.update_listing(gid, listing_id, status=mkt.ACTIVE)
        listing["status"] = mkt.ACTIVE

    return MarketplaceListResult(success=True, message="Craft relisted.", listing_id=listing_id)


@app.post("/api/v1/web/marketplace/{listing_id}/delete", response_model=MarketplaceListResult)
async def web_marketplace_delete(listing_id: str, user: dict = Depends(get_user_token_only)):
    """Permanently delete a craft the caller owns: erases the Storage files and the
    listing document. Irreversible (vs. delist)."""
    gid = int(user["guild_id"])
    uid = int(user["user_id"])

    listing = mkt.get_listing(gid, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") != str(uid):
        raise HTTPException(status_code=403, detail="Not your listing")

    mkt.delete_listing(listing_id)
    return MarketplaceListResult(success=True, message="Craft permanently deleted.", listing_id=listing_id)


# ══════════════════════════════════════════════════════════════════════════════
#  Admin API (/api/v1/web/admin/...)
#
#  The website's admin console, gated in two tiers that mirror cogs/perms.py:
#
#  - `get_owner`: the single BOT_OWNER_ID from .env — the same one person
#    cogs/perms.is_owner_user trusts. Guards everything bot-wide: user accounts
#    (balance, suspensions, deletion), DM-from-bot, DLL publishing, costs,
#    runtime gates and policy — because a role granted in one guild must never
#    carry authority over every guild the bot is in.
#  - `get_admin`: the owner, or a holder of a guild's mapped bot-admin role
#    (guild_config key "admin", set per guild via /admin setrole — guild
#    administrators are still NOT auto-admins, matching is_admin_user). Guards
#    the guild-scoped moderation surface (overview, listings, announcements,
#    channel locks), and every response and action is scoped to the guilds the
#    caller actually admins.
#
#  Both answer 404 (not 403) to anyone else, so the surface is invisible to a
#  probing outsider with a valid session. The website only *hides* tabs
#  client-side; these dependencies are the actual gate.
# ══════════════════════════════════════════════════════════════════════════════

def _is_owner_id(user_id) -> bool:
    """True only when this is the configured owner. OWNER_ID=0 (unset) matches
    nobody — an unconfigured bot has no admin, not an open one."""
    return bool(cfg.OWNER_ID) and str(user_id) == str(cfg.OWNER_ID)


async def get_owner(user: dict = Depends(get_user_token_only)) -> dict:
    if not _is_owner_id(user.get("user_id")):
        raise HTTPException(status_code=404, detail="Not found")
    _rate_limit(f"admin:{user['user_id']}", max_hits=240, window=60.0)
    user["is_owner"] = True
    user["admin_guild_ids"] = None  # None = every guild (owner is unscoped)
    return user


def _admin_role_guild_ids(user_id) -> list[str]:
    """The guilds in which this user holds the mapped bot-admin role.

    Resolved live against the Discord member cache rather than stored: a role
    revoked in Discord must revoke console access on the next request, not on
    the next login. An offline bot yields [] — no roster, no authority."""
    from data import guild_config
    if _bot_instance is None or not str(user_id).isdigit():
        return []
    out: list[str] = []
    for g in _bot_instance.guilds:
        role = guild_config.resolve_role(g, "admin")
        if role is None:
            continue
        member = g.get_member(int(user_id))
        if member is not None and member.get_role(role.id) is not None:
            out.append(str(g.id))
    return out


async def get_admin(user: dict = Depends(get_user_token_only)) -> dict:
    """The owner, or a mapped guild admin. Annotates the user dict with
    `is_owner` and `admin_guild_ids` (None for the owner = unscoped; a non-empty
    list for a guild admin). 404 for everyone else, same as get_owner."""
    if _is_owner_id(user.get("user_id")):
        user["is_owner"] = True
        user["admin_guild_ids"] = None
    else:
        gids = _admin_role_guild_ids(user.get("user_id"))
        if not gids:
            raise HTTPException(status_code=404, detail="Not found")
        user["is_owner"] = False
        user["admin_guild_ids"] = gids
    _rate_limit(f"admin:{user['user_id']}", max_hits=240, window=60.0)
    return user


def _admin_can_guild(user: dict, guild_id) -> bool:
    """May this console user act on this guild? The owner may on all of them."""
    scoped = user.get("admin_guild_ids")
    return scoped is None or str(guild_id) in scoped


def _require_bot():
    """The live discord.py Bot, or 503 — most admin actions act through Discord."""
    if _bot_instance is None:
        raise HTTPException(status_code=503, detail="The Discord bot is not connected yet. Try again in a moment.")
    return _bot_instance


def _admin_audit(user: dict, action: str, detail: str = ""):
    """One log line per admin action; the audit trail for the console. Guild
    admins are labelled distinctly — who held which authority matters when the
    trail is read a week later."""
    tag = "ADMIN" if user.get("is_owner", True) else "GUILD-ADMIN"
    log.warning("%s[%s]: %s %s", tag, user.get("username", user.get("user_id")), action, detail)


# ── Request bodies (admin-only; kept local rather than in api_models) ─────────

class AdminListingEdit(BaseModel):
    craft_name: Optional[str] = None
    price: Optional[int] = None
    seller_name: Optional[str] = None
    status: Optional[str] = None  # "active" | "delisted"

class AdminUserAdjust(BaseModel):
    balance_delta: Optional[int] = None
    balance_set: Optional[int] = None
    xp_set: Optional[int] = None

class AdminSuspend(BaseModel):
    hours: float
    reason: str = ""
    # DM the player. Default on: a suspension nobody was told about is
    # indistinguishable from the mod being broken, and that arrives as a bug
    # report rather than as an appeal.
    notify: bool = True

class AdminDirectMessage(BaseModel):
    user_id: str
    title: str = ""
    content: str

class AdminAnnounce(BaseModel):
    guild_id: str
    channel_id: Optional[str] = None
    role_id: Optional[str] = None
    title: str
    content: str
    # False → one embed in channel_id (mentioning role_id if given).
    # True  → open a private ticket per member of role_id carrying the message.
    open_tickets: bool = False

class AdminChannelLock(BaseModel):
    guild_id: str
    locked: bool
    reason: str = ""

class AdminControls(BaseModel):
    version_check_enabled: Optional[bool] = None
    device_binding_enabled: Optional[bool] = None
    # Cost guard. Budgets are settable at runtime because the failure mode that
    # matters is a *false* stop — wrong price constants freezing a bot that has
    # not actually overspent. Before this, the only way out was editing .env and
    # restarting, which is the worst moment to need a restart.
    cost_guard_enabled: Optional[bool] = None
    firebase_budget_usd: Optional[float] = None
    gemini_budget_usd: Optional[float] = None

class AdminPolicyBump(BaseModel):
    summary: str = ""
    privacy_url: Optional[str] = None
    terms_url: Optional[str] = None


@app.get("/api/v1/web/admin/whoami")
async def admin_whoami(user: dict = Depends(get_admin)):
    """200 for the owner and for mapped guild admins (404 for everyone else) —
    the website's cheap 'should I draw the Admin tab' probe, now also telling it
    which tier to draw."""
    return {
        "is_owner": user["is_owner"],
        "is_admin": True,
        "admin_guild_ids": user["admin_guild_ids"] or [],
        "user_id": user["user_id"],
        "username": user["username"],
    }


@app.get("/api/v1/web/admin/overview")
async def admin_overview(user: dict = Depends(get_admin)):
    """Dashboard numbers: community size, market size, gate + version state.
    A guild admin's guild list is cut to the guilds they admin; the counts stay
    global because they are read-only facts, not levers."""
    bot = _bot_instance
    guilds = []
    if bot is not None:
        for g in bot.guilds:
            if not _admin_can_guild(user, g.id):
                continue
            guilds.append({"id": str(g.id), "name": g.name,
                           "member_count": g.member_count or 0})
    listings = mkt.list_all()
    mv = mver.get_config()
    return {
        "users": len(store.get_all_users(0)),
        "listings_active": sum(1 for l in listings if l.get("status") == mkt.ACTIVE),
        "listings_delisted": sum(1 for l in listings if l.get("status") != mkt.ACTIVE),
        "guilds": guilds,
        "mod_version": {
            "latest_version": mv.get("latest_version"),
            "latest_hash": mv.get("latest_hash"),
            "has_dll": bool(mv.get("has_dll")),
            "updated_at": mv.get("updated_at"),
        },
        "policy_version": policy.get_version(),
        "suspensions_active": len(suspensions.list_active()),
        "version_check_enabled": cfg.KSP_VERSION_CHECK_ENABLED,
        "device_binding_enabled": cfg.KSP_DEVICE_BINDING_ENABLED,
    }


# ── Marketplace moderation ────────────────────────────────────────────────────

@app.get("/api/v1/web/admin/listings")
async def admin_listings(q: str = "", user: dict = Depends(get_admin)):
    """Every listing, any seller, any status — newest first. A guild admin sees
    only listings that originated from a guild they admin (the market is one
    shared grid, but moderation authority follows the community it came from)."""
    items = mkt.list_all()
    if not user["is_owner"]:
        items = [l for l in items if _admin_can_guild(user, l.get("guild_id", ""))]
    if q.strip():
        needle = q.strip().lower()
        items = [l for l in items
                 if needle in (l.get("craft_name", "") or "").lower()
                 or needle in (l.get("seller_name", "") or "").lower()
                 or needle in (l.get("listing_id", "") or "").lower()
                 or needle == str(l.get("seller_id", ""))]
    items.sort(key=lambda l: l.get("created_at") or "", reverse=True)
    # report_count is merged in here rather than added to MarketplaceListing: the
    # same model serves the public grid, and "how many people complained about this"
    # is a moderation fact, not a shopping one.
    return {"listings": [{**_listing_to_model(l, include_download=True).model_dump(),
                          "report_count": int(l.get("report_count", 0) or 0)}
                         for l in items]}


@app.patch("/api/v1/web/admin/listings/{listing_id}")
async def admin_edit_listing(listing_id: str, req: AdminListingEdit,
                             user: dict = Depends(get_admin)):
    """Edit any listing's name / price / status regardless of who owns it.
    Guild admins can only touch listings from their own guilds — out-of-scope
    ones 404 exactly like nonexistent ones, so scope can't be probed."""
    listing = mkt.get_listing(0, listing_id)
    if not listing or not _admin_can_guild(user, listing.get("guild_id", "")):
        raise HTTPException(status_code=404, detail="Listing not found")

    fields: dict = {}
    if req.craft_name is not None and req.craft_name.strip():
        fields["craft_name"] = req.craft_name.strip()[:100]
    if req.price is not None:
        if req.price < 0:
            raise HTTPException(status_code=422, detail="Price cannot be negative.")
        fields["price"] = int(req.price)
    if req.seller_name is not None and req.seller_name.strip():
        fields["seller_name"] = req.seller_name.strip()[:100]
    if req.status is not None:
        if req.status not in (mkt.ACTIVE, mkt.DELISTED):
            raise HTTPException(status_code=422, detail="Status must be 'active' or 'delisted'.")
        fields["status"] = req.status
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to change.")

    mkt.update_listing(0, listing_id, **fields)
    listing.update(fields)
    _admin_audit(user, "edit-listing", f"{listing_id} {fields}")
    return {"success": True, "listing": _listing_to_model(listing, include_download=True).model_dump()}


@app.delete("/api/v1/web/admin/listings/{listing_id}")
async def admin_delete_listing(listing_id: str, user: dict = Depends(get_admin)):
    """Permanently delete any listing: Storage files and the document. Same
    guild scoping as the edit — out-of-scope listings 404."""
    listing = mkt.get_listing(0, listing_id)
    if not listing or not _admin_can_guild(user, listing.get("guild_id", "")):
        raise HTTPException(status_code=404, detail="Listing not found")
    mkt.delete_listing(listing_id)
    _admin_audit(user, "delete-listing", f"{listing_id} ({listing.get('craft_name')})")
    return {"success": True}


# ── User accounts ─────────────────────────────────────────────────────────────

def _admin_user_row(uid: str, u: dict, suspension: dict | None = None) -> dict:
    """One row of the console's user list.

    `suspension` is passed in rather than looked up: the list renders up to 200
    rows and a per-row Firestore read would make the page cost 200 of them, so
    admin_users resolves them all with one `list_active()` query. A single-user
    response (adjust, suspend) passes its own.
    """
    name = u.get("username", "")
    if not name and _bot_instance is not None:
        du = _bot_instance.get_user(int(uid)) if uid.isdigit() else None
        name = str(du) if du else ""
    return {
        "user_id": uid,
        "username": name,
        "xp": u.get("xp", 0),
        "level": u.get("level", 0),
        "balance": u.get("balance", 0),
        "messages": u.get("messages", 0),
        "rescues": u.get("rescues", 0),
        "joined_at": u.get("joined_at", ""),
        "suspension": suspensions.summary(suspension),
    }


@app.get("/api/v1/web/admin/users")
async def admin_users(q: str = "", limit: int = 50, user: dict = Depends(get_owner)):
    """Search the global wallet store by id or name; no query → richest first.

    Suspended accounts are sorted to the top regardless of the query: the list is
    where a suspension is noticed, lifted early or found to have expired, and one
    buried at rank 140 by balance is one nobody reviews."""
    needle = q.strip().lower()
    active = {str(r.get("user_id")): r for r in suspensions.list_active()}
    rows = []
    for uid, u in store.get_all_users(0).items():
        row = _admin_user_row(uid, u, active.get(str(uid)))
        if needle and needle not in uid and needle not in row["username"].lower():
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["suspension"] is not None, r["balance"]), reverse=True)
    return {"users": rows[:max(1, min(limit, 200))], "total": len(rows)}


@app.post("/api/v1/web/admin/users/{user_id}/adjust")
async def admin_user_adjust(user_id: str, req: AdminUserAdjust,
                            user: dict = Depends(get_owner)):
    """Set or shift a user's balance / XP. balance_set wins over balance_delta."""
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail="user_id must be a Discord id.")
    # Only touch users that exist — get_user() would mint a default record for a
    # typo'd id and the auto-save would then persist the ghost.
    if user_id not in store.get_all_users(0):
        raise HTTPException(status_code=404, detail="No such user in the store.")
    uid = int(user_id)
    u = store.get_user(0, uid)

    if req.balance_set is not None:
        await store.add_balance(0, uid, int(req.balance_set) - u.get("balance", 0))
    elif req.balance_delta:
        await store.add_balance(0, uid, int(req.balance_delta))
    if req.xp_set is not None:
        await store.set_xp(0, uid, max(0, int(req.xp_set)))

    _admin_audit(user, "adjust-user",
                 f"{user_id} balance_set={req.balance_set} balance_delta={req.balance_delta} xp_set={req.xp_set}")
    return {"success": True, "user": _admin_user_row(user_id, store.get_user(0, uid),
                                                     suspensions.get_active(user_id))}


@app.post("/api/v1/web/admin/users/{user_id}/logout_all")
async def admin_user_logout_all(user_id: str, user: dict = Depends(get_owner)):
    """Revoke every session token the user holds (KSP clients + website)."""
    version = logout_all_devices(user_id)
    _admin_audit(user, "logout-all", user_id)
    return {"success": True, "token_version": version}


# ── Suspensions ───────────────────────────────────────────────────────────────
#
# A suspension blocks the API surface — the KSP client and the website — and
# nothing else. It is not a Discord ban (that is /mod ban, and it acts on guild
# membership) and it is not a wipe: balance, XP, contracts and listings are all
# untouched and waiting when it expires. Nor does it need to be undone by hand;
# the expiry is the whole mechanism (see data/suspensions.py).

async def _dm_suspension(user_id: str, title: str, body: str, colour) -> bool:
    """Tell the player what happened, best effort.

    Best effort is not laziness: a player with DMs closed still has to be
    suspendable, so a failed DM is reported back to the console rather than
    aborting the suspension — otherwise "this user blocks the bot" would read as
    "this user cannot be moderated"."""
    import discord
    if _bot_instance is None:
        return False
    try:
        target = (_bot_instance.get_user(int(user_id))
                  or await _bot_instance.fetch_user(int(user_id)))
        embed = discord.Embed(title=title, description=body, color=colour)
        embed.set_footer(text="Boundless Missions")
        await target.send(embed=embed)
        return True
    except Exception as exc:
        log.warning("Could not DM %s about their suspension: %s", user_id, exc)
        return False


def _humanise_hours(hours: float) -> str:
    if hours < 24:
        return f"{hours:g} hour" + ("" if hours == 1 else "s")
    days = hours / 24.0
    return f"{days:g} day" + ("" if days == 1 else "s")


@app.post("/api/v1/web/admin/users/{user_id}/suspend")
async def admin_user_suspend(user_id: str, req: AdminSuspend,
                             user: dict = Depends(get_owner)):
    """Suspend an account from the mod and website for a fixed number of hours.

    Sessions are deliberately *not* revoked. A revoked token drops the KSP client
    to its link screen, where the only thing it can say is "link again" — which is
    a lie, since linking would work and nothing else would. Leaving the token
    valid is what lets every request come back 403 `suspended` carrying the reason
    and the expiry, so the client can draw a notice that explains itself."""
    import discord
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail="user_id must be a Discord id.")
    # The owner's own account gates this console: suspending it would 403 the very
    # requests needed to undo it, leaving the DB as the only way back in.
    if _is_owner_id(user_id):
        raise HTTPException(status_code=422, detail="Refusing to suspend the owner account.")
    if req.hours < suspensions.MIN_HOURS or req.hours > suspensions.MAX_HOURS:
        raise HTTPException(
            status_code=422,
            detail=(f"Duration must be between {suspensions.MIN_HOURS} hour and "
                    f"{suspensions.MAX_HOURS // 24} days. Anything longer is a ban, "
                    f"which is a Discord action."))
    reason = (req.reason or "").strip()
    if not reason:
        # Required, because the reason is what the player is shown at the gate and
        # what an appeal is about. A suspension with no stated cause is one the
        # mod team cannot defend a week later either.
        raise HTTPException(status_code=422, detail="A reason is required — the player is shown it.")

    rec = suspensions.suspend(user_id, req.hours, reason, user.get("username", "owner"))
    _admin_audit(user, "suspend", f"{user_id} {req.hours}h: {reason}")

    notified = False
    if req.notify:
        notified = await _dm_suspension(
            user_id,
            "⏸️ Boundless Missions access suspended",
            (f"Your access to the KSP mod and the Boundless Missions website is "
             f"suspended for **{_humanise_hours(rec['hours'])}**.\n\n"
             f"**Reason:** {reason}\n\n"
             f"Access returns on its own at <t:{int(rec['until'])}:F> "
             f"(<t:{int(rec['until'])}:R>). Nothing has been deleted — your balance, "
             f"XP, contracts and listings are all still there. Your Discord "
             f"membership is unaffected.\n\n"
             f"If you think this is a mistake, open a ticket in the server."),
            discord.Color.orange())

    return {"success": True, "suspension": suspensions.summary(rec), "notified": notified}


@app.delete("/api/v1/web/admin/users/{user_id}/suspend")
async def admin_user_unsuspend(user_id: str, notify: bool = True,
                               user: dict = Depends(get_owner)):
    """Lift a suspension early. A no-op (success, `lifted: false`) if none was
    running — an expiry that beat the admin to it is not an error."""
    import discord
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail="user_id must be a Discord id.")
    lifted = suspensions.lift(user_id, user.get("username", "owner"))
    _admin_audit(user, "unsuspend", f"{user_id} lifted={lifted}")

    notified = False
    if lifted and notify:
        notified = await _dm_suspension(
            user_id,
            "▶️ Boundless Missions access restored",
            ("Your suspension has been lifted early. The KSP mod and the website "
             "work again — restart the mod, or press **Check again** on the notice "
             "it is showing you."),
            discord.Color.green())

    return {"success": True, "lifted": lifted, "notified": notified}


@app.delete("/api/v1/web/admin/users/{user_id}")
async def admin_user_delete(user_id: str, user: dict = Depends(get_owner)):
    """Erase a user's global record (XP, balance, levels) and revoke all their
    sessions. The owner cannot delete their own account from here — that would
    orphan the console mid-request."""
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail="user_id must be a Discord id.")
    if _is_owner_id(user_id):
        raise HTTPException(status_code=422, detail="Refusing to delete the owner account.")
    existed = await store.delete_user(0, int(user_id))
    try:
        logout_all_devices(user_id)
    except Exception as exc:
        log.warning("admin delete-user: could not revoke sessions for %s: %s", user_id, exc)
    _admin_audit(user, "delete-user", f"{user_id} existed={existed}")
    return {"success": True, "existed": existed}


# ── Messaging ─────────────────────────────────────────────────────────────────

@app.post("/api/v1/web/admin/message")
async def admin_direct_message(req: AdminDirectMessage, user: dict = Depends(get_owner)):
    """DM a player from the bot account (an official-channel message)."""
    import discord
    bot = _require_bot()
    if not req.content.strip():
        raise HTTPException(status_code=422, detail="Message content is empty.")
    if not req.user_id.isdigit():
        raise HTTPException(status_code=422, detail="user_id must be a Discord id.")
    try:
        target = bot.get_user(int(req.user_id)) or await bot.fetch_user(int(req.user_id))
        embed = discord.Embed(
            title=req.title.strip() or "📣 Message from the Boundless Missions team",
            description=req.content.strip()[:3900],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Sent by the community administrator")
        await target.send(embed=embed)
    except discord.NotFound:
        raise HTTPException(status_code=404, detail="No Discord user with that id.")
    except discord.Forbidden:
        raise HTTPException(status_code=409, detail="That user does not accept DMs from the bot.")
    _admin_audit(user, "dm", req.user_id)
    return {"success": True}


async def _announce_via_tickets(bot, guild, role, title: str, content: str, admin_name: str):
    """Open one private ticket per role member carrying the announcement. Runs as
    a background task: channel creation is heavily rate-limited by Discord, so a
    big role takes minutes — the HTTP request must not wait for it."""
    import discord
    from cogs.tickets import create_ticket
    opened = 0
    for member in list(role.members):
        if member.bot:
            continue
        try:
            ch = await create_ticket(
                bot, guild,
                opener_id=member.id,
                kind="announcement",
                title=title,
                description=content,
                color=discord.Color.gold(),
                ping_mods=False,
            )
            if ch is not None:
                opened += 1
        except Exception as exc:
            log.warning("announce ticket for %s failed: %s", member.id, exc)
        await asyncio.sleep(1.5)  # stay far under the channel-create rate limit
    log.warning("ADMIN[%s]: announce-tickets done — %d/%d tickets opened for role %s",
                admin_name, opened, len(role.members), role.name)


@app.post("/api/v1/web/admin/announce")
async def admin_announce(req: AdminAnnounce, user: dict = Depends(get_admin)):
    """Announce to a channel (optionally pinging a role), or — open_tickets —
    open a private ticket panel per member of the role with the message. Guild
    admins can only announce into guilds they admin; anything else reads as the
    bot not being there."""
    import discord
    bot = _require_bot()
    guild = bot.get_guild(int(req.guild_id)) if req.guild_id.isdigit() else None
    if guild is None or not _admin_can_guild(user, guild.id):
        raise HTTPException(status_code=404, detail="The bot is not in that guild.")
    if not req.content.strip():
        raise HTTPException(status_code=422, detail="Announcement content is empty.")

    role = None
    if req.role_id and req.role_id.isdigit():
        role = guild.get_role(int(req.role_id))
        if role is None:
            raise HTTPException(status_code=404, detail="No such role in that guild.")

    if req.open_tickets:
        if role is None:
            raise HTTPException(status_code=422, detail="Opening tickets needs a role.")
        members = [m for m in role.members if not m.bot]
        if not members:
            raise HTTPException(status_code=422, detail="That role has no (non-bot) members the bot can see.")
        if len(members) > 200:
            raise HTTPException(status_code=422,
                                detail=f"Refusing to open {len(members)} tickets at once (cap is 200).")
        asyncio.create_task(_announce_via_tickets(
            bot, guild, role, req.title.strip() or "📣 Announcement",
            req.content.strip(), user.get("username", "owner")))
        _admin_audit(user, "announce-tickets", f"guild={req.guild_id} role={req.role_id} members={len(members)}")
        return {"success": True, "mode": "tickets", "targets": len(members)}

    if not req.channel_id or not req.channel_id.isdigit():
        raise HTTPException(status_code=422, detail="A channel is required for a channel announcement.")
    channel = guild.get_channel(int(req.channel_id))
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(status_code=404, detail="No such text channel in that guild.")

    embed = discord.Embed(
        title=req.title.strip() or "📣 Announcement",
        description=req.content.strip()[:3900],
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Official announcement")
    try:
        await channel.send(content=role.mention if role else None, embed=embed)
    except discord.Forbidden:
        raise HTTPException(status_code=409, detail="The bot cannot post in that channel.")
    _admin_audit(user, "announce", f"guild={req.guild_id} channel={req.channel_id} role={req.role_id}")
    return {"success": True, "mode": "channel"}


# ── Guild structure (pickers) + channel locks ────────────────────────────────

@app.get("/api/v1/web/admin/guilds")
async def admin_guilds(user: dict = Depends(get_admin)):
    """Guilds with their text channels and roles — feeds the console's pickers.
    A channel is 'locked' when @everyone's send_messages overwrite is False.
    Guild admins get only the guilds they admin."""
    import discord
    bot = _require_bot()
    out = []
    for g in bot.guilds:
        if not _admin_can_guild(user, g.id):
            continue
        channels = []
        for ch in g.text_channels:
            ow = ch.overwrites_for(g.default_role)
            channels.append({
                "id": str(ch.id), "name": ch.name,
                "category": ch.category.name if ch.category else None,
                "locked": ow.send_messages is False,
            })
        roles = [{"id": str(r.id), "name": r.name, "members": len(r.members)}
                 for r in g.roles if not r.is_default() and not r.managed]
        roles.reverse()  # highest role first, matches Discord's own ordering
        out.append({"id": str(g.id), "name": g.name,
                    "member_count": g.member_count or 0,
                    "channels": channels, "roles": roles})
    return {"guilds": out}


@app.post("/api/v1/web/admin/channels/{channel_id}/lock")
async def admin_channel_lock(channel_id: str, req: AdminChannelLock,
                             user: dict = Depends(get_admin)):
    """Lock (or unlock) a text channel by flipping @everyone's send permission.
    Unlock resets the overwrite to neutral rather than forcing True, so category
    and role permissions come back exactly as they were. Guild admins can only
    lock channels in guilds they admin."""
    import discord
    bot = _require_bot()
    guild = bot.get_guild(int(req.guild_id)) if req.guild_id.isdigit() else None
    if guild is None or not _admin_can_guild(user, guild.id):
        raise HTTPException(status_code=404, detail="The bot is not in that guild.")
    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(status_code=404, detail="No such text channel in that guild.")

    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = False if req.locked else None
    overwrite.create_public_threads = False if req.locked else None
    overwrite.create_private_threads = False if req.locked else None
    if overwrite.is_empty():
        overwrite = None
    try:
        await channel.set_permissions(
            guild.default_role, overwrite=overwrite,
            reason=req.reason or f"Remote {'lock' if req.locked else 'unlock'} from the admin console")
        if req.locked:
            embed = discord.Embed(
                description="🔒 This channel has been locked by an administrator.",
                color=discord.Color.red())
            await channel.send(embed=embed)
    except discord.Forbidden:
        raise HTTPException(status_code=409, detail="The bot lacks permission to edit that channel.")
    _admin_audit(user, "channel-lock" if req.locked else "channel-unlock",
                 f"guild={req.guild_id} channel={channel_id} reason={req.reason!r}")
    return {"success": True, "locked": req.locked}


# ── Mod version / DLL publishing ─────────────────────────────────────────────

@app.get("/api/v1/web/admin/modversion")
async def admin_modversion(user: dict = Depends(get_owner)):
    return {"config": mver.get_config()}


@app.post("/api/v1/web/admin/modversion/publish")
async def admin_publish_version(
    version: str = Form(...),
    download_url: str = Form(""),
    set_latest: bool = Form(True),
    sha256: str = Form(""),
    dll: UploadFile | None = File(None),
    user: dict = Depends(get_owner),
):
    """Publish a mod version from the website — the web twin of /publishversion.
    Upload the DLL itself (preferred: enables challenge-response attestation and
    auto-computes the hash) or provide a bare sha256."""
    if not version.strip():
        raise HTTPException(status_code=422, detail="A version label is required.")
    dll_bytes = None
    digest = sha256.strip().lower()
    if dll is not None:
        dll_bytes = await dll.read()
        if not dll_bytes:
            raise HTTPException(status_code=422, detail="The uploaded DLL is empty.")
        if len(dll_bytes) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="That file is too large to be GeneKerman.dll.")
        digest = hashlib.sha256(dll_bytes).hexdigest()
    if not digest:
        raise HTTPException(status_code=422, detail="Provide either a DLL upload or a sha256 hash.")

    record = await asyncio.to_thread(
        mver.publish_version, version, digest, download_url, set_latest,
        f"{user['username']} (web console)", dll_bytes)
    if set_latest or record.get("latest_version") == version.strip():
        broadcast_version_update()
    _admin_audit(user, "publish-version",
                 f"{version} sha256={digest[:12]} latest={set_latest} dll={'yes' if dll_bytes else 'no'}")
    return {"success": True, "config": record}


# ── Master controls ──────────────────────────────────────────────────────────

@app.get("/api/v1/web/admin/costs")
async def admin_costs(user: dict = Depends(get_owner)):
    """This month's spend: what we estimate, what Google measured, and the gap.

    Everything here is read from local state — no Firestore, no Storage. The
    console page that shows the bill must not itself be a line on it, and more
    to the point it has to keep working when the guard has frozen Firebase.
    """
    snap = cost_guard.snapshot()
    snap["history"] = cost_guard.history()
    return snap


@app.post("/api/v1/web/admin/costs/refresh")
async def admin_costs_refresh(user: dict = Depends(get_owner)):
    """Poll Cloud Monitoring now, clearing a stuck permission failure first.

    A 403 is remembered so a missing IAM grant doesn't re-fail every 5 minutes
    for the rest of the month. That memory has to be clearable from somewhere
    once the role is actually granted, or the only fix is a restart."""
    from data import gcp_billing, gcp_metrics

    gcp_metrics.client.reset_disabled()
    usage = await gcp_metrics.fetch_usage()
    if usage.ok:
        cost_guard.ingest_usage(usage)
    else:
        cost_guard.note_metrics_error(usage.error)

    # Same treatment for tier 2. Its two normal-but-unhappy states — export not
    # loaded yet, IAM not granted yet — are both things this button exists to
    # retry after fixing, and resetting clears the remembered table name too so a
    # newly created export table is discovered.
    gcp_billing.client.reset_disabled()
    billed = await gcp_billing.fetch_billing()
    if billed.ok:
        cost_guard.ingest_billing(billed)
    else:
        cost_guard.note_billing_error(billed.error)

    _admin_audit(user, "costs-refresh",
                 f"metrics={'ok' if usage.ok else usage.error}; "
                 f"billing={'ok' if billed.ok else billed.error}")
    snap = cost_guard.snapshot()
    snap["history"] = cost_guard.history()
    return snap


@app.get("/api/v1/web/admin/controls")
async def admin_controls(user: dict = Depends(get_owner)):
    return {
        "version_check_enabled": cfg.KSP_VERSION_CHECK_ENABLED,
        "device_binding_enabled": cfg.KSP_DEVICE_BINDING_ENABLED,
        "policy": policy.get_config(),
        "policy_version": policy.get_version(),
        "cost_guard_enabled": settings.COST_GUARD_ENABLED,
        "firebase_budget_usd": settings.FIREBASE_MONTHLY_BUDGET_USD,
        "gemini_budget_usd": settings.GEMINI_MONTHLY_BUDGET_USD,
    }


@app.post("/api/v1/web/admin/controls")
async def admin_set_controls(req: AdminControls, user: dict = Depends(get_owner)):
    """Flip the runtime gates. These change the running process only — .env is
    the boot-time source of truth, so a restart reverts them (said in the reply,
    so the console can show it)."""
    if req.version_check_enabled is not None:
        cfg.KSP_VERSION_CHECK_ENABLED = bool(req.version_check_enabled)
        broadcast_version_update()
    if req.device_binding_enabled is not None:
        cfg.KSP_DEVICE_BINDING_ENABLED = bool(req.device_binding_enabled)
    # cost_guard reads these out of `settings` on every check rather than caching
    # them, so assigning here takes effect on the next operation — including
    # lifting a freeze that is already in force.
    if req.cost_guard_enabled is not None:
        settings.COST_GUARD_ENABLED = bool(req.cost_guard_enabled)
    if req.firebase_budget_usd is not None:
        settings.FIREBASE_MONTHLY_BUDGET_USD = max(0.0, float(req.firebase_budget_usd))
    if req.gemini_budget_usd is not None:
        settings.GEMINI_MONTHLY_BUDGET_USD = max(0.0, float(req.gemini_budget_usd))
    _admin_audit(user, "controls",
                 f"version_check={req.version_check_enabled} device_binding={req.device_binding_enabled} "
                 f"cost_guard={req.cost_guard_enabled} fb_budget={req.firebase_budget_usd} "
                 f"gemini_budget={req.gemini_budget_usd}")
    return {
        "success": True,
        "persisted": False,
        "version_check_enabled": cfg.KSP_VERSION_CHECK_ENABLED,
        "device_binding_enabled": cfg.KSP_DEVICE_BINDING_ENABLED,
        "cost_guard_enabled": settings.COST_GUARD_ENABLED,
        "firebase_budget_usd": settings.FIREBASE_MONTHLY_BUDGET_USD,
        "gemini_budget_usd": settings.GEMINI_MONTHLY_BUDGET_USD,
        "level": cost_guard.snapshot()["level"],
    }


@app.post("/api/v1/web/admin/policy/bump")
async def admin_policy_bump(req: AdminPolicyBump, user: dict = Depends(get_owner)):
    """Raise the policy version by one: every KSP client that accepted an older
    version re-raises its consent gate and stops transmitting until re-accepted."""
    new_version = policy.get_version() + 1
    doc = await asyncio.to_thread(
        policy.set_version, new_version, f"{user['username']} (web console)",
        req.summary or None, req.privacy_url, req.terms_url)
    broadcast_policy_update()
    _admin_audit(user, "policy-bump", f"→ v{new_version}")
    return {"success": True, "policy": doc}


# ── Checkpoint Hero Shots ─────────────────────────────────────────────────────

# Human-readable titles per checkpoint kind for the Discord post.
_CHECKPOINT_TITLES = {
    "rendezvous": "🤝 Rendezvous",
    "flyby": "🛰️ Flyby",
    "asteroid": "☄️ Asteroid encounter",
    "comet": "☄️ Comet encounter",
}


@app.post("/api/v1/checkpoint-photo", response_model=SubmissionResult)
async def checkpoint_photo(
    photo: UploadFile = File(...),
    kind: str = Form("checkpoint"),
    vessel_name: str = Form(""),
    body: str = Form(""),
    target_name: str = Form(""),
    caption: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """Receive a milestone hero shot from the KSP mod and post it to the
    checkpoint-photos Discord channel.

    The image is sent straight to Discord as an attachment (no Firebase Storage)
    since these are ephemeral community posts, not durable submission records.
    """
    if not settings.CHECKPOINT_PHOTOS_ENABLED:
        return SubmissionResult(success=False, message="Checkpoint photos are disabled on this server.")

    if not guild_config.get_channel_id(int(user["guild_id"]), "checkpoint_photos"):
        return SubmissionResult(success=False, message="Checkpoint photos are not enabled on this server.")

    if _bot_instance is None:
        return SubmissionResult(success=False, message="Bot is not ready.")

    channel = guild_config.resolve_channel(_bot_instance, int(user["guild_id"]), "checkpoint_photos")
    if channel is None:
        return SubmissionResult(success=False, message="The checkpoint photo channel is unavailable.")

    data = await _read_upload(photo)
    if not data:
        return SubmissionResult(success=False, message="Empty image.")

    import discord

    uid = int(user["user_id"])
    username = user.get("username") or "Kerbonaut"
    title = _CHECKPOINT_TITLES.get((kind or "").lower(), "📸 Mission milestone")

    lines = []
    if vessel_name:
        lines.append(f"**Vessel:** {vessel_name}")
    if body:
        lines.append(f"**Location:** {body}")
    if target_name:
        lines.append(f"**Subject:** {target_name}")
    if caption:
        lines.append(caption)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else None,
        color=0x2ECC71,
        timestamp=datetime.now(timezone.utc),
    )

    # Attribute the shot to the uploader, with their avatar when resolvable.
    author_icon = None
    discord_user = _bot_instance.get_user(uid)
    if discord_user is None:
        try:
            discord_user = await _bot_instance.fetch_user(uid)
        except Exception:
            discord_user = None
    if discord_user is not None:
        author_icon = discord_user.display_avatar.url
    embed.set_author(name=username, icon_url=author_icon)

    filename = "checkpoint.png"
    embed.set_image(url=f"attachment://{filename}")

    try:
        file = discord.File(io.BytesIO(data), filename=filename)
        await channel.send(embed=embed, file=file)
    except Exception as exc:
        log.error("Failed to post checkpoint photo for user %s: %s", uid, exc)
        return SubmissionResult(success=False, message="Failed to post the photo.")

    log.info("KSP: %s posted a %s checkpoint photo (vessel '%s')", username, kind, vessel_name)
    return SubmissionResult(success=True, message="Photo shared!")


# At most one AI review + reward per user per minute. Extra captures inside the
# window are still shared to the channel, just not analysed or rewarded.
_ACHIEVEMENT_REVIEW_COOLDOWN = 60.0
_achievement_last_review: dict[int, float] = {}


async def _post_achievement_capture(
    data: bytes, gid: int, uid: int, username: str, vessel_name: str, body: str,
    result: dict | None = None, qualifies: bool = False, is_new: bool = False,
    title_desc: str | None = None,
) -> None:
    """Best-effort post of a capture to the community channel. Never raises — a
    posting failure must not block the role/reward flow. Logs WHY it skipped so a
    misconfigured channel is diagnosable instead of silently dropping the image."""
    if _bot_instance is None:
        log.warning("achievement photo: bot not ready, cannot post for %s", uid)
        return
    channel = guild_config.resolve_channel(_bot_instance, gid, "checkpoint_photos")
    if channel is None:
        log.warning(
            "achievement photo: no resolvable checkpoint_photos channel for guild %s "
            "(configured id=%s) — image NOT posted",
            gid, guild_config.get_channel_id(gid, "checkpoint_photos"),
        )
        return

    import discord

    celestial = situation = None
    desc_text = ""
    if result:
        loc = result.get("location", {}) or {}
        celestial = loc.get("celestial_body")
        situation = loc.get("situation")
        desc_text = (result.get("description") or "").strip()

    lines = []
    if is_new:
        lines.append("🆕 **New title unlocked!**")
    if vessel_name:
        lines.append(f"**Vessel:** {vessel_name}")
    loc_bits = " · ".join(x for x in [celestial or body or None, situation] if x)
    if loc_bits:
        lines.append(f"**Location:** {loc_bits}")
    if desc_text:
        lines.append(desc_text[:300])

    embed = discord.Embed(
        title=f"🏅 {title_desc}" if qualifies else "📸 KSP Capture",
        description="\n".join(lines) if lines else None,
        color=0xF1C40F if qualifies else 0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )
    discord_user = _bot_instance.get_user(uid)
    if discord_user is None:
        try:
            discord_user = await _bot_instance.fetch_user(uid)
        except Exception:
            discord_user = None
    author_icon = discord_user.display_avatar.url if discord_user else None
    embed.set_author(name=username or "Kerbonaut", icon_url=author_icon)
    embed.set_image(url="attachment://achievement.png")
    try:
        file = discord.File(io.BytesIO(data), filename="achievement.png")
        await channel.send(embed=embed, file=file)
    except Exception as exc:
        log.warning("Could not post achievement shot for %s: %s", uid, exc)


@app.post("/api/v1/achievement-photo", response_model=SubmissionResult)
async def achievement_photo(
    photo: UploadFile = File(...),
    vessel_name: str = Form(""),
    body: str = Form(""),
    vessel_id: str = Form(""),
    situation: str = Form(""),
    review: bool = Form(True),
    user: dict = Depends(get_current_user),
):
    """Receive a player-composed achievement hero shot from the KSP mod, verify it
    with the Gemini analyst, award the matching KSP title role if it qualifies, and
    grant XP/KCoins like `/analyze`.

    Two paths:
      * Full review (`review=true`, not rate-limited): Gemini analysis → role +
        XP/KCoins, embed posted to the channel.
      * Post-only (`review=false`, or rate-limited to 1/min): the mod flags a repeat
        of an already-rewarded vessel+position, so the shot is just shared to the
        channel — no analysis, no reward.

    Role/title ROLES are granted only through this verified capture path; the Discord
    `/analyze` command still grants XP/KCoins but no longer awards roles.
    """
    gid = int(user["guild_id"])
    uid = int(user["user_id"])
    username = user.get("username") or "Kerbonaut"
    _note_user_action(gid, uid, username, "achievement", *settings.FLOOD_ACHIEVEMENT)

    data = await _read_upload(photo)
    if not data:
        return SubmissionResult(success=False, message="Empty image.")

    # Rate-limit AI reviews: collapse a burst (multiple captures within a minute)
    # into channel-only shares so one mission can't be farmed for repeat rewards.
    now = time.monotonic()
    last = _achievement_last_review.get(uid, 0.0)
    review_requested = bool(review)
    if review_requested and (now - last) < _ACHIEVEMENT_REVIEW_COOLDOWN:
        review_requested = False

    # ── Post-only path (mod-flagged repeat, or rate-limited) ─────────────────
    if not review_requested:
        await _post_achievement_capture(data, gid, uid, username, vessel_name, body)
        log.info("KSP: %s achievement photo shared (no review: repeat/rate-limited)", uid)
        return SubmissionResult(success=True, message="📸 Shared to the channel.")

    # ── Full review path ─────────────────────────────────────────────────────
    from cogs.screenshots import _run_gemini, _grant_rewards, active_client

    if active_client() is None:
        return SubmissionResult(
            success=False,
            message="Achievement checking is unavailable right now. Try again later.",
        )

    try:
        result = await _run_gemini([data], gid)
    except Exception as exc:
        log.error("Achievement photo analysis failed for %s: %s", uid, exc, exc_info=True)
        return SubmissionResult(success=False, message="Couldn't analyze the shot. Try again.")

    if not result.get("approved", False):
        # Not a KSP shot — don't pour arbitrary images into the community channel.
        return SubmissionResult(
            success=True,
            message="That doesn't look like a KSP shot. Frame your craft and try again.",
        )

    # Count this as a real review for the rate-limiter only once it passed Gemini.
    _achievement_last_review[uid] = now

    # Does it map to a title role?
    try:
        ksp_level = int(result.get("ksp_level", 0))
    except (ValueError, TypeError):
        ksp_level = 0
    qualifies = ksp_level > 0 and ksp_level in settings.LEVEL_ROLES
    title_desc = settings.LEVEL_ROLES[ksp_level][2] if qualifies else None

    # Grant the role on a FIRST-TIME unlock only (idempotent for repeats).
    is_new = False
    if qualifies and _bot_instance is not None:
        from cogs.roles import check_and_award_level
        is_new = await check_and_award_level(_bot_instance, gid, uid, ksp_level)

    # Grant XP + KCoins from the difficulty rating, same as the /analyze command.
    try:
        rating = int(result.get("difficulty_rating", 0) or 0)
    except (ValueError, TypeError):
        rating = 0
    xp_r = coin_r = 0
    if rating > 0:
        xp_r, coin_r = await _grant_rewards(gid, uid, rating)
    reward_suffix = f" (+{xp_r} XP, +{coin_r} KC)" if (xp_r or coin_r) else ""

    # In-game popup message.
    if qualifies and is_new:
        message = f"🏅 {title_desc} unlocked! Check your Discord DMs to equip the title.{reward_suffix}"
    elif qualifies:
        message = f"✅ Verified: {title_desc}. You already hold this title, so the shot was shared!{reward_suffix}"
    else:
        message = f"📸 Shot shared! It doesn't match a title role.{reward_suffix}"

    await _post_achievement_capture(
        data, gid, uid, username, vessel_name, body,
        result=result, qualifies=qualifies, is_new=is_new, title_desc=title_desc,
    )

    log.info(
        "KSP: %s achievement photo → level %d (%s) qualifies=%s new=%s reward=+%dXP/+%dKC",
        uid, ksp_level, title_desc or "—", qualifies, is_new, xp_r, coin_r,
    )
    return SubmissionResult(success=True, message=message)


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Bot Instance & User ID Helper ────────────────────────────────────────────

_bot_user_id: int = 0
_bot_instance = None  # discord.ext.commands.Bot reference

def set_bot_user_id(uid: int):
    global _bot_user_id
    _bot_user_id = uid

def set_bot_instance(bot):
    global _bot_instance
    _bot_instance = bot

def _get_bot_user_id() -> int:
    return _bot_user_id


async def _deliver_craft_to_corp(gid: int, builder_id: int, contract_id: str):
    """Post a completed bot-contract craft to the builder's corporation channel.

    Bot-contract deliverables go to the player's corp (as a downloadable .craft
    blueprint), not into anyone's save — so the corp shares the work and there's
    no live-vessel re-import. No-op if the player has no corp or the contract has
    no craft file (e.g. a flight-only mission).
    """
    if _bot_instance is None:
        return
    try:
        import discord
        from cogs.corps import find_user_corp

        c = cdb.get_contract(gid, contract_id)
        if not c:
            return
        craft_files = [f for f in c.get("submitted_files", []) if f.get("filename", "").endswith(".craft")]
        if not craft_files:
            return

        corp = find_user_corp(gid, builder_id)
        if not corp or not corp.get("channel_id"):
            log.info("Corp delivery skipped for %s: builder %d has no corp channel", contract_id, builder_id)
            return

        # The corp may live in another server (one corp per user globally), so
        # resolve its channel by id at the bot level rather than within `gid`.
        channel = _bot_instance.get_channel(int(corp["channel_id"]))
        if channel is None:
            return

        cf = craft_files[0]
        try:
            data = await cdb.download_url(cf["url"])
        except Exception as exc:
            log.error("Corp delivery: could not download craft for %s: %s", contract_id, exc)
            return

        craft_name = (c.get("vessel_data") or {}).get("vessel_name") or cf["filename"][:-6]
        embed = discord.Embed(
            title="🏢 New craft delivered to the corporation",
            description=f"**{c.get('contractor_name', 'A member')}** completed a contract and added "
                        f"**{craft_name}** to {corp.get('name', 'the corp')}.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Mission", value=(c.get("mission") or "N/A")[:200], inline=False)
        embed.set_footer(text="Download the .craft, or hit Load to KSP to auto-install it.")
        from cogs.contractcraft import CorpCraftView
        craft_file = discord.File(io.BytesIO(data), filename=cf["filename"])
        await channel.send(embed=embed, file=craft_file, view=CorpCraftView(contract_id, gid))
        log.info("Corp delivery: posted craft %s to channel %s", contract_id, corp["channel_id"])
    except Exception as exc:
        log.error("Corp delivery failed for %s: %s", contract_id, exc)


async def _discord_notify_issuer(
    gid: int, issuer_id: int, contract_id: str,
    contract: dict, submitter_name: str, stored_files: list[dict],
    vessel_data: dict | None = None,
):
    """Post a submission notification to the issuer's Discord corp channel."""
    if _bot_instance is None:
        log.warning("Bot instance not set — cannot send Discord notification")
        return

    try:
        import discord
        from cogs.corps import _get_corp
        from cogs.contract_views import ContractReviewView

        # Find issuer's corp channel (may be in another server — resolve globally).
        corp = _get_corp(gid, issuer_id)
        if not corp or not corp.get("channel_id"):
            log.warning("No corp channel for issuer %d — cannot notify", issuer_id)
            return

        channel = _bot_instance.get_channel(int(corp["channel_id"]))
        if channel is None:
            return

        # Build embed
        mission = contract.get("mission", "Unknown mission")
        embed = discord.Embed(
            title="📤 Contract Submission Received",
            description=f"**{submitter_name}** submitted work for your contract.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Mission", value=mission[:200], inline=False)
        embed.add_field(name="Contract ID", value=contract_id, inline=True)
        embed.add_field(name="Payment", value=f"{contract.get('payment', 0)} KCoins", inline=True)

        # Add screenshot URL if available
        screenshots = [f for f in stored_files if f.get("content_type", "").startswith("image/")]
        if screenshots:
            embed.set_image(url=screenshots[0]["url"])

        # Add craft file info
        craft_files = [f for f in stored_files if f.get("filename", "").endswith(".craft")]
        if craft_files:
            embed.add_field(name="📎 Craft File", value=craft_files[0]["filename"], inline=True)

        embed.set_footer(text="Use the buttons below to accept or refuse this submission.")

        # A multi-craft submission ships several renders and one orbit diagram per
        # craft. Discord shows at most one image per embed, so add an embed for each
        # extra render (the first is already on the main embed) and one per orbit
        # diagram. Total embeds are capped at Discord's limit of 10.
        embeds = [embed]
        orbit_files: list = []

        for idx, rf in enumerate(screenshots):
            if idx == 0 or len(embeds) >= 10:
                continue   # first render is on the main embed
            re = discord.Embed(title=f"🚀 Craft Render {idx + 1}", color=discord.Color.blue())
            re.set_image(url=rf["url"])
            embeds.append(re)

        if vessel_data:
            try:
                from orbit_render import render_orbit

                # Active (contract) craft first, then any extras sent with it.
                snaps = []
                active_snap = vessel_data.get("active_vessel") or vessel_data
                if isinstance(active_snap, dict):
                    snaps.append(active_snap)
                for sv in (vessel_data.get("sent_vessels") or []):
                    if isinstance(sv, dict):
                        snaps.append(sv)

                for idx, snap in enumerate(snaps):
                    if len(embeds) >= 10:
                        break
                    try:
                        orbit_png = render_orbit(snap)
                    except Exception as exc:
                        log.warning("orbit render failed for vessel %d on %s: %s", idx, contract_id, exc)
                        continue
                    if not orbit_png:
                        continue
                    fname = f"orbit_{idx}.png"
                    orbit_files.append(discord.File(io.BytesIO(orbit_png), filename=fname))
                    vname = snap.get("vessel_name") or snap.get("vesselName") or "Vessel"
                    body = snap.get("body") or "N/A"
                    orbit_embed = discord.Embed(
                        title=f"🛰️ {vname}: Orbital Telemetry",
                        description=f"State around **{body}**.",
                        color=discord.Color.teal(),
                    )
                    orbit_embed.set_image(url=f"attachment://{fname}")
                    embeds.append(orbit_embed)
            except Exception as exc:
                log.warning("Failed to render orbit diagrams for %s: %s", contract_id, exc)

        # Attach review buttons (✅ Accept / ❌ Refuse) — uses the same
        # persistent view that the Discord-native contract flow uses
        view = ContractReviewView(contract_id, gid)

        # Mention the issuer
        issuer_mention = f"<@{issuer_id}>"
        send_kwargs: dict = {"content": issuer_mention, "embeds": embeds, "view": view}
        if orbit_files:
            send_kwargs["files"] = orbit_files
        await channel.send(**send_kwargs)
        log.info("Discord: Notified issuer %d in channel %s about submission", issuer_id, corp["channel_id"])

    except Exception as exc:
        log.error("Failed to send Discord notification to issuer: %s", exc)
