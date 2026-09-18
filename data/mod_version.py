"""
data/mod_version.py – KSP mod version registry (Firestore).

Stores the published mod versions and which one is current under a single config
document (`config/mod_version`). The KSP client reports its DLL's SHA256; the
server matches it against the latest published hash to decide whether the client
must update.

Document shape:
    config/mod_version
    {
        "latest_version": "1.2.0",
        "latest_hash":    "<sha256 hex>",
        "download_url":   "https://.../download",
        "versions": {                       # history, newest publish wins per label
            # `published_at` is when the build was registered; `superseded_at` is when
            # it stopped being latest, and is what the grace window is measured from
            # (absent on the entry that IS latest, and on anything published before
            # the grace window existed).
            # `mandatory` (default False) is what makes a release *forced*: an older
            # published build is only ever refused when some mandatory version
            # outranks it. Nothing mandatory anywhere means nobody is locked out —
            # they are told an update exists and go on playing. See `acceptance()`.
            "1.2.0": {"hash": "<sha256>", "download_url": "https://...",
                      "published_at": "<iso8601>", "mandatory": false},
            "1.1.0": {"hash": "<sha256>", "download_url": "https://...",
                      "published_at": "<iso8601>", "superseded_at": "<iso8601>",
                      "mandatory": false}
        },
        "updated_at": "<iso8601>",
        "updated_by": "<discord user>"
    }
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import settings
from data.store import _db, _storage_bucket

log = logging.getLogger(__name__)

# `config/mod_version` is read on the anonymous, unauthenticated `/version/check`
# — once per client start, and once per retry of a client that is being told to
# update — plus by every path that resolves the latest hash. One document that
# changes when the owner publishes a build, read at the rate of the whole player
# base: that is a metered Firestore read per anonymous request, which is a free
# amplifier pointed at `cost_guard`.
#
# So it is memoised here, at the source, rather than at any one caller — `check()`
# calls `get_config()` internally, so a cache in front of the route would leave
# the default gate-enabled path uncached, which is the finding.
#
# The TTL is short on purpose. It is a backstop for an edit made outside this
# process (the Firestore console, another instance); the console's own publish
# calls `invalidate()`, so a change made through the product takes effect at once
# rather than up to a minute later. A failed read is NOT cached — an outage must
# not be remembered as "nothing is published", which is the answer that ungates
# every client.
_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "doc": None}

# Pristine DLL bytes per hash, cached in-process so attestation doesn't hit
# Storage on every challenge. Keyed by lowercase sha256 hex.
_dll_cache: dict[str, bytes] = {}


def _doc():
    return _db.collection("config").document("mod_version")


def _dll_path(sha256: str) -> str:
    return f"mod_dll/{sha256}.dll"


def get_config(*, fresh: bool = False) -> dict:
    """Current version-registry document (empty dict if nothing published yet).

    Answered from a 60-second in-process cache; `invalidate()` clears it. Pass
    `fresh=True` to force a read — the publish path does, since it edits the
    document it just read.
    """
    if not fresh:
        with _cache_lock:
            doc, at = _cache["doc"], float(_cache["at"])
        if doc is not None and (time.time() - at) < _CACHE_TTL:
            return dict(doc)
    snap = _doc().get()
    data = snap.to_dict() if snap.exists else {}
    with _cache_lock:
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return dict(data)


def invalidate() -> None:
    """Drop the memoised registry document, so the next read hits Firestore.
    Called by the console after publishing a version."""
    with _cache_lock:
        _cache["doc"] = None
        _cache["at"] = 0.0


def publish_version(version: str, sha256: str, download_url: str,
                    set_latest: bool, updated_by: str,
                    dll_bytes: bytes | None = None,
                    mandatory: bool | None = None) -> dict:
    """Register a version's DLL hash + download URL, optionally marking it latest.

    The first version published is always made latest (so the gate has a target),
    even if set_latest is False. If `dll_bytes` is provided, the pristine DLL is
    stored so the server can answer challenge-response attestations for it (the
    bytes are the only way to verify a nonce-salted hash the client can't precompute).
    Returns the stored document.

    `mandatory` says whether this release *forces* the builds below it — see
    `acceptance()`. `None` means "leave it as it is", which is not the same as
    False: re-publishing a label is a re-upload of the same build (a corrected
    hash, a re-uploaded DLL), and it must not quietly un-force a release that was
    marked mandatory, in exactly the way `published_at` is preserved rather than
    restarting the grace clock. A label being registered for the first time with
    no answer is not mandatory, because forcing is the deliberate act here and a
    default that locks players out is not one anybody chose.
    """
    version = version.strip()
    sha256 = sha256.strip().lower()
    download_url = download_url.strip()

    # https only, checked HERE rather than at a call site. The web console enforced this
    # and explained why ("a compromised owner session shouldn't be able to redirect the
    # whole player base to a hostile download"); the Discord `/publishversion` command
    # did not, so the same reasoning protected one of the two publish paths. The value is
    # stored in config/mod_version and served to every client by /version/check, and
    # although the mod's own SafeDownloadUrl drops anything that is not absolute https,
    # nothing guarantees a future client or a third-party tool reading that field will.
    # A rule that belongs to the data belongs where the data is written.
    if download_url and not download_url.lower().startswith("https://"):
        raise ValueError("download_url must be an https:// URL.")

    # Persist the pristine bytes for attestation (best-effort; never block publish).
    has_dll = False
    if dll_bytes is not None and _storage_bucket is not None:
        try:
            blob = _storage_bucket.blob(_dll_path(sha256))
            blob.upload_from_string(dll_bytes, content_type="application/octet-stream")
            _dll_cache[sha256] = dll_bytes
            has_dll = True
            log.info("Stored pristine DLL for attestation (%s, %d bytes)", sha256[:12], len(dll_bytes))
        except Exception as exc:
            log.warning("Could not store pristine DLL for %s: %s", version, exc)

    ref = _doc()
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}

    now_iso = datetime.now(timezone.utc).isoformat()

    versions = data.get("versions") or {}
    # `published_at` is preserved when an existing label is re-published (a re-upload
    # of the same build, or a corrected hash): the grace window is about how long a
    # build has been stale, and re-registering 1.1.0 does not make it new again.
    prior = versions.get(version) or {}
    versions[version] = {
        "hash": sha256, "download_url": download_url, "has_dll": has_dll,
        "published_at": prior.get("published_at") or now_iso,
        "mandatory": bool(prior.get("mandatory")) if mandatory is None else bool(mandatory),
    }
    # Re-publishing the label that is currently latest must not leave a stale
    # `superseded_at` on it — it is not superseded, it is the target.
    if not (set_latest or not data.get("latest_hash")) and prior.get("superseded_at"):
        versions[version]["superseded_at"] = prior["superseded_at"]
    data["versions"] = versions

    if set_latest or not data.get("latest_hash"):
        # The outgoing latest stops being latest right now, and that instant — not its
        # own publish date — is when its holders' copies went stale. It is what
        # `acceptance()` measures the grace window from, so it is stamped here, at the
        # one moment the transition happens and the only place that can observe it.
        outgoing = data.get("latest_version")
        if outgoing and outgoing != version:
            prev = versions.get(outgoing)
            # Only if it still names the hash that was actually latest: a label whose
            # hash has since been re-published to something else was already replaced,
            # and stamping it now would restart a window that had already run.
            if prev and (prev.get("hash") or "").lower() == (data.get("latest_hash") or "").lower():
                prev.setdefault("superseded_at", now_iso)

        data["latest_version"] = version
        data["latest_hash"] = sha256
        data["download_url"] = download_url
        data["has_dll"] = has_dll
        versions[version].pop("superseded_at", None)

    data["updated_at"] = now_iso
    data["updated_by"] = updated_by
    ref.set(data)
    # The writer refreshes the cache rather than only clearing it: a publish is
    # immediately followed by the version poke, and a cache left empty would send
    # every client that reacts to it straight to Firestore at once.
    with _cache_lock:
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return data


def set_mandatory(version: str, mandatory: bool, updated_by: str) -> dict:
    """Mark an already-published version as forced, or stop forcing it.

    Separate from `publish_version` because the two answer different questions at
    different times: whether a build is worth shipping is known when it is built,
    while whether the builds *below* it must go is often only learned afterwards —
    a bug found a week later in 1.2.x is the case this exists for, and re-uploading
    1.3.0 to say so would restart its `published_at` and re-store its DLL for a
    fact that has nothing to do with its bytes.

    Raises KeyError if the label was never published: forcing a version nobody can
    download is a lockout with no exit, so an unknown label is refused rather than
    registered on the spot.
    """
    version = version.strip()
    ref = _doc()
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}
    versions = data.get("versions") or {}
    if version not in versions or not isinstance(versions[version], dict):
        raise KeyError(version)

    versions[version]["mandatory"] = bool(mandatory)
    data["versions"] = versions
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_by"] = updated_by
    ref.set(data)
    with _cache_lock:
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return data


def get_latest_dll_bytes() -> tuple[str, bytes] | None:
    """Return (sha256, pristine_bytes) for the published-latest DLL, or None if no
    DLL was stored (attestation unavailable → callers fail open). Cached in-process."""
    cfg_doc = get_config()
    h = (cfg_doc.get("latest_hash") or "").lower()
    if not h or not cfg_doc.get("has_dll"):
        return None
    cached = _dll_cache.get(h)
    if cached is not None:
        return h, cached
    if _storage_bucket is None:
        return None
    try:
        blob = _storage_bucket.blob(_dll_path(h))
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        _dll_cache[h] = data
        return h, data
    except Exception as exc:
        log.warning("Could not load pristine DLL for attestation (%s): %s", h[:12], exc)
        return None


def _parse_iso(value) -> datetime | None:
    """Parse a stored ISO8601 stamp, or None if it is missing or unreadable.

    Unreadable is deliberately the same answer as missing: every caller treats
    "no stamp" as "no grace", so a corrupted field costs a player the window
    rather than granting an unbounded one.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Stamps written here are timezone-aware; one hand-edited in the Firestore
    # console may not be, and comparing naive to aware raises.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _grace_days() -> float:
    """The grace window, read fresh on every call.

    Not cached at import: `admin_set_controls` mutates `settings` in the running
    process for the cost-guard budgets, so reading the attribute each time is what
    makes this window adjustable the same way without any further plumbing — and a
    window that can only be widened by a restart is useless during the outage that
    would call for widening it.
    """
    try:
        return max(0.0, float(getattr(settings, "MOD_VERSION_GRACE_DAYS", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _version_key(label) -> tuple | None:
    """Ordering key for a version label, or None if it carries no number at all.

    Only the leading dotted number is read ("1.4.2" from "v1.4.2-rc3"), so a
    pre-release and its release compare EQUAL rather than the trailing "3" sorting
    the release candidate above the thing it is a candidate for. Equal-but-different
    is then handled as undecidable by `_mandatory_applies`, which is the safe end of
    that ambiguity — see there.
    """
    if not label or not isinstance(label, str):
        return None
    m = re.search(r"\d+(?:[._]\d+)*", label)
    if not m:
        return None
    return tuple(int(part) for part in re.split(r"[._]", m.group(0)))


def _mandatory_applies(mandatory_label: str, client_label: str) -> bool:
    """Does a release marked mandatory force the build a client is running?

    Yes when it is genuinely newer, and — deliberately — yes whenever that cannot
    be decided: a label with no number in it, or two different labels whose numbers
    are equal. Undecidable resolves to the strict answer for the same reason a
    missing `superseded_at` means no grace: this direction is the behaviour that
    already existed, so a label we cannot read costs a player the update they were
    going to be told to install anyway, while the other direction silently drops a
    release the owner explicitly marked as one nobody may stay behind.

    The client's own label never forces itself, so being *on* a mandatory build is
    not a reason to be refused.
    """
    if mandatory_label == client_label:
        return False
    mk, ck = _version_key(mandatory_label), _version_key(client_label)
    if mk is None or ck is None or mk == ck:
        return True
    return mk > ck


def required_version(cfg_doc: dict, client_label: str) -> str | None:
    """The highest mandatory release that outranks `client_label`, or None.

    None is the whole point of the feature: with nothing mandatory above it, a
    build we published is out of date and nothing more, and stays playable.
    """
    applying = [name for name, meta in (cfg_doc.get("versions") or {}).items()
                if isinstance(meta, dict) and meta.get("mandatory")
                and _mandatory_applies(name, client_label)]
    if not applying:
        return None
    # Highest by number, falling back to the label so the pick is stable for the
    # unnumbered ones rather than depending on dict order.
    return max(applying, key=lambda n: (_version_key(n) or (), n))


def acceptance(client_hash: str, cfg_doc: dict | None = None) -> dict:
    """Decide whether a client's DLL hash may still talk to this server.

    THE single place that answers that question. Both gate call sites — `check()`
    on the advisory startup route and `enforce_mod_version()` on every authenticated
    request — go through here, because the two drifting apart is the one failure with
    no symptom worth reading: the client is told at startup that it may proceed and
    is then refused 426 by every call it makes, which presents as the mod being broken
    rather than as being out of date.

    Returns {"state", "version", "grace_until", "required_version", "latest_hash",
    "latest_version"} where state is one of:

      "current"  – on the published latest, or nothing is published (fail open).
      "optional" – a build we published, behind the latest, with NO mandatory
                   release above it. Accepted for as long as that stays true;
                   there is no deadline and `grace_until` is null. The client is
                   told an update exists and goes on playing.
      "grace"    – as above, but a mandatory release DOES outrank it, so the
                   clock is running. Out of date; not yet refused. `grace_until`
                   says when that ends and `required_version` names the release
                   that started it.
      "blocked"  – refuse. An unknown hash always lands here, which is what keeps
                   any of this from being a hole in the tamper gate: leniency is
                   only ever extended to bytes we ourselves published.

    The mandatory flag is what separates the middle two, and it is the answer to
    the gate's real cost: forcing every player off a build the day it stops being
    latest treats "not newest" and "must not be used" as the same fact, when almost
    every release is the first and hardly any is the second. So being behind is
    ordinary and survivable by default, and a release the owner marks mandatory —
    a security fix, a protocol change the server can no longer talk to — is what
    turns the grace window back on for everything under it. Marking it mandatory
    does NOT skip that window: the reason it exists (CKAN indexes and upgrades on
    its own schedule, which we neither control nor observe) is unchanged by the
    urgency of the release.
    """
    cfg_doc = get_config() if cfg_doc is None else cfg_doc
    latest_hash = (cfg_doc.get("latest_hash") or "").lower()
    latest_version = cfg_doc.get("latest_version")
    h = (client_hash or "").strip().lower()

    base = {"version": None, "grace_until": None, "required_version": None,
            "latest_hash": latest_hash or None, "latest_version": latest_version}

    # Nothing published — there is no target to be behind, so nobody is gated.
    if not latest_hash:
        return {**base, "state": "current"}
    if h and h == latest_hash:
        return {**base, "state": "current", "version": latest_version}

    # Identify the build first, because the mandatory question is asked about a
    # version label and only a hash we published has one. An empty hash matches no
    # entry, which is the pre-existing behaviour: a client that will not say what it
    # is running is not out of date, it is unidentified.
    label, entry = None, None
    for name, meta in (cfg_doc.get("versions") or {}).items():
        if isinstance(meta, dict) and h and (meta.get("hash") or "").lower() == h:
            label, entry = name, meta
            break
    if entry is None:
        return {**base, "state": "blocked"}

    required = required_version(cfg_doc, label)
    if not required:
        # Behind, and nothing above it is forced. This is the ordinary case and it
        # is not a countdown: `grace_until` stays null, so a client reads it exactly
        # as "no deadline" — which is what every shipped client already does with it.
        return {**base, "state": "optional", "version": label}

    base = {**base, "required_version": required}

    days = _grace_days()
    if days <= 0:
        return {**base, "state": "blocked", "version": label}

    superseded = _parse_iso(entry.get("superseded_at"))
    if superseded is None:
        # Published before this window existed, or currently latest under a second
        # label. No stamp means no window: falling back to the strict behaviour is
        # the answer that cannot accidentally un-gate an ancient build.
        return {**base, "state": "blocked", "version": label}

    until = superseded + timedelta(days=days)
    if datetime.now(timezone.utc) < until:
        return {**base, "state": "grace", "version": label,
                "grace_until": until.isoformat()}
    return {**base, "state": "blocked", "version": label}


def check(client_hash: str, client_version: str) -> dict:
    """Compare a client's reported DLL hash against the published latest.

    Fails open: if nothing is published yet (no latest hash), the client is never
    blocked. Returns a dict matching VersionCheckResponse.

    An out-of-date build with no mandatory release above it answers `up_to_date`
    true, `update_available` true and a null `grace_until` — which is the shape a
    client already reads as "behind, no deadline", so the startup notice it draws
    for a graced build is the same notice, minus the sentence about stopping.

    `up_to_date` answers "may I proceed", NOT "am I on the newest build" — a graced
    client gets True. That naming is inherited and the distinction is load-bearing,
    so both literal questions are answered separately by `on_latest` and
    `update_available`. It has to be this way round: every client already in the
    wild treats `up_to_date: false` as "raise the blocking window" and knows nothing
    about grace, so returning False to a build we have decided to accept would leave
    it self-blocking on a gate the server just opened — and clients already in the
    wild are precisely who the window exists for. A field only new clients understand
    cannot carry the decision; the field every client already obeys has to.
    """
    cfg_doc = get_config()
    verdict = acceptance(client_hash, cfg_doc)
    state = verdict["state"]
    latest_version = verdict["latest_version"]
    latest_hash = verdict["latest_hash"]

    if not latest_hash:
        # Nothing published — don't gate anyone (no hash to advertise yet).
        return {"enabled": True, "up_to_date": True, "on_latest": True,
                "update_available": False, "latest_hash": None,
                "your_version": client_version or None}

    on_latest = state == "current"
    resp = {
        "enabled": True,
        "up_to_date": state in ("current", "optional", "grace"),
        "on_latest": on_latest,
        "update_available": not on_latest,
        "latest_version": latest_version,
        "latest_hash": latest_hash,
        "download_url": cfg_doc.get("download_url"),
        "your_version": client_version or None,
        "grace_until": verdict["grace_until"],
        "message": None,
    }
    # The sentence is written here rather than in the client because this is the
    # side that knows the update route, and because the three cases differ in what
    # they ask of the player rather than in wording alone: nothing, soon, or now.
    if state == "optional":
        resp["message"] = (f"A new version ({latest_version}) is available. "
                           f"Update through CKAN when you get the chance.")
    elif state == "grace":
        resp["message"] = (f"A new version ({latest_version}) is available. "
                           f"This build stops working soon. Update through CKAN.")
    elif state == "blocked":
        resp["message"] = f"A new version ({latest_version}) is available."
    return resp
