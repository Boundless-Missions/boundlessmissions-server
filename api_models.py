"""
api_models.py – Pydantic models for KSP API request/response validation.
"""

from pydantic import BaseModel, Field
from typing import Optional


# ── Auth ─────────────────────────────────────────────────────────────────────

class LinkRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, description="6-digit link code from Discord")

class PollRequest(BaseModel):
    challenge_id: str = Field(..., description="Challenge id returned by the link step")

class LinkResponse(BaseModel):
    # status:
    #   "ok"                → linked, token populated.
    #   "approval_required" → poll /auth/link/poll with challenge_id; the user must
    #                         press the Log-in button in their Discord DM.
    #   "pending"           → still waiting on the user's approval; keep polling.
    status: str = "ok"
    token: str = ""
    username: str = ""
    guild_id: str = ""
    user_id: str = ""
    challenge_id: Optional[str] = None

class DeviceStatusResponse(BaseModel):
    # status: "pending" (keep polling) | "approved" (device trusted, resume) |
    #         "denied" (rejected) | "expired". On a denied report awaiting client
    #         diagnostics, report_id is set so the client uploads MAC + KSP.log.
    status: str = "pending"
    report_id: Optional[str] = None
    # True (once) when the owner pressed "🔔 Ping this PC" in their Discord DM, so
    # the blocked client should flash an on-screen "is this you?" alert.
    ping: bool = False

class AuthError(BaseModel):
    detail: str


# ── Attestation (challenge-response anti-tamper) ──────────────────────────────

class AttestChallenge(BaseModel):
    # enabled=False when no pristine DLL is stored server-side → client skips.
    # Otherwise the client must return SHA256(nonce_utf8 + dll_bytes[offset:offset+length]).
    enabled: bool = False
    attest_id: Optional[str] = None
    nonce: Optional[str] = None
    offset: int = 0
    length: int = 0

class AttestRespondRequest(BaseModel):
    attest_id: str
    digest: str

class AttestResult(BaseModel):
    ok: bool = False


# ── Version gate ─────────────────────────────────────────────────────────────

class VersionCheckResponse(BaseModel):
    # enabled:    False when the server's version gate is turned off (client must
    #             never block, regardless of up_to_date).
    # up_to_date: True when the client's DLL hash matches the published latest, or
    #             when no version has been published yet (fail-open).
    enabled: bool = True
    up_to_date: bool = True
    latest_version: Optional[str] = None
    # SHA256 of the published-latest GeneKerman.dll. Always returned (null only when
    # no version has been published yet) so a client can confirm exactly which build
    # the server expects.
    latest_hash: Optional[str] = None
    download_url: Optional[str] = None
    your_version: Optional[str] = None
    message: Optional[str] = None
    # Current Privacy Policy / Terms version the client must have accepted. When
    # this exceeds the version recorded in the client's consent.cfg, the mod
    # re-prompts the opt-in gate and stops transmitting until the player re-accepts.
    policy_version: Optional[int] = None


# ── User Profile ─────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    user_id: str
    username: str
    guild_id: str
    xp: int = 0
    level: int = 0
    balance: int = 0
    messages: int = 0
    unlocked_levels: list[int] = []
    currency_name: str = "KCoins"


# ── Missions ─────────────────────────────────────────────────────────────────

class Mission(BaseModel):
    id: int
    desc_en: str
    desc_tr: str
    difficulty: int
    category: str
    xp: int
    coins: int
    fine: int
    # AI-classified submission requirements (cached server-side)
    mission_type: str = "active_vessel"  # "craft_build" or "active_vessel"
    required_situation: Optional[str] = None  # KSP situation: ORBITING, LANDED, FLYING, etc.
    required_body: Optional[str] = None  # Celestial body: Kerbin, Mun, Duna, etc.

class WeeklyMissionsResponse(BaseModel):
    week_key: str
    missions: list[Mission]
    is_locked: bool
    closes_at: str  # ISO timestamp

class MissionSelectRequest(BaseModel):
    mission_id: int

class MissionSelectResponse(BaseModel):
    success: bool
    contract_id: Optional[str] = None
    message: str


# ── Contracts ────────────────────────────────────────────────────────────────

class RescueTarget(BaseModel):
    """Where stranded kerbals must be recovered from / delivered to.

    mode == "orbit"   → ap/pe define the target orbit (metres above the body
                        surface); margin_alt is the allowed +/- on each.
    mode == "surface" → lat/lon define the landing spot (degrees); margin_pos is
                        the allowed great-circle tolerance (degrees).
    is_modded is flagged by the issuer's client (it scans the real body list).

    `mode` is where; `recovery` is *what* has to get there:
        "crew"   → the stranded kerbals, aboard whatever ship brought them. The
                   wreck may be stripped, abandoned or destroyed.
        "vessel" → the crew and the wreck itself, towed or flown home. Verified by
                   part flightID (KSP keeps a part's uid across export, import and
                   docking), so the wreck stays identifiable however it gets back.
    min_dv is a floor in m/s on the delivering craft's remaining vacuum delta-v, so
    the crew are dropped somewhere they can actually leave from. 0 = no requirement.
    Both default to the pre-existing behaviour, which is what every rescue issued
    before these modes existed had.

    In "orbit" mode the issuer may also constrain the *shape and plane* of the
    target orbit, which ap/pe alone say nothing about:
        inc / margin_inc → required inclination in degrees, ± tolerance. margin_inc
                           <= 0 (the default) means any plane. Matching the wreck's
                           plane is the expensive half of a rendezvous, so this is
                           what turns "be at 100×100 km" into a real intercept.
        orbit_types      → canonical regime tokens ("polar", "equatorial",
                           "stationary", …) from data/orbit_constraints.REQUIREMENTS,
                           verified with the same tolerances as the ones parsed out
                           of mission text.
    """
    body: str
    mode: str = "orbit"  # "orbit" | "surface"
    ap: Optional[float] = None
    pe: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    margin_alt: float = 0.0
    margin_pos: float = 0.0
    is_modded: bool = False
    recovery: str = "crew"  # "crew" | "vessel"
    min_dv: float = 0.0
    # Orbit-plane / orbit-regime requirements (orbit mode only; see the docstring).
    inc: Optional[float] = None
    margin_inc: float = 0.0
    orbit_types: list[str] = []
    # flightIDs of the wreck's parts as handed over. Sent only to the rescuer, and
    # only on a "vessel" recovery — nobody else has anything to check them against,
    # and on a big craft this is the largest field on the contract.
    wreck_parts: list[str] = []


class PendingRequest(BaseModel):
    """An open ask from the contractor that the issuer has to answer.

    Settle and more-time change nothing until the issuer agrees. This is what makes
    that ask visible outside the Discord DM that used to be its only record, so the
    in-game UI can offer Approve/Refuse on the issuer's side.
    """
    kind: str                          # "settle" | "more_time"
    new_date: Optional[str] = None     # set for "more_time" (YYYY-MM-DD)
    requested_at: Optional[str] = None
    requested_by: Optional[str] = None


class ContractSummary(BaseModel):
    contract_id: str
    mission: str
    issuer_name: str
    contractor_name: str
    payment: int
    fine: int
    due_date: str
    status: str
    created_at: Optional[str] = None
    is_bot_issued: bool = False
    is_outgoing: bool = False  # True when the current user is the issuer (sent, not received)
    modlist: Optional[str] = None  # Comma-separated mod folder names from issuer's KSP client
    # Classification (from mission)
    mission_type: str = "active_vessel"
    required_situation: Optional[str] = None
    required_body: Optional[str] = None
    # Part-restriction ("mission limit") constraints extracted from the mission
    # text. Canonical schema lives in data/mission_constraints.py; the KSP client
    # enforces it in the editor and at submit. None == no restrictions.
    constraints: Optional[dict] = None
    # Flag-design contracts: watermarked preview shown before acceptance.
    flag_preview_url: Optional[str] = None
    # Rescue-mission fields (only set when mission_type == "rescue")
    rescue_target: Optional[RescueTarget] = None
    rescue_kerbals: list[str] = []  # renamed names the rescuer must recover
    is_modded_target: bool = False
    # Wreck snapshot URL — only set for the rescuer (contractor) on an accepted
    # rescue, so their client can spawn/respawn the stranded vessel on demand.
    rescue_vessel_node_url: Optional[str] = None
    # The issuer's local vessel GUID for the craft they handed over. Sent back only
    # to the issuer, and only so their client can verify the removal actually took:
    # the contract existing at all means the ship is no longer theirs, so a copy
    # still sitting in their save is a failed removal to be retried. Never sent to
    # the rescuer — another save's vessel id is no use to them.
    rescue_pid: Optional[str] = None
    # Life-support provisioning of the craft this contract is about: which LS mod its
    # supplies belong to and how long they last per kerbal. Set at creation for a rescue
    # (the wreck is scanned on the issuer's client) and at submission for everything
    # else. The rescuer's client compares life_support with its own install to decide
    # whether the wreck's supplies mean anything there.
    life_support: str = "none"
    ls_endurance_days: float = 0.0
    ls_crew_capacity: int = 0
    # Set on a disputed contract while the contractor is waiting on an answer.
    pending_request: Optional[PendingRequest] = None
    # When the fine collects itself if nobody resolves the dispute (ISO, UTC). Only set
    # while disputed. Sent as an instant rather than a countdown so the client does not
    # have to know the policy, and a clock that is a few minutes out cannot drift.
    auto_fine_at: Optional[str] = None
    # True once the contractor has used their one deadline-extension request for this
    # dispute. The UI hides the control; the server refuses it either way.
    more_time_used: bool = False

class ContractListResponse(BaseModel):
    contracts: list[ContractSummary]


class PartCatalogUpload(BaseModel):
    """The KSP client's full installed part list, used to resolve loosely-typed
    part mentions in mission limits to real parts. `hash` lets the client skip
    re-uploading an unchanged catalog."""
    hash: str
    parts: list[dict] = []  # each: {"name": <internal>, "title": <display>}


class PartCatalogResponse(BaseModel):
    success: bool
    stored: bool = False  # False == server already had this hash, upload skipped
    parts: int = 0

class ContractAcceptResponse(BaseModel):
    success: bool
    message: str
    # Set on rescue accept so the rescuer's client can spawn the wreck.
    rescue_vessel_node_url: Optional[str] = None
    rescue_target: Optional[RescueTarget] = None
    rescue_kerbals: list[str] = []


# ── Corporations ─────────────────────────────────────────────────────────────

class CorpInfo(BaseModel):
    owner_id: str
    owner_name: str
    corp_name: str
    # Extras for the mod's player picker. Optional/defaulted so older mod builds,
    # which parse this with MiniJSON and ignore unknown keys, are unaffected.
    avatar_url: Optional[str] = None
    level: int = 0

class CorpListResponse(BaseModel):
    corps: list[CorpInfo]

class ContractCreateRequest(BaseModel):
    contractor_id: str  # Corp owner's user ID
    mission: str = Field(..., min_length=3, max_length=500)
    payment: int = Field(..., gt=0)
    fine: int = Field(default=0, ge=0)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    modlist: Optional[str] = None  # Comma-separated list of loaded assembly names
    # "auto" keeps the existing AI classification; "craft_build" / "active_vessel"
    # force the type and skip AI. (Rescue contracts use the separate multipart
    # /contracts/create_rescue endpoint.)
    contract_type: str = "auto"


class AuctionCreateRequest(BaseModel):
    """Open a reverse (Dutch) auction from the KSP mod. No contractor — it's open
    to everyone in Discord; the lowest bidder when it ends is bound to the contract.
    start_value is escrowed up front; the leftover is refunded when it closes."""
    mission: str = Field(..., min_length=3, max_length=500)
    start_value: int = Field(..., gt=0)
    fine: int = Field(default=0, ge=0)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    duration_hours: int = Field(..., gt=0)
    modlist: Optional[str] = None  # mods required / limited to
    # craft_build / active_vessel / flag_design — inherited by the winner's contract.
    # Other values (or null) are ignored, leaving the contract untyped.
    contract_type: Optional[str] = None


class ContractReviewRequest(BaseModel):
    approve: bool  # True = accept the submission, False = refuse (→ dispute)


class ContractDisputeRequest(BaseModel):
    # Contractor's response to a refused submission, mirroring the Discord
    # DisputeView buttons.
    action: str  # "settle" | "more_time" | "pay_fine" | "sue"
    # Required for "more_time" on human-issued contracts (YYYY-MM-DD).
    new_date: Optional[str] = None


class ContractRequestResponse(BaseModel):
    """Issuer's answer to a pending settle / more-time request.

    Deliberately just a yes/no. The date being granted comes from the request stored on
    the contract, so approving means approving what was actually asked for.
    """
    approve: bool


# ── Web → game commands ──────────────────────────────────────────────────────

class GameCommandRequest(BaseModel):
    """A request from the website to raise UI inside the caller's running game.

    `command` is checked against a server-side allow-list, not used to dispatch —
    this channel may only raise UI, never cause an irreversible in-game effect.
    """
    command: str
    contract_id: str = ""


class GameCommandResult(BaseModel):
    success: bool
    message: str
    #: Live KSP clients the frame reached. 0 means the game isn't running, which
    #: the page must report — otherwise pressing the button looks like it worked.
    clients: int = 0


# ── Submissions ──────────────────────────────────────────────────────────────

class SubmissionResult(BaseModel):
    success: bool
    message: str
    review_status: Optional[str] = None  # "approved", "refused", "pending"
    reason: Optional[str] = None
    xp_awarded: int = 0
    coins_awarded: int = 0

class VesselSnapshot(BaseModel):
    """Vessel data collected from KSP flight scene."""
    vessel_name: str
    vessel_type: str  # "Ship", "Station", "Probe", etc.
    situation: str  # "ORBITING", "LANDED", "FLYING", etc.
    body: str  # "Kerbin", "Mun", etc.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    # Orbital elements (if orbiting)
    sma: Optional[float] = None
    eccentricity: Optional[float] = None
    inclination: Optional[float] = None
    # Craft metadata
    part_count: int = 0
    total_mass: float = 0.0
    total_cost: float = 0.0
    crew_count: int = 0

class FlightSubmission(BaseModel):
    """Submitted alongside craft/screenshot files for flight missions."""
    contract_id: str
    active_vessel: VesselSnapshot
    nearby_vessels: list[VesselSnapshot] = []
    modlist: Optional[str] = None  # Comma-separated list of loaded assembly names


# ── Marketplace ──────────────────────────────────────────────────────────────

class MarketplaceListResult(BaseModel):
    success: bool
    message: str
    listing_id: Optional[str] = None

class MarketplaceListing(BaseModel):
    listing_id: str
    seller_id: str
    seller_name: str
    craft_name: str
    craft_type: str
    part_count: int
    mass: float
    cost: float
    price: int
    sales_count: int = 0
    created_at: Optional[str] = None
    # Fields the website needs (the KSP mod ignores them). mods powers the
    # filter-by-mod facet; thumbnail_url is the square NW-view card image and
    # blueprint_url the full multi-view render shown in the detail view; status lets
    # the "My Uploads" view show delisted crafts; craft_url is the direct download.
    mods: list[str] = []
    thumbnail_url: Optional[str] = None
    blueprint_url: Optional[str] = None
    craft_url: Optional[str] = None
    craft_filename: Optional[str] = None
    status: str = "active"
    # Life-support flag: which LS mod the craft is provisioned for ("none" if stock/empty),
    # in-game days it lasts per kerbal, and crew capacity (for the min/max endurance range).
    life_support: str = "none"
    ls_endurance_days: float = 0.0
    ls_crew_capacity: int = 0

class MarketplaceListingsResponse(BaseModel):
    listings: list[MarketplaceListing]


class CraftCompatibility(BaseModel):
    """Whether the requesting user can actually load a craft, checked against the
    part catalog their KSP client uploaded.

    `known` is False when we can't tell — the user has never uploaded a catalog, or
    the listing predates part tagging. That is deliberately distinct from "compatible":
    an unknown result must never be shown as a green light."""
    known: bool = False
    compatible: bool = True
    # Parts the craft uses that aren't in the user's catalog under that name.
    missing_parts: list[str] = []
    # Of those, the ones the mod can silently substitute on install because the user
    # has an equivalent (e.g. a DLC part they lack vs the ReStock+ stand-in they have).
    # `compatible` stays True when every missing part is in here.
    substitutable_parts: list[str] = []
    reason: str = ""


# ── Marketplace (website) ────────────────────────────────────────────────────

class MarketplaceListingsPage(BaseModel):
    """A single page of marketplace listings for the website grid (25/page),
    plus the total count and the set of mods present across the filtered result
    so the UI can render a filter facet."""
    listings: list[MarketplaceListing]
    total: int
    page: int
    pages: int
    available_mods: list[str] = []

class WebBuyResult(BaseModel):
    success: bool
    message: str
    balance: int = 0
    # On success, a direct download of the purchased .craft (the listing's public
    # Storage URL). The craft is also queued for KSP auto-import server-side.
    craft_url: Optional[str] = None
    craft_filename: Optional[str] = None
    already_owned: bool = False
    # Pre-flight against the buyer's uploaded part catalog. Advisory only — a craft
    # they can't load yet is still theirs to keep, and the mod substitutes what it can
    # on install, so this warns rather than blocks.
    compatibility: Optional[CraftCompatibility] = None


# ── Notifications ────────────────────────────────────────────────────────────

class Notification(BaseModel):
    id: str
    type: str  # "contract_incoming", "review_result", "reward", "mission_update"
    title: str
    message: str
    timestamp: str
    read: bool = False
    data: Optional[dict] = None  # Extra context (contract_id, reward amounts, etc.)

class NotificationsResponse(BaseModel):
    notifications: list[Notification]
    unread_count: int
