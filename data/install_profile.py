"""
data/install_profile.py — What is actually installed on one player's machine,
reduced to a small set of tags, so the weekly pool can offer them missions their
install can fly.

The problem this solves is visible in `data/mission_templates.py`: the pool has
carried `[Real Solar System] Land on Venus` and `[Far Future Technologies] Build
an antimatter factory` since those sections were written, and `_generate_missions`
sampled them for *everybody*. A stock player drew a mission naming a body their
game does not have, and a weekly mission is a bot-issued contract — it carries a
due date and a fine whether or not it can be flown. The bracketed tag was the only
thing standing between a stock install and an unflyable obligation, and a tag is
a label, not a gate.

So: every template declares what it needs (`requires`), this module says what a
player has, and generation intersects the two.

## Where the evidence comes from

`PartCatalogUploader` (KSP side) already uploads the install's full part list,
hash-gated, and `api_server.upload_part_catalog` stores it at
`guilds/{gid}/part_catalogs/{uid}`. Two fields were added to that upload for this
module, both optional so an older client keeps working:

  • `bodies` — every `FlightGlobals.Bodies` name. This is the only reliable way to
    see a planet pack. `ContractCreation.BuildActiveModlist()` lists GameData
    folders *that contributed a loaded part*, and Real Solar System contributes no
    parts at all — it is configs over the stock system. An RSS install is invisible
    in the modlist and obvious in the body list.
  • `mods` — that same GameData folder list. This is the reliable way to see a
    *parts* mod, which is what the body list cannot show: Near Future and Far
    Future add no bodies.

The two signals are complementary on purpose, and neither is trusted for anything
that pays out. A profile decides which missions a player is *offered*; it never
decides what a mission is worth. That distinction matters because both fields are
reported by the untrusted KSP client (`data/suspicion.py`), so a forged profile
buys the forger a different mission list and nothing else. Difficulty — and
therefore the reward — is authored per template in `data/mission_templates.py`,
where an RSS Mars landing is written as the difficulty-10 mission it is. The pay
difference between an RSS player and a stock player is real, and it is a property
of the mission rather than a multiplier hanging off a claim about an install.

## The tables are closed

Like `data/mission_constraints._TRAIT_MODS`, an unrecognised folder or body
produces no tag rather than a guessed one. The cost of a missing tag is that a
player is offered the universal pool, which every install can fly. The cost of a
guessed one is an unflyable contract with a fine attached, so the tables refuse to
guess.

Two things deliberately have no tag, because nothing in the upload can see them:

  • **JNSQ and other stock-name rescales.** They keep Kerbin/Mun/Duna and change
    the radii, so the body list is identical to stock. `body_radius` at submit time
    would show it (`data/telemetry_check.py` already flags the mismatch), but
    that arrives long after the board is drawn.
  • **Principia.** No parts, no bodies — it replaces the integrator.

Both are listed here so the next person does not spend the afternoon trying to
detect them from this data.
"""
from __future__ import annotations

import hashlib

# ── System class ─────────────────────────────────────────────────────────────
# Exactly one of these is always present, and it chooses the body-role map below.
SYS_STOCK = "sys:stock"
SYS_RSS = "sys:rss"

# ── Additive packs ───────────────────────────────────────────────────────────
PACK_OPM = "pack:opm"
PACK_KCALBELOH = "pack:kcalbeloh"

# ── Parts mods ───────────────────────────────────────────────────────────────
MOD_NEARFUTURE = "mod:nearfuture"
MOD_FARFUTURE = "mod:farfuture"
MOD_RO = "mod:ro"

# ── Life support ─────────────────────────────────────────────────────────────
# The four the KSP side actually has adapters for (KSP Mod Side/GeneKerman/
# LifeSupport/), plus the derived `ls:any` so a mission that needs *some* life
# support model — a greenhouse, a recycler — can be written once instead of four
# times. DeepFreeze is an adapter there too but adds no mission of its own.
LS_KERBALISM = "ls:kerbalism"
LS_USI = "ls:usi"
LS_TAC = "ls:tac"
LS_SNACKS = "ls:snacks"
LS_ANY = "ls:any"

_LS_TAGS = (LS_KERBALISM, LS_USI, LS_TAC, LS_SNACKS)

# Bodies that identify a system or a pack. Membership is required in full: one
# stray name (a mod that adds a body called "Moon") must not flip a stock install
# into the real solar system and start offering it Venus.
_RSS_BODIES = frozenset({"earth", "moon", "mars"})
_OPM_BODIES = frozenset({"sarnus", "urlum", "neidon"})
_KCALBELOH_BODIES = frozenset({"kcalbeloh"})

# GameData folder -> tag. Matched case-insensitively against the exact folder
# name; `_FOLDER_PREFIXES` covers the families that ship one folder per component.
_FOLDER_TAGS = {
    "farfuturetechnologies": MOD_FARFUTURE,
    "kerbalism": LS_KERBALISM,
    "kerbalismconfig": LS_KERBALISM,
    "kerbalismbootstrap": LS_KERBALISM,
    "umbraspaceindustries": LS_USI,
    "usilifesupport": LS_USI,
    "thunderaerospace": LS_TAC,
    "snacks": LS_SNACKS,
    "snacksutils": LS_SNACKS,
    "realismoverhaul": MOD_RO,
    "realfuels": MOD_RO,
    "realsolarsystem": SYS_RSS,
    "kcalbeloh": PACK_KCALBELOH,
    "opm": PACK_OPM,
    "outerplanetsmod": PACK_OPM,
}

# Folder-name prefixes, for the families that ship one folder per component:
# NearFuturePropulsion, NearFutureElectrical, ROEngines, ROTanks…
_FOLDER_PREFIXES = (
    ("nearfuture", MOD_NEARFUTURE),
    ("ro-", MOD_RO),
    ("roengines", MOD_RO),
    ("rotanks", MOD_RO),
    ("rocapsules", MOD_RO),
    ("rolibrary", MOD_RO),
    ("routils", MOD_RO),
)

# ── Body roles ────────────────────────────────────────────────────────────────
# A template writes `{home}` or `{gas_giant}` in its text and in `required_body`,
# and generation resolves the role against the player's system. One template then
# serves stock and RSS both, which is the point: "Reach a stable orbit around
# {home}" is the same mission in either, and it is a far harder one in RSS — which
# is why the RSS-only entries that need a difficulty of their own stay separate.
#
# A role that is None for a system means "this system has no such body", and a
# template naming it is simply not offered there. That is how Minmus-shaped
# missions stay out of an RSS board without a second table saying so.
_BODY_ROLES: dict[str, dict[str, str | None]] = {
    SYS_STOCK: {
        "star": "Kerbol",
        "home": "Kerbin",
        "moon": "Mun",
        "moon2": "Minmus",
        "inner_planet": "Moho",
        "hell_planet": "Eve",
        "hell_moon": "Gilly",
        "desert_planet": "Duna",
        "desert_moon": "Ike",
        "belt_planet": "Dres",
        "gas_giant": "Jool",
        "ocean_moon": "Laythe",
        "big_moon": "Tylo",
        "ice_moon": "Vall",
        "far_planet": "Eeloo",
    },
    SYS_RSS: {
        "star": "Sun",
        "home": "Earth",
        "moon": "Moon",
        "moon2": None,          # Earth has one moon; Minmus-shaped missions drop out
        "inner_planet": "Mercury",
        "hell_planet": "Venus",
        "hell_moon": None,      # Venus has no moon
        "desert_planet": "Mars",
        "desert_moon": "Phobos",
        "belt_planet": "Ceres",
        "gas_giant": "Jupiter",
        "ocean_moon": "Europa",
        "big_moon": "Ganymede",
        "ice_moon": "Callisto",
        "far_planet": "Pluto",
    },
}

ROLE_NAMES = frozenset(_BODY_ROLES[SYS_STOCK])


def system_of(tags) -> str:
    """The system class in `tags`. Stock is the answer when nothing says otherwise,
    which is also the answer for a client too old to send a body list."""
    return SYS_RSS if SYS_RSS in tags else SYS_STOCK


def roles_for(tags) -> dict[str, str | None]:
    """The body-role map for this profile's system."""
    return _BODY_ROLES[system_of(tags)]


def resolve_roles(text: str, roles: dict[str, str | None]) -> str | None:
    """Substitute `{role}` placeholders in `text`. Returns None when the text names
    a role this system has no body for — the caller drops that template.

    A plain string with no placeholder comes back unchanged, so a template that
    names a body outright (every `[Outer Planets Mod]` entry does) is unaffected.
    """
    if not text or "{" not in text:
        return text
    out = text
    for role, body in roles.items():
        token = "{" + role + "}"
        if token not in out:
            continue
        if not body:
            return None
        out = out.replace(token, body)
    # An unresolved placeholder means the template names a role that does not
    # exist. Refuse rather than ship "Land on {moonlet}" to a player.
    return None if "{" in out else out


def tags_for(catalog: dict | None) -> frozenset[str]:
    """Reduce one stored part catalog to its install tags.

    `catalog` is the document `api_server._get_user_catalog` returns:
    {"hash":…, "parts":[{"name","title"}], "bodies":[…], "mods":[…]}. Any of the
    three lists may be missing — an older client sends only `parts`, and the answer
    for that install is the universal pool, which it can certainly fly.
    """
    tags: set[str] = set()
    if not catalog:
        return frozenset({SYS_STOCK})

    bodies = {str(b).strip().lower() for b in (catalog.get("bodies") or []) if b}
    if bodies:
        if _RSS_BODIES <= bodies:
            tags.add(SYS_RSS)
        if _OPM_BODIES <= bodies:
            tags.add(PACK_OPM)
        if _KCALBELOH_BODIES <= bodies:
            tags.add(PACK_KCALBELOH)

    for raw in (catalog.get("mods") or []):
        folder = str(raw).strip().lower()
        if not folder:
            continue
        tag = _FOLDER_TAGS.get(folder)
        if tag:
            tags.add(tag)
            continue
        for prefix, ptag in _FOLDER_PREFIXES:
            if folder.startswith(prefix):
                tags.add(ptag)
                break

    # Exactly one system class, always.
    if SYS_RSS not in tags:
        tags.add(SYS_STOCK)
    else:
        tags.discard(SYS_STOCK)

    # Derived: "some life support model is installed".
    if any(t in tags for t in _LS_TAGS):
        tags.add(LS_ANY)

    return frozenset(tags)


def bucket_id(tags) -> str:
    """A short stable id for this set of tags.

    This is the unit the weekly supplement is generated for. Two players with the
    same mods share a bucket and therefore share a board, which is what keeps the
    cost of generating install-aware missions flat as the community grows: the work
    is per distinct install, not per player, and a community of any size has a few
    dozen distinct installs at most.
    """
    joined = ",".join(sorted(tags))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:8]
