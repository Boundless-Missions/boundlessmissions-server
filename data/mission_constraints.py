"""
data/mission_constraints.py — Part-restriction ("mission limit") extraction & verification.

A contract's mission text can carry restrictions on what the craft is allowed to
use, e.g. "You must use a nuclear engine", "You can't use the Thud engine",
"Lqd He3 powered engines only", or "heatshield-less re-entry". This module turns
that natural-language text into a structured `constraints` dict, and verifies a
craft's actually-used parts (reported by the KSP client) against it.

The same canonical schema is enforced in three places:
  • the KSP editor (forbidden parts are hidden — see EditorPartEnforcer.cs)
  • the KSP submit gate (client-side pre-check — see SubmitWindow.cs)
  • the bot's /submit endpoint (authoritative re-check — see api_server.py)

Canonical constraints dict (every key optional; omitted/empty == no restriction):
    {
      "forbidden_parts":              [str],  # title substrings, e.g. "Thud"
      "required_parts":               [str],
      "forbidden_propellants":        [str],  # resource names, e.g. "LqdHe3"
      "required_propellants":         [str],
      "forbidden_engine_categories":  [str],  # semantic: nuclear/ion/solid/...
      "required_engine_categories":   [str],
      "forbidden_part_categories":    [str],  # e.g. "heatshield", "parachute"
      "required_part_categories":     [str],
      "max_parts":                    int,    # part-count ceiling (optional)
      "min_parts":                    int,    # part-count floor (optional)
      "max_dv":                       float,  # vacuum Δv ceiling, m/s (optional)
      "min_dv":                       float,  # vacuum Δv floor, m/s (optional)
      "max_crew":                     int,    # crew-aboard ceiling (optional)
      "min_crew":                     int,    # crew-aboard floor (optional)
      "crew_traits":                  {str: {"min": int, "max": int}},  # per-profession
      "notes":                        str,    # human-readable summary (optional)
    }

`crew_traits` is the same floor/ceiling idea applied per profession — "send two
pilots and a scientist" => {"Pilot": {"min": 2}, "Scientist": {"min": 1}}, "no
tourists" => {"Tourist": {"max": 0}}. Keys are canonical KSP trait names, spelled
exactly as `ProtoCrewMember.trait` holds them, because that string is what both
ends match on. That matters for modded professions (USI/MKS Kolonist, Miner,
Medic…): the trait string survives in a save even where the mod defining it is
absent, so matching by name works on any install — while a contract that asks for
a profession the contractor hasn't got is simply one they cannot fill, and the
violation message says so rather than pretending nobody was aboard.

Crew is stated as a floor, a ceiling, or both ("exactly N" == min_crew == max_crew).
`max_crew` is the one bound where **0 is a real value**, not "unset": an uncrewed
mission ("unmanned probe", "no kerbals aboard") is a ceiling of zero. Every read of
it therefore tests `is not None` rather than truthiness, and the KSP client uses -1
(not 0) as its "no limit" sentinel.

Δv (delta-v) is a whole-craft metric, so unlike the part rules it can't be
enforced by hiding parts in the editor. It's checked only at submit time: the
KSP client reports the craft's stock-calculated vacuum Δv and the bot verifies
it against max_dv/min_dv (the value is client-reported, like used_parts).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── Vocabulary ──────────────────────────────────────────────────────────────

LIST_KEYS = (
    "forbidden_parts", "required_parts",
    "forbidden_propellants", "required_propellants",
    "forbidden_engine_categories", "required_engine_categories",
    "forbidden_part_categories", "required_part_categories",
)

# Semantic engine categories the KSP-side PartClassifier can derive. Keep these
# in sync with PartClassifier.cs::GetEngineCategories.
ENGINE_CATEGORIES = {
    "nuclear", "ion", "solid", "chemical", "electric", "monoprop", "rcs",
}

# Natural-language profession word -> canonical KSP trait name. The canonical form
# is the exact string KSP stores in ProtoCrewMember.trait, since that is what the
# client counts against and what survives in a save whose defining mod is missing.
#
# Stock ships four; the rest are the USI/MKS professions, which are the ones that
# actually turn up in this community's saves. The table is closed on purpose: a
# count in front of an unknown noun ("2 rovers") must not become a crew rule.
_TRAIT_ALIASES = {
    "pilot": "Pilot", "pilots": "Pilot", "pilotu": "Pilot", "pilotlar": "Pilot",
    "engineer": "Engineer", "engineers": "Engineer",
    "mühendis": "Engineer", "mühendisler": "Engineer", "muhendis": "Engineer",
    "scientist": "Scientist", "scientists": "Scientist",
    "bilim insanı": "Scientist", "bilim insanları": "Scientist",
    "bilim adamı": "Scientist", "bilimci": "Scientist",
    "tourist": "Tourist", "tourists": "Tourist", "turist": "Tourist", "turistler": "Tourist",
    "kolonist": "Kolonist", "kolonists": "Kolonist",
    "colonist": "Kolonist", "colonists": "Kolonist",
    "miner": "Miner", "miners": "Miner", "madenci": "Miner",
    "mechanic": "Mechanic", "mechanics": "Mechanic", "tamirci": "Mechanic",
    "technician": "Technician", "technicians": "Technician", "teknisyen": "Technician",
    "medic": "Medic", "medics": "Medic", "doktor": "Medic", "sağlıkçı": "Medic",
    "quartermaster": "Quartermaster", "quartermasters": "Quartermaster",
    "scout": "Scout", "scouts": "Scout", "izci": "Scout",
    "biologist": "Biologist", "biologists": "Biologist", "biyolog": "Biologist",
    "geologist": "Geologist", "geologists": "Geologist", "jeolog": "Geologist",
    "botanist": "Botanist", "botanists": "Botanist", "botanikçi": "Botanist",
    "chemist": "Chemist", "chemists": "Chemist", "kimyager": "Chemist",
    "farmer": "Farmer", "farmers": "Farmer", "çiftçi": "Farmer",
}

# Canonical trait names, for validating an AI-supplied `crew_traits` object.
TRAIT_NAMES = set(_TRAIT_ALIASES.values())

# Canonical trait -> the mod that defines it. Stock's four are absent on purpose:
# a profession every install already has is not a dependency worth naming.
#
# This is the one thing a trait name cannot express and no part walk can recover.
# Every mod-detection path in this project resolves *parts* to a GameData folder
# (`CkanGenerator.GetModFolder`), and a profession requirement has no part to walk
# — the same blind spot Textures Unlimited needed its own side channel for. So the
# mapping is written down rather than derived.
#
# Deliberately coarse (one mod per profession, the one this community actually gets
# it from) and closed like `_TRAIT_ALIASES`: a trait with no entry produces no hint
# rather than a guessed name, because sending a player to install the wrong mod is
# worse than telling them only which profession they're missing. Kept in sync with
# `ContractConstraints.cs::TraitMods` on the KSP side, which says the same thing in
# the submit pre-flight — the two naming different mods for one profession would
# read as two different problems.
_TRAIT_MODS = {
    "Kolonist": "USI/MKS",
    "Miner": "USI/MKS",
    "Mechanic": "USI/MKS",
    "Technician": "USI/MKS",
    "Medic": "USI/MKS",
    "Quartermaster": "USI/MKS",
    "Scout": "USI/MKS",
    "Biologist": "USI/MKS",
    "Geologist": "USI/MKS",
    "Botanist": "USI/MKS",
    "Chemist": "USI/MKS",
    "Farmer": "USI/MKS",
}


def trait_mod(trait: str | None) -> str | None:
    """The mod that defines a profession, or None for stock's four and for anything
    `_TRAIT_MODS` doesn't know."""
    if not trait:
        return None
    return _TRAIT_MODS.get(str(trait).strip()) or _TRAIT_MODS.get(
        _TRAIT_ALIASES.get(str(trait).strip().lower(), ""))


def crew_trait_mod_requirements(constraints: dict | None) -> list[str]:
    """One phrase per mod a contract's professions need ("USI/MKS (Kolonist, Miner)"),
    grouped by mod so two professions from one install read as one thing to install.

    Only floors count: a ceiling ("no Kolonists aboard") is satisfied by not having
    the mod at all, so naming it would be advice to install something in order to
    obey a ban.
    """
    by_mod: dict[str, list[str]] = {}
    for trait, bounds in ((constraints or {}).get("crew_traits") or {}).items():
        if not bounds.get("min"):
            continue
        mod = trait_mod(trait)
        if mod:
            by_mod.setdefault(mod, []).append(str(trait))
    return [f"{mod} ({', '.join(traits)})" for mod, traits in by_mod.items()]

# Natural-language phrase -> canonical engine category.
_ENGINE_CATEGORY_ALIASES = {
    "nuclear": "nuclear", "ntr": "nuclear", "nerv": "nuclear", "fission": "nuclear",
    "fusion": "nuclear", "atomic": "nuclear", "nükleer": "nuclear",
    "ion": "ion", "iyon": "ion",
    "solid": "solid", "srb": "solid", "solid fuel": "solid", "solid booster": "solid",
    "katı yakıt": "solid",
    "chemical": "chemical", "kimyasal": "chemical",
    "electric": "electric", "electrical": "electric", "elektrik": "electric",
    "monoprop": "monoprop", "monopropellant": "monoprop", "mono propellant": "monoprop",
    "rcs": "rcs",
}

# Natural-language phrase -> canonical KSP resource (propellant) name. The KSP
# client matches these case-insensitively against the real resource names burnt
# by each engine, so modded resources work as long as the phrase appears here or
# the text already uses the resource's exact name.
_PROPELLANT_ALIASES = {
    "lqd he3": "LqdHe3", "lqdhe3": "LqdHe3", "liquid he3": "LqdHe3",
    "liquid helium-3": "LqdHe3", "helium-3": "LqdHe3", "helium 3": "LqdHe3", "he3": "LqdHe3",
    "lqd hydrogen": "LqdHydrogen", "liquid hydrogen": "LqdHydrogen", "lh2": "LqdHydrogen",
    "lqd deuterium": "LqdDeuterium", "deuterium": "LqdDeuterium",
    "liquid fuel": "LiquidFuel", "liquidfuel": "LiquidFuel",
    "oxidizer": "Oxidizer",
    "monopropellant": "MonoPropellant", "monoprop": "MonoPropellant",
    "xenon": "XenonGas", "xenon gas": "XenonGas",
    "solid fuel": "SolidFuel", "solidfuel": "SolidFuel",
    "methane": "LqdMethane", "lqd methane": "LqdMethane", "liquid methane": "LqdMethane",
    "argon": "ArgonGas",
}

# Part-category phrases the client can tag (PartClassifier.cs::GetPartCategories).
_PART_CATEGORY_ALIASES = {
    "heatshield": "heatshield", "heat shield": "heatshield", "heat-shield": "heatshield",
    "ablator": "heatshield", "ısı kalkanı": "heatshield",
    "parachute": "parachute", "chute": "parachute", "paraşüt": "parachute",
    "solar panel": "solarpanel", "solar": "solarpanel", "solarpanel": "solarpanel",
    "güneş paneli": "solarpanel",
    "wheel": "wheel", "landing gear": "wheel", "tekerlek": "wheel",
    "ladder": "ladder", "merdiven": "ladder",
    "reaction wheel": "reactionwheel", "rtg": "rtg",
}

# Negation cues. A clause containing any of these is a *forbidding* clause,
# even if it also reads like a requirement ("doesn't use X-powered engines").
# Negation is checked first and dominates, so it flips "powered"/"use" intent.
_NEG_CUES = (
    "n't",          # doesn't / can't / won't / shouldn't / isn't ...
    "doesnt", "dont", "cant", "wont", "shouldnt", "isnt", "arent",
    "does not", "do not", "can not", "will not",
    "without", "never", "avoid", "not allowed", "forbidden", "banned",
    "prohibited", "no use of", "free of", "-less", "lacking",
    " no ", " not ",
    "kullanma", "yasak", "olmadan", "kullanamaz", "izin yok", "olmasın",
)
# Explicit forbid phrases (negation cues above also count as forbidding).
_FORBID_CUES = (
    "can't use", "cant use", "cannot use", "can not use", "without",
    "not allowed", "forbidden", "banned", "don't use", "dont use", "avoid",
    "may not use", "must not", "prohibited", "-less",
    "kullanma", "yasak", "olmadan", "kullanamaz", "izin yok",
)
_REQUIRE_CUES = (
    "must use", "have to use", "only use", "use only", "required", "must be",
    "should use", "needs to use", "powered by", "powered", "only", "must have",
    "kullanmalı", "kullanmak zorunda", "sadece", "zorunlu", "gerek",
)
# Weaker require cues that need whole-word boundaries (so "with" doesn't match
# "within"). Matched against a space-padded clause. "a craft with a Vector
# engine" / "that has SRBs" reads as a requirement.
_REQUIRE_BOUNDARY_CUES = (
    " with ", " has ", " have ", " having ", " using ", " featuring ",
    " equipped ", " ile ", " olan ", " sahip ",
)


# ── Normalisation ────────────────────────────────────────────────────────────

def empty() -> dict:
    """A constraints dict with no restrictions."""
    return {k: [] for k in LIST_KEYS}


def is_empty(constraints: dict | None) -> bool:
    """True when there is nothing to enforce."""
    if not constraints:
        return True
    if any(constraints.get(k) for k in LIST_KEYS):
        return False
    # max_crew 0 ("uncrewed") is a restriction, so it's tested for presence.
    if constraints.get("max_crew") is not None:
        return False
    return not (constraints.get("max_parts") or constraints.get("min_parts")
                or constraints.get("max_dv") or constraints.get("min_dv")
                or constraints.get("min_crew") or constraints.get("crew_traits"))


def _as_str_list(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        val = [val]
    out = []
    for x in val:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def normalize(raw: dict | None) -> dict:
    """
    Coerce a possibly-AI-produced dict into the canonical schema: every list key
    present, deduped, with categories lower-cased and mapped through the alias
    tables so free-form AI output ("Nuclear", "He-3") lands on canonical tokens.
    """
    raw = raw or {}
    out = empty()

    for key in ("forbidden_parts", "required_parts"):
        out[key] = _dedupe(_as_str_list(raw.get(key)))

    for key in ("forbidden_propellants", "required_propellants"):
        out[key] = _dedupe(_map_tokens(raw.get(key), _PROPELLANT_ALIASES, keep_unknown=True))

    for key in ("forbidden_engine_categories", "required_engine_categories"):
        out[key] = _dedupe(_map_tokens(raw.get(key), _ENGINE_CATEGORY_ALIASES,
                                       allowed=ENGINE_CATEGORIES))

    for key in ("forbidden_part_categories", "required_part_categories"):
        out[key] = _dedupe(_map_tokens(raw.get(key), _PART_CATEGORY_ALIASES, keep_unknown=True,
                                       lower=True))

    for key in ("max_parts", "min_parts", "min_crew"):
        val = raw.get(key)
        if isinstance(val, bool):
            continue
        try:
            iv = int(val)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out[key] = iv

    # max_crew alone keeps 0 — "fly this uncrewed" is a ceiling, not an absent one.
    # A negative value is still treated as "unset" (the client's no-limit sentinel).
    _mx = raw.get("max_crew")
    if not isinstance(_mx, bool):
        try:
            _mxi = int(_mx)
        except (TypeError, ValueError):
            _mxi = None
        if _mxi is not None and _mxi >= 0:
            out["max_crew"] = _mxi

    for key in ("max_dv", "min_dv"):
        val = raw.get(key)
        if isinstance(val, bool):
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            out[key] = fv

    traits = _normalize_crew_traits(raw.get("crew_traits"))
    if traits:
        out["crew_traits"] = traits

    notes = raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:300]
    _resolve_conflicts(out)
    return out


def _normalize_crew_traits(raw) -> dict:
    """Coerce a `crew_traits` object into {CanonicalTrait: {"min": int, "max": int}}.

    Tolerates the two shapes an AI (or a future caller) is likely to produce: the
    full bounds object, and the shorthand {"Pilot": 2} meaning a floor of two.
    Unknown profession words are dropped rather than kept — a name no install
    defines is a requirement nobody could ever satisfy, and silently failing every
    submission is worse than ignoring the phrase.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, val in raw.items():
        canon = _TRAIT_ALIASES.get(str(key).strip().lower())
        if canon is None and str(key).strip() in TRAIT_NAMES:
            canon = str(key).strip()
        if canon is None:
            continue

        bounds = val if isinstance(val, dict) else {"min": val}
        clean: dict[str, int] = {}
        for bound in ("min", "max"):
            v = bounds.get(bound)
            if v is None or isinstance(v, bool):
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            # A floor of zero says nothing; a ceiling of zero says "none of these".
            if iv < 0 or (bound == "min" and iv == 0):
                continue
            clean[bound] = iv
        if not clean:
            continue
        # A floor above its own ceiling is unsatisfiable; the ceiling wins, as it
        # does for the whole-crew band below.
        if "min" in clean and "max" in clean and clean["min"] > clean["max"]:
            clean.pop("min")
        merged = out.setdefault(canon, {})
        for bound, iv in clean.items():
            # Two phrases naming the same profession: keep the tighter of each bound.
            merged[bound] = (max(merged[bound], iv) if bound == "min" and bound in merged
                             else min(merged[bound], iv) if bound in merged else iv)
    return {k: v for k, v in out.items() if v}


# (forbidden, required) key pairs that must not share a token — a craft can't
# both must-use and must-not-use the same thing.
_CONFLICT_PAIRS = (
    ("forbidden_parts", "required_parts"),
    ("forbidden_propellants", "required_propellants"),
    ("forbidden_engine_categories", "required_engine_categories"),
    ("forbidden_part_categories", "required_part_categories"),
)


def resolve_conflicts(constraints: dict | None) -> dict | None:
    """Public entry point for `_resolve_conflicts`, for callers that merge two
    already-normalised constraint dicts (AI + heuristic) and can therefore create a
    contradiction neither source contained. Mutates and returns `constraints`."""
    if constraints:
        _resolve_conflicts(constraints)
    return constraints


def _resolve_conflicts(constraints: dict) -> None:
    """Drop any token present in both a forbidden and required list (forbidden
    wins — an explicit ban is rarely a mistake, while a spurious requirement
    would block an otherwise-valid craft). Mutates `constraints` in place."""
    for forbid_key, require_key in _CONFLICT_PAIRS:
        banned = {v.lower() for v in constraints.get(forbid_key, [])}
        if banned:
            constraints[require_key] = [
                v for v in constraints.get(require_key, []) if v.lower() not in banned
            ]

    # An impossible crew band (floor above ceiling) can only come from two phrases
    # in one text being read as one rule — "fly it uncrewed" next to "rescue 2
    # kerbals". The ceiling wins, for the same reason a ban beats a requirement:
    # dropping the floor leaves a mission that can still be flown.
    mx, mn = constraints.get("max_crew"), constraints.get("min_crew")
    if mx is not None and mn is not None and mn > mx:
        constraints.pop("min_crew", None)

    # Same rule one level down: an uncrewed mission cannot also demand a pilot, and
    # profession floors cannot add up to more than the whole-crew ceiling. Both come
    # from one text describing two things, and both make the mission unflyable — so
    # the floors go and the ceiling stands.
    traits = constraints.get("crew_traits")
    if traits:
        if mx is not None:
            for name, bounds in list(traits.items()):
                if bounds.get("min", 0) > mx:
                    bounds.pop("min", None)
                if not bounds:
                    traits.pop(name)
            if sum(b.get("min", 0) for b in traits.values()) > mx:
                for bounds in traits.values():
                    bounds.pop("min", None)
                traits = {k: v for k, v in traits.items() if v}
        constraints["crew_traits"] = traits
        if not traits:
            constraints.pop("crew_traits")


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        k = it.lower()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _map_tokens(val, aliases: dict, *, allowed: set | None = None,
                keep_unknown: bool = False, lower: bool = False) -> list[str]:
    result = []
    for tok in _as_str_list(val):
        low = tok.lower().strip()
        mapped = aliases.get(low)
        if mapped is None:
            # Try a contains-match so "nuclear engine" -> "nuclear".
            for phrase, canon in aliases.items():
                if phrase in low:
                    mapped = canon
                    break
        if mapped is not None:
            result.append(mapped)
        elif allowed is not None and low in allowed:
            result.append(low)
        elif keep_unknown:
            result.append(low if lower else tok)
    return result


# ── Heuristic extraction (fallback when no AI / AI failure) ──────────────────

def extract_heuristic(text: str) -> dict:
    """
    Keyword-based constraint extraction. Splits the text into clauses, decides
    whether each clause forbids or requires, and scans it for known engine
    categories, propellants and part categories. Deliberately conservative —
    only emits a restriction when a clause clearly pairs a cue word with a known
    term, so ordinary mission flavour text produces no constraints.
    """
    out = empty()
    if not text:
        return out

    low = text.lower()
    # Split into clauses on sentence / list punctuation.
    import re
    clauses = re.split(r"[.;\n!?]|,(?=\s*(?:no|you|the|only|and|must|can))", low)

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        forbid = _clause_polarity(clause)
        if forbid is None:
            continue
        eng_key = "forbidden_engine_categories" if forbid else "required_engine_categories"
        prop_key = "forbidden_propellants" if forbid else "required_propellants"
        cat_key = "forbidden_part_categories" if forbid else "required_part_categories"

        for phrase, canon in _ENGINE_CATEGORY_ALIASES.items():
            if _word_in(phrase, clause):
                out[eng_key].append(canon)
        for phrase, canon in _PROPELLANT_ALIASES.items():
            if _word_in(phrase, clause):
                out[prop_key].append(canon)
        for phrase, canon in _PART_CATEGORY_ALIASES.items():
            if _word_in(phrase, clause):
                out[cat_key].append(canon)

    # "heatshield-less" / "X-free" style: forbid even without a separate cue.
    for phrase, canon in _PART_CATEGORY_ALIASES.items():
        if f"{phrase}-less" in low or f"{phrase}less" in low or f"{phrase} free" in low \
                or f"{phrase}-free" in low or f"no {phrase}" in low:
            out["forbidden_part_categories"].append(canon)

    # Named parts, e.g. "can't use the Thud engine" / "use only the Mainsail".
    _extract_named_parts(text, out)

    # Part-count limits, e.g. "max 10 parts" / "at least 5 parts".
    _extract_part_count(text, out)

    # Delta-v limits, e.g. "at least 3000 m/s of delta-v" / "no more than 5 km/s dv".
    _extract_delta_v(text, out)

    # Crew-aboard limits, e.g. "crew of 3" / "carry at least 2 kerbals" / "2-4 crew".
    _extract_crew(text, out)

    # Per-profession crew, e.g. "two pilots and a scientist" / "no tourists".
    _extract_crew_traits(text, out)

    return normalize(out)


# Written-out counts accepted in crew phrases ("a crew of three", "üç kerbal").
# They're rewritten to digits before matching, so every pattern below only has to
# handle digits. Deliberately excludes Turkish "on" (10) and "bir" (1), which double
# as the English preposition and the Turkish indefinite article, and English "a"/"an",
# which would read "send a kerbal" as a hard count of one.
_CREW_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "single": 1, "solo": 1, "lone": 1,
    "sıfır": 0, "tek": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5,
    "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
}

# Phrases that mean "nobody aboard" — a crew ceiling of exactly 0.
_UNCREWED_CUES = (
    "uncrewed", "un-crewed", "unmanned", "crewless", "crew-less", "crewed by none",
    "no crew", "without crew", "without a crew", "no kerbals", "without kerbals",
    "zero crew", "0 crew", "probe only", "probe-only",
    "mürettebatsız", "insansız", "kerbalsız", "mürettebat olmadan", "mürettebat yok",
)

# Words that, right before a crew mention, mean the count describes *other people* —
# the kerbals to be rescued, not the crew the contractor flies with. Without this,
# "rescue 2 stranded kerbals" would demand the rescue ship carry exactly 2.
_CREW_ANTI_CUES = ("rescue", "rescuing", "save ", "saving", "stranded", "strand",
                   "kurtar", "mahsur", "return ", "bring back", "pick up", "retrieve")


def _crew_blocked(low: str, start: int) -> bool:
    """True when a crew mention at `start` is about kerbals being collected rather
    than kerbals aboard at launch."""
    window = low[max(0, start - 28):start]
    return any(cue in window for cue in _CREW_ANTI_CUES)


# Bound words that, sitting just before a bare "N kerbals", mean the number is one
# end of a range rather than an exact count — "at least 1 kerbal" is a floor, so the
# exact-count pattern has to keep its hands off it.
_CREW_QUALIFIERS = (
    "at least", "at most", "no more than", "no fewer than", "no less than", "up to",
    "max", "min", "more than", "less than", "fewer than", "greater than", "over",
    "under", "above", "below", "between", "or ", "to ", "and ",
    "en az", "en fazla", "en çok", "altında", "üzerinde", "ile ",
)


def _crew_qualified(low: str, start: int) -> bool:
    """True when a bound word immediately precedes the count at `start`."""
    return any(low[max(0, start - 18):start].rstrip().endswith(q.rstrip())
               for q in _CREW_QUALIFIERS)


def _digitize_counts(low: str) -> str:
    """Rewrite the number words in `_CREW_NUM_WORDS` to digits, whole-token only."""
    import re
    alts = "|".join(sorted((re.escape(w) for w in _CREW_NUM_WORDS), key=len, reverse=True))
    return re.sub(rf"(?<!\w)({alts})(?!\w)", lambda m: str(_CREW_NUM_WORDS[m.group(1)]), low)


def _extract_crew(text: str, out: dict) -> None:
    """Detect min/max crew-aboard limits. Handles 'crew of N', 'carry N kerbals',
    'N crew', inclusive ('at most/least N crew') and strict ('more/fewer than N')
    bounds, ranges ('between 2 and 4 crew' / '2-4 kerbals'), exact counts written as
    words ('a crew of three', 'tek kerbal'), the reverse word order ('crew size of
    at least 2'), and uncrewed missions ('unmanned probe' => max_crew 0). Most
    restrictive bound wins. A crew noun must be present so plain numbers don't trip
    it, and a count that belongs to a rescue ('save 2 stranded kerbals') is ignored —
    that's who's being fetched, not who's flying."""
    import re
    low = _digitize_counts(text.lower())
    maxes: list[int] = []
    mins: list[int] = []
    # English crew/kerbal(s)/astronaut(s) or Turkish mürettebat/kerbal.
    K = r"(?:crew(?:\s*members?)?|kerbals?|astronauts?|mürettebat\w*)"
    # "or fewer" after the noun turns what reads like a floor ("with 5 kerbals") into
    # a ceiling, so the floor patterns refuse to fire in front of it.
    # The \b matters: without it "kerbals" backtracks to "kerbal" and the lookahead
    # then reads the leftover "s" instead of the " or fewer" it exists to catch.
    NOT_CEIL = r"\b(?!\s*(?:or\s*(?:fewer|less)|veya\s*az))"

    def n(m, delta=0, gi=1):
        return int(m.group(gi)) + delta

    def add(bucket: list[int], m, value: int) -> None:
        if not _crew_blocked(low, m.start()):
            bucket.append(value)

    # Uncrewed: a ceiling of zero. Checked first — "no crew" must not also be read as
    # a floor by anything below.
    for cue in _UNCREWED_CUES:
        idx = low.find(cue)
        if idx < 0 or _crew_blocked(low, idx):
            continue
        # "uncrewed launches not allowed" says the opposite of "uncrewed" — and a
        # wrong ceiling of zero fails every submission, so the doubt is worth the
        # scan of what follows.
        tail = low[idx + len(cue):idx + len(cue) + 26]
        if any(neg in tail for neg in ("not allowed", "forbidden", "banned",
                                       "yasak", "olmaz", "kabul edilmez")):
            continue
        maxes.append(0)
        break

    # exactly N ("crew of 3", "exactly 2 kerbals", "crew: 3")
    for m in re.finditer(rf"(?:crew\s*of|exactly|precisely|tam)\s*(\d+)\s*{K}?", low):
        add(maxes, m, n(m)); add(mins, m, n(m))
    for m in re.finditer(rf"{K}\s*(?:size|count)?\s*[:=]\s*(\d+)", low):
        add(maxes, m, n(m)); add(mins, m, n(m))
    # adjectival exact counts: "a 3-kerbal lander", "3 kişilik mürettebat"
    for m in re.finditer(rf"(\d+)\s*-\s*{K}\b", low):
        add(maxes, m, n(m)); add(mins, m, n(m))
    for m in re.finditer(r"(\d+)\s*kişilik", low):
        add(maxes, m, n(m)); add(mins, m, n(m))
    for m in re.finditer(rf"{K}\s*(\d+)\s*kişi", low):
        add(maxes, m, n(m)); add(mins, m, n(m))
    # bare "one/single/solo kerbal" (already digitised) — an exact count, since
    # "send one kerbal to the Mun" is not satisfied by sending three.
    for m in re.finditer(rf"(?<!\w)(1)\s*{K}(?!\s*(?:or|veya)\b)", low):
        if _crew_qualified(low, m.start()):
            continue
        add(maxes, m, n(m)); add(mins, m, n(m))
    # range: "between N and M crew" / "N to M kerbals" / "N-M crew"
    for m in re.finditer(rf"(?:between\s*)?(\d+)\s*(?:and|to|-|–|ile)\s*(\d+)\s*{K}", low):
        add(mins, m, int(m.group(1))); add(maxes, m, int(m.group(2)))
    # inclusive max
    for m in re.finditer(rf"(?:max(?:imum)?|no more than|at most|up to|no greater than|"
                         rf"en fazla|en çok)\s*(?:of\s*)?(\d+)\s*{K}", low):
        add(maxes, m, n(m))
    for m in re.finditer(rf"(\d+)\s*{K}\s*or\s*(?:fewer|less)", low):
        add(maxes, m, n(m))
    # strict max ("fewer/less than N crew" => N-1)
    for m in re.finditer(rf"(?:fewer than|less than|under|below|altında)\s*(\d+)\s*{K}", low):
        add(maxes, m, max(0, n(m, -1)))
    # inclusive min ("at least N crew", "carry N kerbals", "fly 3 kerbals to the Mun").
    # A transport verb reads as a floor, not an exact count, matching how "carry" has
    # always been read — the mission is still done if a fourth seat is filled.
    for m in re.finditer(rf"(?:min(?:imum)?|at least|no fewer than|no less than|"
                         rf"carry|carrying|with|fly|flying|send|sending|take|taking|"
                         rf"launch|launching|transport|deliver|"
                         rf"en az|taşı\w*|götür\w*|gönder\w*)\s*(?:of\s*)?(\d+)\s*{K}{NOT_CEIL}", low):
        add(mins, m, n(m))
    for m in re.finditer(rf"(\d+)\s*{K}\s*or\s*more", low):
        add(mins, m, n(m))
    for m in re.finditer(rf"(\d+)\+\s*{K}", low):
        add(mins, m, n(m))
    # strict min ("more than N crew" => N+1); "no " lookbehind avoids "no more than".
    for m in re.finditer(rf"(?<!no )(?:more than|over|greater than|above)\s*(\d+)\s*{K}", low):
        add(mins, m, n(m, 1))

    # Reverse word order, where the bound follows the noun: "crew size of at least 2",
    # "crew count under 4", "mürettebat en az 2".
    CONN = r"(?:\s*(?:of|is|:|=)\s*|\s+)"
    CS = rf"{K}\s*(?:size|count|say\w*)?"
    for m in re.finditer(rf"{CS}{CONN}(?:at least|min(?:imum)?|no fewer than|no less than|en az)\s*(\d+)", low):
        add(mins, m, n(m))
    for m in re.finditer(rf"{CS}{CONN}(?<!no )(?:more than|over|greater than|above)\s*(\d+)", low):
        add(mins, m, n(m, 1))
    for m in re.finditer(rf"{CS}{CONN}(?:at most|max(?:imum)?|no more than|up to|no greater than|"
                         rf"en fazla|en çok)\s*(\d+)", low):
        add(maxes, m, n(m))
    for m in re.finditer(rf"{CS}{CONN}(?:fewer than|less than|under|below|altında)\s*(\d+)", low):
        add(maxes, m, max(0, n(m, -1)))

    # "crewed / manned mission" with no number: at least somebody aboard. The \b in
    # front keeps "uncrewed"/"unmanned" out (their "un" is a word character).
    if not maxes or min(maxes) > 0:
        for m in re.finditer(r"\b(?:crewed|manned|mürettebatlı|insanlı)\b", low):
            add(mins, m, 1)

    if maxes:
        out["max_crew"] = min(maxes)
    if mins:
        out["min_crew"] = max(mins)


# Nouns that can follow a profession word and mean something else entirely — a
# pilot chute is hardware, not a kerbal. Only needed for the bare "a pilot" form;
# a number in front ("2 pilots") is unambiguous.
_TRAIT_NOT_A_KERBAL = ("chute", "light", "hole", "program", "wave")

# Professions whose name is also an ordinary word for a *craft* — "send a scout to
# Duna" is a probe, not a kerbal. These are only read from a counted phrase ("2
# scouts"), never from a bare article, where the reading is a coin toss and a wrong
# requirement rejects every submission.
_TRAIT_ARTICLE_UNSAFE = ("scout", "scouts", "izci")


def _extract_crew_traits(text: str, out: dict) -> None:
    """Detect per-profession crew requirements — "send two pilots and a scientist",
    "at least one engineer aboard", "no tourists", "en az 2 mühendis".

    Same grammar as `_extract_crew`, applied to a profession noun instead of the
    generic crew noun, and sharing its guards: counts written as words are already
    digits by the time the patterns run, a bare count is a floor (asking for "2
    pilots" is not a ban on a third), and a mention inside a rescue phrase ("bring
    back 2 stranded scientists") describes who is being fetched, not who is flying.
    """
    import re
    low = _digitize_counts(text.lower())
    if not any(w in low for w in _TRAIT_ALIASES):
        return

    mins: dict[str, list[int]] = {}
    maxes: dict[str, list[int]] = {}
    # Longest first, so "bilim insanları" wins over "bilim insanı".
    T = "|".join(re.escape(w) for w in sorted(_TRAIT_ALIASES, key=len, reverse=True))

    def add(bucket: dict[str, list[int]], m, value: int, gi: int) -> None:
        if _crew_blocked(low, m.start()):
            return
        canon = _TRAIT_ALIASES.get(m.group(gi))
        if canon:
            bucket.setdefault(canon, []).append(value)

    # "no tourists" / "without an engineer" / "hiç turist" — a ceiling of zero.
    for m in re.finditer(rf"\b(?:no|without(?:\s+an?)?|zero|hiç|hicbir|hiçbir)\s+({T})\b", low):
        add(maxes, m, 0, 1)

    # "exactly 2 pilots"
    for m in re.finditer(rf"\b(?:exactly|precisely|tam)\s*(\d+)\s*({T})\b", low):
        add(mins, m, int(m.group(1)), 2)
        add(maxes, m, int(m.group(1)), 2)

    # Inclusive and strict bounds, either side of the number.
    for m in re.finditer(rf"\b(?:at least|min(?:imum)?|no fewer than|no less than|en az)\s*(\d+)\s*({T})\b", low):
        add(mins, m, int(m.group(1)), 2)
    for m in re.finditer(rf"\b(?<!no )(?:more than|over|greater than|above)\s*(\d+)\s*({T})\b", low):
        add(mins, m, int(m.group(1)) + 1, 2)
    for m in re.finditer(rf"\b(?:at most|max(?:imum)?|no more than|up to|en fazla|en çok)\s*(\d+)\s*({T})\b", low):
        add(maxes, m, int(m.group(1)), 2)
    for m in re.finditer(rf"\b(?:fewer than|less than|under|below|altında)\s*(\d+)\s*({T})\b", low):
        add(maxes, m, max(0, int(m.group(1)) - 1), 2)

    # Trailing bound: "2 pilots or fewer" is a ceiling wearing a bare count's clothes.
    for m in re.finditer(rf"\b(\d+)\s*({T})\b\s*(?:or\s*(?:fewer|less)|veya\s*az)", low):
        add(maxes, m, int(m.group(1)), 2)

    # Bare count — a floor. Skipped when a bound word owns the number, or the
    # phrase is "2 pilots or fewer", both of which the patterns above already read.
    for m in re.finditer(rf"\b(\d+)\s*({T})\b(?!\s*(?:or\s*(?:fewer|less)|veya\s*az))", low):
        if not _crew_qualified(low, m.start()):
            add(mins, m, int(m.group(1)), 2)

    # "with a pilot aboard" / "needs an engineer" / "bir mühendis" — a floor of one.
    # "one pilot" is already a digit by now, so this only has to cover the article.
    for m in re.finditer(rf"\b(?:a|an|bir)\s+({T})\b(?!\s+(?:{'|'.join(_TRAIT_NOT_A_KERBAL)}))", low):
        if m.group(1) not in _TRAIT_ARTICLE_UNSAFE:
            add(mins, m, 1, 1)

    traits: dict[str, dict] = {}
    for name, vals in mins.items():
        traits.setdefault(name, {})["min"] = max(vals)
    for name, vals in maxes.items():
        traits.setdefault(name, {})["max"] = min(vals)
    if traits:
        out["crew_traits"] = traits


def _extract_part_count(text: str, out: dict) -> None:
    """Detect min/max part-count limits. Handles inclusive ('at most N', 'up to N')
    and strict ('fewer than N' => N-1, 'more than N' => N+1) bounds, plus
    'N+ parts', 'N parts or more/fewer', and a few Turkish forms. The most
    restrictive bound wins when several appear."""
    import re
    low = text.lower()
    maxes: list[int] = []
    mins: list[int] = []
    P = r"(?:parts?|parça\w*)"  # English "part(s)" or Turkish "parça/parçası"

    def n(m, delta=0):
        return int(m.group(1)) + delta

    # exactly N
    for m in re.finditer(rf"(?:exactly|precisely|tam)\s*(\d+)\s*{P}", low):
        maxes.append(n(m)); mins.append(n(m))
    # range: "between N and M parts" / "N to M parts"
    for m in re.finditer(rf"(?:between\s*)?(\d+)\s*(?:and|to|-|ile)\s*(\d+)\s*{P}", low):
        mins.append(int(m.group(1))); maxes.append(int(m.group(2)))
    # inclusive max
    for m in re.finditer(rf"(?:max(?:imum)?|no more than|at most|up to|no greater than|"
                         rf"en fazla|en çok)\s*(?:of\s*)?(\d+)\s*{P}", low):
        maxes.append(n(m))
    for m in re.finditer(rf"(\d+)\s*{P}\s*or\s*(?:fewer|less)", low):
        maxes.append(n(m))
    # strict max ("fewer/less/under N" => N-1)
    for m in re.finditer(rf"(?:fewer than|less than|under|below|altında)\s*(\d+)\s*{P}", low):
        maxes.append(max(1, n(m, -1)))
    # inclusive min
    for m in re.finditer(rf"(?:min(?:imum)?|at least|no fewer than|no less than|"
                         rf"en az)\s*(?:of\s*)?(\d+)\s*{P}", low):
        mins.append(n(m))
    for m in re.finditer(rf"(\d+)\s*{P}\s*or\s*more", low):
        mins.append(n(m))
    for m in re.finditer(rf"(\d+)\+\s*{P}", low):
        mins.append(n(m))
    # strict min ("more than/over N" => N+1). The "no " lookbehind keeps
    # "no more than N" (an inclusive max) from being read as a minimum.
    for m in re.finditer(rf"(?<!no )(?:more than|over|greater than|above|üzerinde)\s*(\d+)\s*{P}", low):
        mins.append(n(m, 1))

    # Reverse phrasing where the part-count noun precedes the bound and no
    # trailing "parts" follows the number, e.g. "part count above 10",
    # "part count of at least 10", "parça sayısı 10 üzerinde".
    PC = r"(?:parts?\s*count|parça\s*say\w*)"
    CONN = r"(?:\s*(?:of|is|:|=)\s*|\s+)"  # optional "of"/"is"/":"/"=" connector
    for m in re.finditer(rf"{PC}{CONN}(?:more than|over|greater than|above|üzerinde)\s*(\d+)", low):
        mins.append(n(m, 1))
    for m in re.finditer(rf"{PC}{CONN}(?:at least|min(?:imum)?|no fewer than|no less than|en az)\s*(\d+)", low):
        mins.append(n(m))
    for m in re.finditer(rf"{PC}{CONN}(?:fewer than|less than|under|below|altında)\s*(\d+)", low):
        maxes.append(max(1, n(m, -1)))
    for m in re.finditer(rf"{PC}{CONN}(?:at most|max(?:imum)?|no more than|up to|no greater than|en fazla|en çok)\s*(\d+)", low):
        maxes.append(n(m))

    if maxes:
        out["max_parts"] = min(maxes)
    if mins:
        out["min_parts"] = max(mins)


def _extract_delta_v(text: str, out: dict) -> None:
    """Detect min/max vacuum delta-v (Δv) limits, normalised to m/s. Understands
    'at least 3000 m/s of delta-v', 'no more than 5 km/s dv', 'under 4000 m/s Δv',
    the reverse order ('delta-v of at least 3000'), and ranges ('3000-5000 m/s
    dv'). km/s values are converted to m/s. The most restrictive bound wins when
    several appear. A Δv token must be present, so plain numbers in flavour text
    don't trip it."""
    import re
    low = text.lower()
    maxes: list[float] = []
    mins: list[float] = []

    NUM = r"(\d[\d,]*(?:\.\d+)?)"
    UNIT = r"\s*(km/s|km/sec|kps|m/s|m/sec|mps)?"
    # Δ lower-cases to δ; also accept "delta-v", "deltav", standalone "dv".
    DV = r"(?:[δ]\s*-?\s*v|delta[\s\-]*v|deltav|\bdv\b)"
    MAXQ = (r"(?:at most|maximum|max\.?|no more than|up to|no greater than|"
            r"under|below|less than|fewer than|lower than|en fazla|en çok)")
    MINQ = r"(?:at least|minimum|min\.?|no less than|no fewer than|en az)"
    OF = r"\s*(?:of\s*)?"

    def val(m, gi=1):
        x = float(m.group(gi).replace(",", ""))
        unit = m.group(gi + 1) or ""
        return x * 1000.0 if ("km" in unit or "kps" in unit) else x

    # Inclusive max — both word orders.
    for m in re.finditer(rf"{MAXQ}\s*{NUM}{UNIT}{OF}{DV}", low):
        maxes.append(val(m))
    for m in re.finditer(rf"{DV}{OF}{MAXQ}\s*{NUM}{UNIT}", low):
        maxes.append(val(m))
    # Inclusive min — both word orders.
    for m in re.finditer(rf"{MINQ}\s*{NUM}{UNIT}{OF}{DV}", low):
        mins.append(val(m))
    for m in re.finditer(rf"{DV}{OF}{MINQ}\s*{NUM}{UNIT}", low):
        mins.append(val(m))
    # Strict min ("more than/over N"). Δv is continuous, so N (not N+1) is used.
    # The "no " lookbehind keeps "no more than" (an inclusive max) out of here.
    SMIN = r"(?<!no )(?:more than|greater than|over|above)"
    for m in re.finditer(rf"{SMIN}\s*{NUM}{UNIT}{OF}{DV}", low):
        mins.append(val(m))
    for m in re.finditer(rf"{DV}{OF}{SMIN}\s*{NUM}{UNIT}", low):
        mins.append(val(m))
    # Range: "between 3000 and 5000 m/s delta-v" / "3000 to 5000 m/s dv".
    for m in re.finditer(rf"(?:between\s*)?{NUM}{UNIT}\s*(?:and|to|-|–|ile)\s*{NUM}{UNIT}{OF}{DV}", low):
        mins.append(val(m, 1))
        maxes.append(val(m, 3))

    if maxes:
        out["max_dv"] = min(maxes)
    if mins:
        out["min_dv"] = max(mins)


# Tokens that look like a part name but are really a category/generic word —
# captured by the "<name> engine" pattern but should not become a part name.
_GENERIC_PART_WORDS = {
    "the", "a", "an", "any", "this", "that", "your", "single", "one", "main",
    "nuclear", "atomic", "ion", "solid", "chemical", "electric", "liquid",
    "rocket", "jet", "ion", "rcs", "the", "new", "only", "use", "powered",
    "fusion", "fission", "lqd", "no", "only", "must", "type", "kind",
}


def _extract_named_parts(text: str, out: dict) -> None:
    """
    Capture proper-noun part names sitting just before engine/motor/booster/
    thruster, and route them to forbidden_parts/required_parts by the polarity of
    the surrounding text. Quoted names ("Thud") are always captured.
    """
    import re
    low = text.lower()

    # Quoted names: "Thud", 'Mainsail'.
    quoted = re.findall(r'["“‘\']([A-Za-z][A-Za-z0-9 .\-]{1,30})["”’\']', text)

    # The word right before an engine noun (any case, so lowercase "thud engine"
    # is caught), optionally preceded by a Capitalised brand token ("LV-N Nerv").
    # Generic/fuel/category head words are filtered out below.
    pattern = re.compile(
        r'(?:([A-Z][A-Za-z0-9\-]+)\s+)?'                    # optional brand prefix
        r'([A-Za-z][A-Za-z0-9\-]+)\s+'                      # head word before the noun
        r'(?:[Ee]ngine|[Mm]otor|[Bb]ooster|[Tt]hruster|[Rr]ocket)s?\b'
    )
    candidates = []
    for m in pattern.finditer(text):
        prefix, head = m.group(1), m.group(2)
        # Only keep the brand prefix when the head itself isn't a generic/fuel
        # word it would otherwise be glued to (e.g. keep "LV-N" + "Nerv").
        name = (prefix + " " + head) if prefix and not _is_generic_part_word(head) else head
        candidates.append((name, m.start(2)))
    for q in quoted:
        idx = text.find(q)
        candidates.append((q, idx if idx >= 0 else 0))

    for name, pos in candidates:
        clean = name.strip()
        if not clean or clean.lower() in _GENERIC_PART_WORDS:
            continue
        words = clean.split()
        # Skip if every word is a generic/fuel/category word, OR any word is a
        # known fuel/engine-category term (e.g. "He3 Powered" describes a fuel,
        # "Ion engine" a category — not part names).
        if all(_is_generic_part_word(w) for w in words) \
                or any(_is_fuel_or_category_word(w) for w in words):
            continue
        # Polarity from a window of text just before the mention.
        window = low[max(0, pos - 45):pos + len(clean)]
        forbid = _clause_polarity(window)
        if forbid is None:
            continue
        key = "forbidden_parts" if forbid else "required_parts"
        out[key].append(clean)


def _is_generic_part_word(word: str) -> bool:
    return word.lower() in _GENERIC_PART_WORDS


def _is_fuel_or_category_word(word: str) -> bool:
    """True when a token is a known fuel/engine/part-category term, so it should
    not be mistaken for a part *name*."""
    w = word.lower()
    return (w in _PROPELLANT_ALIASES or w in _ENGINE_CATEGORY_ALIASES
            or w in _PART_CATEGORY_ALIASES)


def _clause_polarity(clause: str) -> bool | None:
    """
    True=forbidding clause, False=requiring clause, None=neither cue present.

    Negation is checked first and dominates: "doesn't use deuterium-powered
    engines" forbids deuterium even though it also contains the require-cue
    "powered". Clauses mixing both (e.g. "use only X, no Y") are split upstream.
    """
    padded = f" {clause} "  # so boundary cues (" no ", " not ") match at edges
    if any(cue in padded for cue in _NEG_CUES) or any(cue in clause for cue in _FORBID_CUES):
        return True
    if any(cue in clause for cue in _REQUIRE_CUES) \
            or any(cue in padded for cue in _REQUIRE_BOUNDARY_CUES):
        return False
    return None


def _word_in(phrase: str, clause: str) -> bool:
    """Whole-token containment so 'ion' doesn't match 'station'."""
    import re
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", clause) is not None


# ── Part-name resolution against the real catalog ────────────────────────────

def resolve_parts(constraints: dict | None, resolver) -> dict:
    """
    Resolve the loose `forbidden_parts` / `required_parts` mentions to concrete
    installed parts using `resolver(loose_name) -> internal_name | None`.

    Returns a copy of `constraints` with, for each kind, two derived lists added:
        <kind>_part_names         resolved internal names (match a part exactly)
        <kind>_parts_unresolved   mentions that couldn't be pinned (loose fallback)

    Mentions are deduped by their resolution so the same part isn't listed twice.
    """
    if not constraints:
        return constraints or {}
    out = dict(constraints)
    for kind in ("forbidden", "required"):
        names, unresolved = [], []
        for loose in constraints.get(f"{kind}_parts", []) or []:
            name = None
            try:
                name = resolver(loose)
            except Exception:
                name = None
            if name and name not in names:
                names.append(name)
            elif not name:
                unresolved.append(loose)
        out[f"{kind}_part_names"] = names
        out[f"{kind}_parts_unresolved"] = unresolved
    return out


def _part_match_sets(constraints: dict, kind: str) -> tuple[list[str], list[str]]:
    """(resolved internal names, loose names to substring-match) for a
    forbidden/required kind. Falls back to the loose list when resolution hasn't
    been applied to these constraints."""
    if f"{kind}_part_names" in constraints or f"{kind}_parts_unresolved" in constraints:
        names = constraints.get(f"{kind}_part_names", [])
        loose = constraints.get(f"{kind}_parts_unresolved", [])
    else:
        names = []
        loose = constraints.get(f"{kind}_parts", [])
    return names, loose


# ── Verification (server-side authoritative check) ───────────────────────────

def verify_used_parts(constraints: dict | None, used_parts: list[dict],
                      delta_v: float | None = None,
                      crew_count: int | None = None,
                      crew_traits: dict | None = None) -> list[str]:
    """
    Compare the craft's actually-used parts against the constraints and return a
    list of human-readable violation messages (empty == passes).

    `used_parts` is the per-part summary reported by the KSP client; each item:
        {
          "name":               "radialLiquidEngine1-2",  # internal part name
          "title":              "Mk-55 \"Thud\" Liquid Fuel Engine",
          "propellants":        ["LiquidFuel", "Oxidizer"],
          "engine_categories":  ["chemical"],
          "part_categories":    ["engine"],
        }

    `delta_v` is the craft's stock-calculated vacuum Δv (m/s) as reported by the
    client; the bot can't recompute it, so a min/max-Δv limit is only checked
    when the client supplies a value (None => skip, don't wrongly fail).
    """
    if is_empty(constraints):
        return []
    used_parts = used_parts or []

    # Whole-craft scalar limits (part count + Δv + crew aboard).
    scalar_violations = _check_part_count(constraints, len(used_parts))
    scalar_violations += _check_delta_v(constraints, delta_v)
    scalar_violations += _check_crew(constraints, crew_count)
    scalar_violations += _check_crew_traits(constraints, crew_traits)

    if not used_parts:
        return scalar_violations + _missing_required(constraints, [])

    props = _flatten(used_parts, "propellants")
    eng = _flatten(used_parts, "engine_categories")
    cats = _flatten(used_parts, "part_categories")

    violations: list[str] = list(scalar_violations)

    # ── Forbidden parts: match resolved internal names exactly, and any
    #    unresolved mentions by case-insensitive title substring (loose fallback).
    bad_names_list, bad_loose = _part_match_sets(constraints, "forbidden")
    bad_names = {n.lower() for n in bad_names_list}
    for p in used_parts:
        if bad_names and (p.get("name") or "").lower() in bad_names:
            violations.append(f"Craft uses a forbidden part: '{p.get('title') or p.get('name')}'.")
    for bad in bad_loose:
        hit = next((p.get("title") for p in used_parts
                    if bad.lower() in (p.get("title") or "").lower()), None)
        if hit:
            violations.append(f"Craft uses a forbidden part: '{hit}' (matched '{bad}').")

    for bad in constraints.get("forbidden_propellants", []):
        if bad.lower() in props:
            violations.append(f"Craft has an engine powered by forbidden fuel: {bad}.")

    for bad in constraints.get("forbidden_engine_categories", []):
        if bad.lower() in eng:
            violations.append(f"Craft uses a forbidden engine type: {bad}.")

    for bad in constraints.get("forbidden_part_categories", []):
        if bad.lower() in cats:
            violations.append(f"Craft includes a forbidden part category: {bad}.")

    violations.extend(_missing_required(constraints, used_parts))
    return violations


def _missing_required(constraints: dict, used_parts: list[dict]) -> list[str]:
    titles = [(p.get("title") or "").lower() for p in used_parts]
    used_names = {(p.get("name") or "").lower() for p in used_parts}
    props = _flatten(used_parts, "propellants")
    eng = _flatten(used_parts, "engine_categories")
    cats = _flatten(used_parts, "part_categories")
    out: list[str] = []

    need_names, need_loose = _part_match_sets(constraints, "required")
    for need in need_names:
        if need.lower() not in used_names:
            out.append(f"Required part not found: '{need}'.")
    for need in need_loose:
        if not any(need.lower() in t for t in titles):
            out.append(f"Required part not found: '{need}'.")
    for need in constraints.get("required_propellants", []):
        if need.lower() not in props:
            out.append(f"Required: an engine powered by {need}.")
    for need in constraints.get("required_engine_categories", []):
        if need.lower() not in eng:
            out.append(f"Required engine type not found: {need}.")
    for need in constraints.get("required_part_categories", []):
        if need.lower() not in cats:
            out.append(f"Required part category missing: {need}.")
    return out


def _check_part_count(constraints: dict, count: int) -> list[str]:
    out: list[str] = []
    mx = constraints.get("max_parts")
    mn = constraints.get("min_parts")
    if mx and count > mx:
        out.append(f"Too many parts: {count} (max {mx}).")
    if mn and count < mn:
        out.append(f"Too few parts: {count} (min {mn}).")
    return out


# A small slack so a craft that rounds to the limit isn't unfairly rejected
# (the client's Δv and the constraint can differ by sub-percent rounding).
_DV_TOLERANCE = 0.005


def verify_crew(constraints: dict | None, crew_count: int | None,
                crew_traits: dict | None = None) -> list[str]:
    """Standalone crew check for missions that report telemetry but no parts
    list (e.g. active-vessel flights). Returns violation messages (empty == passes)."""
    if is_empty(constraints):
        return []
    return _check_crew(constraints, crew_count) + _check_crew_traits(constraints, crew_traits)


def _check_crew_traits(constraints: dict, crew_traits: dict | None) -> list[str]:
    """Per-profession crew floors/ceilings, against the profession head-count the
    client reports ({"Pilot": 2, "Engineer": 1}).

    Skipped when the client reported nothing, like Δv — an older client that doesn't
    send the field must not have every submission rejected. A client that *did*
    report is trusted for who was aboard exactly as it is for crew_count; this is a
    rule check, not an anti-cheat.

    The violation lines match `ContractConstraints.CheckCrewTraits` word for word, so
    the client's pre-flight and this re-check don't read as two different problems.
    The mod-naming hint below is the twin of the client's "no mod installed here
    defines this profession" line, worded for what each end knows: the client can
    test its own install, this end can only say which mod the profession comes from.
    """
    required = constraints.get("crew_traits") or {}
    if not required or crew_traits is None:
        return []

    have: dict[str, int] = {}
    if isinstance(crew_traits, dict):
        for name, count in crew_traits.items():
            try:
                have[str(name).strip().lower()] = int(count)
            except (TypeError, ValueError):
                continue

    out: list[str] = []
    for trait, bounds in required.items():
        aboard = have.get(str(trait).lower(), 0)
        mn, mx = bounds.get("min"), bounds.get("max")
        if mn and aboard < mn:
            out.append(f"Too few {trait}s aboard: {aboard} (need {mn}).")
            # None aboard of a modded profession is usually a missing mod rather than
            # a crewing mistake, and "0 (need 2)" on its own is true but unactionable
            # when no kerbal in the save could ever have been one.
            mod = trait_mod(trait)
            if aboard == 0 and mod:
                out.append(f"The '{trait}' profession comes from {mod} — an install "
                           "without it cannot field one.")
        if mx is not None and aboard > mx:
            out.append(f"No {trait} may fly this mission: {aboard} aboard." if mx == 0
                       else f"Too many {trait}s aboard: {aboard} (max {mx}).")
    return out


def _check_crew(constraints: dict, crew_count: int | None) -> list[str]:
    """Crew-aboard floor/ceiling. Like Δv it's a whole-craft metric the client reports,
    so a missing value (None) is skipped rather than failed."""
    out: list[str] = []
    if crew_count is None:
        return out
    mx = constraints.get("max_crew")
    mn = constraints.get("min_crew")
    if mx is not None and crew_count > mx:
        out.append(f"This mission must fly uncrewed: {crew_count} crew aboard." if mx == 0
                   else f"Too many crew aboard: {crew_count} (max {mx}).")
    if mn and crew_count < mn:
        out.append(f"Too few crew aboard: {crew_count} (min {mn}).")
    return out


def _check_delta_v(constraints: dict, delta_v: float | None) -> list[str]:
    out: list[str] = []
    if delta_v is None:
        return out  # client didn't report Δv — can't check, so don't fail
    mx = constraints.get("max_dv")
    mn = constraints.get("min_dv")
    if mx and delta_v > mx * (1 + _DV_TOLERANCE):
        out.append(f"Too much delta-v: {delta_v:.0f} m/s (max {mx:.0f}).")
    if mn and delta_v < mn * (1 - _DV_TOLERANCE):
        out.append(f"Not enough delta-v: {delta_v:.0f} m/s (min {mn:.0f}).")
    return out


def _flatten(used_parts: list[dict], key: str) -> set[str]:
    out = set()
    for p in used_parts:
        for v in (p.get(key) or []):
            if v:
                out.add(str(v).lower())
    return out


def summary_line(constraints: dict | None) -> str | None:
    """Short one-line description for logs / notifications, or None if empty."""
    if is_empty(constraints):
        return None
    if constraints.get("notes"):
        return constraints["notes"]
    bits = []
    for key in LIST_KEYS:
        vals = constraints.get(key)
        if vals:
            bits.append(f"{key.replace('_', ' ')}: {', '.join(vals)}")
    if constraints.get("max_parts"):
        bits.append(f"max {constraints['max_parts']} parts")
    if constraints.get("min_parts"):
        bits.append(f"min {constraints['min_parts']} parts")
    if constraints.get("max_dv"):
        bits.append(f"max {constraints['max_dv']:.0f} m/s Δv")
    if constraints.get("min_dv"):
        bits.append(f"min {constraints['min_dv']:.0f} m/s Δv")
    if constraints.get("max_crew") is not None:
        bits.append("uncrewed" if constraints["max_crew"] == 0
                    else f"max {constraints['max_crew']} crew")
    if constraints.get("min_crew"):
        bits.append(f"min {constraints['min_crew']} crew")
    bits.extend(crew_trait_phrases(constraints))
    return "; ".join(bits) or None


def crew_trait_phrases(constraints: dict | None) -> list[str]:
    """One short phrase per profession requirement ("2× Pilot", "no Tourist"), for
    the log line, the Discord embed and anything else that has to say it out loud."""
    out: list[str] = []
    for trait, bounds in ((constraints or {}).get("crew_traits") or {}).items():
        mn, mx = bounds.get("min"), bounds.get("max")
        if mx == 0:
            out.append(f"no {trait}")
        elif mn and mx and mn == mx:
            out.append(f"exactly {mn}× {trait}")
        elif mn and mx:
            out.append(f"{mn}–{mx}× {trait}")
        elif mx:
            out.append(f"up to {mx}× {trait}")
        elif mn:
            article = "an" if trait[:1].upper() in "AEIOU" else "a"
            out.append(f"{mn}× {trait}" if mn > 1 else f"{article} {trait}")
    return out
