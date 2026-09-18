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
from datetime import datetime, timezone
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage

import settings
from config import cfg

log = logging.getLogger(__name__)


class WalletUnavailable(RuntimeError):
    """Raised by a mutator when the wallet is not loaded, so nothing can be saved.

    A distinct type from `FirebaseBudgetExceeded` because the cause differs: that
    one means "Firestore is refused right now", this means "this process never got
    the real records, so a write would be invented". Both answer 503.
    """


# Type alias
UserData = dict[str, Any]

# ── Firebase init ────────────────────────────────────────────────────────────
_cred = credentials.Certificate(cfg.FIREBASE_CREDENTIALS)
_bucket_name = os.getenv("FIREBASE_STORAGE_BUCKET", "")
_app = firebase_admin.initialize_app(_cred, {
    "storageBucket": _bucket_name,
} if _bucket_name else None)
from cost_guard import guard, FirebaseBudgetExceeded
from data.firebase_guard import wrap_firestore, wrap_bucket

# All Firestore / Storage access flows through these two handles (every cog
# imports them from here), so wrapping them is enough to meter spend and enforce
# the Firebase budget cap project-wide. See cost_guard.py / firebase_guard.py.
_db_unguarded = firestore.client()
_db = wrap_firestore(_db_unguarded)

# The one deliberate hole in the meter, and it is one document read.
#
# Every gated call on `_db` raises `FirebaseBudgetExceeded` at Level.FROZEN. The
# session token-version lookup in `api_auth._get_token_version` is on the auth
# dependency of EVERY authenticated route, and it fails closed on a read error
# with a cold cache — so under a freeze it raised, became a 503, and took down
# every authenticated route with it. Including `/api/v1/web/admin/*`, which is
# the documented and only in-product way to lift the freeze: the recovery path
# went through the process that the freeze had just disabled.
#
# Refusing this particular read buys nothing anyway — it is one keyed read whose
# alternative is refusing the entire request — so it is exempted from the BRAKE
# rather than from the METER: the caller reports it with
# `cost_guard.guard.note_firestore(reads=1)`, so it still shows up on the bill.
_storage_bucket = wrap_bucket(fb_storage.bucket() if _bucket_name else None)
if _storage_bucket:
    log.info("Firebase Storage configured: %s", _bucket_name)
else:
    log.warning("FIREBASE_STORAGE_BUCKET not set: contract file uploads disabled")


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


# What a stored file is *called* — on a contract document, in a Discord embed, in
# the in-game preview — is a separate question from where it is stored, and it
# used to be answered with the raw UploadFile.filename. That string is the
# client's, so it could be 1,100 characters (which breaks the embed that carries
# the moderators' dispute buttons) or `click here](https://evil/) [x` (which
# Discord renders as a masked link to somewhere else in a moderator ticket).
DISPLAY_FILENAME_MAX = 80
_MD_LINK_CHARS_RE = _re.compile(r"([\[\]()\\`])")


def display_filename(name: str, default: str = "file",
                     limit: int = DISPLAY_FILENAME_MAX) -> str:
    """The name a client-supplied file is shown under: `safe_filename`'s
    character set, capped at `limit` with the extension kept — several readers
    pick craft files out of a submission by `.endswith(".craft")`, so a cut that
    ate the suffix would hide the craft from the very code that delivers it."""
    name = safe_filename(name, default)
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) < 16:
        return stem[:max(1, limit - len(ext) - 1)] + "." + ext
    return name[:limit]


def md_filename(name: str, default: str = "file") -> str:
    """`display_filename` plus a backslash before every character that could
    close a markdown link's text or open its target. New submissions never carry
    those (the display name is sanitised when it is stored); this is for entries
    written before it was, and for any renderer that pastes a name into
    `[text](url)`, which is the one place a filename can become a link."""
    return _MD_LINK_CHARS_RE.sub(r"\\\1", display_filename(name, default))


# Magic bytes of the raster formats a rendered preview can legitimately be. This
# is the *storage layer's* half of the "is this an image" question: the API layer
# decodes the file with Pillow under a pixel ceiling (`_looks_like_image` /
# `_open_image_bounded` in api_server), which is the stronger check and the one
# that must stay the primary gate. This one exists because it is cheap, needs no
# Pillow, and sits at the last point before bytes are made world-readable — so a
# public-upload helper cannot be handed 25 MB of arbitrary data by a caller that
# forgot the decode check, which is exactly how the quicksend blueprint path came
# to publish attacker-chosen bytes on the project's own bucket.
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",          # JPEG
    b"GIF87a", b"GIF89a",       # GIF
    b"BM",                      # BMP
)


def looks_like_image(data: bytes | None) -> bool:
    """Whether these bytes begin like a raster image. Header sniff only — never a
    substitute for decoding one that will be shown to somebody."""
    if not data or len(data) < 12:
        return False
    if data.startswith(_IMAGE_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


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


# ── Transaction ledger vocabulary ────────────────────────────────────────────
#
# The category is the *only* thing that turns a row of numbers into an answer to
# "what did I spend it on", so the set is closed and lives here rather than being
# spelled out at each call site — a typo'd category would silently open a new
# bucket in every summary. Call sites import these names; the UIs render
# TX_LABELS and are free to not know about a category a newer server has added.
#
# The split is by *what happened*, not by which module happened to call: a
# marketplace sale and a contract payout are both income, but a player asking
# where their money went needs them apart.

TX_OTHER = "other"                        # untagged — a call site nobody flagged
TX_CONTRACT_PAYMENT = "contract_payment"  # payout for delivering a contract
TX_CONTRACT_ESCROW = "contract_escrow"    # payment locked when issuing one
TX_CONTRACT_REFUND = "contract_refund"    # that escrow coming back
TX_CONTRACT_FINE = "contract_fine"        # a fine charged for failing one
TX_FINE_RECEIVED = "fine_received"        # the other side of someone's fine
TX_DEBT_REPAYMENT = "debt_repayment"      # garnished out of an earning
TX_MARKET_SALE = "market_sale"            # sold a craft on the marketplace
TX_MARKET_PURCHASE = "market_purchase"    # bought one
TX_AUCTION_ESCROW = "auction_escrow"      # bid/listing value locked
TX_AUCTION_REFUND = "auction_refund"      # that escrow coming back
TX_TRANSFER_IN = "transfer_in"            # another player sent coins
TX_TRANSFER_OUT = "transfer_out"          # sent coins to another player
TX_REWARD = "reward"                      # screenshots, daily rewards, XP levels
TX_ADMIN = "admin"                        # a moderator/owner correction

# Human labels, so all three front ends name a category the same way. A UI that
# meets a category not in here should fall back to the raw key rather than hide
# the row: an unexplained movement is still a movement of the player's money.
TX_LABELS = {
    TX_OTHER: "Other",
    TX_CONTRACT_PAYMENT: "Contract payment",
    TX_CONTRACT_ESCROW: "Contract escrow",
    TX_CONTRACT_REFUND: "Contract refund",
    TX_CONTRACT_FINE: "Contract fine",
    TX_FINE_RECEIVED: "Fine received",
    TX_DEBT_REPAYMENT: "Debt repayment",
    TX_MARKET_SALE: "Marketplace sale",
    TX_MARKET_PURCHASE: "Marketplace purchase",
    TX_AUCTION_ESCROW: "Auction escrow",
    TX_AUCTION_REFUND: "Auction refund",
    TX_TRANSFER_IN: "Received from player",
    TX_TRANSFER_OUT: "Sent to player",
    TX_REWARD: "Reward",
    TX_ADMIN: "Admin adjustment",
}

# How many entries the ring buffer holds. The whole user record is re-serialised
# on every flush, so this is a size budget, not a preference: at roughly 90 bytes
# an entry, 250 is about 22 KB against Firestore's 1 MiB document limit — room to
# spare, while still covering months of an ordinary player's activity.
TX_MAX = 250

# Detail strings are truncated to this. Some are player-supplied (a craft name, a
# transfer note), so this is a bound on the document, not a formatting choice.
TX_DETAIL_MAX = 120


def tx_detail(text: str, fallback: str = "", limit: int = 60) -> str:
    """Normalise a free-text ledger detail to one short readable line.

    Shared rather than duplicated because every caller wants the same three things
    — whitespace collapsed (a mission body is multi-line), a cut at a word boundary
    rather than mid-sentence, and a fallback when the source text is empty. The
    store truncates again on write; this cut is what makes that one never fire.
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return fallback
    if len(cleaned) > limit:
        head = cleaned[:limit].rsplit(" ", 1)[0]
        cleaned = (head or cleaned[:limit]) + "…"
    return cleaned


def _default_user(*, opening_balance: bool = True) -> UserData:
    """Return a fresh user record with default values.

    `opening_balance=False` is for the three callers that are not minting a new
    player: the template `load()` lays under a stored document, the accumulator
    the per-guild migration builds, and the detached record `get_user` returns
    when the store failed to load. None of those is somebody arriving, and a
    non-zero `STARTING_BALANCE` would credit all three.

    The load template is the one that would actually corrupt something. A record
    written before the ledger existed carries no `tx`, so the seeded opening
    entry survives `merged.update(doc)` and claims a payment the player never
    received, sitting next to a balance that does not account for it. The
    detached one is quieter but no better: the documented contract there is that
    reads degrade to zeros, and a balance of 1000 reported during a Firestore
    outage is a number somebody could try to spend.
    """
    seed = int(settings.STARTING_BALANCE) if opening_balance else 0
    return {
        "user_id": "",
        "username": "",
        "language": "",
        "xp": 0,
        "level": 0,
        "balance": seed,
        "messages": 0,
        "last_xp_time": 0.0,
        "joined_at": "",
        "unlocked_levels": [],
        "rescues": 0,
        # reward key → unix timestamp of the last payout, for rewards that are
        # capped to one per window (see try_claim_timed_reward).
        "reward_cooldowns": {},
        # Unpaid contract fines, oldest first: [{"creditor_id": str, "amount": int}].
        # Lives on the user record rather than in its own collection so the debt and
        # the balance it is collected from are one document and one flush — a crash
        # between two writes could otherwise credit a creditor without debiting the
        # debtor, or the reverse. See `add_debt` / `_garnish_locked`.
        "debts": [],
        # Whether corp-channel messages from the bot @-mention this player.
        # Default on: a message nobody is notified of is one nobody answers, and
        # every caller of `deliver_to_player` is waiting for a reply. Off is a
        # deliberate "I read my corp channel myself", so the mention is sent with
        # allowed_mentions=none rather than removed — the post still says who it
        # is for, it just does not light up Discord.
        "corp_pings": True,
        # Player-issued contracts completed as the contractor, newest last:
        # [{"peer": issuer id, "t": unix time, "xp": granted}], trimmed to the window. With
        # `last_contract_xp_at` (unix time of the last XP actually granted from a
        # player-issued contract) this is what `rewards.human_contract_xp` reads to
        # apply the cooldown and the per-pair limit. On the user record for the
        # reason the debts and the ledger are: one document, one flush, no extra
        # Firestore read on the approval path.
        "contract_xp_log": [],
        "last_contract_xp_at": 0.0,
        # Transaction ledger — the last TX_MAX movements, oldest first, plus
        # lifetime per-category totals that survive entries rolling off the end.
        # See the "Transaction ledger" section below for why it lives here.
        #
        # Seeded with the opening balance when there is one, because the ledger's
        # whole claim is that its entries add up to the balance they explain, and
        # money placed in a wallet by the schema rather than by a call would break
        # that silently. `STARTING_BALANCE` was 0 when this was written, so the
        # seeding was dead code kept for the day the setting changed; that day was
        # 2026-09-08, and this is the line that makes the new player's Finance tab
        # say where their coins came from instead of showing 1000 and no history.
        "tx": ([{"t": round(time.time(), 3), "a": seed,
                 "c": TX_REWARD, "d": "Opening balance", "p": ""}]
               if seed else []),
        "tx_totals": ({TX_REWARD: {"in": seed, "out": 0, "n": 1}}
                      if seed else {}),
    }


def xp_for_level(level: int) -> int:
    """Calculate total XP needed to reach a given level."""
    if level <= 0:
        return 0
    return int(settings.LEVEL_XP_BASE * (level ** settings.LEVEL_XP_EXPONENT))


def level_from_xp(xp: int) -> int:
    """Derive the current level from total XP.

    Closed-form: the level is the inverse of `xp_for_level`, `(xp / BASE) **
    (1 / EXPONENT)`, then nudged by at most a step or two to agree with the
    integer truncation `xp_for_level` applies. It used to count levels one at a
    time, which is fine at level 40 and is ~2e9 iterations at the 2**53 a
    Discord integer option can carry — run under `store._lock` on the event
    loop by `set_xp`, so one `/setxp` could stop every command and API request
    until it finished. The nudge loops are bounded by float error, not by the
    input, and a negative or zero XP is level 0 either way.
    """
    if xp <= 0:
        return 0
    level = int((xp / settings.LEVEL_XP_BASE) ** (1.0 / settings.LEVEL_XP_EXPONENT))
    while level > 0 and xp < xp_for_level(level):
        level -= 1
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


class UserStore:
    """In-memory store backed by Firestore."""

    # Ledger vocabulary, re-exported on the class so a call site that already has
    # `store` imported can name a category without a second import of the module.
    # The module-level constants above stay the definitions; these are aliases, so
    # there is still exactly one place a category is spelled.
    TX_OTHER = TX_OTHER
    TX_CONTRACT_PAYMENT = TX_CONTRACT_PAYMENT
    TX_CONTRACT_ESCROW = TX_CONTRACT_ESCROW
    TX_CONTRACT_REFUND = TX_CONTRACT_REFUND
    TX_CONTRACT_FINE = TX_CONTRACT_FINE
    TX_FINE_RECEIVED = TX_FINE_RECEIVED
    TX_DEBT_REPAYMENT = TX_DEBT_REPAYMENT
    TX_MARKET_SALE = TX_MARKET_SALE
    TX_MARKET_PURCHASE = TX_MARKET_PURCHASE
    TX_AUCTION_ESCROW = TX_AUCTION_ESCROW
    TX_AUCTION_REFUND = TX_AUCTION_REFUND
    TX_TRANSFER_IN = TX_TRANSFER_IN
    TX_TRANSFER_OUT = TX_TRANSFER_OUT
    TX_REWARD = TX_REWARD
    TX_ADMIN = TX_ADMIN
    TX_LABELS = TX_LABELS
    TX_MAX = TX_MAX
    tx_detail = staticmethod(tx_detail)

    def __init__(self) -> None:
        # GLOBAL wallet: user_id (str) -> UserData. Balances/XP/levels are now one
        # record per user across every server (see the economy-migration in load()).
        # Methods still take guild_id for call-site compatibility, but it is only
        # used as context (e.g. which guild announced a level-up), never as a key.
        self._users: dict[str, UserData] = {}
        self._lock = asyncio.Lock()
        self._dirty_users: set[str] = set()  # user_id strings
        # Set by `load()` when the boot read of the whole collection failed. It
        # starts False because nothing has failed yet — it records a *failed*
        # load, not the absence of one, which is why the standalone test suites
        # (which never load, having no Firestore) are unaffected by it. While it
        # is True this store will neither persist nor mint: see load().
        self._load_failed = False
        # Set once `load()` has completed a full, successful read. Distinct from
        # `not _load_failed`, which is also true before any load has been *tried* —
        # and an untried load is the same dangerous state as a failed one for a
        # live bot: an empty `_users` whose writes are not blocked, so the first
        # member scan mints zeroed records and the next flush replaces every real
        # document with them. `bot.py` refuses to start unless this is True.
        self._loaded = False
        # Set when the boot read was refused by our own cost brake rather than by
        # Firestore. A different fact from a failed read, needing a different answer.
        self._budget_blocked = False
        # Set the moment `load()` is entered, and never cleared.
        #
        # This separates "a load ran and did not finish" from "no load has been
        # attempted", which `_loaded` alone cannot. Only the first is dangerous to
        # a *writer*: the process is live, the caller believes their payout landed,
        # and `save()` will refuse to flush it. The second is the offline case —
        # the standalone `test_*.py` suites drive the real singleton entirely in
        # memory and never flush — plus the window before `cogs.xp.cog_load`, and
        # in production it cannot outlive startup because `bot.py` refuses to run
        # an unloaded store.
        #
        # `save()` deliberately does NOT use this: it gates on `_loaded`, because a
        # full-document write from memory is unsafe in BOTH states.
        self._load_attempted = False

    def _require_writable(self) -> None:
        """Refuse a mutation while the wallet is not loaded.

        The budget-freeze path (`load()` catching `FirebaseBudgetExceeded`) leaves
        the process running with `_loaded` False so that a frozen guard is not a
        crash loop. That is right, but it created a worse failure than the one it
        replaced: `save()` refuses forever on `not _loaded`, while every mutator
        happily edited the in-memory record and marked it dirty — so the wallet
        ACCEPTED writes and dropped them.

        While the freeze holds that is at least loud, because most Firestore-backed
        commands raise anyway. The dangerous window is after it clears: the month
        rolls over (or the owner raises the budget), Firestore starts working, the
        bot looks entirely healthy — and every payout, fine, transfer and XP gain
        still lands in memory and is discarded at the next flush, while contracts
        settle COMPLETED in Firestore with the money never arriving.

        So a write in this state raises instead. `ensure_loaded()` closes the window
        by re-reading once the guard drops, and this is the backstop for the moment
        before it does: refusing a payout is recoverable, silently losing it is not.
        """
        if self._load_attempted and not self._loaded:
            raise WalletUnavailable(
                "The wallet is not loaded, so this change cannot be saved. "
                + ("The Firebase cost guard is FROZEN. Raise the budget or clear "
                   "the freeze, and the wallet reloads by itself."
                   if self._budget_blocked else
                   "The boot read did not complete.")
            )

    async def ensure_loaded(self) -> bool:
        """Re-attempt the boot read if a budget freeze is the only thing blocking it.

        Called from the auto-save loop, which is the heartbeat that already exists.
        Without this the read-only state is permanent until someone restarts the
        process — including long after the freeze that caused it has gone, since
        `load()` is awaited from exactly one place (`cogs.xp.cog_load`) and the level
        resets itself on the UTC month rollover with nobody in the loop.

        Deliberately narrow: it retries ONLY the budget-blocked state, never a failed
        Firestore read. A failed read stops the bot at startup by design, and quietly
        retrying it in the background would work around that gate rather than through
        it. Returns True when a reload happened and succeeded.
        """
        if self._loaded or not self._budget_blocked:
            return False
        try:
            from cost_guard import Level, guard as _guard
            if _guard.level is Level.FROZEN:
                return False          # still frozen; nothing to retry yet
        except Exception:             # pragma: no cover - guard import/attr issues
            return False
        log.info("Cost guard is no longer FROZEN. Re-reading the wallet.")
        await self.load()
        if self._loaded:
            log.info("Wallet reloaded after the budget freeze cleared; writes are live again.")
        return self._loaded

    @property
    def budget_blocked(self) -> bool:
        """True when the wallet is unloaded because `cost_guard` is FROZEN.

        Kept apart from a read *failure* because the recovery differs. A frozen
        guard is cleared through the admin console — an API route in this same
        process — so exiting on it removes the only way back, and since the level
        is persisted to `data/cost_state.json` and the month has not rolled, it
        would do so on every restart: a crash loop under `Restart=always`, armed by
        anyone able to drive the Firebase bill up. Running read-only is safe here in
        a way it is not after a failed read, because under FROZEN every Firestore
        and Storage call is already refused — there is nothing to overwrite.
        """
        return self._budget_blocked

    @property
    def loaded(self) -> bool:
        """True once the boot read of the wallet collection has succeeded.

        The bot's startup gate reads this. It is deliberately a positive assertion
        ("we hold the real records") rather than the absence of a failure, because
        the two states that must both stop the bot — the read failed, and the read
        never ran — differ only in whether anything raised.
        """
        return self._loaded

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def load(self) -> None:
        """Load the GLOBAL user wallet from Firestore into memory, running a
        one-time migration from the legacy per-guild layout on first start."""
        self._load_attempted = True
        total = 0
        try:
            for user_doc in _db.collection("users").stream():
                merged = _default_user(opening_balance=False)
                merged.update(user_doc.to_dict() or {})
                self._users[user_doc.id] = merged
                total += 1
            self._load_failed = False
            self._budget_blocked = False
            self._loaded = True
            log.info("Loaded %d global user records from Firestore", total)
        except FirebaseBudgetExceeded as exc:
            # Our own brake, not Firestore. Writes stay blocked (`_loaded` is False),
            # but this must not kill the process — see the `budget_blocked` docstring.
            self._budget_blocked = True
            self._loaded = False
            log.critical(
                "The wallet could not be loaded because the Firebase cost guard is "
                "FROZEN (%s). Running read-only: balances read as zero and nothing "
                "is written. Raise the budget or clear the freeze from the admin "
                "console, then restart.", exc)
            return
        except Exception as exc:
            # A failed load must never become an empty store. `save` writes every
            # record with `batch.set` — a whole-document REPLACE — and the member
            # scan in cogs/xp mints a zeroed `_default_user()` for anyone it
            # cannot find, so "starting fresh" plus one auto-save cycle was every
            # balance, XP total, unlocked level, rescue count, debt ledger and
            # transaction history in the game overwritten with zeros, from one
            # transient 503 that nobody caused.
            #
            # Two locks, because the two failure modes differ. The flag is the one
            # that cannot be bypassed: whatever a caller does with this exception,
            # the store now refuses to persist and refuses to mint, so the worst
            # case is a read-only bot rather than a wipe. Re-raising is what makes
            # it *loud* — `cogs/xp.cog_load` awaits this, so the bot declines to
            # start. A bot that will not start is recoverable in the time it takes
            # to restart it; a zeroed `users` collection is not.
            #
            # Whatever did stream is deliberately kept rather than discarded: with
            # writes blocked it can do no harm, and it is the only evidence of how
            # far the read got.
            self._load_failed = True
            self._loaded = False
            log.critical(
                "Failed to load the global wallet from Firestore after %d record(s): "
                "%s. Refusing to continue: writes and record creation are blocked so "
                "this process cannot overwrite the real records with an empty one.",
                total, exc)
            raise

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
                        rec = _default_user(opening_balance=False)
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
        if not self._loaded:
            # Every write below is a whole-document `batch.set`. Unless the boot read
            # completed we do not know what those documents hold, so the only safe
            # number of writes is zero — see load(). The dirty set is left intact:
            # the records stay inspectable and nothing silently claims to have saved.
            #
            # The test is `not _loaded`, NOT `_load_failed`. They differ in exactly
            # the state that matters: a load that never *ran* (an import error in
            # `cogs.xp`, say, before `store.load()` is awaited) leaves `_load_failed`
            # False and `_users` empty — and `bot.close()` calls `save_if_dirty()` on
            # the way out, so the startup gate's own shutdown would have flushed
            # zeroed records over real ones. A failed load and an absent one are the
            # same fact here: memory is not a safe source of truth.
            log.critical("Refusing to flush %d user record(s): the wallet was never "
                         "loaded%s, so memory is not a safe source of truth for a "
                         "full-document write.",
                         len(self._dirty_users),
                         " (the boot read failed)" if self._load_failed else "")
            return
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
            log.error("Failed to save to Firestore in one batch: %s", exc, exc_info=True)
            # One record can sink the whole batch — a value Firestore's encoder
            # refuses (an int past int64) raises before anything is sent, and a
            # batch is all-or-nothing anyway. Re-queueing all of `dirty` and
            # retrying the same batch next cycle meant that from the moment one
            # bad record existed, no user's XP or balance was persisted again
            # until a restart dropped everything unsaved. So retry one document
            # at a time: the good ones land now, and only the bad one stays
            # dirty — named in the log, since a record that never saves is a
            # bug report rather than a symptom to be inferred from silence.
            failed = await asyncio.to_thread(self._save_each, dirty)
            if failed:
                async with self._lock:
                    self._dirty_users.update(failed)

    def _save_each(self, user_ids: list[str]) -> list[str]:
        """Write each record on its own; return the ids that still failed."""
        if not self._loaded:
            return list(user_ids)   # same refusal as save(); nothing may be written
        failed: list[str] = []
        for user_id in user_ids:
            user_data = self._users.get(user_id)
            if user_data is None:
                continue
            try:
                with guard.final_flush():
                    _db.collection("users").document(user_id).set(user_data)
            except Exception as exc:
                log.error("User record %s cannot be saved (kept dirty): %s", user_id, exc)
                failed.append(user_id)
        if len(failed) < len(user_ids):
            log.info("Saved %d of %d user records individually", len(user_ids) - len(failed), len(user_ids))
        return failed

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
            if self._load_failed:
                # Do not mint. A zeroed default is indistinguishable from a real
                # new player, and marking it dirty is exactly how it would reach
                # Firestore over the top of somebody's real record. Hand back a
                # detached default instead: reads degrade to zeros, mutations go
                # nowhere, and nothing is remembered or written.
                return _default_user(opening_balance=False)
            self._users[key] = _default_user()
            # Only queue it if the wallet is actually loaded. `load()` merges into
            # `_users` rather than replacing it, so a record minted before a load
            # would survive one (`/reload cogs.xp` re-runs it on a live bot) and be
            # flushed over the real document afterwards. Minting into memory is
            # harmless — reads degrade to zeros — but remembering it is not.
            if self._loaded:
                self._mark_dirty(guild_id, user_id)
        return self._users[key]

    def has_user(self, user_id) -> bool:
        """Whether a record already exists — WITHOUT creating one.

        `get_user` creates a default record as a side effect, so it can never
        answer this. The whole collection is loaded at boot, so the in-memory dict
        is the authority here and no read is needed.
        """
        return str(user_id) in self._users

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
            # What others owed this account goes with it. Left in place, the next
            # garnish aimed at the deleted id would either re-mint its record (the
            # bug) or skim a debtor for a creditor who can no longer be paid.
            for other_id, other in self._users.items():
                debts = other.get("debts") or []
                kept = [d for d in debts if str(d.get("creditor_id") or "") != ukey]
                if len(kept) != len(debts):
                    other["debts"] = kept
                    self._mark_dirty(guild_id, other_id)
                    log.info("Forgave %s's debt to deleted account %s", other_id, ukey)
        try:
            _db.collection("users").document(ukey).delete()
        except Exception as exc:
            log.error("Failed to delete user %s from Firestore: %s", ukey, exc)
            raise
        log.warning("Deleted global user record %s (existed=%s)", ukey, existed)
        return existed

    # ── Preferences ──────────────────────────────────────────────────────────
    #
    # Account settings that only the *server* can act on, so they cannot live in
    # the mod's settings.cfg: the @-mention on a corp post is added by the bot, and
    # a file on the player's disk has no say in it. They ride the user record for
    # the same reason the debt ledger does — one document, one flush, and no extra
    # Firestore operation for a value read on every delivery.

    # ── Player-contract XP history ───────────────────────────────────────────

    def contract_xp_log(self, guild_id: int, user_id: int,
                        window_seconds: float) -> list[dict]:
        """Player-issued contracts this user completed as contractor inside the
        window: copies of [{"peer": issuer id, "t": unix time}]."""
        cutoff = time.time() - max(0.0, window_seconds)
        return [dict(e) for e in (self.get_user(guild_id, user_id).get("contract_xp_log") or [])
                if float(e.get("t", 0) or 0) >= cutoff]

    def last_contract_xp_at(self, guild_id: int, user_id: int) -> float:
        return float(self.get_user(guild_id, user_id).get("last_contract_xp_at") or 0.0)

    async def note_contract_completion(self, guild_id: int, user_id: int, peer_id,
                                       *, xp_granted: int, window_seconds: float,
                                       now: float | None = None) -> None:
        """Record that `user_id` completed a player-issued contract from `peer_id`,
        and — if XP was paid for it — start the cooldown. Entries older than the
        window are dropped here, so the list stays bounded by what one player can
        actually complete in a day."""
        now = time.time() if now is None else now
        cutoff = now - max(0.0, window_seconds)
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            log_ = [e for e in (user.get("contract_xp_log") or [])
                    if float(e.get("t", 0) or 0) >= cutoff]
            log_.append({"peer": str(peer_id), "t": round(now, 3), "xp": int(xp_granted)})
            user["contract_xp_log"] = log_
            if xp_granted > 0:
                user["last_contract_xp_at"] = round(now, 3)
            self._mark_dirty(guild_id, user_id)

    async def claim_contract_xp(self, guild_id: int, user_id, peer_id, *,
                                candidate_xp: int, cooldown_seconds: float,
                                daily_max: int, pair_free: int,
                                window_seconds: float,
                                now: float | None = None) -> tuple[int, str, bool]:
        """Atomically DECIDE the gated XP for a player-issued contract completion
        and RECORD it, in one critical section. Returns (granted_xp, gate,
        flag_pair) — the same triple `rewards.human_contract_xp` used to compute
        itself from lock-free reads followed by a separate locked write.

        That read-decide-then-write straddled the lock, so two concurrent
        `review`s for a colluding pair could each read the pre-write state and
        both pass the cooldown/daily/pair gate — an XP (and, through the level-up
        reward, coin) mint. It was not exploitable while nothing awaited between
        the read and the write, but that was an accident of the call graph, not a
        guarantee (see contract_actions.contract_lock). Folding both into this
        one lock is the mirror of `try_claim_timed_reward`'s double-spend guard.
        The gate *policy* stays in rewards; only its atomicity lives here.
        """
        # Imported lazily: rewards imports store at module load, so a top-level
        # import here would be circular. By call time rewards is fully loaded.
        from rewards import XP_GATE_COOLDOWN, XP_GATE_DAILY, XP_GATE_PAIR
        now = time.time() if now is None else now
        cutoff = now - max(0.0, window_seconds)
        a, b = str(user_id), str(peer_id)
        xp = int(candidate_xp)
        gate = ""
        flag_pair = False
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            my_log = [e for e in (user.get("contract_xp_log") or [])
                      if float(e.get("t", 0) or 0) >= cutoff]
            if xp > 0 and cooldown_seconds:
                last_at = float(user.get("last_contract_xp_at") or 0.0)
                if now - last_at < cooldown_seconds:
                    gate = XP_GATE_COOLDOWN
            if xp > 0 and not gate and daily_max > 0:
                earned = sum(int(e.get("xp", 0) or 0) for e in my_log)
                if earned >= daily_max:
                    gate = XP_GATE_DAILY
                else:
                    xp = min(xp, daily_max - earned)
            if pair_free > 0:
                peer_rec = self.get_user(guild_id, peer_id)
                peer_log = [e for e in (peer_rec.get("contract_xp_log") or [])
                            if float(e.get("t", 0) or 0) >= cutoff]
                seen = (sum(1 for e in my_log if str(e.get("peer")) == b)
                        + sum(1 for e in peer_log if str(e.get("peer")) == a))
                if seen >= pair_free:
                    # Flag on the first crossing only (see rewards).
                    flag_pair = seen == pair_free
                    if xp > 0 and not gate:
                        gate = XP_GATE_PAIR
            if gate:
                xp = 0
            # Record the completion in the SAME lock that decided it — this is
            # note_contract_completion inlined so no await separates them.
            my_log.append({"peer": b, "t": round(now, 3), "xp": int(xp)})
            user["contract_xp_log"] = my_log
            if xp > 0:
                user["last_contract_xp_at"] = round(now, 3)
            self._mark_dirty(guild_id, user_id)
        return xp, gate, flag_pair

    def corp_pings_enabled(self, guild_id: int, user_id: int) -> bool:
        """Whether corp-channel deliveries should @-mention this player.

        Deliberately reads the dict directly instead of `get_user`: this is asked
        on every delivery, including ones aimed at an account with no record yet,
        and `get_user` would mint one as a side effect. Absent means on — the
        default the schema carries, and the safe answer for a delivery that
        someone is expected to reply to.
        """
        rec = self._users.get(str(user_id))
        return True if rec is None else bool(rec.get("corp_pings", True))

    async def set_corp_pings(self, guild_id: int, user_id: int, enabled: bool) -> bool:
        """Set the corp-ping preference. Returns the value now stored."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["corp_pings"] = bool(enabled)
            self._mark_dirty(guild_id, user_id)
            return user["corp_pings"]

    # ── XP operations ────────────────────────────────────────────────────────

    async def award_xp(
        self, guild_id: int, user_id: int, amount: int
    ) -> tuple[int, int, bool]:
        """Award earned XP. Returns (new_xp, new_level, leveled_up).

        The whole read-modify-write happens under the lock. That is the point of
        this method: the award sites used to do `set_xp(get_user()["xp"] + n)`,
        where the read sat OUTSIDE the lock `set_xp` then took, so two awards
        landing together silently lost one.

        There is deliberately no cooldown. Every caller is a discrete thing the
        player earned — a completed contract, an analysed screenshot — not a
        stream of chat messages to be damped, and dropping one would be dropping
        the reward for the work rather than throttling a farm.

        `set_xp` stays what it says it is: the admin setter.
        """
        if amount <= 0:
            user = self.get_user(guild_id, user_id)
            return user["xp"], user["level"], False

        self._require_writable()
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            old_level = user["level"]
            user["xp"] = min(user["xp"] + amount, settings.MAX_XP)
            new_level = level_from_xp(user["xp"])
            user["level"] = new_level
            self._mark_dirty(guild_id, user_id)

            return user["xp"], new_level, new_level > old_level

    async def set_xp(self, guild_id: int, user_id: int, amount: int) -> None:
        """Directly set a user's XP (admin use). Clamped into
        `[0, settings.MAX_XP]` — see the note on MAX_XP for why the ceiling
        exists; the caller reports the stored value, so a clamp is visible."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            user["xp"] = max(0, min(int(amount), settings.MAX_XP))
            user["level"] = level_from_xp(user["xp"])
            self._mark_dirty(guild_id, user_id)

    # ── Debt & garnishment ───────────────────────────────────────────────────
    #
    # An unpayable contract fine is not forgiven; the remainder is recorded as a debt
    # to the issuer and repaid out of a share of the debtor's later *earnings*. The
    # design notes that matter:
    #
    #  • **Earnings, not credits.** Garnishment is opt-in per call site
    #    (`garnishable=True`), never a blanket hook on `add_balance`. Roughly half of
    #    this codebase's credits are refunds and admin corrections — auction escrow
    #    coming back, a marketplace double-buy refund, an owner-console balance fix,
    #    and `/take`, which passes a *negative* amount. Skimming those would confiscate
    #    a player's own money. The default is therefore off, so a call site nobody
    #    flagged repays a debt slower rather than stealing from its owner.
    #
    #  • **The rate scales with the amount owed, not the number of creditors.** Owing
    #    two people a little is not worse than owing one person a lot, and a count-based
    #    rate is gameable from both ends — an issuer with an alt could split one
    #    contract in two to push a debtor into a higher bracket.
    #
    #  • **Splitting is pro-rata with a largest-remainder rule.** The wallet is an
    #    integer, so a naive share leaves dust that never clears and garnishes someone
    #    who has effectively paid; `DEBT_FORGIVE_BELOW` sweeps what is left.
    #
    #  • **A bot-issued contract has no creditor.** `_pay_issuer` already skips paying
    #    the bot ("no wallet to pay into"), so those debts carry `creditor_id == ""`:
    #    still collected, because the deterrent is the point, but paid to nobody.

    # ── Transaction ledger ───────────────────────────────────────────────
    #
    # Every movement of a wallet is recorded here, so a player can be shown what
    # they spent money on and what they earned it from. Four decisions shape it.
    #
    #  • **It lives on the user document**, as a capped list, rather than in a
    #    `transactions` subcollection. The store already buffers writes and flushes
    #    the whole record with `batch.set` every few minutes, so a ledger on the
    #    record rides that flush and costs **no additional Firestore write** — where
    #    a subcollection would cost one write per movement, on a project whose
    #    `cost_guard` exists because that bill is the thing being defended against.
    #    Reading a history is likewise the read the profile already does.
    #
    #  • **The list is a ring buffer and the totals are not.** A capped list is what
    #    keeps the document small (Firestore's limit is 1 MiB, and this record is
    #    rewritten in full on every flush), but a cap means old entries fall off the
    #    end — so a summary computed by summing the list would quietly start
    #    shrinking. `tx_totals` is therefore a set of running lifetime counters
    #    updated at the same moment, and it is what the summary and the category
    #    breakdown are built from; the list is only ever the recent detail.
    #
    #  • **The recorded amount is the delta that actually happened**, read off the
    #    balance either side of the change, never the amount the caller asked for.
    #    `add_balance` clamps at zero, so a deduction larger than the balance moves
    #    less than it was given — a ledger that recorded the request would fail to
    #    add up to the balance it claims to explain, which is the one property that
    #    makes it worth showing at all.
    #
    #  • **An untagged call is recorded, not dropped.** `category` defaults to
    #    OTHER rather than being required, so a call site nobody thought to tag
    #    shows up as an unexplained movement the player can still see, instead of
    #    a gap that makes the running total disagree with the wallet.
    #
    # Keys are short (`t`, `a`, `c`, `d`, `p`) because up to TX_MAX of them are
    # serialised into the user document on every save.

    @staticmethod
    def _record_locked(user: UserData, amount: int, category: str,
                       detail: str = "", counterparty: str = "") -> None:
        """Append one movement to `user`'s ledger and fold it into the totals.

        **Must be called with `self._lock` held** and *after* the balance has been
        changed, with `amount` being the delta that actually landed. A zero delta is
        not recorded: it explains nothing and would push a real entry off the end.
        """
        if not amount:
            return

        cat = str(category or TX_OTHER)
        entries = user.setdefault("tx", [])
        entries.append({
            "t": round(time.time(), 3),
            "a": int(amount),
            "c": cat,
            # Truncated rather than trusted: some details are player-supplied
            # (a craft name, a transfer note) and this is written to a document
            # with a size limit.
            "d": str(detail or "")[:TX_DETAIL_MAX],
            "p": str(counterparty or "")[:64],
        })
        if len(entries) > TX_MAX:
            del entries[:len(entries) - TX_MAX]

        totals = user.setdefault("tx_totals", {})
        bucket = totals.get(cat)
        if not isinstance(bucket, dict):
            bucket = {"in": 0, "out": 0, "n": 0}
            totals[cat] = bucket
        if amount > 0:
            bucket["in"] = int(bucket.get("in", 0)) + int(amount)
        else:
            bucket["out"] = int(bucket.get("out", 0)) + int(-amount)
        bucket["n"] = int(bucket.get("n", 0)) + 1

    def list_transactions(self, guild_id: int, user_id: int, *,
                          limit: int = 50, offset: int = 0,
                          category: str = "") -> list[dict]:
        """Recent movements, **newest first** — the order every UI renders.

        Stored oldest-first (an append is cheaper than an insert), so this reverses.
        Returns copies: a caller renders these, and must not be able to edit the
        ledger by editing what it was handed.
        """
        entries = self.get_user(guild_id, user_id).get("tx") or []
        if category:
            entries = [e for e in entries if str(e.get("c", "")) == category]
        entries = list(reversed(entries))
        offset = max(0, offset)
        limit = max(0, limit)
        return [dict(e) for e in entries[offset:offset + limit]]

    def transaction_count(self, guild_id: int, user_id: int,
                          category: str = "") -> int:
        """How many entries the ledger is *holding* — for paging the list.

        Not a lifetime count: that is `tx_totals[cat]["n"]`, which keeps counting
        after an entry has rolled off the end of the ring buffer.
        """
        entries = self.get_user(guild_id, user_id).get("tx") or []
        if category:
            return sum(1 for e in entries if str(e.get("c", "")) == category)
        return len(entries)

    def transaction_totals(self, guild_id: int, user_id: int) -> dict[str, dict]:
        """Lifetime in/out/count per category. Copies, for the same reason as above."""
        totals = self.get_user(guild_id, user_id).get("tx_totals") or {}
        out: dict[str, dict] = {}
        for cat, b in totals.items():
            if not isinstance(b, dict):
                continue
            out[str(cat)] = {
                "in": int(b.get("in", 0) or 0),
                "out": int(b.get("out", 0) or 0),
                "n": int(b.get("n", 0) or 0),
            }
        return out

    def transaction_series(self, guild_id: int, user_id: int,
                           days: int = 14) -> list[dict]:
        """Daily in/out/net over the last `days` days, oldest first — the graph.

        Built from the ring buffer, so it is only as complete as the buffer is: a
        very busy player's window can be shorter than `days`. Every day in the range
        is emitted, zeros included, because a bar chart that silently omits quiet
        days draws a misleading shape — the gaps are the information.

        Days are UTC calendar days, matching the timestamps as stored; no attempt is
        made to guess the player's timezone, which the server does not know.
        """
        days = max(1, min(365, days))
        now = time.time()
        # Midnight UTC today, then step back day by day. Integer arithmetic on the
        # epoch is exact here: UTC has no DST, so every day is 86400s long.
        day_len = 86400
        today_start = (int(now) // day_len) * day_len
        first_start = today_start - (days - 1) * day_len

        buckets = {first_start + i * day_len: {"in": 0, "out": 0} for i in range(days)}
        for e in self.get_user(guild_id, user_id).get("tx") or []:
            try:
                ts = float(e.get("t", 0) or 0)
                amount = int(e.get("a", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ts < first_start or not amount:
                continue
            key = (int(ts) // day_len) * day_len
            b = buckets.get(key)
            if b is None:
                continue
            if amount > 0:
                b["in"] += amount
            else:
                b["out"] += -amount

        return [
            {
                "day": datetime.fromtimestamp(k, tz=timezone.utc).strftime("%Y-%m-%d"),
                "ts": k,
                "in": v["in"],
                "out": v["out"],
                "net": v["in"] - v["out"],
            }
            for k, v in sorted(buckets.items())
        ]

    @staticmethod
    def _garnish_percent(total_debt: int) -> int:
        """The share of an earning taken, scaled by how much is owed in total."""
        base = max(0, min(100, settings.DEBT_GARNISH_PERCENT))
        top = max(base, min(100, settings.DEBT_GARNISH_PERCENT_MAX))
        at = max(1, settings.DEBT_GARNISH_ESCALATE_AT)
        return top if total_debt >= at else base

    @staticmethod
    def _debt_total(user: UserData) -> int:
        return sum(max(0, int(d.get("amount", 0))) for d in user.get("debts") or [])

    def _garnish_locked(self, guild_id: int, user: UserData, user_id: int,
                        gross: int) -> list[tuple[str, int]]:
        """Take the garnishable share of `gross` off `user` and pay their creditors.

        **Must be called with `self._lock` held**, and does not take it itself — the
        skim has to be part of the same critical section as the credit that triggered
        it, or a concurrent spend takes the coins in between. Creditor records are
        mutated directly for the same reason (`add_balance` would deadlock on the
        non-reentrant lock). Returns the (creditor_id, amount) pairs actually paid so
        the caller can tell the player what happened.
        """
        debts = user.get("debts") or []
        # A creditor with no record ran the delete-my-data flow. Paying them through
        # `get_user` MINTED a `users/{id}` document for the leaver — holding a balance,
        # marked dirty, flushed by the next auto-save — every time a debtor earned.
        # Their claim leaves with them (`delete_user` forgives it at the moment of
        # deletion; this catches a record that vanished by any other route), for
        # the same reason a bot's fine pays nobody: there is no wallet on the other
        # side. Bot debts (empty creditor) are kept — collected and paid to no one.
        gone = [d for d in debts if d.get("creditor_id")
                and not self.has_user(str(d["creditor_id"]))]
        if gone:
            debts = user["debts"] = [d for d in debts if d not in gone]
            self._mark_dirty(guild_id, user_id)
            log.info("Forgave %s's debts to deleted accounts: %s", user_id,
                     [(str(d.get("creditor_id")), int(d.get("amount", 0))) for d in gone])
        total = self._debt_total(user)
        if gross <= 0 or total <= 0:
            return []

        rate = self._garnish_percent(total)
        take = min(total, user["balance"], (gross * rate) // 100)
        if take <= 0:
            return []

        # Pro-rata by amount owed, largest-remainder for the rounding dust. Sorted by
        # (remainder, amount, creditor) so the same inputs always split the same way.
        live = [d for d in debts if int(d.get("amount", 0)) > 0]
        shares: list[tuple[dict, int, int]] = []
        for d in live:
            exact = take * int(d["amount"])
            shares.append((d, exact // total, exact % total))
        assigned = sum(w for _, w, _ in shares)
        for d, _, _ in sorted(shares, key=lambda x: (-x[2], -int(x[0]["amount"]),
                                                     str(x[0].get("creditor_id", "")))):
            if assigned >= take:
                break
            for i, (dd, whole, rem) in enumerate(shares):
                if dd is d:
                    shares[i] = (dd, whole + 1, rem)
                    break
            assigned += 1

        paid: list[tuple[str, int]] = []
        for d, whole, _ in shares:
            if whole <= 0:
                continue
            cid = str(d.get("creditor_id") or "")
            # A bot-issued fine is collected but paid to nobody — there is no wallet.
            # The creditor id is used as the string it is: a website issuer's id is
            # `a_<uid>`, and `get_user` keys on `str()` anyway. The debt is decremented
            # only once the creditor has actually been credited, so nothing that
            # fails in between can write a coin of debt off unpaid.
            if cid:
                creditor = self.get_user(guild_id, cid)
                creditor["balance"] = max(0, creditor["balance"] + whole)
                self._mark_dirty(guild_id, cid)
                # The creditor's side. Recorded here rather than left to the caller
                # because this is the only place that knows a payment happened at
                # all — the creditor is not the one making the call, and money
                # arriving in a wallet with nothing in the ledger to explain it is
                # exactly the "the economy is broken" bug report this file's debt
                # section exists to avoid.
                self._record_locked(creditor, whole, TX_FINE_RECEIVED,
                                    "Garnished from an unpaid fine", str(user_id))
            d["amount"] = max(0, int(d["amount"]) - whole)
            paid.append((cid, whole))

        user["balance"] = max(0, user["balance"] - take)

        # The debtor's side, one entry per creditor paid rather than one for the
        # total: debts are merged per creditor, so the count stays small, and a
        # player repaying two people needs to see which of them this went to.
        for cid, whole in paid:
            self._record_locked(user, -whole, TX_DEBT_REPAYMENT,
                                "Repaid out of earnings" if cid
                                else "Fine repayment (no creditor)", cid)

        # Drop settled entries, and forgive dust: a debt of a coin or two would
        # otherwise garnish someone who has paid, forever.
        floor = max(0, settings.DEBT_FORGIVE_BELOW)
        user["debts"] = [d for d in debts if int(d.get("amount", 0)) > floor]
        self._mark_dirty(guild_id, user_id)
        return paid

    def garnish_percent(self, guild_id: int, user_id: int) -> int:
        """The share of this user's earnings currently going to their creditors.
        0 when they owe nothing — the UIs use it to decide whether to say anything."""
        total = self.debt_total(guild_id, user_id)
        return self._garnish_percent(total) if total > 0 else 0

    def debt_total(self, guild_id: int, user_id: int) -> int:
        """Total unpaid fine debt. Synchronous, like the other reads."""
        return self._debt_total(self.get_user(guild_id, user_id))

    def list_debts(self, guild_id: int, user_id: int) -> list[dict]:
        """Copy of the debt ledger, oldest first — safe for a caller to render."""
        return [dict(d) for d in (self.get_user(guild_id, user_id).get("debts") or [])
                if int(d.get("amount", 0)) > 0]

    async def add_debt(self, guild_id: int, user_id: int, creditor_id: str,
                       amount: int) -> int:
        """Record `amount` still owed to `creditor_id`. Returns the new debt total.

        Merged into the existing entry for that creditor rather than appended, so a
        repeat offender owes one growing sum per person instead of a ledger that grows
        without bound. `creditor_id` may be "" for a bot-issued contract.
        """
        if amount <= 0:
            return self.debt_total(guild_id, user_id)
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            debts = user.setdefault("debts", [])
            cid = str(creditor_id or "")
            for d in debts:
                if str(d.get("creditor_id") or "") == cid:
                    d["amount"] = int(d.get("amount", 0)) + amount
                    break
            else:
                debts.append({"creditor_id": cid, "amount": int(amount)})
            self._mark_dirty(guild_id, user_id)
            return self._debt_total(user)

    async def clear_debts(self, guild_id: int, user_id: int) -> int:
        """Wipe a user's debt ledger (moderator/owner correction). Returns what was
        written off, so the action can be logged with a number."""
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            wiped = self._debt_total(user)
            user["debts"] = []
            self._mark_dirty(guild_id, user_id)
            return wiped

    async def add_balance(self, guild_id: int, user_id: int, amount: int, *,
                          garnishable: bool = False,
                          category: str = TX_OTHER, detail: str = "",
                          counterparty: str = "") -> int:
        """Add (or subtract) from a user's balance. Returns new balance.

        NOTE: use this only for credits (refunds, payouts) or deductions that are
        already known to be covered. For a spend that must not overdraw, use
        `try_debit` — `add_balance` clamps at 0, so a too-large deduction silently
        vanishes instead of failing, which a concurrent caller can exploit to spend
        coins they don't have (TOCTOU double-spend).

        `garnishable=True` marks this credit as **earnings**, from which an unpaid
        fine debt is repaid. Pass it only for money the player earned — a payout, a
        sale, a reward — never for a refund of their own coins or an admin correction.
        The skim happens under the same lock as the credit. Use `add_balance_gross`
        when the caller needs to report what was taken.
        """
        new_balance, _ = await self.add_balance_gross(
            guild_id, user_id, amount, garnishable=garnishable,
            category=category, detail=detail, counterparty=counterparty)
        return new_balance

    async def add_balance_gross(self, guild_id: int, user_id: int, amount: int, *,
                                garnishable: bool = False,
                                category: str = TX_OTHER, detail: str = "",
                                counterparty: str = "") -> tuple[int, list[tuple[str, int]]]:
        """`add_balance`, additionally returning what garnishment took.

        The second element is the (creditor_id, amount) pairs paid out of this credit,
        so a caller can say "+10 (−5 to debt)" rather than leaving the player to notice
        that their reward silently halved. An invisible skim reads as the economy being
        broken and arrives as a bug report instead of an appeal.
        """
        self._require_writable()
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            # The ledger records what actually moved, and `max(0, …)` means a
            # deduction bigger than the balance moves less than it was given.
            before = user["balance"]
            user["balance"] = max(0, before + amount)
            self._mark_dirty(guild_id, user_id)
            self._record_locked(user, user["balance"] - before, category,
                                detail, counterparty)
            paid: list[tuple[str, int]] = []
            if garnishable and amount > 0:
                # Records its own debtor-side and creditor-side entries: the skim
                # is a second movement of this wallet, not a smaller version of
                # the credit above, and showing it as one would hide the debt.
                paid = self._garnish_locked(guild_id, user, user_id, amount)
            return user["balance"], paid

    async def try_claim_timed_reward(
        self, guild_id: int, user_id: int, key: str,
        amount: int, cooldown_seconds: float, *, garnishable: bool = False,
        category: str = TX_REWARD, detail: str = "",
    ) -> tuple[bool, float]:
        """Credit `amount` KCoins for `key` at most once per `cooldown_seconds`.

        Returns (granted, seconds_until_next). On a refusal nothing is written and
        the remaining wait is returned so the caller can say when it reopens. The
        cooldown check and the credit happen under one lock, so two uploads landing
        together can't both collect — the mirror of `try_debit`'s double-spend guard.

        Use `try_claim_timed_reward_gross` when the caller prints the amount: a
        garnishable reward can be skimmed on the way in, and a caller that reports
        `amount` is telling a debtor "+300" while they receive 75.
        """
        granted, wait, _paid = await self.try_claim_timed_reward_gross(
            guild_id, user_id, key, amount, cooldown_seconds,
            garnishable=garnishable, category=category, detail=detail)
        return granted, wait

    async def try_claim_timed_reward_gross(
        self, guild_id: int, user_id: int, key: str,
        amount: int, cooldown_seconds: float, *, garnishable: bool = False,
        category: str = TX_REWARD, detail: str = "",
    ) -> tuple[bool, float, list[tuple[str, int]]]:
        """`try_claim_timed_reward`, additionally returning what garnishment took.

        The third element is the (creditor_id, amount) pairs paid out of this
        credit — the same shape `add_balance_gross` returns and the same one `/pay`
        and `/finance/send` already use to say how much was skimmed. The player
        actually received `amount - sum(a for _c, a in paid)`.

        The split exists for the reason the whole debt design does: a reward that
        silently halves arrives as a bug report rather than as an appeal. This is
        the implementation; the two-value form above is a wrapper for the callers
        that never print a figure.
        """
        self._require_writable()
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            # Records written before this field existed are merged over the defaults
            # at load, so the dict is present; setdefault covers a hand-edited doc.
            stamps = user.setdefault("reward_cooldowns", {})
            now = time.time()
            elapsed = now - float(stamps.get(key, 0.0) or 0.0)
            if elapsed < cooldown_seconds:
                return False, cooldown_seconds - elapsed, []

            stamps[key] = now
            before = user["balance"]
            user["balance"] = max(0, before + amount)
            self._mark_dirty(guild_id, user_id)
            self._record_locked(user, user["balance"] - before, category,
                                detail or key)
            paid: list[tuple[str, int]] = []
            if garnishable and amount > 0:
                # Records its own debtor-side and creditor-side ledger entries,
                # exactly as in add_balance_gross — the skim is a second movement
                # of this wallet, not a smaller version of the credit above.
                paid = self._garnish_locked(guild_id, user, user_id, amount)
            return True, cooldown_seconds, paid

    async def try_debit(self, guild_id: int, user_id: int, amount: int, *,
                        category: str = TX_OTHER, detail: str = "",
                        counterparty: str = "") -> bool:
        """Atomically deduct `amount` only if the balance fully covers it.

        Returns True if the debit was applied, False on insufficient funds. The
        check and the deduction happen under one lock, so two concurrent requests
        can't both pass a balance check on the same funds and overdraw (the bug a
        separate get_user()+add_balance() pair has). A zero/negative amount is a
        no-op success. Never drives the balance below zero."""
        if amount <= 0:
            return True
        self._require_writable()
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            if user["balance"] < amount:
                return False
            user["balance"] -= amount
            self._mark_dirty(guild_id, user_id)
            self._record_locked(user, -amount, category, detail, counterparty)
            return True

    async def debit_up_to(self, guild_id: int, user_id: int, amount: int, *,
                          category: str = TX_OTHER, detail: str = "",
                          counterparty: str = "") -> int:
        """Atomically deduct up to `amount`, capped at the available balance.

        Returns the amount actually taken. For "take whatever they can pay" fines
        where a partial charge is intended; the read + deduction are atomic so the
        amount returned is exactly what left the account."""
        if amount <= 0:
            return 0
        self._require_writable()
        async with self._lock:
            user = self.get_user(guild_id, user_id)
            taken = min(amount, user["balance"])
            if taken > 0:
                user["balance"] -= taken
                self._mark_dirty(guild_id, user_id)
                self._record_locked(user, -taken, category, detail, counterparty)
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
