"""Offline automated checks for the signed-Storage-URL security work.

Runs without network: firebase-admin V4 signing is local (uses the service-account
key), and module import doesn't touch Firestore/Storage. Exercises the ACTUAL
functions the serve points use — sign_stored, _sign_import_entry, _listing_to_model —
plus a faithful copy of the website download-proxy allow-list, so a signed
marketplace URL is proven to still pass it.

Run:  ./.venv/bin/python test_security_signing.py
"""
import os
import sys
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.store import sign_stored, is_storage_path, SIGNED_URL_MAX_TTL
import api_server

# Mirrors Website/src/app/api/marketplace/download/route.ts exactly.
STORAGE_HOST = "storage.googleapis.com"
BUCKET = "upoksp-gk-backend.firebasestorage.app"


def _proxy_allows(url: str) -> bool:
    p = urlparse(url)
    return (
        p.scheme == "https"
        and p.hostname == STORAGE_HOST
        and p.path.startswith(f"/{BUCKET}/marketplace/")
        and p.path.lower().endswith(".craft")
    )


PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def _is_signed_storage(url: str) -> bool:
    p = urlparse(url or "")
    return (p.scheme == "https" and p.hostname == STORAGE_HOST
            and "X-Goog-Signature" in parse_qs(p.query))


print("\n[1] is_storage_path classification")
check("bare contract path is signable", is_storage_path("contracts/abc/vessel_node.cfg"))
check("bare gift path is signable", is_storage_path("gifts/x/y.craft"))
check("https URL passes through", not is_storage_path("https://storage.googleapis.com/b/o/x"))
check("http URL passes through", not is_storage_path("http://legacy/x"))
check("empty passes through", not is_storage_path(""))
check("None passes through", not is_storage_path(None))

print("\n[2] sign_stored: passthrough vs real signing (backward compat)")
legacy = "https://storage.googleapis.com/b/o/x.craft"
check("legacy public URL unchanged", sign_stored(legacy) == legacy)
check("None unchanged", sign_stored(None) is None)
signed = sign_stored("contracts/abc/vessel_node.cfg")
check("bare path -> signed storage URL", _is_signed_storage(signed))
check("signed URL path preserves the object path",
      urlparse(signed).path == f"/{BUCKET}/contracts/abc/vessel_node.cfg")

print("\n[3] signed marketplace URL still satisfies the website download proxy allow-list")
mkt_signed = sign_stored("marketplace/L123/cool ship.craft".replace(" ", "%20")
                         if False else "marketplace/L123/coolship.craft")
check("signed marketplace .craft passes the proxy allow-list", _proxy_allows(mkt_signed))
check("proxy rejects a non-storage host",
      not _proxy_allows("https://evil.example/upoksp/marketplace/x.craft"))
check("proxy rejects a non-marketplace prefix",
      not _proxy_allows(sign_stored("contracts/abc/x.craft")))
check("proxy rejects a non-.craft object",
      not _proxy_allows(sign_stored("marketplace/L1/blueprint.png")))

print("\n[4] _sign_import_entry signs file fields, leaves preview/image + metadata alone")
entry = {
    "import_id": "i1", "source": "gift_craft", "ref_id": "r1",
    "craft_url": "gifts/i1/ship.craft",
    "vessel_node_url": "contracts/c1/vessel_node.cfg",
    "flag_url": "contracts/c1/flag.png",
    "blueprint_url": "https://storage.googleapis.com/b/o/blueprint.png",  # public preview
    "craft_name": "Ship",
}
out = api_server._sign_import_entry(entry)
check("craft_url signed", _is_signed_storage(out["craft_url"]))
check("vessel_node_url signed", _is_signed_storage(out["vessel_node_url"]))
check("flag_url signed", _is_signed_storage(out["flag_url"]))
check("blueprint_url (public preview) untouched", out["blueprint_url"] == entry["blueprint_url"])
check("craft_name untouched", out["craft_name"] == "Ship")
check("original entry not mutated", entry["craft_url"] == "gifts/i1/ship.craft")

print("\n[5] _listing_to_model withholds craft_url from the public grid, signs it for owners")
listing = {
    "listing_id": "L1", "seller_id": "42", "seller_name": "S",
    "craft_name": "C", "craft_type": "VAB", "part_count": 1,
    "mass": 1.0, "cost": 1.0, "price": 10, "sales_count": 0,
    "created_at": "2026-01-01", "mods": [],
    "thumbnail_url": None, "blueprint_url": None,
    "craft_url": "marketplace/L1/c.craft", "craft_filename": "c.craft",
    "status": "active", "life_support": "none",
    "ls_endurance_days": 0.0, "ls_crew_capacity": 0, "likes": 0, "dislikes": 0,
}
public = api_server._listing_to_model(listing)
owner = api_server._listing_to_model(listing, include_download=True)
check("public grid: craft_url withheld (None)", public.craft_url is None)
check("owner/buyer: craft_url is a signed storage URL", _is_signed_storage(owner.craft_url))
check("owner/buyer: signed craft_url passes the proxy allow-list", _proxy_allows(owner.craft_url))

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
