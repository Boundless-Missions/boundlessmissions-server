"""
data/store.py – Firestore-backed persistent user data store.

Keeps all user data in memory for fast access (every message triggers XP),
syncs to Firestore periodically and on shutdown.

Firestore structure:
    guilds/{guild_id}/users/{user_id} → { xp, level, balance, messages, ... }

User record schema:
{
    "xp": int,
    "level": int,
    "balance": int,
    "messages": int,
    "last_xp_time": float (unix timestamp),
    "joined_at": str (ISO 8601),
}
"""

import asyncio
import logging
import os
import time
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage

import settings
from config import cfg

log = logging.getLogger(__name__)

# Type alias
UserData = dict[str, Any]

# ── Firebase init ────────────────────────────────────────────────────────────
_cred = credentials.Certificate(cfg.FIREBASE_CREDENTIALS)
_bucket_name = os.getenv("FIREBASE_STORAGE_BUCKET", "")
_app = firebase_admin.initialize_app(_cred, {
    "storageBucket": _bucket_name,
} if _bucket_name else None)
from cost_guard import guard
from data.firebase_guard import wrap_firestore, wrap_bucket

# All Firestore / Storage access flows through these two handles (every cog
# imports them from here), so wrapping them is enough to meter spend and enforce
# the Firebase budget cap project-wide. See cost_guard.py / firebase_guard.py.
_db = wrap_firestore(firestore.client())
_storage_bucket = wrap_bucket(fb_storage.bucket() if _bucket_name else None)
if _storage_bucket:
    log.info("Firebase Storage configured: %s", _bucket_name)
else:
    log.warning("FIREBASE_STORAGE_BUCKET not set — contract file uploads disabled")


# ── Upload sanitization (client-supplied filenames / content types) ──────────
#
# Client-supplied filenames flow into Firebase Storage object paths
# (contracts/{id}/{filename} etc.). GCS treats the object name literally, so a
# name with "/" or ".." can't traverse out of its prefix, but it CAN collide with
# or shadow a sibling object and lets the client control the public object name.
# safe_filename reduces any name to a single safe basename. safe_content_type
# stops a client from having its public blob served as active content (HTML/SVG/JS).

import re as _re

_SAFE_NAME_RE = _re.compile(r"[^A-Za-z0-9._-]")

_SAFE_UPLOAD_CTYPES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/gzip", "application/octet-stream", "text/plain",
}


def safe_filename(name: str, default: str = "file") -> str:
    """Reduce a client-supplied filename to a safe storage basename.

    Strips any directory components (so it can't escape its prefix or shadow a
    sibling via '..'/slashes), replaces anything outside [A-Za-z0-9._-], drops
    leading dots (so '..' / '.env' can't become hidden/dot names), and caps the
    length. Falls back to `default` when nothing usable remains."""
    name = (name or "").replace("\\", "/")
    name = name.rsplit("/", 1)[-1]          # basename only
    name = _SAFE_NAME_RE.sub("_", name)
    name = name.lstrip(".")                 # ".." -> "", ".craft" -> "craft"
    name = name[:128]
    return name or default


def safe_content_type(claimed: str) -> str:
    """Clamp a client-claimed content type to an inert allowlist. Anything not
    explicitly safe (text/html, image/svg+xml, application/javascript, …) becomes
    application/octet-stream, so a public blob can't be served as active content."""
    c = (claimed or "").split(";", 1)[0].strip().lower()
    return c if c in _SAFE_UPLOAD_CTYPES else "application/octet-stream"


# ── Private objects + signed URLs ────────────────────────────────────────────
#
# File objects that should not be world-readable (a contract craft "private to the
# two parties", or a friend's quicksent payload) are uploaded WITHOUT make_public()
# and stored on their Firestore doc as a bucket *path* rather than a public URL. A
# short-lived V4 signed URL is minted at serve time, right before the consumer
# downloads. `sign_stored` is the read side used by every serve point: it signs a
# bare storage path and passes a legacy/public http URL through unchanged, so
# already-stored public URLs keep working and the migration can stay partial.

# A minted URL only has to outlive one API response plus the download that follows
# it (the mod fetches the URL from a response and downloads immediately), so this is
# kept just generous enough for a large craft on a slow link.
SIGNED_URL_TTL = 900  # seconds (15 min)

# For a URL that has to live inside a durable surface (a Discord embed a player may
# click days after a contract completes). 7 days is the V4 signed-URL maximum. The
# in-game import queue re-signs on every poll, so this link is only the secondary
# "also download here" convenience — after it lapses the craft still imports in game.
SIGNED_URL_MAX_TTL = 7 * 24 * 3600  # seconds (GCS V4 hard cap)


def upload_private(path: str, data: bytes,
                   content_type: str = "application/octet-stream") -> str:
    """Upload bytes to a NON-public object and return its bucket path (not a URL).

    Unlike the public upload helpers, this never calls make_public(): the object is
    reachable only through a signed URL minted by sign_stored()/signed_url() at
    serve time. The returned path is what gets stored on the Firestore doc."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type(content_type))
    log.info("Uploaded private object %s (%d bytes)", path, len(data) if data else 0)
    return path


def signed_url(path: str, ttl: int = SIGNED_URL_TTL) -> str:
    """A short-lived V4 signed GET URL for a bucket path. The service-account key
    signs locally, so this is a cheap local operation with no extra round-trip."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    from datetime import timedelta
    return _storage_bucket.blob(path).generate_signed_url(
        version="v4", expiration=timedelta(seconds=ttl), method="GET")


def is_storage_path(value: str | None) -> bool:
    """True for a bare bucket path (a private object), False for a full http(s) URL
    or an empty value. The one predicate every serve point uses to tell a
    signable private reference from a pass-through legacy/public URL."""
    return bool(value) and not (value.startswith("http://") or value.startswith("https://"))


def sign_stored(value: str | None, ttl: int = SIGNED_URL_TTL) -> str | None:
    """Serve-time resolver for a stored craft/vessel reference.

    New private objects are stored as a bare bucket path → sign it. Legacy values
    and objects still public (marketplace/rescue) are full http(s) URLs → pass
    through unchanged. None/empty → unchanged. This is what lets the migration be
    partial and backward-compatible: a serve point can call it on every reference
    without knowing which storage scheme produced it. Never raises — on a signing
    failure it returns the original value so the caller degrades rather than 500s."""
    if not is_storage_path(value):
        return value
    try:
        return signed_url(value, ttl)
    except Exception as exc:
        log.warning("Could not sign stored object %r: %s", value, exc)
        return value


def _default_user() -> UserData:
    """Return a fresh user record with default values."""
    return {
        "user_id": "",
        "username": "",
        "language": "",
        "xp": 0,
        "level": 0,
        "balance": settings.STARTING_BALANCE,
        "messages": 0,
        "last_xp_time": 0.0,
        "joined_at": "",
        "unlocked_levels": [],
        "rescues": 0,
        # reward key → unix timestamp of the last payout, for rewards that are
        # capped to one per window (see try_claim_timed_reward).
        "reward_cooldowns": {},
    }


def xp_for_level(level: int) -> int:
    """Calculate total XP needed to reach a given level."""
    if level <= 0:
        return 0
    return int(settings.LEVEL_XP_BASE * (level ** settings.LEVEL_XP_EXPONENT))


def level_from_xp(xp: int) -> int:
    """Derive the current level from total XP."""
    level = 0
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


class UserStore:
    """In-memory store backed by Firestore."""

    def __init__(self) -> None:
        # GLOBAL wallet: user_id (str) -> UserData. Balances/XP/levels are now one
        # record per user across every server (see the economy-migration in load()).
        # Methods still take guild_id for call-site compatibility, but it is only
        # used as context (e.g. which guild announced a level-up), never as a key.
        self._users: dict[str, UserData] = {}
        self._lock = asyncio.Lock()
        self._dirty_users: set[str] = set()  # user_id strings

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def load(self) -> None:
        """Load the GLOBAL user wallet from Firestore into memory, running a
        one-time migration from the legacy per-guild layout on first start."""
        total = 0
        try:
            for user_doc in _db.collection("users").stream():
                merged = _default_user()
                merged.update(user_doc.to_dict() or {})
                self._users[user_doc.id] = merged
                total += 1
            log.info("Loaded %d global user records from Firestore", total)
        except Exception as exc:
            log.error("Failed to load from Firestore: %s — starting fresh", exc)
            self._users = {}

        # One-time merge of legacy guilds/{gid}/users/{uid} wallets into the global
        # store. Guarded by a flag doc so it runs exactly once.
        try:
            flag = _db.collection("meta").document("economy_migration").get()
            if not (flag.exists and (flag.to_dict() or {}).get("done")):
                await self._migrate_legacy_economy()
        except Exception as exc:
            log.error("Economy migration check failed: %s", exc)

    async def _migrate_legacy_economy(self) -> None:
        """Merge legacy per-guild wallets into the global store (sum balance/xp/
        messages/rescues, union unlocked_levels, max last_xp_time), then copy any
        in-flight marketplace/auction/contract docs into the new global
        collections.

        The merge is computed purely from the (immutable) legacy data into a fresh
        accumulator and then SET onto the global records — so even if this crashes
        before the meta/economy_migration flag is written, a re-run recomputes the
        same values instead of double-counting."""
        acc: dict[str, UserData] = {}
        try:
            for guild_doc in _db.collection("guilds").stream():
                users_ref = _db.collection("guilds").document(guild_doc.id).collection("users")
                for udoc in users_ref.stream():
                    data = udoc.to_dict() or {}
                    uid = udoc.id
                    rec = acc.get(uid)
                    if rec is None:
                        rec = _default_user()
                        rec.update({"user_id": uid, "balance": 0, "xp": 0, "messages": 0,
                                    "rescues": 0, "unlocked_levels": [], "last_xp_time": 0.0,
                                    "joined_at": "", "language": "", "username": ""})
                        acc[uid] = rec
                    rec["balance"] += int(data.get("balance", 0) or 0)
                    rec["xp"] += int(data.get("xp", 0) or 0)
                    rec["messages"] += int(data.get("messages", 0) or 0)
                    rec["rescues"] = int(rec.get("rescues", 0) or 0) + int(data.get("rescues", 0) or 0)
                    levels = set(rec.get("unlocked_levels", []) or []) | set(data.get("unlocked_levels", []) or [])
                    old_max = int(data.get("max_unlocked_level", 0) or 0)
                    if old_max > 0:
                        levels.add(old_max)
                    rec["unlocked_levels"] = sorted(levels)
                    rec["last_xp_time"] = max(float(rec.get("last_xp_time", 0.0) or 0.0),
                                              float(data.get("last_xp_time", 0.0) or 0.0))
                    ja = data.get("joined_at") or ""
                    if ja and (not rec.get("joined_at") or ja < rec["joined_at"]):
                        rec["joined_at"] = ja
                    if not rec.get("language") and data.get("language"):
                        rec["language"] = data["language"]
                    if not rec.get("username") and data.get("username"):
                        rec["username"] = data["username"]

            # SET the merged values onto the global records (idempotent).
            for uid, rec in acc.items():
                rec["level"] = level_from_xp(rec["xp"])
                self._users[uid] = rec
                self._mark_dirty(0, uid)
            merged_users = len(acc)

            await self.save()  # flush merged global wallets immediately

            moved = _migrate_inflight_economic_docs()
            _db.collection("meta").document("economy_migration").set(
                {"done": True, "merged_users": merged_users, "moved_docs": moved})
            log.warning("Economy migration complete: merged %d legacy wallet rows, "
                        "moved %d in-flight docs to global collections.", merged_users, moved)
        except Exception as exc:
            log.error("Economy migration failed (will retry next start): %s", exc, exc_info=True)

    async def save(self) -> None:
        """Flush all dirty user records to Firestore."""
        async with self._lock:
            if not self._dirty_users:
                return
            dirty = list(self._dirty_users)
            self._dirty_users.clear()

        try:
            # If the cost guard has just frozen Firebase, this flush is exactly
            # the write that must still get through: everything in `dirty` is
            # minutes of XP and balance held only in memory, and a freeze that
            # lasts until the 1st guarantees a restart drops it. The guard arms
            # one grace pass on freezing; outside that this is a no-op and normal
            # gating applies.
            with guard.final_flush():
                batch = _db.batch()
                count = 0

                for user_id in dirty:
                    user_data = self._users.get(user_id)
                    if user_data is None:
                        continue

                    doc_ref = _db.collection("users").document(user_id)
                    batch.set(doc_ref, user_data)
                    count += 1

                    # Firestore batches max out at 500 operations
                    if count >= 450:
                        batch.commit()
                        log.info("Committed Firestore batch (%d docs)", count)
                        batch = _db.batch()
                        count = 0

                if count > 0:
                    batch.commit()
            log.info("Saved %d global user records to Firestore", len(dirty))
        except Exception as exc:
            log.error("Failed to save to Firestore: %s", exc, exc_info=True)
            # Re-add to dirty so we retry next cycle
            async with self._lock:
                self._dirty_users.update(dirty)

    async def save_if_dirty(self) -> None:
        """Save only if data has changed since last save."""
        if self._dirty_users:
            await self.save()

    def _mark_dirty(self, guild_id: int, user_id: int) -> None:
        """Mark a user record as needing a Firestore write (guild_id ignored —
        the wallet is global)."""
        self._dirty_users.add(str(user_id))

    # ── User access ──────────────────────────────────────────────────────────

    def get_user(self, guild_id: int, user_id: int) -> UserData:
        """Get a user's GLOBAL record, creating a default one if needed.
        (guild_id is accepted for call-site compatibility but not used as a key.)"""
        key = str(user_id)
        if key not in self._users:
            self._users[key] = _default_user()
            self._mark_dirty(guild_id, user_id)
        return self._users[key]

    def get_all_users(self, guild_id: int) -> dict[str, UserData]:
        """Get all (global) user records. guild_id is ignored."""
        return self._users

    async def delete_user(self, guild_id: int, user_id: int) -> bool:
        """Erase a user's GLOBAL profile record from memory and Firestore. Used by
        the user-initiated 'delete my data' flow. Returns True if a record existed."""
        ukey = str(user_id)
        async with self._lock:
            existed = self._users.pop(ukey, None) is not None
            self._dirty_users.discard(ukey)  # don't let a pending write resurrect it
        try:
            _db.collection("users").document(ukey).delete()
        except Exception as exc:
            log.error("Failed to delete user %s from Firestore: %s", ukey, exc)
            raise
        log.warning("Deleted global user record %s (existed=%s)", ukey, existed)
        return existed

    # ── XP operations ────────────────────────────────────────────────────────

    async def add_xp(
        self, guild_id: int, user_id: int, amount: int
    ) -> tuple[int, int, bool]:
        """
        Add XP to a user. Returns (new_xp, new_level, leveled_up).
        Respects the cooldown from settings.
        """
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            now = time.time()

            # Check cooldown
            if now - user["last_xp_time"] < settings.XP_COOLDOWN_SECONDS:
                return user["xp"], user["level"], False

            old_level = user["level"]
            user["xp"] += amount
            user["messages"] += 1
            user["last_xp_time"] = now

            new_level = level_from_xp(user["xp"])
            user["level"] = new_level
            self._mark_dirty(guild_id, user_id)

            return user["xp"], new_level, new_level > old_level

    async def set_xp(self, guild_id: int, user_id: int, amount: int) -> None:
        """Directly set a user's XP (admin use)."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["xp"] = max(0, amount)
            user["level"] = level_from_xp(user["xp"])
            self._mark_dirty(guild_id, user_id)

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        """Add (or subtract) from a user's balance. Returns new balance.

        NOTE: use this only for credits (refunds, payouts) or deductions that are
        already known to be covered. For a spend that must not overdraw, use
        `try_debit` — `add_balance` clamps at 0, so a too-large deduction silently
        vanishes instead of failing, which a concurrent caller can exploit to spend
        coins they don't have (TOCTOU double-spend)."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["balance"] = max(0, user["balance"] + amount)
            self._mark_dirty(guild_id, user_id)
            return user["balance"]

    async def try_claim_timed_reward(
        self, guild_id: int, user_id: int, key: str,
        amount: int, cooldown_seconds: float,
    ) -> tuple[bool, float]:
        """Credit `amount` KCoins for `key` at most once per `cooldown_seconds`.

        Returns (granted, seconds_until_next). On a refusal nothing is written and
        the remaining wait is returned so the caller can say when it reopens. The
        cooldown check and the credit happen under one lock, so two uploads landing
        together can't both collect — the mirror of `try_debit`'s double-spend guard.
        """
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            # Records written before this field existed are merged over the defaults
            # at load, so the dict is present; setdefault covers a hand-edited doc.
            stamps = user.setdefault("reward_cooldowns", {})
            now = time.time()
            elapsed = now - float(stamps.get(key, 0.0) or 0.0)
            if elapsed < cooldown_seconds:
                return False, cooldown_seconds - elapsed

            stamps[key] = now
            user["balance"] = max(0, user["balance"] + amount)
            self._mark_dirty(guild_id, user_id)
            return True, cooldown_seconds

    async def try_debit(self, guild_id: int, user_id: int, amount: int) -> bool:
        """Atomically deduct `amount` only if the balance fully covers it.

        Returns True if the debit was applied, False on insufficient funds. The
        check and the deduction happen under one lock, so two concurrent requests
        can't both pass a balance check on the same funds and overdraw (the bug a
        separate get_user()+add_balance() pair has). A zero/negative amount is a
        no-op success. Never drives the balance below zero."""
        if amount <= 0:
            return True
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            if user["balance"] < amount:
                return False
            user["balance"] -= amount
            self._mark_dirty(guild_id, user_id)
            return True

    async def debit_up_to(self, guild_id: int, user_id: int, amount: int) -> int:
        """Atomically deduct up to `amount`, capped at the available balance.

        Returns the amount actually taken. For "take whatever they can pay" fines
        where a partial charge is intended; the read + deduction are atomic so the
        amount returned is exactly what left the account."""
        if amount <= 0:
            return 0
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            taken = min(amount, user["balance"])
            if taken > 0:
                user["balance"] -= taken
                self._mark_dirty(guild_id, user_id)
            return taken

    async def add_rescue(self, guild_id: int, user_id: int, amount: int = 1) -> int:
        """Increment a user's completed-rescue counter. Returns the new total."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["rescues"] = max(0, user.get("rescues", 0) + amount)
            self._mark_dirty(guild_id, user_id)
            return user["rescues"]

    async def add_unlocked_level(self, guild_id: int, user_id: int, level: int) -> bool:
        """Add a level to unlocked_levels if not already present. Returns True if newly added."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            # handle legacy data safely
            if "unlocked_levels" not in user:
                old_max = user.pop("max_unlocked_level", 0)
                user["unlocked_levels"] = [old_max] if old_max > 0 else []
                
            unlocked = set(user["unlocked_levels"])
            if level not in unlocked:
                unlocked.add(level)
                user["unlocked_levels"] = sorted(list(unlocked))
                self._mark_dirty(guild_id, user_id)
                return True
            return False

    async def remove_unlocked_level(self, guild_id: int, user_id: int, level: int) -> bool:
        """Remove a level from unlocked_levels. Use level=0 to clear all. Returns True if changed."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            if "unlocked_levels" not in user:
                old_max = user.pop("max_unlocked_level", 0)
                user["unlocked_levels"] = [old_max] if old_max > 0 else []
                
            unlocked = set(user["unlocked_levels"])
            if level == 0 and unlocked:
                user["unlocked_levels"] = []
                self._mark_dirty(guild_id, user_id)
                return True
            elif level in unlocked:
                unlocked.remove(level)
                user["unlocked_levels"] = sorted(list(unlocked))
                self._mark_dirty(guild_id, user_id)
                return True
            return False

    # ── Leaderboard ──────────────────────────────────────────────────────────

    def leaderboard(
        self, guild_id: int, key: str = "xp", limit: int | None = None
    ) -> list[tuple[str, UserData]]:
        """
        Return GLOBAL users sorted by `key` (descending). guild_id is ignored —
        with a global wallet the leaderboard is global.
        Each item is (user_id_str, user_data).
        """
        limit = limit or settings.LEADERBOARD_PAGE_SIZE
        return sorted(
            self._users.items(),
            key=lambda kv: kv[1].get(key, 0),
            reverse=True,
        )[:limit]


# ── One-time migration of in-flight economic docs ────────────────────────────

def _migrate_inflight_economic_docs() -> int:
    """Copy non-terminal marketplace/auction/contract docs out of the legacy
    guilds/{gid}/{coll} subcollections into the new top-level global collections,
    preserving document ids. Returns the number of docs moved. Idempotent (set by
    document id) and only invoked from the guarded economy migration."""
    # collection -> set of statuses that are still "live" and worth moving.
    live = {
        "marketplace": {"active"},
        "auctions": {"open"},
        "contracts": {"pending", "active", "submitted", "disputed", "mod_review"},
    }
    moved = 0
    try:
        for guild_doc in _db.collection("guilds").stream():
            for coll, keep in live.items():
                sub = _db.collection("guilds").document(guild_doc.id).collection(coll)
                for doc in sub.stream():
                    data = doc.to_dict() or {}
                    if data.get("status") in keep:
                        _db.collection(coll).document(doc.id).set(data)
                        moved += 1
    except Exception as exc:
        log.error("In-flight economic doc migration failed: %s", exc)
    return moved


# Singleton – import this from anywhere
store = UserStore()
