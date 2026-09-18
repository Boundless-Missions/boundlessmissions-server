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
    #                         press the Log-in button in their Discord DM, or approve
    #                         in the account panel for a panel-minted code.
    #   "totp_required"     → this account has an authenticator. POST the code to
    #                         /auth/link/totp with challenge_id. No polling, no DM —
    #                         which is why it works with DMs closed and for an
    #                         account that has no Discord at all.
    #   "pending"           → still waiting on the user's approval; keep polling.
    status: str = "ok"
    token: str = ""
    username: str = ""
    guild_id: str = ""
    user_id: str = ""
    challenge_id: Optional[str] = None
    # WHERE an "approval_required" challenge is answered: "discord" (the DM the bot
    # just sent) or "panel" (the account page on the website that minted the code —
    # which is also the only route an account with no Discord ever takes). The client
    # cannot derive this: both routes return the same status and the same
    # challenge_id, so without it the waiting screen has to guess, and it guessed
    # Discord, telling a website player to check DMs they do not have. Empty means an
    # older server that never sent it, and a client reading it must then name both
    # surfaces rather than pick one.
    approve_via: str = ""

# ── Website accounts (Google / email sign-in) ────────────────────────────────

class WebSignInRequest(BaseModel):
    id_token: str = Field(..., description="Firebase ID token from the browser SDK")

class WebSignInResponse(BaseModel):
    # "ok"            → signed in, token populated.
    # "totp_required" → the account has a second factor; post the code to
    #                   /web/auth/totp with challenge_id. NO token is issued here,
    #                   because a token that works without the second factor is
    #                   exactly what the second factor exists to prevent.
    status: str = "ok"
    challenge_id: str = ""
    # Stripped by the website's BFF into the httpOnly session cookie, exactly as
    # the link flow's token is — it must never reach browser JS.
    token: str = ""
    account_id: str = ""
    display_name: str = ""
    # True until the account has claimed its permanent username. The website sends
    # the user to onboarding on this; the claim endpoint is the real gate.
    needs_onboarding: bool = False

class AccountProfile(BaseModel):
    account_id: str
    username: str = ""           # permanent, claimed once, "" until onboarded
    display_name: str = ""
    avatar_url: str = ""
    email: str = ""
    # Which sign-ins can reach this account. The website draws "link/unlink" from
    # these; the account document is the authority.
    has_discord: bool = False
    has_password_login: bool = False
    discord_id: str = ""
    needs_onboarding: bool = False

class ClaimUsernameRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=40)

class DisplayNameRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=64)

class AccountActionResult(BaseModel):
    success: bool = True
    message: str = ""
    value: str = ""

class KspLinkCodeResponse(BaseModel):
    code: str
    # Seconds remaining, not an absolute timestamp: the browser's clock is not ours
    # to trust, and a countdown is what the panel actually renders.
    expires_in: int

class KspLinkPending(BaseModel):
    # pending=False means there is nothing to approve right now.
    pending: bool = False
    challenge_id: str = ""
    client_ip: str = ""
    device_id: str = ""
    requested_at: str = ""

class KspLinkApproveRequest(BaseModel):
    challenge_id: str
    approve: bool = True

# ── Two-factor authentication ────────────────────────────────────────────────

class TwoFactorStatus(BaseModel):
    enabled: bool = False
    # A secret has been minted but no working code has proved it yet.
    pending: bool = False
    recovery_remaining: int = 0
    # Whether enrolling requires re-proving a Firebase credential first, and with
    # which provider. The client cannot work this out for itself: a Discord-origin
    # account has no Firebase identity at all and must not be asked for one (that
    # would put 2FA out of reach of most of the player base), while a
    # password-registered account cannot re-prove itself through a Google popup.
    # "" = no re-auth needed, "google" / "password" = which one to ask for.
    reauth: str = ""

class TwoFactorBeginResponse(BaseModel):
    # Three ways in: scan the QR, tap the link, or type the secret. `qr_svg` is a
    # self-contained inline SVG (no script, no external refs) so the page can drop
    # it straight in without a QR library or a CSP change; "" if rendering failed,
    # which leaves the other two working.
    secret: str
    uri: str
    qr_svg: str = ""

class TwoFactorCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=24)
    # A fresh Firebase ID token, required to *enable* a factor (see
    # api_server.web_2fa_begin). Optional on the model because the same shape is
    # used by the paths that already prove themselves with a working code.
    id_token: str = Field(default="", max_length=4096)


class TwoFactorBeginRequest(BaseModel):
    """Starting an enrolment re-proves the primary credential.

    Removing a factor already requires a working code, on the stated grounds that
    "a borrowed signed-in browser must not be able to strip the protection that
    exists for exactly that case". Adding one had no such requirement, and adding
    is the worse direction: an attacker holding a borrowed session could enrol
    their own authenticator, keep the recovery codes, and lock the real owner out
    of their account permanently — neither removal path would help, because both
    need a code the owner never had.
    """
    # Empty is valid for an account with no Firebase identity (a Discord-origin
    # account signs in by link code, not through Firebase) — the server decides,
    # see api_server._require_fresh_firebase.
    id_token: str = Field(default="", max_length=4096)

class TwoFactorConfirmResponse(BaseModel):
    success: bool = True
    message: str = ""
    # Returned exactly once, at enrolment or regeneration. Only hashes are stored.
    recovery_codes: list[str] = []

class TwoFactorLoginRequest(BaseModel):
    challenge_id: str
    code: str = Field(..., min_length=4, max_length=24)


# ── Support tickets ──────────────────────────────────────────────────────────

class TicketMessage(BaseModel):
    message_id: str
    author_name: str = ""
    # "opener" | "staff" | "system" — what the UI needs to know to place a bubble.
    author_kind: str = "system"
    body: str = ""
    attachments: list[dict] = []
    created_at: str = ""

class TicketSummary(BaseModel):
    ticket_id: str
    number: int = 0
    kind: str = "other"
    title: str = ""
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    unread: bool = False

class TicketListResponse(BaseModel):
    tickets: list[TicketSummary] = []

class TicketThread(BaseModel):
    ticket: TicketSummary
    description: str = ""
    messages: list[TicketMessage] = []

class TicketCreateRequest(BaseModel):
    kind: str = Field("other", max_length=20)
    title: str = Field(..., min_length=1, max_length=150)
    body: str = Field(..., min_length=1, max_length=4000)

class TicketReplyRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)

class DiscordLinkCodeResponse(BaseModel):
    code: str
    expires_in: int

class DeviceStatusResponse(BaseModel):
    # status: "pending" (keep polling) | "approved" (device trusted, resume) |
    #         "denied" (rejected) | "expired". On a denied report awaiting client
    #         diagnostics, report_id is set so the client uploads its KSP.log.
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
    # up_to_date: "may I proceed" — NOT "am I on the newest build". True when the
    #             client's hash matches the published latest, when nothing has been
    #             published yet (fail-open), when the build is out of date with no
    #             mandatory release above it, AND when it is inside its grace window.
    #             Every client already in the wild treats False as "raise the
    #             blocking window", so this is the field that has to carry the gate's
    #             decision; the two literal questions are answered below instead.
    #             See data/mod_version.check().
    enabled: bool = True
    up_to_date: bool = True
    # on_latest:        the client's hash IS the published latest.
    # update_available: a newer build exists — true for a graced client, which is
    #                   being told to update without being refused.
    # grace_until:      ISO8601 instant this build stops being accepted, set only
    #                   while it is inside the window. Null means there is nothing
    #                   to count down — up to date, already refused, or (the ordinary
    #                   case) behind with no mandatory release above it, which is
    #                   accepted for as long as that stays true.
    on_latest: bool = True
    update_available: bool = False
    grace_until: Optional[str] = None
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
    # Unpaid contract fines, and the share of earnings currently going to them. Sent
    # so the mod and the website can show it next to the balance: a player whose
    # rewards arrive halved with no explanation reads it as the economy being broken.
    debt: int = 0
    debt_garnish_percent: int = 0
    # Account preferences that only the server can act on, sent with the profile so
    # a client can draw the switch without a second round trip. See
    # `store.corp_pings_enabled`: this one decides whether a corp-channel delivery
    # @-mentions the player.
    corp_pings: bool = True
    # True only for the single BOT_OWNER_ID account. The website uses it to decide
    # whether to draw the Admin tab; every admin endpoint re-checks server-side.
    is_owner: bool = False


class PreferencesUpdate(BaseModel):
    """A partial update of the account preferences on `UserProfile`.

    Every field is optional and `None` means "leave it alone", so a client that
    knows about one preference cannot clear the ones it has never heard of by
    omitting them — the same shape the settings screens already use, and the
    reason this is not a full replacement of the block.
    """
    corp_pings: bool | None = None


# ── Finance ──────────────────────────────────────────────────────────────────
#
# The wallet's history, for the mod's Finance panel and the website's account
# page. Three shapes, because they answer three different questions and are
# sized very differently:
#
#   `entries`  the recent movements, in detail — the list a player scrolls.
#   `totals`   lifetime per-category sums. NOT derivable from `entries`: the
#              store's ledger is a ring buffer, so a summary computed from the
#              list would start shrinking once the oldest entries rolled off.
#   `series`   one bucket per day, for the graph. Sent pre-bucketed rather than
#              leaving the client to group timestamps, so all three front ends
#              draw the same bars and none of them has to agree about timezones.

class FinanceEntry(BaseModel):
    """One movement of the wallet. `amount` is signed: negative money left."""
    ts: float = 0.0
    amount: int = 0
    category: str = "other"
    # The category's display name, resolved server-side. Sent with every entry so a
    # client that predates a category still labels it rather than printing the raw
    # key — the vocabulary can grow without a client update.
    category_label: str = ""
    detail: str = ""
    # The other party's account id, when there was one, plus the name to show for
    # it. The name is resolved server-side because only the bot has the member
    # cache that turns a snowflake into the name people know someone by.
    counterparty_id: str = ""
    counterparty_name: str = ""


class FinanceCategoryTotal(BaseModel):
    category: str
    label: str = ""
    incoming: int = 0
    outgoing: int = 0
    count: int = 0


class FinanceDay(BaseModel):
    day: str = ""          # YYYY-MM-DD, UTC
    ts: int = 0            # start-of-day epoch, for ordering without parsing
    incoming: int = 0
    outgoing: int = 0
    net: int = 0


class FinanceResponse(BaseModel):
    balance: int = 0
    currency_name: str = "KCoins"
    debt: int = 0
    debt_garnish_percent: int = 0
    # Money that has left the wallet but has not been spent: the payment on every
    # contract this player issued that has not settled yet, plus the start value of
    # every auction of theirs still open. It is sent separately from `balance`
    # because the two answer different questions — the balance is what can be spent
    # now, the escrow is what is already committed — and because a player who has
    # just issued a contract otherwise sees only that their balance dropped.
    #
    # Deliberately *not* derivable from `totals`: an escrow that ends by paying the
    # contractor is recorded on the contractor's ledger, never on the issuer's, so
    # the issuer's own history cannot say how much of what they escrowed is still
    # locked. It is derived from the contracts themselves (see `_escrow_held`).
    escrow: int = 0
    escrow_contracts: int = 0
    escrow_auctions: int = 0
    # Lifetime, across every category — the two headline numbers.
    total_in: int = 0
    total_out: int = 0
    totals: list[FinanceCategoryTotal] = []
    series: list[FinanceDay] = []
    entries: list[FinanceEntry] = []
    # How many entries the ledger currently holds (for paging), and the cap it
    # holds them up to — the client says "showing the last N" rather than
    # implying it has the player's whole history.
    entry_count: int = 0
    ledger_capacity: int = 0
    min_transfer: int = 1


class FinanceSendRequest(BaseModel):
    to_user_id: str
    amount: int = Field(gt=0)
    note: str = ""


class FinanceSendResult(BaseModel):
    success: bool = False
    message: str = ""
    balance: int = 0
    # What garnishment took out of the recipient's side, so the sender can be told
    # the recipient did not receive the whole amount rather than being left to
    # discover it. Zero for a recipient who owes nothing.
    garnished: int = 0


# ── Missions ─────────────────────────────────────────────────────────────────

class Mission(BaseModel):
    # The claim key, derived from the mission text rather than its position on the
    # board (cogs.weeklymissions._mission_id). Send this back to /missions/select.
    id: int
    # The number the player sees — 1..20 down the board. Display only: two installs
    # are shown different boards, so position identifies nothing on its own. 0 on a
    # board stored before the two were split, where the id *was* the position.
    n: int = 0
    desc_en: str
    desc_tr: str
    difficulty: int
    category: str
    xp: int
    coins: int
    fine: int
    # AI-classified submission requirements (cached server-side)
    # "craft_build", "active_vessel" or "constellation" (a network mission judged
    # on every vessel in the target body's SOI — see data/fleet_constraints.py).
    mission_type: str = "active_vessel"
    required_situation: Optional[str] = None  # KSP situation: ORBITING, LANDED, FLYING, etc.
    required_body: Optional[str] = None  # Celestial body: Kerbin, Mun, Duna, etc.
    # True on a mission from the test board — a second, admin-rotated board served
    # only to an allow-listed dev client. Clients should say so on the card: a test
    # mission mints a real contract, and by default it pays nothing.
    test: bool = False

class WeeklyMissionsResponse(BaseModel):
    week_key: str
    missions: list[Mission]
    is_locked: bool
    closes_at: str  # ISO timestamp
    # Which board these came from: "live" or "test".
    board: str = "live"
    # Set when the caller asked for the test board and was given the live one
    # instead. Serving live with a reason rather than failing keeps a dev client
    # usable when the test board is off, which is most of the time — an outright
    # 403 would read in game as "missions are broken".
    test_refusal: Optional[str] = None

class MissionSelectRequest(BaseModel):
    mission_id: int
    # Which board the id came from. A test id is meaningless against the live board
    # and vice versa, so this is not a hint — it selects the claim namespace
    # (`_selection_ref` keys on the week, and a test board's week is its rotation).
    board: str = "live"

class MissionSelectResponse(BaseModel):
    success: bool
    contract_id: Optional[str] = None
    message: str


# ── Contracts ────────────────────────────────────────────────────────────────

class WreckMod(BaseModel):
    """One mod the stranded vessel is built from, and the parts it supplies.

    Resolved on the ISSUER's client at hand-over, because that is the only machine
    that ever has this ship: every mod-detection path in the project walks an
    *installed* part to its GameData folder, so the rescuer — who by definition may
    be missing these mods — cannot work it out for themselves.

    The wreck node already carries the same list in its GKMODS block, but that is
    read only when the wreck is spawned, which is after the rescuer has accepted and
    flown out. This is the same answer, placed where the offer can show it while it
    is still an offer.

    `parts` is trimmed (client and server both); `part_count` is how many distinct
    parts the mod really supplies, so a trimmed list can say "and 6 more" rather than
    quietly looking complete.
    """
    folder: str
    path: str = ""
    ckan: str = ""
    name: str = ""
    parts: list[str] = []
    part_count: int = 0


class RescueTarget(BaseModel):
    """Where stranded kerbals must be recovered from / delivered to.

    mode == "orbit"   → ap/pe define the target orbit (metres above the body
                        surface); margin_alt is the allowed +/- on each.
    mode == "surface" → lat/lon define the landing spot (degrees); margin_pos is
                        the allowed great-circle tolerance (degrees).

    Both pairs are optional, and *absent means no requirement*, never zero: no ap/pe
    is "any orbit of the body", no lat/lon is "anywhere on it". The mode's situation
    still holds either way — an any-orbit rescue must still be delivered in orbit —
    and the plane / regime constraints below can be asked for on their own. Only the
    pair belonging to the other mode is normally missing, so a contract issued before
    the issuer could switch these off always carries both halves of its own pair.
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
    # Which mods the wreck is built from. Unlike wreck_parts this is sent to
    # everyone and on every rescue: its whole purpose is to be read *before* the
    # offer is accepted, so withholding it from a pending contractor — the one
    # person it is for — would leave it useless. Empty for a stock wreck, and for
    # every rescue issued before the issuer's client sent it.
    wreck_mods: list[WreckMod] = []


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
    # The two parties' immutable ACCOUNT ids. Sent because the client has to decide
    # crew ownership on a transfer, and `issuer_name`/`contractor_name` are display
    # names — self-chosen, mutable and not unique, so deciding on them let anyone
    # take a victim's name and have the victim's own kerbals adopted onto an
    # arriving vessel (see data/imports.enqueue's owner_id). Names stay for display;
    # ids are what the decision keys on. No new disclosure: the same ids are already
    # public in the corp/player pickers.
    issuer_id: str = ""
    contractor_id: str = ""
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
    # What the wreck looks like, and where it actually is: the blueprint sheet the
    # issuer's client rendered at issuance, and the orbit diagram the server drew from
    # the telemetry captured with it. Ap/Pe numbers say how high, never which orbit,
    # and the mission text says nothing at all about how big the ship is or what is
    # left of it — both are what a rescuer plans against, so they are carried on the
    # offer as well as on the accepted contract.
    #
    # Both are PUBLIC storage URLs (see _store_rescue_schematics) and both are absent
    # on every rescue issued before this existed and from every older client. Absent
    # means "no schematic", never an error: a client that has neither draws nothing.
    rescue_blueprint_url: Optional[str] = None
    rescue_orbit_url: Optional[str] = None
    # The wreck was landed/splashed when it was handed over, so the diagram shows a
    # surface marker rather than an orbit and must not be captioned as one. Distinct
    # from rescue_target.mode, which is where the crew must be *delivered* — a wreck
    # in orbit can be a surface-delivery rescue and vice versa.
    rescue_orbit_surface: bool = False
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


class ContractFlagResponse(BaseModel):
    """The deliverable of a `flag_design` contract, as the website may see it.

    One field carries the whole gate: `watermarked` says which image this is.
    Until the contract completes — i.e. is paid for — `url` is the public,
    stamped, downscaled preview; afterwards it is a signed URL to the clean
    full-res file the issuer bought. A client must never infer that from the
    status it happens to be holding, which can be stale by a review.
    """
    url: Optional[str] = None
    filename: str = ""
    watermarked: bool = True


class PartCatalogUpload(BaseModel):
    """The KSP client's full installed part list, used to resolve loosely-typed
    part mentions in mission limits to real parts. `hash` lets the client skip
    re-uploading an unchanged catalog."""
    # Bounded at the model, because the handler's `[:8000]` slice bounds the entry
    # *count* only — the strings inside were unbounded, so a body just under the
    # request ceiling produced a ~78 MB catalog that then failed its (>1 MiB)
    # Firestore write and lived on in memory alone, permanently. See
    # api_server.upload_part_catalog, which also caps each name and title.
    hash: str = Field(max_length=128)
    # Every FlightGlobals.Bodies name, and every GameData folder that contributed a
    # loaded part. Both optional: a client older than these fields sends neither and
    # is read as a stock install, which is the board it was already being served.
    # They exist for data/install_profile.py — see the note in upload_part_catalog
    # on why it takes two signals rather than one.
    bodies: list[str] = Field(default_factory=list, max_length=500)
    mods: list[str] = Field(default_factory=list, max_length=500)
    # Deliberately NOT capped at the working limit (8000): the handler truncates to
    # that and returns 200, and turning the truncation into a 422 would fail the
    # upload outright on the heavily-modded installs this project exists for —
    # PartCatalogUploader sends the whole LoadedPartsList, logs a warning and never
    # retries for the session, so a refusal costs part resolution and marketplace
    # compatibility silently. This bound is a defence against an absurd payload, not
    # a behaviour any real install meets.
    parts: list[dict] = Field(default_factory=list, max_length=30000)  # {"name":…, "title":…}


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
    # The claimed Boundless username, exactly as FriendInfo carries it and for the
    # same reason: a display name is a nickname a guild can change, while this is
    # the permanent handle someone else has to type to find this player. It is what
    # the pickers draw under the display name — the corp name is still sent because
    # it is the corp's own name and nothing else answers for it, but no picker draws
    # it any more. "" for a player who has not claimed one yet.
    username: str = ""
    # Extras for the mod's player picker. Optional/defaulted so older mod builds,
    # which parse this with MiniJSON and ignore unknown keys, are unaffected.
    avatar_url: Optional[str] = None
    level: int = 0

class CorpListResponse(BaseModel):
    corps: list[CorpInfo]


# ── Friends ──────────────────────────────────────────────────────────────────
# One card shape for all three lists (friends, requests in and out): the client
# draws the same row and only the buttons under it differ, so splitting the model
# by list would guarantee they drift apart.

class FriendInfo(BaseModel):
    user_id: str
    name: str
    # The claimed Boundless username — the handle a friend request is addressed
    # to. Sent alongside the display name because those are two different things
    # for a Discord player, whose card says their nickname while their username is
    # what someone else has to type to find them.
    username: str = ""
    avatar_url: Optional[str] = None
    level: int = 0
    # Epoch seconds: when the friendship began, or when the request was sent.
    at: float = 0.0
    # Whether this player has a Discord behind them. Presentation only — the
    # friendship itself does not care, and nothing may gate on it.
    discord: bool = False

class FriendListResponse(BaseModel):
    friends: list[FriendInfo] = []
    incoming: list[FriendInfo] = []
    outgoing: list[FriendInfo] = []
    max_friends: int = 0

class FriendRequestPayload(BaseModel):
    """Ask by username (what a website account is found by) or by account id
    (what the in-game roster picker already holds). Exactly one is used;
    `username` wins when both arrive, since it is the one a human typed."""
    username: str = Field(default="", max_length=64)
    user_id: str = Field(default="", max_length=64)

class FriendActionResult(BaseModel):
    success: bool
    message: str
    # "requested" | "accepted" | "" — lets a client tell "sent, now wait" apart
    # from "that completed a handshake, you are friends now" without parsing the
    # sentence it also shows.
    state: str = ""

# The ceiling on a client-sent mod list, shared by the contract and auction models.
# See the note at its use site: measured against real heavy installs, not guessed.
MODLIST_MAX_LENGTH = 8000


class ContractCreateRequest(BaseModel):
    contractor_id: str  # Corp owner's user ID
    mission: str = Field(..., min_length=3, max_length=500)
    payment: int = Field(..., gt=0)
    fine: int = Field(default=0, ge=0)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Bounded: this string is stored on the contract and echoed to the offeree on
    # every poll. Unbounded it was both an amplifier and — over Firestore's 1 MiB
    # document limit — a way to make `create_contract` fail *after* the escrow had
    # been debited.
    #
    # 8000, not 2000. The original bound was reasoned from "a real KSP mod list is a
    # few hundred folder names" without measuring one. Measured on this machine's own
    # dev installs, `ModlistJanitor` (which sends every GameData folder, not just the
    # ones contributing parts) produces 1924 characters on FAK1 — 96% of a 2000-char
    # cap, on a modpack that is not unusual by community standards. A 200-folder
    # install simply exceeded it, and the failure was a bare FastAPI 422 with nothing
    # in the mod's UI to explain it. This is still three orders of magnitude under
    # the Firestore limit the bound exists to stay beneath.
    modlist: Optional[str] = Field(default=None, max_length=MODLIST_MAX_LENGTH)  # Comma-separated list of loaded assembly names
    # "auto" keeps the existing AI classification; "craft_build" / "active_vessel"
    # force the type and skip AI. (Rescue contracts use the separate multipart
    # /contracts/create_rescue endpoint.)
    contract_type: str = "auto"
    # A relay-network contract states its terms here rather than in its prose. The
    # count is what the issuer typed on the form; the spacing is derived from it
    # (fleet_constraints.default_spread), so there is nothing else to ask for.
    # Bounded at the model so a nonsense number never reaches the constraint.
    relay_count: Optional[int] = Field(default=None, ge=2, le=64)
    # The body the network has to be at. Only read for a constellation — every other
    # type either has no body (craft_build, flag_design) or carries it in telemetry.
    body: Optional[str] = Field(default=None, max_length=64)
    # How strong each satellite's relay antenna must be, as antenna power. Omitted or 0
    # means any relay-capable antenna, which is what a relay network has always meant.
    # A number, not a part name: the issuer picks an antenna they own and the form sends
    # what it is worth, so a contractor on a different mod set can meet it with an
    # equivalent. Bounded well above any stock or modded antenna (the RA-100 is 1e11).
    min_relay_power: Optional[float] = Field(default=None, ge=0, le=1e13)
    # Which antenna model that number is written in ("stock", "realantennas", …). Sent
    # by the issuer's client from its own install, because the figure is only
    # comparable on an install sharing the model.
    antenna_model: Optional[str] = Field(default=None, max_length=32)


class AuctionCreateRequest(BaseModel):
    """Open a reverse (Dutch) auction from the KSP mod. No contractor — it's open
    to everyone in Discord; the lowest bidder when it ends is bound to the contract.
    start_value is escrowed up front; the leftover is refunded when it closes."""
    mission: str = Field(..., min_length=3, max_length=500)
    # The real floor is settings.AUCTION_MIN_START_VALUE, enforced in the handler so
    # the refusal can say why; this bound only keeps nonsense off the model.
    start_value: int = Field(..., gt=0)
    fine: int = Field(default=0, ge=0)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    duration_hours: int = Field(..., gt=0)
    # Bounded: this string is stored on the contract and echoed to the offeree on
    # every poll. Unbounded it was both an amplifier and — over Firestore's 1 MiB
    # document limit — a way to make `create_contract` fail *after* the escrow had
    # been debited.
    #
    # 8000, not 2000. The original bound was reasoned from "a real KSP mod list is a
    # few hundred folder names" without measuring one. Measured on this machine's own
    # dev installs, `ModlistJanitor` (which sends every GameData folder, not just the
    # ones contributing parts) produces 1924 characters on FAK1 — 96% of a 2000-char
    # cap, on a modpack that is not unusual by community standards. A 200-folder
    # install simply exceeded it, and the failure was a bare FastAPI 422 with nothing
    # in the mod's UI to explain it. This is still three orders of magnitude under
    # the Firestore limit the bound exists to stay beneath.
    modlist: Optional[str] = Field(default=None, max_length=MODLIST_MAX_LENGTH)  # mods required / limited to
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
    # Complexity bonus paid for this upload (0 when the craft didn't qualify or the
    # seller already collected today). `reward_note` is the one-line explanation the
    # mod appends to its own success line — it builds that line itself and never
    # shows `message` on success, so the note has to be its own field.
    reward: int = 0
    reward_note: str = ""

class MarketplaceDownload(BaseModel):
    """One entitled download link. Deliberately not a listing: the website's
    download proxy needs exactly a URL and a filename, and resolving that through
    the "My Uploads" / "My Purchases" list views cost two full collection queries
    and one signature per row, on the bot's event loop, per click."""
    url: str
    filename: str


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
    # Whether the craft carries a custom paint job — Textures Unlimited or Reforged
    # Materials Redux — the website's "Modded Textures Available" tag. Sent by the KSP
    # client at list-time; for a listing made before the flag existed it is inferred
    # from the mod row instead (see _has_custom_textures).
    custom_textures: bool = False
    # Community rating. `score` is likes minus dislikes and is the number the site
    # actually shows — one signed value per craft, SCP-wiki style; the two tallies
    # ride along for the admin console, which wants the split behind it. All three
    # are public (they're on every card); *who* voted is not — the caller learns
    # only their own vote, from /web/marketplace/votes.
    score: int = 0
    likes: int = 0
    dislikes: int = 0
    # The score, not the seller, is why this listing is delisted.
    auto_delisted: bool = False

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


class VoteRequest(BaseModel):
    """A vote is the state the caller wants, not a toggle: 1 like, -1 dislike,
    0 "take my vote back". Sending the state means a double-submit is a no-op
    rather than an unvote of something the user still meant to like."""
    vote: int = 0

class VoteResult(BaseModel):
    success: bool
    score: int = 0
    likes: int = 0
    dislikes: int = 0
    my_vote: int = 0
    # Set when this very vote pushed the listing to the auto-delist floor. The
    # voter is told, because the craft disappearing from the grid the instant they
    # pressed a button otherwise reads as the site breaking.
    listing_removed: bool = False
    # "delisted" or "deleted" when listing_removed — which of the two the server is
    # configured to do (settings.MARKETPLACE_AUTO_DELIST_DELETE).
    removal_kind: str = ""

class MyVotesResponse(BaseModel):
    """Every vote the caller has cast, {listing_id: 1 | -1}. One read serves the
    whole grid, so the UI can light up the buttons on crafts they already voted on."""
    votes: dict[str, int] = {}

class ReportRequest(BaseModel):
    reason: str

class ReportResult(BaseModel):
    success: bool
    message: str


# ── Auctions (website) ───────────────────────────────────────────────────────

class WebAuction(BaseModel):
    """One open reverse auction as the website shows it. Only OPEN auctions are
    served — a closed auction becomes a contract, which the Contracts tab shows."""
    auction_id: str
    mission: str
    issuer_name: str
    start_value: int
    #: The lowest bid so far; equals start_value until someone bids.
    current_bid: int
    current_bidder_name: Optional[str] = None
    bid_count: int = 0
    #: A new bid must undercut current_bid by at least this much.
    min_decrement: int = 1
    fine: int = 0
    due_date: str
    ends_at: str
    created_at: Optional[str] = None
    #: craft_build / active_vessel / flag_design, or null when untyped.
    mission_type: Optional[str] = None
    modlist: Optional[str] = None
    #: The caller issued this auction (can end it, cannot bid on it).
    is_yours: bool = False
    #: The caller holds the current lowest bid.
    is_leading: bool = False


class WebAuctionListResponse(BaseModel):
    auctions: list[WebAuction] = []


class WebAuctionBidRequest(BaseModel):
    amount: int = Field(..., gt=0)


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
