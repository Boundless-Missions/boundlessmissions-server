"""Static + algorithmic guards for the security fixes.

No network, no build, no Firebase. Two kinds of check:
  (A) SOURCE GUARDS — assert the fix is wired into the real source (catches a
      regression that silently reintroduces a hole, e.g. re-adding make_public()).
  (B) ALGORITHM SPEC — a faithful Python port of the two mod sanitizers, exercised
      against the attack vectors. The port documents the intended behaviour; the
      source guards in (A) prove the C# actually calls that logic. (The port is a
      spec check, NOT the shipped code — trust the C# + guard for wiring.)

Run:  ./.venv/bin/python test_security_invariants.py
"""
import os
import re
import sys

BOT = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.normpath(os.path.join(BOT, "..", "KSP Mod Side", "GeneKerman"))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── (A) SOURCE GUARDS ─────────────────────────────────────────────────────────

print("\n[A1] CRITICAL — FlagTransfer.cs no longer trusts sender url/ext")
ft = read(os.path.join(MOD, "FlagTransfer.cs"))
m = re.search(r"private static bool TryInstallFlagNode\(ConfigNode fn\)(.*?)\n        \}",
              ft, re.S)
body = m.group(1) if m else ""
check("TryInstallFlagNode found", bool(body))
check("recomputes url via ComputeContentUrl(data)", "ComputeContentUrl(data)" in body)
check("clamps ext via SafeFlagExt", "SafeFlagExt(" in body)
check("does NOT pass the sender-declared url straight to InstallOneFlag",
      "InstallOneFlag(url," not in body or "ComputeContentUrl(data)" in body)
check("SafeFlagExt helper exists and clamps to FLAG_EXTS",
      "private static string SafeFlagExt" in ft and "FLAG_EXTS" in ft)
check("InstallOneFlag has a GameData containment guard",
      "refusing flag write outside GameData" in ft or "GetFullPath(GameDataRoot)" in ft)

print("\n[A2] HIGH — CraftInstaller.cs sanitizes the server-supplied filename")
ci = read(os.path.join(MOD, "CraftInstaller.cs"))
check("SanitizeCraftFileName helper exists", "private static string SanitizeCraftFileName" in ci)
check("Install() calls SanitizeCraftFileName", "SanitizeCraftFileName(craftFileName)" in ci)
check("old unsanitized 'safeName = craftFileName;' assignment is gone",
      "string safeName = craftFileName;" not in ci)

print("\n[A3] Upload split — craft/vessel/gift/marketplace objects are PRIVATE")
contracts = read(os.path.join(BOT, "data", "contracts.py"))
imports = read(os.path.join(BOT, "data", "imports.py"))
mkt = read(os.path.join(BOT, "data", "marketplace.py"))
check("contracts.upload_private_to_storage exists", "def upload_private_to_storage" in contracts)
check("imports.upload_gift no longer calls make_public()",
      "make_public" not in imports.split("def upload_gift(", 1)[-1].split("def ", 1)[0])
check("marketplace.upload_craft no longer calls make_public()",
      "make_public" not in mkt.split("async def upload_craft(", 1)[-1].split("\nasync def ", 1)[0])
check("marketplace.upload_craft returns a private path (upload_private)",
      "return upload_private(" in mkt.split("async def upload_craft(", 1)[-1].split("\nasync def ", 1)[0])

print("\n[A4] Serve points sign; public grid withholds; correct import (not the singleton)")
api = read(os.path.join(BOT, "api_server.py"))
check("api_server imports sign_stored directly (not store.sign_stored)",
      "from data.store import" in api and "sign_stored" in api
      and "store.sign_stored" not in api)
check("_sign_import_entry covers craft_url, vessel_node_url, flag_url",
      all(k in api.split("def _sign_import_entry", 1)[-1].split("\n\n\n", 1)[0]
          for k in ('"craft_url"', '"vessel_node_url"', '"flag_url"')))
check("_listing_to_model default withholds craft_url (include_download gate)",
      "include_download: bool = False" in api and "if include_download else None" in api)
check("public listings grid uses the default (no include_download=True)",
      "listings=[_listing_to_model(l) for l in window]" in api)

print("\n[A5] Debug test panel is gated OUT of production builds")
panel = read(os.path.join(MOD, "DebugTestPanel.cs"))
check("DebugTestPanel.cs is wrapped in #if GK_DEBUG_PANEL",
      panel.lstrip().startswith("#if GK_DEBUG_PANEL") and panel.rstrip().endswith("#endif"))
csproj = read(os.path.join(MOD, "GeneKerman.csproj"))
check("csproj defaults GKChannel to production",
      "<GKChannel Condition=\"'$(GKChannel)' == ''\">production</GKChannel>" in csproj)
check("csproj defines GK_DEBUG_PANEL only off-production",
      "'$(GKChannel)' != 'production'" in csproj and "GK_DEBUG_PANEL" in csproj)
buildsh = read(os.path.join(MOD, "..", "build.sh"))
check("build.sh CHANNEL defaults to production",
      'CHANNEL="${GK_CHANNEL:-production}"' in buildsh)
check("build.sh passes GKChannel to the build", "-p:GKChannel=" in buildsh)
check("build.sh refuses a release on a non-production channel",
      'RELEASE" = "1" ] && [ "$CHANNEL" != "production"' in buildsh)
check("server debug endpoint is gated (404 when DEBUG_ENDPOINTS_ENABLED off)",
      "if not cfg.DEBUG_ENDPOINTS_ENABLED:" in api and "/api/v1/debug/signtest" in api)


# ── (B) ALGORITHM SPEC (ported; attack-vector behaviour) ──────────────────────

FLAG_EXTS = {"png", "dds", "jpg", "jpeg", "truecolor", "mbm", "tga"}


def safe_flag_ext(ext):
    ext = (ext or "").strip().lstrip(".").lower()
    return ext if ext in FLAG_EXTS else "png"


def sanitize_craft_filename(name):
    if not name:
        return "received_craft.craft"
    name = name.replace("\\", "/")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]           # basename only
    name = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name)
    name = name.lstrip(".").strip()
    name = name[:128]
    return name or "received_craft.craft"


print("\n[B1] SafeFlagExt spec — dangerous extensions are refused")
check("dll -> png", safe_flag_ext("dll") == "png")
check("cfg -> png", safe_flag_ext("cfg") == "png")
check(".DLL (dot/case) -> png", safe_flag_ext(".DLL") == "png")
check("empty -> png", safe_flag_ext("") == "png")
check("png stays png", safe_flag_ext("png") == "png")
check("dds (real texture) stays dds", safe_flag_ext("dds") == "dds")

print("\n[B2] SanitizeCraftFileName spec — traversal/rooting collapse to a basename")
check("../../ traversal -> basename", sanitize_craft_filename("../../../../evil.craft") == "evil.craft")
check("windows \\..\\ traversal -> basename",
      sanitize_craft_filename("..\\..\\evil.craft") == "evil.craft")
check("absolute posix path -> basename", sanitize_craft_filename("/etc/cron.d/x.craft") == "x.craft")
check("absolute windows path -> basename",
      sanitize_craft_filename("C:\\Windows\\System32\\x.craft") == "x.craft")
check("pure '..' -> default", sanitize_craft_filename("..") == "received_craft.craft")
check("leading dots stripped", not sanitize_craft_filename("...craft").startswith("."))
check("normal name with space preserved", sanitize_craft_filename("lil guy.craft") == "lil guy.craft")
check("empty -> default", sanitize_craft_filename("") == "received_craft.craft")

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
