# ─────────────────────────────────────────────────────────────────────────────
#  settings.py – PUBLIC tunable settings (safe to commit, no secrets here)
#
#  Unlike .env, these are gameplay / balance values anyone can see.
#  Adjust these to tune the XP economy for your server.
# ─────────────────────────────────────────────────────────────────────────────

import os as _os


def _env_float(key: str, default: float) -> float:
    """Read a float from .env, falling back to the default below if unset/blank."""
    raw = _os.getenv(key, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


def _env_id(key: str) -> int | None:
    """Read a Discord snowflake from .env; None when unset/blank/unparseable."""
    raw = _os.getenv(key, "")
    try:
        return int(raw) if raw.strip() else None
    except ValueError:
        return None


# ── Blueprint render scale ───────────────────────────────────────────────────
#
# The KSP mod renders vessel "blueprint" images at a fixed base size of
# 2048×1100 px, multiplied by this scale (SCALE=2 → 4096×2200). The server uses
# this value to derive a tight per-file upload cap for blueprint/screenshot
# uploads (see MAX_BLUEPRINT_BYTES in api_server.py), so a tampered client can't
# pad a render to the generic 25 MB cap and spray oversized uploads at the API.
#
# IMPORTANT: keep this in sync with `const int SCALE` in
# KSP Mod Side/GeneKerman/VesselRenderer.cs. If you raise the mod's SCALE,
# raise this too or legitimate renders will be rejected as too large.
BLUEPRINT_SCALE = 2


# ── Cost Guard: paid-service spending caps ───────────────────────────────────
#
# The bot leans on two paid Google services: Gemini (AI screenshot/mission
# analysis) and Firebase (Firestore + Storage). cost_guard.py meters estimated
# monthly spend on each — to a LOCAL file, never to Firestore — and cuts a
# service off once its monthly budget is hit. Budgets reset on the 1st (UTC).
#
#   • Gemini over budget  → SOFT degrade: AI calls fall back to heuristics /
#     "temporarily disabled". The bot keeps running normally.
#   • Firebase over budget → HARD stop: every Firestore read/write and Storage
#     transfer raises, so the bot stops persisting until the budget resets.
#
# Set a budget to 0 (or negative) to mean "unlimited" — that service is never
# capped. The values below are the defaults; each can be overridden in .env.
COST_GUARD_ENABLED: bool = _os.getenv("COST_GUARD_ENABLED", "true").lower() not in ("false", "0", "no", "off")

# Monthly budgets in USD (0 = unlimited).
GEMINI_MONTHLY_BUDGET_USD: float = _env_float("GEMINI_MONTHLY_BUDGET_USD", 5.0)
FIREBASE_MONTHLY_BUDGET_USD: float = _env_float("FIREBASE_MONTHLY_BUDGET_USD", 10.0)

# Estimated unit prices used to convert usage → USD. These are ESTIMATES (Google
# prices vary by region/tier); tune them in .env to match your billing reality.
# Gemini token prices are per 1,000,000 tokens.
GEMINI_INPUT_USD_PER_1M: float = _env_float("GEMINI_INPUT_USD_PER_1M", 0.10)
GEMINI_OUTPUT_USD_PER_1M: float = _env_float("GEMINI_OUTPUT_USD_PER_1M", 0.40)
# Firestore operation prices, per 100,000 operations.
FIRESTORE_READ_USD_PER_100K: float = _env_float("FIRESTORE_READ_USD_PER_100K", 0.06)
FIRESTORE_WRITE_USD_PER_100K: float = _env_float("FIRESTORE_WRITE_USD_PER_100K", 0.18)
FIRESTORE_DELETE_USD_PER_100K: float = _env_float("FIRESTORE_DELETE_USD_PER_100K", 0.02)
# Firebase Storage prices, per gigabyte (download = egress, the usual cost driver).
STORAGE_DOWNLOAD_USD_PER_GB: float = _env_float("STORAGE_DOWNLOAD_USD_PER_GB", 0.12)
STORAGE_UPLOAD_USD_PER_GB: float = _env_float("STORAGE_UPLOAD_USD_PER_GB", 0.0)


# ── Moderation & Roles ───────────────────────────────────────────────────────

# Role ID that grants access to moderation commands (/kick, /ban, /gk setchannel, etc.)
# If set to None, users must have Discord's built-in Kick Members or Admin permissions.
MOD_ROLE_ID: int | None = 1492234876273823916

# Role notified when an in-game bug report opens a ticket. A bug report is for
# whoever maintains the bot and the KSP mod, which is not the same team as
# moderation — so it is a separate role, and the mods are deliberately NOT pinged
# for one. Read from .env because that team differs per deployment; it can also be
# mapped per guild with `/admin setrole`. Unset (or unresolvable in the guild)
# falls back to pinging MOD_ROLE_ID — an unread report is worse than one that
# reached the wrong inbox.
BUG_REPORT_ROLE_ID: int | None = _env_id("BUG_REPORT_ROLE_ID")

# ── Tickets ──────────────────────────────────────────────────────────────────

# Private support/report tickets. The panel channel holds a persistent "Open a
# Ticket" button; each ticket becomes a private channel under the category below,
# visible only to the filer + mods (MOD_ROLE_ID). Device-sharing reports and
# contract "sue" escalations also open as tickets here. Set either to None to
# disable the ticket system (flows then fall back to CONTRACT_MOD_CHANNEL_ID).
TICKET_CATEGORY_ID: int | None = 1518238099505680516
TICKET_PANEL_CHANNEL_ID: int | None = 1518238266686443660

# ── XP System ────────────────────────────────────────────────────────────────

# XP awarded per qualifying message
XP_PER_MESSAGE = 15

# Random bonus range added on top (0 = no randomness)
XP_BONUS_MIN = 0
XP_BONUS_MAX = 10

# Cooldown in seconds between XP-eligible messages (prevents spam farming)
XP_COOLDOWN_SECONDS = 45

# XP multiplier for server boosters (2.0 = double XP)
BOOSTER_XP_MULTIPLIER = 2.0

# Channels where XP is NOT awarded (by channel ID)
# Example: XP_BLACKLISTED_CHANNELS = [123456789, 987654321]
XP_BLACKLISTED_CHANNELS: list[int] = []

# ── Leveling ─────────────────────────────────────────────────────────────────

# Formula: XP needed for level N = BASE * (N ^ EXPONENT)
LEVEL_XP_BASE = 100
LEVEL_XP_EXPONENT = 1.5

# Whether to announce level-ups in the channel where it happened
ANNOUNCE_LEVEL_UP = True

# Optional: dedicated channel ID for level-up announcements (None = same channel)
LEVEL_UP_CHANNEL_ID: int | None = None

# ── Economy ──────────────────────────────────────────────────────────────────

# Starting balance for new users
STARTING_BALANCE = 0
CURRENCY_NAME = "KCoins"
CURRENCY_SYMBOL = "<:KCoin:1510200111253291258>"

# Public URLs for the Terms of Service / Privacy Policy, shown by /privacy.
# Leave blank to show only the in-message summary.
TERMS_URL = ""
PRIVACY_URL = ""

# KCoins awarded per level-up
LEVEL_UP_REWARD = 200

# Minimum transfer amount for /pay
MIN_TRANSFER = 1

# ── Leaderboard ──────────────────────────────────────────────────────────────

# How many users to show on the leaderboard
LEADERBOARD_PAGE_SIZE = 10

# ── Screenshot Rewards ───────────────────────────────────────────────────────

# XP awarded per difficulty point (e.g. difficulty 7 × 50 = 350 XP)
SCREENSHOT_XP_PER_DIFFICULTY = 50

# KCoins awarded per difficulty point (e.g. difficulty 7 × 18 = 126 KCoins)
SCREENSHOT_COINS_PER_DIFFICULTY = 18

# Per-user rate limit on /analyze (each call is a paid Gemini request drawn from
# the shared monthly budget). At most SCREENSHOT_RATELIMIT_RATE calls per
# SCREENSHOT_RATELIMIT_PER seconds per user; further calls are rejected until the
# window clears. Stops one user from draining everyone's AI budget.
SCREENSHOT_RATELIMIT_RATE = 3
SCREENSHOT_RATELIMIT_PER = 60.0

# Anti-cheat: extreme-rate flood detection on authenticated, cost/reward-bearing
# API endpoints. These are far above any human play rate, so crossing one is a
# strong scripted-abuse signal that opens a (deduped) moderator ticket via the
# suspicion system. Tuple = (max actions, window seconds) per user.
FLOOD_SUBMIT = (12, 60.0)        # contract submissions
FLOOD_ACHIEVEMENT = (20, 60.0)   # achievement-photo captures

# ── Corporations ─────────────────────────────────────────────────────────────

# Discord category ID where corp channels are created
CORP_CATEGORY_ID = 1492379906925924352

# ── Contracts ────────────────────────────────────────────────────────────────

# Max active contracts a user can have at once (as either issuer or contractor)
MAX_ACTIVE_CONTRACTS_PER_USER = 10

# Channel ID where mod escalations ("sue" button) are posted.
# Set to None to disable suing. Must be set for the sue flow to work.
CONTRACT_MOD_CHANNEL_ID: int | None = 1513934242315374744

# Allow users to send contracts to themselves (for testing only!)
CONTRACT_ALLOW_SELF = False

# ── Disputes ─────────────────────────────────────────────────────────────────
# A refused submission puts the contract in dispute, where the contractor chooses how
# to resolve it. Left alone it would sit there forever — which is a free way to dodge
# the fine, since nothing else in the system ever closes it. So the fine collects
# itself this many days after the dispute opened.
#
# The clock is absolute from the moment the dispute opened and does NOT pause for a
# pending settle or extension request. Pausing it would hand back the same loophole
# through a different door: ask to settle, and stall for as long as the issuer takes
# to answer. The agreed penalty is the default outcome; everything else needs someone
# to actively agree to it in time.
DISPUTE_AUTO_FINE_DAYS = 3

# A contractor gets one deadline-extension request per dispute. Without this they can
# keep asking with a new date every time one is refused, which is stalling by another
# name. Submitting again and being refused again opens a *new* dispute, which resets it.
DISPUTE_MAX_MORE_TIME_REQUESTS = 1

# ── Mod-only gameplay ─────────────────────────────────────────────────────────
# When True, gameplay commands that the in-game KSP mod can perform itself
# (screenshot analysis, player-to-player contracts) are disabled on Discord, so
# the actions can only be triggered from inside the game. Players who invoke
# them on Discord get an ephemeral notice pointing them to the mod. Gated
# commands: /analyze, /contract, /flagcontract.
MOD_ONLY_GAMEPLAY = False

# ── Auctions (reverse / Dutch) ───────────────────────────────────────────────
# An issuer posts a mission with a STARTING price (escrowed up front). Contractors
# bid the price DOWN; the lowest bid when the auction ends wins and is bound to an
# active contract for that amount. The leftover escrow is refunded to the issuer.
# Channel where auctions are posted. None disables the /auction command.
AUCTION_CHANNEL_ID: int | None = 1518305724667527198
# A new bid must undercut the current lowest by at least this many KCoins.
AUCTION_MIN_DECREMENT = 1
# Bids placed within this many seconds of the end push the end back by the same
# amount (anti-snipe). Set to 0 to disable.
AUCTION_ANTISNIPE_SECONDS = 60
# Bounds on how long an auction may run (hours).
AUCTION_MIN_DURATION_HOURS = 1
AUCTION_MAX_DURATION_HOURS = 168  # 7 days

# ── Marketplace ──────────────────────────────────────────────────────────────

# Channel where craft sale listings are posted. Set to None to disable the
# marketplace (listing attempts from the KSP mod will be rejected).
MARKETPLACE_CHANNEL_ID: int | None = 1515424482020429875

# Bounds on the price a seller may set for a listing (in KCoins).
MARKETPLACE_MIN_PRICE = 1
MARKETPLACE_MAX_PRICE = 10_000_000


# ── Weekly Missions ──────────────────────────────────────────────────────────

# Channel where the weekly missions embed is posted
WEEKLY_MISSIONS_CHANNEL_ID = 1510353237922938949

# Number of missions generated per week
WEEKLY_MISSIONS_COUNT = 20

# Rewards per difficulty point
WEEKLY_XP_PER_DIFFICULTY = 100
WEEKLY_COINS_PER_DIFFICULTY = 60

# Fine = 50% of money reward
WEEKLY_FINE_PERCENT = 50

# Allow mods to select missions even when the week is locked (e.g., Sundays)
WEEKLY_MISSIONS_MODS_IGNORE_LOCK = False

# ── Checkpoint Photos ────────────────────────────────────────────────────────

# Master switch for the auto-screenshot ("hero shot") feature. When False the
# server rejects all checkpoint photo uploads regardless of the channel below.
CHECKPOINT_PHOTOS_ENABLED = False

# Channel where milestone "hero shots" captured in-game (rendezvous, flyby,
# asteroid/comet) are posted. Set to None to disable — uploads from the KSP mod
# will then be rejected.
CHECKPOINT_PHOTOS_CHANNEL_ID: int | None = 1492244166418108467

# ── Data Persistence ─────────────────────────────────────────────────────────

# Path to the JSON data file (relative to project root)
DATA_FILE = "data/users.json"

# How often to auto-save in-memory data to disk (seconds)
AUTO_SAVE_INTERVAL = 300  # 5 minutes

# ── KSP Achievement Levels ───────────────────────────────────────────────────

# Mapping of level integers (1-15) to a tuple of (Role ID, Title Name, Description)
LEVEL_ROLES = {
    1:  (1492381704948551740, "Level-1", "Kerbin Orbit"),
    2:  (1492382379329851422, "Level-2", "Mun Landing"),
    3:  (1492382794498703551, "Level-3", "Docking (Space Stations are also considered to be on this level)"),
    4:  (1492382733769506876, "Level-4", "Duna Landing"),
    5:  (1492383069141864488, "Level-5", "RSS Earth Orbit"),
    6:  (1492384757139378197, "Level-6", "Eve Landing"),
    7:  (1492957576621719693, "Level-7", "Asteroid Redirect"),
    8:  (1492383446566310081, "Level-8", "RSS Moon Landing"),
    9:  (1492383547519012934, "Level-9", "Jool 5"),
    10: (1492383718357340362, "Level-10", "Interstellar Mission"),
    11: (1492383914851827874, "Level-11", "RSS Mars"),
    12: (1498035194760790108, "Level-12", "RSS Venus Landing"),
    13: (1492384267798450277, "Level-13", "RSS Gas Giant"),
    14: (1498035361564065892, "Level-14", "Kerbol Grand Tour to all planets at once"),
    15: (1492384471775707146, "Level-15", "RSS Interstellar Mission"),
}

# ── KSP Mod Integration ──────────────────────────────────────────────────────

# How often the KSP mod should check for new notifications (seconds)
KSP_NOTIFICATION_CHECK_INTERVAL = 600  # 10 minutes

# API server port (should match API_PORT in .env)
KSP_API_PORT = 5022

# ── KSP link / 2FA brute-force rate limits ───────────────────────────────────
# Per-IP is the real brute-force defense: at 10/min over a code's 3-min life that
# is ~30 guesses against a 1,000,000-code space. The global cap is only a coarse
# backstop — keep it high enough that normal traffic on a shared public IP can
# never trip it, or one attacker flooding the endpoint locks every player out of
# linking (a self-inflicted DoS). 600/min is still <0.2% of the code space per
# code lifetime, so it costs nothing defensively.
KSP_LINK_RATELIMIT_PER_IP = 10       # link/2FA attempts per IP per minute
KSP_LINK_RATELIMIT_GLOBAL = 600      # global backstop per minute (anti self-DoS)

# ── KSP anti-exploit: flight-telemetry consistency ───────────────────────────
# The KSP client is untrusted: a modified DLL could report a vessel as "ORBITING
# Minmus" while it is really at Mun, to clear a contract it didn't complete. The
# orbital snapshot it sends is over-determined, though — apoapsis, periapsis, sma
# and eccentricity are bound by pure geometry (no GM needed, so these hold on any
# rescaled install). data/telemetry_check.py re-derives those identities on submit
# and catches a snapshot whose numbers don't add up. See data/telemetry_check.py.
#
# Mode controls what a violation does:
#   "reject_and_flag" – hard (impossible) violations reject the submission AND open
#                       a moderator suspicion; soft (body-radius) ones only flag.
#   "flag_only"       – never reject; record a suspicion for mods to review.
#   "reject_only"     – reject hard violations, but open no ticket (quieter for mods).
#   "off"             – disable the check entirely (equivalent to ENABLED = False).
TELEMETRY_CHECK_ENABLED = True
TELEMETRY_CHECK_MODE = "reject_and_flag"

# Relative tolerance on the Kepler geometry identity sma == r + (apo+peri)/2.
# KSP reports these consistent to many digits, so 2% is comfortably slack for an
# honest client while still catching a hand-edited field.
TELEMETRY_SMA_TOLERANCE = 0.02
# Absolute tolerance on the eccentricity identity (eccentricity is itself a small
# 0..1 number, so an absolute band is the natural comparison).
TELEMETRY_ECC_TOLERANCE = 0.05
# Fractional mismatch between the claimed body's catalogued radius and the radius
# the client reports, above which we SOFT-flag a possible body spoof. Generous on
# purpose: legitimate rescale packs (RSS ≈10×, 2.5×, …) change radii a lot, so this
# only ever flags (never rejects) and only when the gap is large.
TELEMETRY_BODY_RADIUS_TOLERANCE = 0.5

# ── Orbit-type ("orbital regime") enforcement ─────────────────────────────────
# A contract whose mission text names a specific orbit (polar, equatorial,
# keostationary, Molniya, …) is verified against the craft's reported orbital
# elements at submit time, on top of the body/situation gate — see
# data/orbit_constraints.py. Pure client-reported values like Δv, backstopped by
# the telemetry-consistency check above.
ORBIT_CHECK_ENABLED = True
# Inclination is reported in degrees (0..180); the bands below are absolute degrees.
ORBIT_POLAR_INCL_TOL = 10.0       # |i - 90| ≤ this counts as polar
ORBIT_EQUATORIAL_INCL_TOL = 5.0   # i ≤ this (or ≥ 180 - this) counts as equatorial
ORBIT_INCLINED_MARGIN = 1.0       # prograde i < 90 - margin; retrograde i > 90 + margin
ORBIT_CIRCULAR_ECC_TOL = 0.05     # e ≤ this counts as circular
ORBIT_ELLIPTIC_ECC_MIN = 0.20     # e ≥ this counts as elliptical / eccentric
ORBIT_SYNC_PERIOD_TOL = 0.05      # |T - factor·T_body| / target ≤ this counts as synchronous
ORBIT_FROZEN_INCL = 63.4          # Molniya / Tundra critical ("frozen") inclination
ORBIT_FROZEN_INCL_TOL = 5.0
ORBIT_MOLNIYA_ECC_MIN = 0.50
ORBIT_TUNDRA_ECC_MIN = 0.20

# ── Rescue target: orbital plane ─────────────────────────────────────────────
# A rescue in "orbit" mode names an Ap/Pe the rescuer has to reach, which says
# nothing about the *plane* — the expensive half of a rendezvous. The issuer can
# additionally require an inclination (rescue_target.inc, degrees) with a tolerance
# (rescue_target.margin_inc). Absent / margin <= 0 == any plane, which is what every
# rescue issued before this existed meant.
RESCUE_INCL_MARGIN_DEFAULT = 5.0   # used when an inclination is set with no margin
RESCUE_INCL_MARGIN_MIN = 1.0       # floor, like the Ap/Pe margin: tighter is impossible

# ── Known Celestial Bodies ───────────────────────────────────────────────────
# Used by the heuristic mission classifier (fallback when Gemini is unavailable).
# The AI classifier handles any body name from text automatically — this list
# only matters when AI is down. Add any modded bodies your community uses here.
# Sorted roughly by distance from the star so the first match wins on short names
# (e.g. "Kerbin" matches before "Kerbol" would if listed after).
KNOWN_CELESTIAL_BODIES: list[str] = [
    # ── Stock Kerbol System ──────────────────────────────────────────────
    "Kerbol",
    "Moho",
    "Eve", "Gilly",
    "Kerbin", "Mun", "Minmus",
    "Duna", "Ike",
    "Dres",
    "Jool", "Laythe", "Vall", "Tylo", "Bop", "Pol",
    "Eeloo",
    # ── Outer Planets Mod (OPM) ──────────────────────────────────────────
    "Sarnus", "Hale", "Ovok", "Slate", "Tekto",
    "Urlum", "Polta", "Priax", "Wal", "Tal",
    "Neidon", "Thatmo", "Nissee",
    "Plock", "Karen",
    # ── Kcalbeloh System ─────────────────────────────────────────────────
    "Kcalbeloh",
    "Suluco", "Yeldo", "Noyreg", "Efil", "Otsol", "Ambrosh",
    "Doru", "Krul", "Iehus", "Cet", "Lond",
    # ── Real Solar System (RSS) / Real Exoplanets ────────────────────────
    "Sun", "Mercury", "Venus", "Earth", "Moon",
    "Mars", "Phobos", "Deimos",
    "Ceres",
    "Jupiter", "Io", "Europa", "Ganymede", "Callisto",
    "Saturn", "Titan", "Enceladus", "Rhea", "Dione", "Tethys", "Mimas",
    "Uranus", "Miranda", "Ariel", "Umbriel", "Titania", "Oberon",
    "Neptune", "Triton",
    "Pluto", "Charon",
    "Eris",
]
