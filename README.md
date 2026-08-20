# Boundless Missions — Discord Bot & API Server

> A **discord.py 2.x** bot for the Unified Players of KSP (UPoK) community, with
> a **FastAPI** server running in the same process. The bot is the community
> side — economy, XP, corporations, moderation, auctions. The API is the game
> side: the KSP mod and the website both talk to it, and neither ever sees a
> secret.

One process, two faces. Discord and the API are concurrent asyncio tasks started
by `bot.py`; they share one Firestore-backed store, one config, one cost meter.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Slash Commands](#slash-commands)
5. [The KSP API](#the-ksp-api)
6. [The Website API](#the-website-api)
7. [Owner & Admin Console](#owner--admin-console)
8. [Data & Persistence](#data--persistence)
9. [Contracts](#contracts)
10. [AI Integration](#ai-integration)
11. [Cost Guard](#cost-guard)
12. [Security Notes](#security-notes)
13. [What Discord No Longer Does](#what-discord-no-longer-does)
14. [Tests](#tests)
15. [Related Docs](#related-docs)

---

## Quick Start

```bash
cd "GK Discord Bot"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env            # fill in the required values below

python bot.py           # normal start
python bot.py --sync    # only after adding or changing slash commands
```

**Only pass `--sync` when slash commands have actually changed.** Syncing on
every restart will hit Discord's rate limit.

### Required configuration

| Variable | What it is |
|----------|------------|
| `DISCORD_TOKEN` | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `BOT_OWNER_ID` | The single Discord user id that owns the bot (see [Owner & Admin Console](#owner--admin-console)) |
| `FIREBASE_CREDENTIALS` | Path to the Firebase Admin SDK service-account JSON |
| `FIREBASE_STORAGE_BUCKET` | Firebase Storage bucket for crafts, renders and flags |
| `API_SECRET_KEY` | HMAC key that signs KSP session tokens |
| `GEMINI_API_KEY` | Optional. Without it every AI call degrades to a fallback rather than failing |

`.env.example` is the full list, grouped and commented — including the API
server, TLS, cost-guard budgets and the runtime gates. Gameplay balance
(XP curves, cooldowns, economy rates, auction and marketplace limits, channel
and role ids) lives in **`settings.py`**, which is safe to commit and has no
secrets in it.

### Command grouping

`COMMAND_GROUP` nests every command under one group: `COMMAND_GROUP=g` gives
`/g help`, `/g balance`, … It defaults to **empty**, which leaves commands at the
top level. Admin commands (`default_permissions=administrator`) always go under a
top-level `admin` group and moderation commands under `mod`, regardless. The
nesting is applied at startup in `bot.py:setup_hook`.

---

## Architecture

```
                       ┌──────────────────────────────────┐
   Discord  ◄────────► │  GeneKermanBot  (discord.py 2.x) │
                       │    cogs/  ·  i18n  ·  perms      │
                       ├──────────────────────────────────┤
   KSP mod  ◄────────► │  api_server.py  (FastAPI)        │
   Website  ◄────────► │    /api/v1/…  ·  /ws/v1/…        │
                       ├──────────────────────────────────┤
                       │  data/store.py  (buffered)       │
                       │  cost_guard  ·  firebase_guard   │
                       └───────────────┬──────────────────┘
                                       ▼
                          Firestore  +  Cloud Storage
                                       ▲
                          Gemini  ·  Cloud Monitoring  ·  BigQuery
```

- **`bot.py`** — entry point. Defines `GeneKermanBot`, loads every cog,
  optionally syncs the command tree, and starts the Discord client and the
  uvicorn API server as concurrent asyncio tasks. It also contains the
  monkey-patch of discord.py's dispatch internals that powers the admin
  **mimic** system.
- **`config.py`** — the singleton `cfg`, loaded from `.env` at import time.
  `from config import cfg` everywhere; never read `os.environ` directly.
- **`settings.py`** — public, committable gameplay and balance values.
- **`i18n.py`** — two-tier localization: `t(guild_id, key)` for public/server
  messages, `tp(guild_id, user_id, key)` for ephemeral/personal ones. Both take
  `**kwargs` for placeholders.

  > The machinery is live but there is currently **one language**:
  > `SUPPORTED_LANGS = ("en",)`. Keep using `t`/`tp` for new strings anyway —
  > the per-guild and per-user lookup is what makes adding a language later a
  > data change rather than a code change.
- **`api_server.py`** — the FastAPI app (port 5022 by default), plus every
  request handler for the KSP client, the website, and the owner console.
- **`api_auth.py`** — the linking and session layer.
- **`contract_actions.py`** — the single implementation of every contract state
  transition, shared by Discord, the KSP client and the website.

Cogs share state through `store`, `settings` and `i18n` rather than through each
other. Direct cog-to-cog imports do exist where one cog genuinely owns a helper
another needs — `cogs.perms` (nearly everywhere), `contract_views` (imported by
`contracts` and `auctions`, that direction only), `gkchannels` (by `corps` and
`tickets`), `screenshots.active_client` (by `contract_views` and `api_server`),
plus helpers from `roles`, `moderation`, `corps`, `tickets` and `auctions`.
Prefer the shared modules for new work.

---

## Project Structure

```
GK Discord Bot/
├── bot.py                  # Entry point: cogs, command tree, both servers
├── config.py               # .env loader (the `cfg` singleton)
├── settings.py             # Gameplay/balance values — safe to commit
├── i18n.py                 # t() / tp() localization
├── command_group.py        # The shared /<group> parent, when COMMAND_GROUP is set
│
├── api_server.py           # FastAPI: KSP + website + owner console
├── api_auth.py             # Link codes, signed session tokens, device binding
├── api_models.py           # Pydantic request/response models
├── contract_actions.py     # Every contract state transition, in one place
│
├── cost_guard.py           # Three-tier spend meter and the enforcement ladder
├── orbit_render.py         # Stylized 2D orbit diagrams for submissions
├── flag_preview.py         # Watermarked preview of a flag-design submission
│
├── cogs/                   # Discord features (see below)
├── data/                   # Firestore integration and domain data
├── tools/gen_part_aliases.py   # Regenerates data/part_aliases.py from the mod
├── scripts/                # One-off maintenance/backfill scripts
└── test_*.py               # Pytest suites (see Tests)
```

### `cogs/`

Every entry is a loaded extension except `perms.py` and `contract_views.py`,
which are helper modules living here because they belong to this half of the
codebase. `moderation.py` is loaded only when `ENABLE_MOD_COMMANDS` is set.

| Module | Role |
|--------|------|
| `admin.py` | Admin and owner commands: channel/role mapping, announcements, cog reload, mimic, version and policy publishing, `/costs` |
| `perms.py` | **Mimic-safe** permission helpers. Every gate must go through these |
| `economy.py` | KCoins — balance, pay, and the mod tools (give / fine / set) |
| `xp.py` | Levelling, rank, leaderboard, and the `auto_save` loop that flushes the store |
| `roles.py` | Self-assignable role menus |
| `corps.py` | Corporations — one per user, each with a dedicated channel |
| `contracts.py` | Player-to-player contracts, Discord side (**creation lives in the mod and the website**) |
| `contract_views.py` | The interactive contract buttons and the Discord-side AI review |
| `contractcraft.py` | "Load to KSP" on a corp-delivered contract craft |
| `auctions.py` | Reverse (Dutch) auctions — escrowed start price, bids drive it *down* |
| `weeklymissions.py` | The weekly mission board, drawn from `data/mission_templates.py` |
| `screenshots.py` | `/analyze` — Gemini screenshot analysis; also owns `active_client()` |
| `tickets.py` | Private support / report / bug tickets |
| `moderation.py` | Kick, ban, mute, purge, warn |
| `gkchannels.py` | Gates bot commands to designated channels |
| `ksp_bridge.py` | `/linkcode`, `/privacy`, `/deletemydata`, and the persistent Link button |
| `costwatch.py` | Feeds the cost guard from Cloud Monitoring / BigQuery and warns the owner |
| `general.py`, `info.py` | Help, ping, server/user info |
| `marketplace.py` | **Tombstone.** Claims the three retired custom_ids and points at the website |

### `data/`

| Module | Role |
|--------|------|
| `store.py` | The `store` singleton — user data, in-memory write buffer, auto-save |
| `contracts.py` | Contract CRUD against Firestore |
| `marketplace.py` | Listings, votes, reports, the `recommended` sort |
| `auctions.py` | Auction documents |
| `guild_config.py` | Per-guild channel and role mapping (`/admin setchannel`, `/admin setrole`) |
| `imports.py` | The craft import / gift queue |
| `suspensions.py` | Timed API-surface suspensions (see [Security Notes](#security-notes)) |
| `suspicion.py` | Flags that feed moderation review |
| `mission_constraints.py` | Part-restriction extraction and verification, incl. `_TRAIT_MODS` |
| `orbit_constraints.py` | Orbital-regime extraction and verification |
| `mission_templates.py` | The authored pool the weekly board draws from |
| `part_resolver.py` | Resolves a loosely-typed part mention to a real installed part |
| `part_aliases.py` | **Generated** — see [Related Docs](#related-docs) |
| `celestial_bodies.py` | Visual and physical reference data for bodies |
| `telemetry_check.py` | Server-side plausibility check on submitted flight telemetry |
| `achievements.py`, `mod_version.py`, `policy.py` | Achievements, the DLL version gate, the consent policy version |
| `firebase_guard.py` | Transparent proxies that meter every Firestore/Storage call |
| `gcp_metrics.py`, `gcp_billing.py` | Cloud Monitoring and BigQuery billing readers |

---

## Slash Commands

Everything is a slash command. Admin commands sit under `/admin`, moderation
under `/mod`, and the rest at the top level (or under `COMMAND_GROUP`, if set).

| Group | Commands |
|-------|----------|
| **Economy** | `/balance` · `/pay` · `/richest` · `/givemoney`* · `/fine`* · `/setbalance`* |
| **XP** | `/rank` · `/leaderboard` · `/setxp`* |
| **Contracts** | `/contractreset`* · `/rescues` · `/rescueboard` |
| **Corps** | `/corpsetup` · `/corpsgenerate`* · `/corpsprivacy` |
| **KSP link** | `/linkcode` · `/privacy` · `/deletemydata` |
| **Screenshots** | `/analyze` |
| **Roles** | `/roles` · `/removeroles` |
| **Info** | `/help` · `/ping` · `/serverinfo` · `/userinfo` · `/botinfo` |
| **Tickets** | `/ticketpanel`* |
| **Missions** | `/add_custom_mission`* |
| **Mod** (`/mod …`) | `kick` · `ban` · `unban` · `mute` · `unmute` · `purge` · `warn` · `warnings` · `gkchannel` |
| **Admin** (`/admin …`) | `setchannel` · `setrole` · `announce` · `setprefix` · `linkas` · `publishversion` · `versioninfo` · `policyversion` · `costs` · `reload`† · `shutdown`† · `mimic`† · `unmimic`† |

\* moderator or admin only  ·  † bot owner only

---

## The KSP API

`api_server.py` serves `/api/v1/…` in-process with the bot. Every endpoint
requires a valid signed session token except the link handshake and `/health`.

### Linking

1. The player runs `/linkcode` in Discord → a one-time 6-digit code is written
   to Firestore with a **3-minute** lifetime (`LINK_CODE_LIFETIME`).
2. The KSP client `POST`s it to `/api/v1/auth/link`.
3. The server validates it and returns a **30-day HMAC-SHA256 signed session
   token** (`TOKEN_LIFETIME`).
4. The client sends that token on every request as `Authorization: Bearer …`.

No API key, Firebase credential or other secret is ever sent to a client.

### Endpoint groups

| Prefix | What it covers |
|--------|----------------|
| `/api/v1/auth/…` | Link, poll, verify, device list/remove, WS ticket, `logout_all`, suspension check |
| `/api/v1/version/check`, `/api/v1/attest/…` | The update gate, the policy version, and DLL challenge-response attestation |
| `/api/v1/contracts/…` | Create (incl. `create_rescue`), incoming, active, accept, submit, review, dispute, give up, settle, more-time |
| `/api/v1/craft/…` | Download, send, the gift offer inbox (accept / reject), and the import queue |
| `/api/v1/missions/…` | The weekly board and mission selection |
| `/api/v1/marketplace/…` | List and delist (the **selling** half — browsing lives on the website) |
| `/api/v1/auctions/create` | Auction creation |
| `/api/v1/user/…` | Profile and the notification feed (mark read, dismiss) |
| `/api/v1/parts/catalog` | Part catalog upload, so the bot can resolve fuzzy part mentions |
| `/api/v1/checkpoint-photo`, `/achievement-photo` | Cinematic milestone captures |
| `/api/v1/bugreport` | See below |
| `/ws/v1/notifications` | The WebSocket push channel (ticket-authenticated) |

### Bug reports

`POST /api/v1/bugreport` takes a summary, details and optionally the player's
`KSP.log` from the mod's Tools tab, and opens a normal ticket via
`cogs/tickets.create_ticket` — but with `ping_mods=False` and
`notify_role_key="bug_report"`. A bug is not a moderation matter, so it pings the
`bug_report` role (`BUG_REPORT_ROLE_ID`, or mapped per guild with
`/admin setrole`); left unset it falls back to pinging the mods rather than going
unread. That role can also close the ticket.

The log is trimmed to **head 2 MB + tail 7 MB on the client**
(`DeviceId.GetKspLogCapped`) so a 300 MB modded log is never uploaded whole;
`_trim_log` on the server cuts to the same shape as a backstop.

---

## The Website API

The website talks to the same process over `/api/v1/web/…`, with its own link
handshake (`/web/auth/link`) issuing the same kind of token.

| Prefix | What it covers |
|--------|----------------|
| `/web/marketplace/…` | Browse, buy, vote, report, relist, delist, delete, purchases, compatibility |
| `/web/contracts/…` | The same transitions as the KSP path, through `contract_actions` |
| `/web/auctions/…` | Bid and end (creation is KSP-only) |
| `/web/profile` | Profile |
| `/web/game/command` | Queue an action for the player's running KSP client |

### Marketplace votes & reports

All three actions are authenticated (`get_user_token_only`) — an anonymous like
is worth nothing and an anonymous report costs a moderator a channel.

- A vote is a **tri-state the client sends** (`VOTE_UP` / `VOTE_DOWN` /
  `VOTE_NONE`), never a "flip it". Toggle-on-second-press is a UI convention
  living only in the website's `listing-actions.tsx`, so a double-submit cannot
  undo a vote the user meant to keep.
- Storage answers the two questions the UI actually asks. "Which of these 25
  crafts have I voted on?" is **one** document read
  (`marketplace_votes/{user_id}` holds the whole map, rather than a doc per pair
  needing 25 reads or a collection-group index). The per-card tallies are stored
  counters moved by `firestore.Increment`, not sums. `set_vote` is the only
  writer of either, and undoes its increment if the user's own record fails to
  save.
- Sorting adds `likes` (net, all-time) and `recommended`, which is a *discovery*
  sort: net likes per day for listings under `RECOMMENDED_WINDOW_DAYS` (15), then
  everything else by net likes as a tail so a quiet fortnight doesn't empty the
  tab. The window is what makes it work — by likes alone, a year-old listing
  outranks a good one from Tuesday.
- A report opens a ticket in the **reporter's** guild (a ticket they cannot see
  is no use to them; the listing's origin server is named in the embed instead)
  with the seller as `subject_user_id` — shown to mods, deliberately not given
  access. The report doc id is the (listing, reporter) pair, so "already
  reported" is a keyed read and the same complaint can't be filed twice to look
  louder. `report_count` is merged into the owner console's rows only, never into
  the public listing.

### Craft compatibility pre-flight

Listings record the craft's exact part names (`parts`, sent by the KSP client
alongside `mods`), which `_craft_compatibility` checks against the buyer's
uploaded part catalog. Served by
`GET /api/v1/web/marketplace/{id}/compatibility` and returned on the web buy
result.

Parts the mod will **substitute** on install are reported separately and do *not*
count as incompatible — warning about a problem that fixes itself on arrival is a
false alarm. `known=False` (no catalog uploaded yet, or a listing predating part
tagging) is deliberately distinct from compatible and must never render as a
green light. The whole thing is advisory, never a gate.

---

## Owner & Admin Console

`/api/v1/web/admin/*` is the website's developer master console: listings
moderation, user accounts (balance/XP set, revoke sessions, suspend/lift,
delete), DM-from-bot and announcements, remote channel lock/unlock, DLL
publishing, cost readouts and runtime gates.

It is gated in **two tiers**, mirroring `cogs/perms.py`:

| Dependency | Who | What it opens |
|------------|-----|---------------|
| `get_owner` | Only the `BOT_OWNER_ID` account — the same single owner as `cogs/perms.is_owner_user` | Everything bot-wide: user accounts, DM-from-bot, DLL publishing, costs, runtime gates, policy |
| `get_admin` | The owner, **or** a holder of a guild's mapped bot-admin role (key `"admin"` via `/admin setrole`) | The guild-scoped moderation surface: overview, listings whose origin `guild_id` is a guild they admin, announcements, channel locks, guild pickers — every response and action cut to their guilds |

Guild administrators are **not** auto-admins. A role granted in one guild must
never carry authority over every guild the bot is in.

Both tiers answer **404**, not 403, to everyone else — the surface is invisible.
Admin-role membership is resolved live from the Discord member cache on every
request, so revoking the role revokes console access immediately.

The website's `/admin` tab is drawn for whoever `whoami` accepts, with the
owner-only tabs (Users, Mod Version, Costs, Controls) and the DM card hidden from
guild admins — but that visibility is **presentation only**; the API dependency
is the real gate.

> Runtime gate flips are **process-local**. `.env` is the boot-time truth, and a
> restart reverts them.

---

## Data & Persistence

**User data is global.** XP, balance, message counts, unlocked levels and the
language preference live at the **top-level, guild-independent** `users/{user_id}`,
mirrored in an in-memory dict by `data/store.py`. The wallet spans every server:
`store.get_user(guild_id, user_id)` still takes a `guild_id` for call-site
compatibility but ignores it as a key.

**Guild-scoped state** — config, corps, weekly missions and selections,
tickets — lives under `guilds/{guild_id}/…`. Contracts and notifications are
stored directly in Firestore too.

**Writes are buffered.** `store.save_if_dirty()` is driven by the `auto_save`
loop in `cogs/xp.py` every `settings.AUTO_SAVE_INTERVAL` seconds (300). The flush
goes to **Firestore only** — `settings.DATA_FILE` (`data/users.json`) is a
leftover of the pre-Firestore store that nothing reads or writes any more.

All Firestore operations are **synchronous** (the firebase-admin SDK), while
Discord and API handlers are async. Use `await store.add_balance(...)` for async
write helpers; reads like `store.get_user(...)` are plain synchronous calls.

---

## Contracts

`contract_actions.py` is the one implementation of every contract state
transition — accept, submit, review, dispute, give up, settle, more-time. Discord
views, the KSP endpoints and the website endpoints are all thin callers. That
matters because the same transition used to exist in three places and had already
drifted.

Craft compatibility, mission limits and orbit requirements are checked at three
points: in the editor (mod-side `EditorPartEnforcer`), at submit time on the
client, and authoritatively on `/submit` here.

**Crew professions** (`crew_traits`) are matched by the exact
`ProtoCrewMember.trait` string on both ends, which is what lets a contract written
on a modded install still mean something on one without it. Which *mod* defines a
modded profession is the one thing that string cannot express and no part walk
can recover, so it is written down twice and kept in sync by comment:
`data/mission_constraints.py::_TRAIT_MODS` and the mod's
`ContractConstraints.cs::TraitMods` (the same convention as `ENGINE_CATEGORIES` ↔
`PartClassifier.GetEngineCategories`).

Both tables are **closed** — an unlisted profession yields no mod name rather
than a guessed one. Only a *floor* names its mod: a ceiling ("no Kolonists") is
satisfied by not having the mod, so naming it would read as advice to install
something in order to obey a ban.

---

## AI Integration

Gemini (`google-genai`, one model — `_MODEL` in `cogs/screenshots.py`) is called
from **six** places:

| Call site | What it does |
|-----------|--------------|
| `cogs/screenshots.py::_run_gemini` | Screenshot analysis — images in, craft/location/difficulty JSON out |
| `cogs/contract_views.py` | Reviews a Discord-issued contract submission (mission text + screenshots) |
| `api_server.py::_ai_review_submission` | The same review for an in-game submission, plus vessel telemetry and loadmeta |
| `api_server.py::_classify_single_contract` | One contract's mission text → `craft_build` vs `active_vessel`, situation, body, `constraints` |
| `api_server.py::_classify_missions` | The same for a whole week's missions at once |
| `api_server.py::_ai_resolve_part` | Pins a loosely-named part in a mission text to an installed one |

**Every call site goes through `active_client()`**, which returns `None` when
`GEMINI_API_KEY` is unset **or** the monthly `cost_guard` budget is spent — so a
blown budget degrades exactly like a missing key. Each site has a fallback:
screenshot analysis is disabled, classification and constraint extraction fall
back to the keyword heuristics in `data/mission_constraints.py`, and a submission
review auto-accepts.

Weekly missions are **not** AI-generated. `cogs/weeklymissions.py` draws them
from `data/mission_templates.py`, and their descriptions are authored by an admin
command; AI only *classifies* them. Classification results are cached in
Firestore (`mission_classifications`) so AI runs at most once per week per set of
missions.

**On language.** Anything Gemini *reads* is language-agnostic — a mission can be
written in any language and still be classified. Two things are not: the
heuristic fallbacks (`extract_heuristic`, `_classify_text_heuristic`,
`orbit_constraints`) match hardcoded English + Turkish keyword tables, and
screenshot analysis pins its text fields to English on purpose. The Turkish
tables are **not dead code** even though the bot's own strings are English-only:
the crew and crew-profession heuristic runs on every classification to fill
bounds the AI left unset, not only when the AI is unavailable. Of everything
Gemini returns, the only free text is a review's `reason`.

---

## Cost Guard

`cost_guard.py` tracks what Gemini and Firebase cost each month, and brakes
before the bill does. **Three tiers**, because no single source is both fast
enough to brake on and accurate enough to trust.

| Tier | Source | Latency | Role |
|------|--------|---------|------|
| **0** | `data/firebase_guard.py` — transparent proxies counting every op and byte as `store`'s `_db` / `_storage_bucket` handles are used | instant | **The trigger.** Wrapping those two handles meters all 16 call-site modules at once |
| **1** | `data/gcp_metrics.py` — Cloud Monitoring | minutes | **The truth.** Adopted as a baseline by `ingest_usage`; tier 0 keeps counting on top and the gap is kept and reported as `drift` |
| **2** | `data/gcp_billing.py` — the BigQuery billing export | hours | **Display only.** Actual billed dollars, net of free-tier credits |

Tier 0 is instant, which is the only property a brake needs — and it cannot be
right. A signed or public URL is fetched straight from GCS, so those bytes never
pass through the process. That is not a small gap: measured on this project it
was **773 KB seen against 469 MB actual**. Tier 1 sees that egress, the bytes at
rest, and any usage that isn't the bot at all (scripts, the console).

Tier 2 is deliberately never read by `_level_locked`: a brake fed by a source
that lands a few times a day would let a runaway spend for a whole export cycle
first. Its value is being right *without any modelling* — the free tier arrives
as negative `credits` rows, so `cost + credits` is the invoice rather than a
guess at one, and where it disagrees with tier 1 the error is in our own price
constants.

Both GCP readers call the REST APIs with `google-auth` + `aiohttp` (both already
present) instead of `google-cloud-monitoring` / `google-cloud-bigquery`, so they
add **no dependency**. Each needs an IAM grant the Firebase key lacks by default:
`roles/monitoring.viewer` on the project for tier 1, and
`roles/bigquery.dataViewer` on the dataset plus `roles/bigquery.jobUser` on the
project for tier 2. Without them every call 403s and the guard simply runs on the
tiers it does have.

### The enforcement ladder

```
NORMAL → WARN → DEGRADED → FROZEN
                    │          └─ require_firebase raises FirebaseBudgetExceeded
                    │             on everything
                    └─ Storage *uploads* refused; reads, downloads and Firestore
                       keep working, so the bot stays usable
```

Freezing arms exactly one `final_flush` grace pass, because flushing `store`'s
memory buffer is itself a Firestore write — and a stop that refuses it converts
"we stopped spending" into "we lost everyone's last few minutes of XP".

The free tier is modelled **per day and in US/Pacific** (`FREE_TIER_TZ`), which
is how Google actually applies it. Charging from operation #1, as the first
version did, reported $0.0059 on a $0.00 bill.

State is **local only** (`data/cost_state.json`, archived to
`data/cost_history.jsonl` on rollover): the meter must survive Firebase being the
thing cut off, and must not itself cost Firestore reads. Levels are announced to
the owner by `cogs/costwatch.py`, which drains a queue rather than being called
directly — the guard runs on whatever thread firebase-admin is on, which is no
place to touch Discord. Surfaced by `/costs` and the console's Costs tab; budgets
and the guard switch are runtime-flippable, since the failure worth planning for
is a *false* stop from wrong price constants.

---

## Security Notes

- `.env` and the Firebase credentials are **never** committed. Check
  `.gitignore` before adding either.
- All persistent data lives in Firestore; nothing sensitive is kept on disk
  besides the cost meter's own state.
- **No secret ever reaches a client.** The KSP mod and the website hold only a
  signed session token.
- Session tokens are HMAC-SHA256 signed. `API_SECRET_KEY_PREVIOUS` exists so the
  key can be rotated without invalidating every live session at once.
- **Mimic safety**: the admin mimic system patches three internal discord.py
  dispatch points so `interaction.user` is swapped before any handler sees it.
  Any code that needs the *real* user must read
  `interaction.extras.get("_mimic_real_user")` — and every permission check must
  go through `cogs/perms.py`, or an admin mimicking the owner would borrow the
  owner's authority.
- **Runtime gates** (`KSP_VERSION_CHECK_ENABLED`, `KSP_DEVICE_BINDING_ENABLED`,
  `KSP_2FA_ENABLED`, `API_DOCS_ENABLED`, `DEBUG_ENDPOINTS_ENABLED`) can be
  flipped from the console, but `.env` is the boot-time truth.

### Service suspensions

A suspension (`data/suspensions.py` + `enforce_not_suspended`) is a **timed block
on the API surface** — the KSP client and the website — issued from the console's
Users tab and enforced in `get_user_token_only`, so every token-gated endpoint is
covered by one check.

It is deliberately **not** a Discord ban (`cogs/moderation.py` owns those, and
they act on guild membership) and **not** a wipe: balance, XP, contracts and
listings are untouched and waiting. There is no permanent option — `MAX_HOURS`
caps one at a year — because a suspension that never ends is a ban wearing a
disguise.

Refusal is `403 {"code": "suspended", reason, until}`, structured like the device
gate so the client can *draw* it. Sessions are deliberately **not** revoked: a
revoked token drops the mod to its link screen, whose only offer — link again —
would work and change nothing, whereas a live token means every request comes
back carrying the explanation.

Three endpoints are exempt, via `get_user_allow_suspended`:

- `/api/v1/auth/suspension` — the "check again" read, the only endpoint that
  answers a suspended client normally, so a *lifted* suspension has something to
  report
- `/api/v1/auth/logout_all` — the user's own privacy control; a punishment must
  not take it away
- the Discord-side data purge, which was never an API endpoint

Expiry is resolved **on read** (`until > now`, no sweeper) — on the server, in
the 30-second in-process cache (clamped so an entry never outlives the suspension
it describes), and in the mod's own `Update`. A Firestore read failure fails
**open**: an outage that suspended everyone would be far worse than a suspended
player getting extra minutes.

---

## What Discord No Longer Does

Three surfaces were retired from the bot, and each left something behind on
purpose.

**Contract creation** (`/contract`, `/flagcontract`) is gone. A contract is
written in the mod's `ContractForm` or on the website — the only places that can
read the craft, the mod list, and the orbit/Δv margins the mission is later
judged against.

**Contract submission** went with it, bar one case: `SubmitButton` still serves a
`flag_design` contract, because a flag genuinely *is* just an image handed over,
and Discord is the only submission path it has ever had (there is no in-game
upload for one, and `_submit_flag` in `cogs/contract_views.py` is the sole writer
of `flag_fullres_url` / `flag_preview_url`). Everything else answers with
`SUBMIT_MOVED_NOTICE` rather than a dead interaction, since work views posted to
corp channels outlive the code that drew them — so `ContractWorkView` takes a
`mission_type` and only draws Submit for a flag. The AI auto-review that used to
live in that view is not lost: `api_server._ai_review_submission` /
`_auto_accept_contract` are the same thing on the in-game path, and the weekly
mission card says so (`weeklymissions.SUBMIT_IN_KSP`).

**The marketplace** is gone entirely — no mirrored listings, no Buy/Delist/Load
buttons, no `/market` or `/delist`, no `marketplace` channel key, and
`POST /api/v1/marketplace/list` no longer requires a Discord channel to exist
before it will accept a listing. `cogs/marketplace.py` survives as a
**tombstone** that claims the three old custom_ids (`mk_buy`, `mk_delist`,
`mk_load`) and answers them with a pointer to the website: the mirrored messages
are still in people's channels, and Discord renders an unclaimed custom_id as
"This interaction failed", which reads as *broken* rather than *moved*. The
`mirrors` field on a listing is kept for shape only; nothing writes or reads it
now.

**Auctions are untouched** and still live in Discord — a bidding game between
people is the one thing a channel does better than a game client. (Creation moved
to the mod; bidding and ending are Discord and the website.)

---

## Tests

The suites are **standalone scripts**, not pytest — each one runs itself and
exits non-zero on failure, so they need no test runner and no plugins:

```bash
source .venv/bin/activate
python test_cost_guard.py
python test_contract_actions.py
python test_suspensions.py
python test_security_signing.py
python test_security_invariants.py
python test_auth_hardening.py
```

| Suite | Covers |
|-------|--------|
| `test_cost_guard.py` | The three tiers, the free-tier day model, the enforcement ladder, the final-flush grace pass |
| `test_contract_actions.py` | Every contract state transition |
| `test_suspensions.py` | The gate, the exemptions, expiry-on-read, the bounded maximum |
| `test_security_signing.py` | Signed-URL behaviour, with real V4 signing done locally |
| `test_security_invariants.py` | Source invariants and the sanitizer algorithm spec |
| `test_auth_hardening.py` | Key rotation, device-gate fail-open, sweep defense |

`../run_security_tests.sh` runs the security-focused subset from the repo root —
the three security suites above plus the website's own header and allow-list
checks (`Website/test_website_security.mjs`, which needs Node). It is offline:
no network, no Firebase operations, no KSP.

There is also `test_checks.py` / `test_limit.py`, small scratch checks kept
alongside the real suites.

---

## Related Docs

| Doc | What it is |
|-----|------------|
| [`../CLAUDE.md`](../CLAUDE.md) | The architectural rationale for both components — read this before changing anything load-bearing |
| [`../DEV-SETUP.md`](../DEV-SETUP.md) | Toolchain state, project structure, day-to-day commands |
| [`../DEPLOYMENT.md`](../DEPLOYMENT.md) | The production VPS runbook (systemd, Caddy, secrets, health checks) |
| [`../KSP Mod Side/README.md`](../KSP%20Mod%20Side/README.md) | The other half of this project |

**`data/part_aliases.py` is generated**, not authored. Its source of truth is
`PartAliases.cs` in the KSP mod; regenerate it with
`python tools/gen_part_aliases.py` whenever that table changes.
