"""
data/mission_templates.py – The weekly-mission pool.

Each template is a 7-tuple:

    (desc_en, desc_tr, difficulty, category,
     mission_type, required_situation, required_body)

The last three used to be *inferred* — `api_server._classify_missions` asked
Gemini (or, without it, `_classify_heuristic`) what each week's twenty missions
required, and cached the answer. They are authored here instead, and that is the
whole point of this file's current shape: a weekly mission is written by us, so
what the submit gate will demand of it is a fact we can write down rather than a
guess a model has to re-derive every week. Authoring it also costs one Firestore
read and one AI call less per week, but that is the bonus, not the reason.

The reason is that the guess was wrong often enough to ship missions **nobody
could submit**, and a bot-issued contract that cannot be submitted still carries a
fine and a due date. Three of those failures are worth naming, because they are
what every entry below is checked against:

  • `"Perform a suborbital flight and recover the vessel"` — the heuristic tests
    `"orbit" in text`, and `"suborbital"` contains it, so a *suborbital* mission
    demanded `ORBITING`. And even classified correctly it is unsubmittable: the
    mission ends with the vessel recovered, and a recovered vessel is not an
    active vessel, so there is nothing left to submit from.
  • `"Land on Duna and return to Kerbin"` — body extraction walks
    `settings.KNOWN_CELESTIAL_BODIES` in list order and takes the first name that
    appears in the text. `Kerbin` precedes `Duna` there, so the contract required
    a craft landed on *Kerbin*. A round trip has no single moment that shows it
    either way: at Duna the return has not happened, and back at Kerbin nothing in
    the snapshot says the craft was ever at Duna.
  • `"[Kcalbeloh] Land on Rouqea"` — the bracketed mod tag is matched by the same
    body scan, so the required body came out `Kcalbeloh` and a correct landing on
    Rouqea was refused for being at the wrong body.

## What "supported" means here

`SubmissionSession` (KSP side) and `_bot_mission_evidence_problem` (server side)
both judge **one moment**, and they judge the same one:

  • `craft_build` — the player is in the VAB/SPH. Evidence is the `.craft` file,
    the used-part list and a render.
  • `active_vessel` — the player is in flight with one vessel. Evidence is that
    vessel's telemetry: `body`, `situation`, apoapsis/periapsis/inclination/
    eccentricity, latitude/longitude/altitude, crew count and traits, part count,
    mass. Plus screenshots, which the AI reviewer reads alongside it.

There is no history in that snapshot. It cannot say where the craft has been, how
far it drove, how long a kerbal has been alive, or that a vessel now recovered
once existed. So a template earns its place only if there is a moment when the
player is in the editor with the craft, or in flight with one vessel, such that
that moment both **satisfies** the body/situation gate and **is** the evidence.

That rules four kinds of mission out, and they are gone from this file:

  1. **Recovery** — the end state is "no vessel" (`…and recover the vessel`).
  2. **Round trips** — `…and return to Kerbin`. Rewritten to end at the
     destination, which is the half a snapshot can actually show.
  3. **Multi-target** — Jool-5, grand tours, `visit both Mun and Minmus`,
     `a probe to every inner planet`. One snapshot, one place.
  4. **Processes and durations** — `drive 5 km`, `walk 1 km`, `fly through the
     R&D bridge`, `keep a Kerbal alive for 10 years`, `survive a solar storm`,
     `use a gravity assist from Eve`. The snapshot records a state, never an
     event that led to it.

## Authoring rules

  • `mission_type` is `"craft_build"`, `"active_vessel"` or `"constellation"`.
    A `constellation` mission is an `active_vessel` mission in every respect the
    two gates already check — it submits from flight, and its `required_situation`
    and `required_body` are compared against the active vessel exactly as before —
    plus one thing a single snapshot could never carry: a scan of every vessel in
    the target body's sphere of influence, counted and measured against the network
    the text describes (`data/fleet_constraints.py`). Use it only when the text
    genuinely asks for several craft at once; `fleet_constraints.extract_heuristic`
    must find a count in the text, or the mission demands a network nothing will
    check. This is the one exception to the "multi-target" exclusion in rule 3
    above, and it is narrow on purpose: several vessels, but still ONE body and
    ONE moment.
  • `required_situation` is one of KSP's own situation strings — `ORBITING`,
    `LANDED`, `SPLASHED`, `FLYING`, `SUB_ORBITAL`, `ESCAPING` — or `None`.
    `DOCKED` is deliberately never used: docking merges two vessels and the
    result reports `ORBITING`, so requiring it would refuse every successful
    docking. Docking missions ask for `ORBITING` and are judged on the
    screenshot and the part count.
  • `required_body` must be a name from `settings.KNOWN_CELESTIAL_BODIES`, or
    `None`. A body outside that list is one nothing else in the system can
    resolve, which is why the Kcalbeloh entries name only bodies that are in it.
  • `craft_build` templates carry `None` for both — an editor submission has no
    situation and no body.
  • The text is still read by `data/mission_constraints.extract_heuristic` and
    `data/orbit_constraints.extract_heuristic`, which is a feature: "with at
    least 3 crew aboard" becomes an enforced crew floor, "polar orbit" an
    enforced inclination, "using argon-fuelled ion propulsion" an enforced
    propellant and engine category. Word new entries with that in mind — a
    number in the text can become a bound.

  • `requires` is the eighth field: the install tags a player must have before
    this mission is offered to them at all (`data/install_profile.py`). `()`
    means every install, which in practice means an editor mission — anything
    naming a body needs a system that has one. This field exists because the
    bracketed `[Real Solar System]` tag in the text never gated anything:
    `_generate_missions` sampled the whole pool, so a stock player could draw
    `Land on Venus` and a weekly mission is a bot-issued contract with a due
    date and a fine on it. The tag was documentation; this is the gate.
  • **Stock-system missions carry `_STOCK`.** They are not universal. An RSS
    install has no Kerbin, no Mun and no Minmus, and can fly none of them.
  • Body **roles** — `{home}`, `{moon}`, `{gas_giant}`, `{star}`… — may be used
    in `desc_en`, `desc_tr` and `required_body`, and are resolved per system by
    `install_profile.resolve_roles` before a mission is issued. One entry then
    serves both systems, which is what the mod pools want: a Near Future ion
    mission is the same mission whether the target is Duna or Mars. A role that
    the player's system has no body for (`{moon2}` in RSS — Earth has one moon)
    drops the template for that player rather than resolving to nothing.
    A template that names its body outright was priced for that body; a template
    using a role was priced for the stock system.
  • Difficulty is the *only* reward lever, and it is authored. XP and coins are
    `difficulty × WEEKLY_XP_PER_DIFFICULTY / WEEKLY_COINS_PER_DIFFICULTY`, so
    "an RSS player earns more than a stock player" is expressed by writing the
    RSS pool at the difficulty it actually is — reaching orbit at all is a 5
    there — and never by a multiplier keyed on what an install claims to be.
    That matters because `requires` is matched against a profile derived from a
    client-reported part catalog: a forged profile buys a different mission list
    and nothing else, and the missions on it still have to be flown and
    submitted through the same gates.

Categories are presentation only: orbital, landing, return, construction,
exploration, extreme.
"""

import re                                               # noqa: E402

from data.install_profile import (                      # noqa: E402
    SYS_STOCK, SYS_RSS, PACK_OPM, PACK_KCALBELOH,
    MOD_NEARFUTURE, MOD_FARFUTURE, MOD_RO,
    LS_KERBALISM, LS_USI, LS_TAC, LS_SNACKS, LS_ANY,
)

# Shorthands for the `requires` field, so the pool below reads as missions rather
# than as tag algebra. `_ANY` is the empty requirement: a mission every install can
# fly, which in practice means an editor mission, since any mission naming a body
# needs a system that has one.
_ANY: tuple[str, ...] = ()
_STOCK = (SYS_STOCK,)
_RSS = (SYS_RSS,)
_OPM = (PACK_OPM,)
_KCAL = (PACK_KCALBELOH,)
_NF = (MOD_NEARFUTURE,)
_FF = (MOD_FARFUTURE,)
_RO = (MOD_RO,)
_LS = (LS_ANY,)
_KERBALISM = (LS_KERBALISM,)
_USI = (LS_USI,)
_TAC = (LS_TAC,)
_SNACKS = (LS_SNACKS,)


_MOD_TAG_RE = re.compile(r"^\s*\[[^\]]{1,48}\]\s*")


def strip_mod_tag(text: str) -> str:
    """Drop a leading `[Mod Name]` tag before the text is read for constraints.

    The tag is for the player; the extractors in `data/mission_constraints.py` read
    it as part of the mission and it has bitten twice now. First the body scan, which
    turned `[Kcalbeloh] Land on Rouqea` into a contract requiring a landing on
    *Kcalbeloh* (see the header). Then the part-category table, which read the
    `Solar` in `[Real Solar System] Establish a station in low Earth orbit with at
    least 3 crew aboard` as a required **solar panel** — an enforced part
    requirement nobody wrote, on every RSS mission whose text happens to contain a
    requirement cue.

    Both are the same mistake: a label about the mission being read as part of the
    mission. `_generate_missions` strips it once, at generation, and stores the
    result on the mission as `constraints`, which both selection paths already
    prefer over re-extracting the text themselves.
    """
    return _MOD_TAG_RE.sub("", text or "")

# (desc_en, desc_tr, difficulty, category, mission_type,
#  required_situation, required_body, requires)
TEMPLATES = [
    # ═════════════════════════════════════════════════════════════════════════
    # Any install — editor missions, which name no body and so need no system.
    # ═════════════════════════════════════════════════════════════════════════
    ('Build a rover that can drive upside down',
     'Ters dönmüş halde sürülebilen bir gezici tasarlayın',
     3, 'construction', 'craft_build', None, None, _ANY),
    ('Design a heavy cargo aircraft in the SPH',
     "SPH'de ağır kargo uçağı tasarlayın",
     4, 'construction', 'craft_build', None, None, _ANY),
    ('Design a modular tug with four docking ports in the VAB',
     "VAB'de dört kenetlenme portlu modüler bir römorkör tasarlayın",
     4, 'construction', 'craft_build', None, None, _ANY),
    ('Build a deep-diving submersible in the SPH',
     "SPH'de derine dalan bir denizaltı tasarlayın",
     5, 'construction', 'craft_build', None, None, _ANY),
    ('Design a single-stage-to-orbit cargo lifter in the VAB',
     "VAB'de tek kademeli yörünge kargo taşıyıcısı tasarlayın",
     5, 'construction', 'craft_build', None, None, _ANY),
    ('Build a walking mech in the VAB',
     "VAB'de yürüyen bir mecha tasarlayın",
     6, 'construction', 'craft_build', None, None, _ANY),

    # ═════════════════════════════════════════════════════════════════════════
    # Stock Kerbol system. Gated on the system, not left universal: an install
    # that replaced Kerbin with Earth cannot fly a single one of these, and the
    # bracketed-tag convention below never stopped the sampler handing them out.
    # ═════════════════════════════════════════════════════════════════════════

    # ── Easy (difficulty 1-3) ────────────────────────────────────────────────
    ('Reach a stable orbit around Kerbin',
     'Kerbin etrafında kararlı bir yörüngeye ulaşın',
     1, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Reach space on a suborbital trajectory above Kerbin',
     'Kerbin üzerinde yörünge altı bir rotayla uzaya ulaşın',
     1, 'orbital', 'active_vessel', 'SUB_ORBITAL', 'Kerbin', _STOCK),
    ("Splash down in Kerbin's ocean",
     'Kerbin okyanusuna suya iniş yapın',
     1, 'landing', 'active_vessel', 'SPLASHED', 'Kerbin', _STOCK),
    ('Fly an aircraft above 10 km over Kerbin',
     'Kerbin üzerinde 10 km yüksekliğin üzerinde bir uçak uçurun',
     1, 'exploration', 'active_vessel', 'FLYING', 'Kerbin', _STOCK),
    ('Reach a polar orbit around Kerbin',
     'Kerbin etrafında kutupsal yörüngeye ulaşın',
     2, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Deploy a satellite into Kerbin orbit',
     'Kerbin yörüngesine bir uydu yerleştirin',
     2, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Perform an EVA in Kerbin orbit',
     'Kerbin yörüngesinde EVA yapın',
     2, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ("Enter the Mun's sphere of influence on a flyby trajectory",
     "Mun'un etki alanına yakın geçiş rotasıyla girin",
     2, 'orbital', 'active_vessel', 'ESCAPING', 'Mun', _STOCK),
    ("Enter Minmus's sphere of influence on a flyby trajectory",
     "Minmus'un etki alanına yakın geçiş rotasıyla girin",
     2, 'orbital', 'active_vessel', 'ESCAPING', 'Minmus', _STOCK),
    ('Achieve orbit with a spaceplane (SSTO to low Kerbin orbit)',
     'Bir uzay uçağıyla yörüngeye ulaşın (SSTO ile alçak Kerbin yörüngesi)',
     3, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Dock two vessels in Kerbin orbit',
     'Kerbin yörüngesinde iki aracı kenetleyin',
     3, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Deploy three relay satellites into Kerbin orbit',
     'Kerbin yörüngesine üç röle uydusu yerleştirin',
     3, 'orbital', 'constellation', 'ORBITING', 'Kerbin', _STOCK),
    ('Land on the Mun and plant a flag',
     "Mun'a iniş yapın ve bayrak dikin",
     3, 'landing', 'active_vessel', 'LANDED', 'Mun', _STOCK),
    ('Land on Minmus with a scientist aboard',
     "Minmus'a bir bilim insanıyla iniş yapın",
     3, 'landing', 'active_vessel', 'LANDED', 'Minmus', _STOCK),
    ('Land a rover on the Mun',
     "Mun'a bir gezici indirin",
     3, 'landing', 'active_vessel', 'LANDED', 'Mun', _STOCK),
    ('Perform a crewed Mun landing',
     'Mürettebatlı bir Mun inişi yapın',
     3, 'landing', 'active_vessel', 'LANDED', 'Mun', _STOCK),

    # ── Medium (difficulty 4-6) ──────────────────────────────────────────────
    ('Land on the Mun with at least 3 crew aboard',
     "Mun'a en az 3 mürettebatla iniş yapın",
     4, 'landing', 'active_vessel', 'LANDED', 'Mun', _STOCK),
    ('Land on Minmus with at least 3 crew aboard',
     "Minmus'a en az 3 mürettebatla iniş yapın",
     4, 'landing', 'active_vessel', 'LANDED', 'Minmus', _STOCK),
    ('Construct an orbital fuel depot around Kerbin',
     'Kerbin çevresinde yörünge yakıt deposu inşa edin',
     4, 'construction', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Deploy a communication relay network around the Mun',
     'Mun etrafında iletişim röle ağı kurun',
     4, 'construction', 'constellation', 'ORBITING', 'Mun', _STOCK),
    ('Perform a flyby of Jool',
     'Jool yakın geçişi yapın',
     4, 'exploration', 'active_vessel', 'ESCAPING', 'Jool', _STOCK),
    ("Sail a boat out to a capsule splashed down in Kerbin's ocean",
     'Kerbin okyanusuna inmiş bir kapsüle tekneyle ulaşın',
     4, 'exploration', 'active_vessel', 'SPLASHED', 'Kerbin', _STOCK),
    ('Land on Ike',
     "Ike'a iniş yapın",
     5, 'landing', 'active_vessel', 'LANDED', 'Ike', _STOCK),
    ('Enter orbit around Eve',
     'Eve yörüngesine girin',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Eve', _STOCK),
    ('Land on Gilly',
     "Gilly'ye iniş yapın",
     5, 'landing', 'active_vessel', 'LANDED', 'Gilly', _STOCK),
    ('Enter orbit around Dres',
     'Dres yörüngesine girin',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Dres', _STOCK),
    ('Land a rover on Duna',
     "Duna'ya bir gezici indirin",
     5, 'landing', 'active_vessel', 'LANDED', 'Duna', _STOCK),
    ('Capture an asteroid and bring it into Kerbin orbit',
     'Bir asteroidi yakalayıp Kerbin yörüngesine getirin',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Build a space station with at least 3 modules in Kerbin orbit',
     'Kerbin yörüngesinde en az 3 modüllü uzay istasyonu kurun',
     5, 'construction', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Build a Mun surface base with at least 2 modules',
     'Mun yüzeyinde en az 2 modüllü üs kurun',
     5, 'construction', 'active_vessel', 'LANDED', 'Mun', _STOCK),
    ('Build a mining operation on Minmus',
     "Minmus'ta bir madencilik operasyonu kurun",
     5, 'construction', 'active_vessel', 'LANDED', 'Minmus', _STOCK),
    ('Construct a research base at a Mun arch',
     'Mun kemerinin yanında bir araştırma üssü kurun',
     5, 'construction', 'active_vessel', 'LANDED', 'Mun', _STOCK),
    ('Deploy a constellation of 10 satellites into Kerbin orbit from a single launch',
     'Tek fırlatmayla Kerbin yörüngesine 10 uyduluk bir ağ yerleştirin',
     5, 'orbital', 'constellation', 'ORBITING', 'Kerbin', _STOCK),
    ('Land on Duna',
     "Duna'ya iniş yapın",
     6, 'landing', 'active_vessel', 'LANDED', 'Duna', _STOCK),
    ('Assemble a large interplanetary ship in Kerbin orbit',
     'Kerbin yörüngesinde büyük bir gezegenlerarası gemi monte edin',
     6, 'construction', 'active_vessel', 'ORBITING', 'Kerbin', _STOCK),
    ('Land a returning craft on the KSC helipad',
     'Dönen bir aracı KSC helikopter pistine indirin',
     6, 'landing', 'active_vessel', 'LANDED', 'Kerbin', _STOCK),

    # ── Hard (difficulty 7-8) ────────────────────────────────────────────────
    ('Land on Tylo',
     "Tylo'ya iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Tylo', _STOCK),
    ('Land on Laythe',
     "Laythe'e iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Laythe', _STOCK),
    ('Land a rover on Laythe',
     "Laythe'e bir gezici indirin",
     7, 'landing', 'active_vessel', 'LANDED', 'Laythe', _STOCK),
    ('Land on Moho',
     "Moho'ya iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Moho', _STOCK),
    ("Land on Eve's surface",
     'Eve yüzeyine iniş yapın',
     7, 'landing', 'active_vessel', 'LANDED', 'Eve', _STOCK),
    ('Perform a crewed Duna landing',
     'Mürettebatlı bir Duna inişi yapın',
     7, 'landing', 'active_vessel', 'LANDED', 'Duna', _STOCK),
    ('Build a space station in orbit around Jool',
     'Jool yörüngesinde uzay istasyonu kurun',
     7, 'construction', 'active_vessel', 'ORBITING', 'Jool', _STOCK),
    ('Build a self-sustaining Mun base with mining and refuelling',
     'Madencilik ve yakıt ikmali ile kendi kendine yeten bir Mun üssü kurun',
     7, 'construction', 'active_vessel', 'LANDED', 'Mun', _STOCK),
    ('Enter a low orbit around Kerbol',
     'Kerbol etrafında alçak bir yörüngeye girin',
     7, 'exploration', 'active_vessel', 'ORBITING', 'Kerbol', _STOCK),
    ("Take a submersible into Laythe's oceans",
     'Laythe okyanuslarına bir denizaltı indirin',
     7, 'exploration', 'active_vessel', 'SPLASHED', 'Laythe', _STOCK),
    ('Build a helicopter and fly it on Duna',
     "Bir helikopter yapıp Duna'da uçurun",
     7, 'exploration', 'active_vessel', 'FLYING', 'Duna', _STOCK),
    ("Land on the Mohole at Moho's north pole",
     "Moho'nun kuzey kutbundaki Mohole'a iniş yapın",
     8, 'landing', 'active_vessel', 'LANDED', 'Moho', _STOCK),
    ('Build a fully operational colony on Duna with ISRU',
     "Duna'da ISRU ile tam operasyonel bir koloni kurun",
     8, 'construction', 'active_vessel', 'LANDED', 'Duna', _STOCK),
    ("Fly a probe into Jool's atmosphere",
     "Jool'un atmosferine bir sonda uçurun",
     8, 'exploration', 'active_vessel', 'FLYING', 'Jool', _STOCK),
    ('Create an orbital ring around Minmus',
     'Minmus etrafında yörüngesel bir halka inşa edin',
     8, 'construction', 'active_vessel', 'ORBITING', 'Minmus', _STOCK),

    # ── Extreme (difficulty 9-10) ────────────────────────────────────────────
    ('Colonize Laythe with a self-sustaining base',
     "Laythe'de kendi kendine yeten bir üsle kolonileşin",
     9, 'extreme', 'active_vessel', 'LANDED', 'Laythe', _STOCK),
    ('Build a fully crewed colony on Tylo',
     "Tylo'da tam mürettebatlı bir koloni kurun",
     9, 'extreme', 'active_vessel', 'LANDED', 'Tylo', _STOCK),
    ('Capture a Class E asteroid and land it on Kerbin',
     "E sınıfı bir asteroidi yakalayıp Kerbin'e indirin",
     9, 'extreme', 'active_vessel', 'LANDED', 'Kerbin', _STOCK),
    ('Fly a stock propeller aircraft above 20 km on Eve',
     "Eve'de stok pervaneli bir uçağı 20 km üzerine çıkarın",
     9, 'extreme', 'active_vessel', 'FLYING', 'Eve', _STOCK),
    ('Establish a crewed station in orbit around Eeloo',
     'Eeloo yörüngesinde mürettebatlı bir istasyon kurun',
     9, 'extreme', 'active_vessel', 'ORBITING', 'Eeloo', _STOCK),
    ("Reach Eve orbit launching from Eve's surface",
     'Eve yüzeyinden fırlatıp Eve yörüngesine ulaşın',
     10, 'extreme', 'active_vessel', 'ORBITING', 'Eve', _STOCK),

    # ═════════════════════════════════════════════════════════════════════════
    # Real Solar System. Priced as its own pool rather than by scaling the stock
    # one: reaching orbit at all is a difficulty-5 problem here, and a Mars
    # landing is the hardest thing in the game. That authored difficulty is the
    # whole of "an RSS player earns more" — WEEKLY_COINS_PER_DIFFICULTY does the
    # rest, and no multiplier hangs off a claim about an install.
    # ═════════════════════════════════════════════════════════════════════════
    ('[Real Solar System] Fly an aircraft above 10 km over Earth',
     '[Real Solar System] Dünya üzerinde 10 km yüksekliğin üzerinde bir uçak uçurun',
     2, 'exploration', 'active_vessel', 'FLYING', 'Earth', _RSS),
    ('[Real Solar System] Launch a sounding rocket above 100 km over Earth',
     '[Real Solar System] Dünya üzerinde 100 km üzerine bir sondaj roketi fırlatın',
     3, 'orbital', 'active_vessel', 'SUB_ORBITAL', 'Earth', _RSS),
    ('[Real Solar System] Reach space on a suborbital trajectory above Earth',
     '[Real Solar System] Dünya üzerinde yörünge altı bir rotayla uzaya ulaşın',
     3, 'orbital', 'active_vessel', 'SUB_ORBITAL', 'Earth', _RSS),
    ('[Real Solar System] Splash down a capsule in an Earth ocean',
     '[Real Solar System] Bir kapsülü Dünya okyanusuna indirin',
     4, 'landing', 'active_vessel', 'SPLASHED', 'Earth', _RSS),
    ('[Real Solar System] Reach a stable orbit around Earth',
     '[Real Solar System] Dünya etrafında kararlı bir yörüngeye ulaşın',
     5, 'orbital', 'active_vessel', 'ORBITING', 'Earth', _RSS),
    ('[Real Solar System] Launch a satellite into a polar Earth orbit',
     '[Real Solar System] Kutupsal Dünya yörüngesine bir uydu yerleştirin',
     5, 'orbital', 'active_vessel', 'ORBITING', 'Earth', _RSS),
    ('[Real Solar System] Fly a supersonic aircraft above 20 km over Earth',
     '[Real Solar System] Dünya üzerinde 20 km üzerinde süpersonik bir uçak uçurun',
     5, 'exploration', 'active_vessel', 'FLYING', 'Earth', _RSS),
    ('[Real Solar System] Reach a geostationary orbit around Earth',
     '[Real Solar System] Dünya etrafında sabit bir yörüngeye ulaşın',
     6, 'orbital', 'active_vessel', 'ORBITING', 'Earth', _RSS),
    ('[Real Solar System] Perform a flyby of the Moon',
     '[Real Solar System] Ay yakın geçişi yapın',
     6, 'exploration', 'active_vessel', 'ESCAPING', 'Moon', _RSS),
    ('[Real Solar System] Enter orbit around the Moon',
     '[Real Solar System] Ay yörüngesine girin',
     6, 'orbital', 'active_vessel', 'ORBITING', 'Moon', _RSS),
    ('[Real Solar System] Deploy four relay satellites into Earth orbit',
     '[Real Solar System] Dünya yörüngesine dört röle uydusu yerleştirin',
     6, 'orbital', 'constellation', 'ORBITING', 'Earth', _RSS),
    ('[Real Solar System] Perform a Moon landing',
     '[Real Solar System] Ay inişi yapın',
     7, 'landing', 'active_vessel', 'LANDED', 'Moon', _RSS),
    ('[Real Solar System] Land a rover on the Moon',
     "[Real Solar System] Ay'a bir gezici indirin",
     7, 'landing', 'active_vessel', 'LANDED', 'Moon', _RSS),
    ('[Real Solar System] Put a probe into Jupiter orbit',
     '[Real Solar System] Jüpiter yörüngesine bir sonda yerleştirin',
     7, 'exploration', 'active_vessel', 'ORBITING', 'Jupiter', _RSS),
    ('[Real Solar System] Establish a station in low Earth orbit with at least 3 crew aboard',
     '[Real Solar System] Alçak Dünya yörüngesinde en az 3 mürettebatlı bir istasyon kurun',
     7, 'construction', 'active_vessel', 'ORBITING', 'Earth', _RSS),
    ('[Real Solar System] Land a rover on Mars',
     "[Real Solar System] Mars'a bir gezici indirin",
     8, 'landing', 'active_vessel', 'LANDED', 'Mars', _RSS),
    ('[Real Solar System] Land on Venus',
     "[Real Solar System] Venüs'e iniş yapın",
     8, 'landing', 'active_vessel', 'LANDED', 'Venus', _RSS),
    ('[Real Solar System] Land a crew of three on the Moon',
     "[Real Solar System] Ay'a üç kişilik bir mürettebat indirin",
     8, 'landing', 'active_vessel', 'LANDED', 'Moon', _RSS),
    ('[Real Solar System] Build the ISS in Earth orbit',
     "[Real Solar System] Dünya yörüngesinde ISS'i inşa edin",
     8, 'construction', 'active_vessel', 'ORBITING', 'Earth', _RSS),
    ('[Real Solar System] Enter orbit around Mercury',
     '[Real Solar System] Merkür yörüngesine girin',
     8, 'exploration', 'active_vessel', 'ORBITING', 'Mercury', _RSS),
    ('[Real Solar System] Enter orbit around Saturn',
     '[Real Solar System] Satürn yörüngesine girin',
     8, 'exploration', 'active_vessel', 'ORBITING', 'Saturn', _RSS),
    ('[Real Solar System] Land a probe on Titan',
     "[Real Solar System] Titan'a bir sonda indirin",
     9, 'landing', 'active_vessel', 'LANDED', 'Titan', _RSS),
    ('[Real Solar System] Land on Europa',
     "[Real Solar System] Europa'ya iniş yapın",
     9, 'landing', 'active_vessel', 'LANDED', 'Europa', _RSS),
    ('[Real Solar System] Send a Voyager-style probe out of the solar system',
     '[Real Solar System] Güneş sistemi dışına Voyager tarzı bir sonda gönderin',
     9, 'extreme', 'active_vessel', 'ESCAPING', 'Sun', _RSS),
    ('[Real Solar System] Perform a crewed Mars landing',
     '[Real Solar System] Mürettebatlı bir Mars inişi yapın',
     10, 'extreme', 'active_vessel', 'LANDED', 'Mars', _RSS),

    # ═════════════════════════════════════════════════════════════════════════
    # Realism Overhaul. Written with body roles, so the same entry serves an RO
    # install on the stock system and one on RSS; what it tests is the engine
    # and the propellant, which is what RO changes.
    # ═════════════════════════════════════════════════════════════════════════
    ('[Realism Overhaul] Build a solid-fuelled launch vehicle in the VAB',
     "[Realism Overhaul] VAB'de katı yakıtlı bir fırlatma aracı tasarlayın",
     5, 'construction', 'craft_build', None, None, _RO),
    ('[Realism Overhaul] Build a pressure-fed orbital insertion stage in the VAB',
     "[Realism Overhaul] VAB'de basınç beslemeli bir yörünge kademesi tasarlayın",
     5, 'construction', 'craft_build', None, None, _RO),
    ('[Realism Overhaul] Reach orbit around {home} with a hypergolic upper stage',
     '[Realism Overhaul] Hipergolik üst kademeyle {home} yörüngesine ulaşın',
     6, 'orbital', 'active_vessel', 'ORBITING', '{home}', _RO),
    ('[Realism Overhaul] Put a craft with a cryogenic upper stage into {home} orbit',
     '[Realism Overhaul] Kriyojenik üst kademeli bir aracı {home} yörüngesine yerleştirin',
     6, 'orbital', 'active_vessel', 'ORBITING', '{home}', _RO),
    ('[Realism Overhaul] Put a crewed capsule into {home} orbit with at least 2 crew aboard',
     '[Realism Overhaul] En az 2 mürettebatlı bir kapsülü {home} yörüngesine yerleştirin',
     6, 'construction', 'active_vessel', 'ORBITING', '{home}', _RO),
    ('[Realism Overhaul] Enter {moon} orbit with a storable-propellant stage',
     '[Realism Overhaul] Depolanabilir yakıtlı bir kademeyle {moon} yörüngesine girin',
     7, 'exploration', 'active_vessel', 'ORBITING', '{moon}', _RO),
    ('[Realism Overhaul] Deploy three relay satellites into {home} orbit',
     '[Realism Overhaul] {home} yörüngesine üç röle uydusu yerleştirin',
     7, 'orbital', 'constellation', 'ORBITING', '{home}', _RO),
    ('[Realism Overhaul] Land a probe softly on {moon}',
     '[Realism Overhaul] {moon} üzerine yumuşak bir sonda inişi yapın',
     8, 'landing', 'active_vessel', 'LANDED', '{moon}', _RO),

    # ═════════════════════════════════════════════════════════════════════════
    # Near Future Technologies.
    # ═════════════════════════════════════════════════════════════════════════
    ('[Near Future Technologies] Build a high-power ion probe in the VAB',
     "[Near Future Technologies] VAB'de yüksek güçlü bir iyon sondası tasarlayın",
     4, 'construction', 'craft_build', None, None, _NF),
    ('[Near Future Technologies] Build a base on {moon} with an onboard reactor',
     '[Near Future Technologies] {moon} üzerinde kendi reaktörü olan bir üs kurun',
     4, 'construction', 'active_vessel', 'LANDED', '{moon}', _NF),
    ('[Near Future Technologies] Build a nuclear thermal orbital tug',
     '[Near Future Technologies] Nükleer termal bir yörünge römorkörü tasarlayın',
     5, 'construction', 'craft_build', None, None, _NF),
    ('[Near Future Technologies] Reach {desert_planet} orbit using argon-fuelled ion propulsion',
     '[Near Future Technologies] Argon yakıtlı iyon itkisiyle {desert_planet} yörüngesine ulaşın',
     5, 'exploration', 'active_vessel', 'ORBITING', '{desert_planet}', _NF),
    ('[Near Future Technologies] Put a nuclear electric tug into {home} orbit',
     '[Near Future Technologies] {home} yörüngesine nükleer elektrikli bir römorkör yerleştirin',
     5, 'construction', 'active_vessel', 'ORBITING', '{home}', _NF),
    ('[Near Future Technologies] Deploy four relay satellites into {home} orbit',
     '[Near Future Technologies] {home} yörüngesine dört röle uydusu yerleştirin',
     5, 'orbital', 'constellation', 'ORBITING', '{home}', _NF),
    ('[Near Future Technologies] Deploy a large solar array station in low {star} orbit',
     '[Near Future Technologies] Alçak {star} yörüngesine büyük bir güneş paneli istasyonu kurun',
     6, 'construction', 'active_vessel', 'ORBITING', '{star}', _NF),
    ('[Near Future Technologies] Reach {far_planet} orbit using ion propulsion only',
     '[Near Future Technologies] Yalnızca iyon itkisiyle {far_planet} yörüngesine ulaşın',
     6, 'exploration', 'active_vessel', 'ORBITING', '{far_planet}', _NF),
    ('[Near Future Technologies] Build a modular station around {moon} with truss segments',
     '[Near Future Technologies] {moon} etrafında kafes kirişli modüler bir istasyon kurun',
     6, 'construction', 'active_vessel', 'ORBITING', '{moon}', _NF),
    ('[Near Future Technologies] Land a long-range rover with its own reactor on {moon}',
     '[Near Future Technologies] {moon} üzerine kendi reaktörü olan uzun menzilli bir gezici indirin',
     6, 'landing', 'active_vessel', 'LANDED', '{moon}', _NF),
    ('[Near Future Technologies] Enter orbit around {gas_giant} using a nuclear thermal stage',
     '[Near Future Technologies] Nükleer termal kademeyle {gas_giant} yörüngesine girin',
     7, 'exploration', 'active_vessel', 'ORBITING', '{gas_giant}', _NF),

    # ═════════════════════════════════════════════════════════════════════════
    # Far Future Technologies.
    # ═════════════════════════════════════════════════════════════════════════
    ('[Far Future Technologies] Build a fusion-powered interplanetary ship in the VAB',
     "[Far Future Technologies] VAB'de füzyon güçlü bir gezegenlerarası gemi tasarlayın",
     8, 'construction', 'craft_build', None, None, _FF),
    ('[Far Future Technologies] Build an antimatter factory in {home} orbit',
     '[Far Future Technologies] {home} yörüngesinde antimadde fabrikası kurun',
     8, 'construction', 'active_vessel', 'ORBITING', '{home}', _FF),
    ('[Far Future Technologies] Put an antimatter collector into {gas_giant} orbit',
     '[Far Future Technologies] {gas_giant} yörüngesine antimadde toplayıcı yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', '{gas_giant}', _FF),
    ('[Far Future Technologies] Deploy a laser propulsion relay in {home} orbit',
     '[Far Future Technologies] {home} yörüngesine lazer itki rölesi yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', '{home}', _FF),
    ('[Far Future Technologies] Put a fission fragment rocket into {home} orbit',
     '[Far Future Technologies] {home} yörüngesine bir fisyon parçacık roketi yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', '{home}', _FF),
    ('[Far Future Technologies] Construct a massive interstellar generation ship',
     '[Far Future Technologies] Devasa bir yıldızlararası nesil gemisi tasarlayın',
     9, 'construction', 'craft_build', None, None, _FF),
    ('[Far Future Technologies] Enter orbit around {far_planet} using a fusion drive',
     '[Far Future Technologies] Füzyon itkisiyle {far_planet} yörüngesine girin',
     9, 'exploration', 'active_vessel', 'ORBITING', '{far_planet}', _FF),
    ('[Far Future Technologies] Deploy three laser relay satellites into {home} orbit',
     '[Far Future Technologies] {home} yörüngesine üç lazer röle uydusu yerleştirin',
     9, 'orbital', 'constellation', 'ORBITING', '{home}', _FF),
    ('[Far Future Technologies] Enter a low orbit around {star} with an antimatter-powered probe',
     '[Far Future Technologies] Antimadde güçlü bir sondayla alçak {star} yörüngesine girin',
     9, 'extreme', 'active_vessel', 'ORBITING', '{star}', _FF),
    ('[Far Future Technologies] Land a crewed torchship on {big_moon}',
     '[Far Future Technologies] {big_moon} üzerine mürettebatlı bir meşale gemisi indirin',
     10, 'extreme', 'active_vessel', 'LANDED', '{big_moon}', _FF),

    # ═════════════════════════════════════════════════════════════════════════
    # Life support. The generic entries need only that *some* life-support model
    # is installed (ls:any, derived in data/install_profile.py); the rest name the
    # mod whose mechanic they are about. Nothing here asks how long a crew
    # survived — that is a duration, and a submission is one snapshot.
    # ═════════════════════════════════════════════════════════════════════════
    ('Build a crew shuttle with a life-support recycler in the VAB',
     "VAB'de yaşam destek geri dönüştürücülü bir mürettebat mekiği tasarlayın",
     4, 'construction', 'craft_build', None, None, _LS),
    ('Put a station with a life-support recycler into {home} orbit with at least 2 crew aboard',
     '{home} yörüngesine en az 2 mürettebatlı, yaşam destek geri dönüştürücülü bir istasyon yerleştirin',
     5, 'construction', 'active_vessel', 'ORBITING', '{home}', _LS),
    ('Fit a greenhouse module to a station in {home} orbit',
     '{home} yörüngesindeki bir istasyona sera modülü ekleyin',
     6, 'construction', 'active_vessel', 'ORBITING', '{home}', _LS),
    ('Land a long-endurance crewed lander on {moon2}',
     '{moon2} üzerine uzun menzilli mürettebatlı bir iniş aracı indirin',
     6, 'landing', 'active_vessel', 'LANDED', '{moon2}', _LS),
    ('Establish a surface base on {moon} with a life-support recycler and at least 3 crew aboard',
     '{moon} üzerinde yaşam destek geri dönüştürücülü ve en az 3 mürettebatlı bir üs kurun',
     7, 'construction', 'active_vessel', 'LANDED', '{moon}', _LS),
    ('[Kerbalism] Put a station with a pressurised habitat into {home} orbit with at least 2 crew aboard',
     '[Kerbalism] {home} yörüngesine en az 2 mürettebatlı, basınçlı yaşam alanlı bir istasyon yerleştirin',
     6, 'construction', 'active_vessel', 'ORBITING', '{home}', _KERBALISM),
    ('[Kerbalism] Land a radiation-shielded lander on {moon}',
     '[Kerbalism] {moon} üzerine radyasyon kalkanlı bir iniş aracı indirin',
     7, 'landing', 'active_vessel', 'LANDED', '{moon}', _KERBALISM),
    ('[Kerbalism] Build a greenhouse base on {desert_planet}',
     '[Kerbalism] {desert_planet} üzerinde bir sera üssü kurun',
     8, 'construction', 'active_vessel', 'LANDED', '{desert_planet}', _KERBALISM),
    ('[USI Life Support] Land a Kolonist on {moon}',
     '[USI Life Support] {moon} üzerine bir Kolonist indirin',
     6, 'landing', 'active_vessel', 'LANDED', '{moon}', _USI),
    ('[USI Life Support] Establish an MKS logistics hub in {moon} orbit',
     '[USI Life Support] {moon} yörüngesinde bir MKS lojistik merkezi kurun',
     7, 'construction', 'active_vessel', 'ORBITING', '{moon}', _USI),
    ('[USI Life Support] Set up a resource extraction chain on {moon2}',
     '[USI Life Support] {moon2} üzerinde bir kaynak çıkarma zinciri kurun',
     7, 'construction', 'active_vessel', 'LANDED', '{moon2}', _USI),
    ('[USI Life Support] Build a self-sufficient colony module on {moon}',
     '[USI Life Support] {moon} üzerinde kendi kendine yeten bir koloni modülü kurun',
     8, 'construction', 'active_vessel', 'LANDED', '{moon}', _USI),
    ('[TAC Life Support] Put a long-endurance station into {home} orbit with at least 3 crew aboard',
     '[TAC Life Support] {home} yörüngesine en az 3 mürettebatlı, uzun menzilli bir istasyon yerleştirin',
     6, 'construction', 'active_vessel', 'ORBITING', '{home}', _TAC),
    ('[TAC Life Support] Build a water-recycling base on {moon}',
     '[TAC Life Support] {moon} üzerinde su geri dönüşümlü bir üs kurun',
     7, 'construction', 'active_vessel', 'LANDED', '{moon}', _TAC),
    ('[Snacks] Put a snack-stocked station into {home} orbit with at least 2 crew aboard',
     '[Snacks] {home} yörüngesine en az 2 mürettebatlı, atıştırmalık dolu bir istasyon yerleştirin',
     5, 'construction', 'active_vessel', 'ORBITING', '{home}', _SNACKS),
    ('[Snacks] Land a crewed base with a soil recycler on {moon}',
     '[Snacks] {moon} üzerine toprak geri dönüştürücülü mürettebatlı bir üs indirin',
     7, 'construction', 'active_vessel', 'LANDED', '{moon}', _SNACKS),

    # ═════════════════════════════════════════════════════════════════════════
    # Outer Planets Mod. Additive — it adds bodies to whatever system is there,
    # so these name their bodies outright and carry no system requirement.
    # ═════════════════════════════════════════════════════════════════════════
    ('[Outer Planets Mod] Perform a flyby of Sarnus',
     '[Outer Planets Mod] Sarnus yakın geçişi yapın',
     5, 'exploration', 'active_vessel', 'ESCAPING', 'Sarnus', _OPM),
    ('[Outer Planets Mod] Land on Hale',
     "[Outer Planets Mod] Hale'ye iniş yapın",
     6, 'landing', 'active_vessel', 'LANDED', 'Hale', _OPM),
    ('[Outer Planets Mod] Deploy a relay network around Urlum',
     '[Outer Planets Mod] Urlum etrafında röle ağı kurun',
     6, 'construction', 'constellation', 'ORBITING', 'Urlum', _OPM),
    ('[Outer Planets Mod] Enter orbit around Sarnus',
     '[Outer Planets Mod] Sarnus yörüngesine girin',
     6, 'exploration', 'active_vessel', 'ORBITING', 'Sarnus', _OPM),
    ('[Outer Planets Mod] Land on Tekto',
     "[Outer Planets Mod] Tekto'ya iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Tekto', _OPM),
    ('[Outer Planets Mod] Enter orbit around Neidon',
     '[Outer Planets Mod] Neidon yörüngesine girin',
     7, 'exploration', 'active_vessel', 'ORBITING', 'Neidon', _OPM),
    ('[Outer Planets Mod] Land a rover on Plock',
     "[Outer Planets Mod] Plock'a bir gezici indirin",
     7, 'landing', 'active_vessel', 'LANDED', 'Plock', _OPM),
    ('[Outer Planets Mod] Build a refuelling station in Sarnus orbit',
     '[Outer Planets Mod] Sarnus yörüngesinde yakıt ikmal istasyonu kurun',
     7, 'construction', 'active_vessel', 'ORBITING', 'Sarnus', _OPM),
    ('[Outer Planets Mod] Land a probe on Thatmo',
     "[Outer Planets Mod] Thatmo'ya bir sonda indirin",
     8, 'landing', 'active_vessel', 'LANDED', 'Thatmo', _OPM),
    ('[Outer Planets Mod] Perform a crewed landing on Slate',
     "[Outer Planets Mod] Slate'e mürettebatlı iniş yapın",
     8, 'landing', 'active_vessel', 'LANDED', 'Slate', _OPM),

    # ═════════════════════════════════════════════════════════════════════════
    # Kcalbeloh System. Only bodies that are in settings.KNOWN_CELESTIAL_BODIES
    # are named here — see the header.
    # ═════════════════════════════════════════════════════════════════════════
    ('[Kcalbeloh System] Enter orbit around the Kcalbeloh black hole',
     '[Kcalbeloh System] Kcalbeloh kara deliği etrafında yörüngeye girin',
     8, 'exploration', 'active_vessel', 'ORBITING', 'Kcalbeloh', _KCAL),
    ('[Kcalbeloh System] Deploy a science probe into Kcalbeloh orbit',
     '[Kcalbeloh System] Kcalbeloh yörüngesine bir bilim sondası yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', 'Kcalbeloh', _KCAL),
    ('[Kcalbeloh System] Land on Suluco',
     "[Kcalbeloh System] Suluco'ya iniş yapın",
     9, 'landing', 'active_vessel', 'LANDED', 'Suluco', _KCAL),
    ('[Kcalbeloh System] Send a crewed station into Kcalbeloh orbit',
     '[Kcalbeloh System] Kcalbeloh yörüngesine mürettebatlı bir istasyon gönderin',
     10, 'extreme', 'active_vessel', 'ORBITING', 'Kcalbeloh', _KCAL),
]
