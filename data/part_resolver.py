"""
data/part_resolver.py — Resolve a loosely-typed part mention to a real installed
part, using the KSP client's uploaded part catalog.

A mission author writes "the Thud engine" or even a typo'd "thudd"; the actual
part has a weird title like 'Mk-55 "Thud" Liquid Fuel Engine' and a stable
internal name 'radialLiquidEngine1-2'. This module fuzzy-matches the mention
against the catalog and returns the part's internal name when it can pin it down
1:1. Ambiguous / low-confidence mentions are handed to an optional AI resolver;
if that can't decide either, it returns None and the caller falls back to loose
substring matching.

Catalog entries are dicts: {"name": <internal>, "title": <display>}.
"""
from __future__ import annotations

import difflib
import logging
import re

log = logging.getLogger(__name__)

# Confidence thresholds (scores are 0-100; see _score).
_HIGH = 80      # a match at/above this can stand on its own ...
_MARGIN = 10    # ... if it also leads the runner-up by this much.
# Below this a mention is not a reading of this part at all: it is neither offered to
# the AI as a candidate nor counted as evidence the part exists (see plausible_parts).
# One number for both, so the list the AI chooses from and the list that says "this
# names something real" can never disagree.
#
# Measured against real catalogs — 496 parts (stock + light mods) and 1331 (RO). An
# invented word ("floopygloop", "zorbulator", "blorpotron", "sparklewhomp") tops out
# at 60 on fuzz alone, while every genuine mention of an installed part scored 84 or
# better. 65 sits in that gap with room on both sides.
_LOW = 65
_FUZZY_ACCEPT = 0.86  # difflib ratio that counts as a confident typo match.
# A whole-mention token match: every word the author wrote is accounted for by this
# part. Below the 100 an exact title/name/nickname gets, above the 85 a single word
# in the title gets — "vector engine" naming the Vector must beat "Vernor Engine"
# scoring on the shape of the whole phrase.
_TOKEN_FULL = 95
# What the run-together form of a mention scores against a part word, and vice
# versa: "AJ-10" normalises to "aj 10" while the part reads "AJ10-137", and the two
# are the same string with a separator moved. Just short of an exact word.
#
# Deliberately NOT a substring test in either direction. "tron" IS a substring of
# "blorpotron" and "fl" of "floopygloop", so containment scored invented words at 0.9
# against half the catalog — which is the whole failure this file is being repaired
# for, reintroduced one rule further down.
_JOINED = 0.95

_QUOTE_RE = re.compile(r'["“‘\']([^"”’\']{1,40})["”’\']')

# Category nouns a mention may carry that the part's own title need not repeat.
#
# They are *optional*, not ignored: "the Vector engine" against a title that does say
# "Engine" scores on all three words, while "aj-10 engine" against "AJ10-137 (Service
# Propulsion System)" would otherwise be dragged down by the one word that part has no
# reason to spell out — scoring the exact same part LOWER than the shorter "AJ-10" did,
# which is backwards. Dropping them cannot rescue an invented name, because whatever is
# left still has to be accounted for: "floopygloop engine" minus "engine" is
# "floopygloop", which matches nothing either way.
#
# Deliberately only nouns for a *kind* of part. "Decoupler", "parachute" and the like
# are absent: they name the thing itself, and a part called one really does say so.
_GENERIC_WORDS = frozenset({
    "engine", "engines", "motor", "motors", "thruster", "thrusters",
    "booster", "boosters", "tank", "tanks", "pod", "pods", "part", "parts",
    "module", "modules", "rocket", "rockets", "capsule", "capsules",
    "nozzle", "nozzles", "system", "systems", "unit", "units", "stage", "stages",
})


_LEADING_ARTICLES = re.compile(r"^(?:the|a|an)\s+")


def _norm(s: str) -> str:
    """Lower-case, drop punctuation, strip a leading article, collapse whitespace."""
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()
    return _LEADING_ARTICLES.sub("", s)


def _part_keys(part: dict) -> dict:
    """Pre-compute the searchable strings for a catalog part (cached on the dict)."""
    title = part.get("title") or ""
    name = part.get("name") or ""
    tnorm = _norm(title)
    nnorm = _norm(name)
    keys = set()
    if tnorm:
        keys.add(tnorm)
    if nnorm:
        keys.add(nnorm)
    m = _QUOTE_RE.search(title)            # the "Thud" nickname inside the title
    if m:
        nick = _norm(m.group(1))
        if nick:
            keys.add(nick)
    twords = set(tnorm.split())
    # Every word this part can be recognised by — the title's and the internal
    # name's alike, since a mention often quotes the name ("liquidEngineMainsail").
    words = twords | set(nnorm.split())
    # Adjacent words run together, so a part written "AJ-10 137" is still found by
    # someone who typed "AJ10". The mention gets the same treatment from the other
    # side (see _coverage), which is what makes the pair symmetric.
    joins = set()
    for seq in (tnorm.split(), nnorm.split()):
        for i in range(len(seq) - 1):
            joins.add(seq[i] + seq[i + 1])
    return {"tnorm": tnorm, "nnorm": nnorm, "keys": keys, "twords": twords,
            "words": words, "joins": joins}


def _word_match(lw: str, k: dict) -> float:
    """How well one word of the mention is accounted for by a part's own words.

    1.0 for the same word, `_JOINED` for one of the part's adjacent words run
    together, otherwise the best difflib ratio — which is what carries a typo.
    """
    if not lw:
        return 0.0
    if lw in k["words"]:
        return 1.0
    if lw in k["joins"]:
        return _JOINED
    best = 0.0
    for pw in k["words"]:
        # A ratio cannot exceed 2*min/(len sum), so a pair too different in length
        # to reach anything useful is skipped rather than measured. Worth doing:
        # this runs per word per part, over catalogs of several thousand parts.
        if 2 * min(len(lw), len(pw)) < 0.5 * (len(lw) + len(pw)):
            continue
        best = max(best, difflib.SequenceMatcher(None, lw, pw).ratio())
    return best


def _coverage(loose_words: list, k: dict) -> float:
    """
    The WORST-matched word of the mention, which is the whole point.

    A mean would let a generic noun carry a nonsense one: "floopygloop engine" scores
    1.0 on "engine" against half the catalog, and averaging that with a word nothing
    matches reads as a decent match for an engine nobody has. Taking the minimum says
    what the question actually is — is every word the author wrote accounted for by
    this part — so one unaccountable word sinks the whole mention, which is exactly
    how an invented name differs from a partial one.
    """
    if not loose_words or not k["words"]:
        return 0.0

    best = _worst_word(loose_words, k)

    # Read again with the category nouns dropped, when doing so leaves something to
    # match. See _GENERIC_WORDS: they are optional, so the better of the two readings
    # stands — the full one still wins where the title does name the category.
    core = [w for w in loose_words if w not in _GENERIC_WORDS]
    if core and len(core) < len(loose_words):
        best = max(best, _worst_word(core, k))

    return best


def _worst_word(words: list, k: dict) -> float:
    """The worst-matched word of one reading of a mention, or the run-together form
    of it — whichever reads better. See _coverage for why worst rather than mean."""
    worst = 1.0
    for lw in words:
        worst = min(worst, _word_match(lw, k))
        if worst == 0.0:
            break
    if len(words) > 1:
        # The mention run together, so "AJ-10" ("aj 10" once normalised) also asks
        # whether any part is called "aj10". Taken as an alternative reading rather
        # than as another word to account for — it is the same mention, spelled the
        # way the part author spelled it.
        worst = max(worst, _word_match("".join(words), k))
    return worst


def _score(loose_norm: str, k: dict, loose_words: list | None = None) -> int:
    """
    Match score for a normalised mention against one part's keys.

    Every reading is scored and the best one wins, rather than the first one that
    fires. An earlier version returned on the first hit down a ladder, so "LV-909"
    took the 65 for being a substring of the title and never reached the word-by-word
    reading that scores it 95 — a real part name that had to be sent to the AI to be
    recognised.
    """
    if not loose_norm:
        return 0
    if loose_norm in k["keys"]:
        return 100                          # exact title / name / nickname

    best = 0
    if loose_norm in k["twords"]:
        best = 85                           # whole mention is one word of the title
    elif loose_norm in k["tnorm"] or loose_norm in k["nnorm"]:
        best = 65                           # substring

    # Fuzzy over the whole phrase (handles a typo in a one-word mention).
    ratio = 0.0
    for cand in list(k["keys"]) + list(k["twords"]):
        ratio = max(ratio, difflib.SequenceMatcher(None, loose_norm, cand).ratio())
    best = max(best, int(ratio * 80))        # capped below "word" so exacts win

    # ... and word by word, the only reading that survives a mention made of a
    # distinctive word plus a generic one. See _coverage.
    if loose_words is None:
        loose_words = loose_norm.split()
    return max(best, int(_coverage(loose_words, k) * _TOKEN_FULL))


def resolve_part(loose: str, catalog: list[dict], ai_resolver=None) -> str | None:
    """
    Resolve `loose` to a single part's internal name, or None if it can't be
    pinned down confidently (caller should then fall back to loose matching).

    `ai_resolver(loose, candidates)` is an optional callable returning a chosen
    internal name (must be one of the candidates' names) or None. It's only
    invoked when the deterministic pass is ambiguous or weak.
    """
    scored = _rank(loose, catalog)
    if not scored:
        return None

    best_sc, best_part = scored[0]
    second_sc = scored[1][0] if len(scored) > 1 else 0

    # Someone who typed a part's whole title, internal name or nickname has named it.
    # This needs no margin, because the runner-up is scored on a *different* reading:
    # "Mk1 Command Pod" is the exact title of one part and word-for-word contained in
    # "Mk1-3 Command Pod", which scores 95 and would otherwise make the exact match
    # look ambiguous. Two parts sharing a title still tie at 100 and still go to the
    # AI, which is right — that pair really is ambiguous.
    if best_sc == 100 and second_sc < 100:
        return best_part.get("name")

    # Confident, unique winner — no AI needed.
    if best_sc >= _HIGH and (best_sc - second_sc) >= _MARGIN:
        return best_part.get("name")

    # A strong fuzzy match that's clearly ahead also stands on its own (typo case).
    if best_sc >= int(_FUZZY_ACCEPT * 80) and (best_sc - second_sc) >= _MARGIN * 2:
        return best_part.get("name")

    # Ambiguous → let the AI choose among the plausible candidates.
    #
    # Only ever the plausible ones. This used to fall back to `scored[:8]` — the top
    # eight of *anything scoring above zero* — whenever nothing cleared `_LOW`, which
    # handed the model a list of unrelated parts for a mention that matched none of
    # them and asked it to pick. It picked: "the floopygloop engine" came back as the
    # IX-6315 "Dawn", and the contract went out requiring a part its author had never
    # named. A mention nothing plausibly matches has no answer, and saying so is the
    # answer.
    candidates = [p for sc, p in scored if sc >= _LOW][:12]
    if not candidates:
        return None
    return _ai(loose, candidates, ai_resolver)


def _rank(loose: str, catalog: list[dict]) -> list:
    """Every catalog part that matches `loose` at all, best first."""
    loose_norm = _norm(loose)
    if not loose_norm or not catalog:
        return []
    loose_words = loose_norm.split()

    scored = []
    for part in catalog:
        k = part.get("_keys")
        if k is None:
            k = part["_keys"] = _part_keys(part)
        sc = _score(loose_norm, k, loose_words)
        if sc > 0:
            scored.append((sc, part))
    scored.sort(key=lambda x: -x[0])
    return scored


def plausible_parts(loose: str, catalog: list[dict]) -> list[dict]:
    """
    The installed parts a mention could plausibly be naming, best first — empty when
    it names nothing this catalog has.

    Deliberately a different question from `resolve_part`, which answers "*which* part
    is this" and says None both for a mention nothing matches and for one several
    parts match equally well. Contract creation needs the two pulled apart: "AJ-10"
    with an AJ10-137 and an AJ10-190 installed is ambiguous but perfectly real and
    must be accepted (the submit check falls back to substring matching, which is
    what it is for), while "floopygloop" is not a part at all and the contract that
    requires it can never be delivered. Refusing the second is only safe because this
    does not refuse the first.
    """
    return [p for sc, p in _rank(loose, catalog) if sc >= _LOW]


def _ai(loose: str, candidates: list[dict], ai_resolver) -> str | None:
    if not ai_resolver:
        return None
    try:
        chosen = ai_resolver(loose, candidates)
    except Exception as exc:
        log.warning("AI part resolution failed for %r: %s", loose, exc)
        return None
    if not chosen:
        return None
    valid = {c.get("name") for c in candidates}
    # When candidates were empty we trust the AI's name as-is (it saw the catalog
    # upstream); otherwise it must pick one we offered.
    if candidates and chosen not in valid:
        return None
    return chosen


def catalog_hash(parts: list[dict]) -> str:
    """Stable hash of a catalog, for caching resolutions and skipping re-uploads."""
    import hashlib
    joined = "\n".join(sorted(f"{p.get('name','')}|{p.get('title','')}" for p in parts))
    return hashlib.sha1(joined.encode("utf-8", "ignore")).hexdigest()
