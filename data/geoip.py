"""
data/geoip.py – approximate location of a GAME SERVER's public address.

For the website's server manager header ("Frankfurt, DE"). Only ever called
with the address a registered game server is listed at — public already, in
every player's server browser — and never with a player's or visitor's address.

The lookup goes to IPinfo (https://ipinfo.io, free tier, no key needed at this
volume) over HTTPS, carrying nothing but the IP. Answers are cached in memory for
a week, so a server's address is sent at most once a week per bot process; a
failure (or IPinfo being down) just means no location is shown. Disclosed in the
privacy policy, Section 6.
"""

import ipaddress
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

LOOKUP_URL = "https://ipinfo.io/{ip}/json"
CACHE_TTL = 7 * 24 * 3600.0
FAIL_TTL = 15 * 60.0          # retry a failed lookup after this
TIMEOUT = 3.0

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict | None]] = {}


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global


def _clean(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isprintable()).strip()[:64]


def lookup(ip: str, *, fetch=None) -> dict | None:
    """{"city", "region", "country", "label"} or None. Never raises."""
    ip = str(ip or "").strip()
    if not _public(ip):
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(ip)
    if hit and now < hit[0]:
        return hit[1]
    out = None
    try:
        raw = (fetch or _fetch)(ip)
        if isinstance(raw, dict) and not raw.get("bogon"):
            city, region, country = _clean(raw.get("city")), _clean(raw.get("region")), _clean(raw.get("country"))
            if city or country:
                label = ", ".join(x for x in (city, country) if x)
                out = {"city": city, "region": region, "country": country, "label": label}
    except Exception as exc:  # noqa: BLE001
        log.info("geoip lookup for %s failed: %s", ip, exc)
    with _lock:
        _cache[ip] = (now + (CACHE_TTL if out else FAIL_TTL), out)
    return out


def _fetch(ip: str) -> dict:
    req = urllib.request.Request(LOOKUP_URL.format(ip=ip),
                                 headers={"Accept": "application/json",
                                          "User-Agent": "boundlessmissions-server-manager"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _reset_for_tests() -> None:
    with _lock:
        _cache.clear()
