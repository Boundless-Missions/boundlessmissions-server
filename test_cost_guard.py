"""test_cost_guard.py – Behavioural tests for the spending brake and its killswitch.

No network, no Firebase, no Discord. Everything runs against throwaway
`_CostGuard` instances whose state files live in a temp dir, plus fake Firestore
/ Storage objects handed to the real `data/firebase_guard.py` proxies — so the
gating code under test is the shipped code, not a re-implementation of it.

What is covered:
  [A] the ladder            NORMAL → WARN → DEGRADED → FROZEN, driven by spend
  [B] enforcement           what require_firebase() refuses at each rung
  [C] the escape hatch      final_flush(), one grace pass per freeze
  [D] alerts                queued once per crossing, drained by cogs/costwatch
  [E] recovery              the ladder comes back down, and re-arms
  [F] the killswitch        COST_GUARD_ENABLED off, and budget<=0 (unlimited)
  [G] Gemini                soft budget: degrades to fallbacks, never raises
  [H] the free tier         applied per Pacific day, not per month
  [I] tier 1                Cloud Monitoring adopted as baseline + local tail
  [J] tier 2                the invoice is display only — never moves the ladder
  [K] signed URLs           counted, never priced, and (note) never gated
  [L] rollover              month change archives to history and resets
  [M] persistence           state survives a restart, incl. pre-ladder files
  [N] the proxies           metering + gating through data/firebase_guard.py
  [O] the flush path        data/store.py's save() shape under a freeze

Run:  ./.venv/bin/python test_cost_guard.py
"""
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

BOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BOT)

import cost_guard  # noqa: E402
import settings    # noqa: E402
from cost_guard import FirebaseBudgetExceeded, Level  # noqa: E402

REAL_STATE = os.path.join(BOT, "data", "cost_state.json")


def _live_totals():
    """A fingerprint of the live meter, so a test run can prove it did not reset
    it. Not an mtime: the bot may well be running and persisting as we go — what
    must never happen is this run erasing or rewinding the real month."""
    try:
        with open(REAL_STATE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    daily = d.get("daily") or {}
    return (d.get("month"),
            sum(v.get("reads", 0) for v in daily.values()),
            sum(v.get("writes", 0) for v in daily.values()),
            float(d.get("gemini_usd", 0.0)))


_REAL_BEFORE = _live_totals()

# Everything below writes to a scratch dir. The live meter's files are never
# touched — a test run must not spend or reset the real month.
TMP = tempfile.mkdtemp(prefix="costguard-test-")
cost_guard._STATE_PATH = os.path.join(TMP, "cost_state.json")
cost_guard._HISTORY_PATH = os.path.join(TMP, "cost_history.jsonl")

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


def raises(fn, exc=FirebaseBudgetExceeded):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # wrong exception is still a failure
        return f"wrong exception: {e!r}"
    return False


@contextmanager
def tuned(**over):
    """Temporarily override settings (the guard reads them live, by design —
    that is what makes the budgets runtime-flippable from the admin console)."""
    old = {k: getattr(settings, k) for k in over}
    for k, v in over.items():
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(settings, k, v)


def fresh():
    """A guard with no history at all."""
    for p in (cost_guard._STATE_PATH, cost_guard._HISTORY_PATH):
        if os.path.exists(p):
            os.remove(p)
    return cost_guard._CostGuard()


# Pinned pricing so the arithmetic in the tests is stable regardless of .env.
# Budget $0.60 at $0.06/100k reads ⇒ freeze at exactly 1,000,000 billable reads.
PRICING = dict(
    COST_GUARD_ENABLED=True,
    FIREBASE_MONTHLY_BUDGET_USD=0.60,
    GEMINI_MONTHLY_BUDGET_USD=1.0,
    COST_WARN_FRACTION=0.5,
    COST_DEGRADE_FRACTION=0.8,
    FIRESTORE_READ_USD_PER_100K=0.06,
    FIRESTORE_WRITE_USD_PER_100K=0.18,
    FIRESTORE_DELETE_USD_PER_100K=0.02,
    STORAGE_DOWNLOAD_USD_PER_GB=0.12,
    STORAGE_UPLOAD_USD_PER_GB=0.0,
    STORAGE_STORED_USD_PER_GB_MONTH=0.026,
    FREE_FIRESTORE_READS_PER_DAY=50_000,
    FREE_FIRESTORE_WRITES_PER_DAY=20_000,
    FREE_FIRESTORE_DELETES_PER_DAY=20_000,
    FREE_STORAGE_EGRESS_GB_PER_DAY=1.0,
    FREE_STORAGE_STORED_GB=5.0,
)
FREE = PRICING["FREE_FIRESTORE_READS_PER_DAY"]


# ── [A] the ladder ───────────────────────────────────────────────────────────
print("\n[A] The ladder climbs with spend")
with tuned(**PRICING):
    g = fresh()
    check("fresh guard is NORMAL", g.level is Level.NORMAL)
    check("fresh guard: firebase_ok", g.firebase_ok is True)
    check("fresh guard: not degraded", g.degraded is False)

    g.note_firestore(reads=FREE)          # the whole daily free tier
    check("free-tier reads cost nothing", abs(g.snapshot()["firebase"]["usd"]) < 1e-9,
          g.snapshot()["firebase"]["usd"])
    check("still NORMAL on a free day", g.level is Level.NORMAL)

    g.note_firestore(reads=500_000)       # $0.30 = 50% of $0.60
    check("50% of budget → WARN", g.level is Level.WARN, g.level.label)

    g.note_firestore(reads=300_000)       # $0.48 = 80%
    check("80% of budget → DEGRADED", g.level is Level.DEGRADED, g.level.label)
    check("degraded property agrees", g.degraded is True)
    check("firebase_ok still True at DEGRADED (bot stays usable)", g.firebase_ok is True)

    g.note_firestore(reads=200_000)       # $0.60 = 100%
    check("100% of budget → FROZEN", g.level is Level.FROZEN, g.level.label)
    check("firebase_ok False at FROZEN", g.firebase_ok is False)
    snap = g.snapshot()
    check("snapshot reports the level", snap["level"] == "frozen", snap["level"])
    check("snapshot fraction is 1.0", abs(snap["firebase"]["fraction"] - 1.0) < 1e-6,
          snap["firebase"]["fraction"])
    check("snapshot prices reads only", abs(snap["firebase"]["usd"] - 0.60) < 1e-9,
          snap["firebase"]["usd"])
FROZEN_GUARD = g


# ── [B] enforcement ──────────────────────────────────────────────────────────
print("\n[B] What each rung actually refuses")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=FREE + 500_000)                  # WARN
    check("WARN: ordinary op allowed", raises(lambda: g.require_firebase()) is False)
    check("WARN: upload allowed", raises(lambda: g.require_firebase(upload=True)) is False)

    g.note_firestore(reads=300_000)                          # DEGRADED
    check("DEGRADED: ordinary op still allowed",
          raises(lambda: g.require_firebase()) is False)
    check("DEGRADED: upload refused",
          raises(lambda: g.require_firebase(upload=True)) is True)

    g.note_firestore(reads=200_000)                          # FROZEN
    check("FROZEN: ordinary op refused", raises(lambda: g.require_firebase()) is True)
    check("FROZEN: upload refused", raises(lambda: g.require_firebase(upload=True)) is True)
    try:
        g.require_firebase()
    except FirebaseBudgetExceeded as exc:
        check("FROZEN message names the reset", "1st" in str(exc), str(exc))


# ── [C] the escape hatch ─────────────────────────────────────────────────────
print("\n[C] final_flush() — exactly one grace pass per freeze")
with tuned(**PRICING):
    g = fresh()
    with g.final_flush() as armed:
        check("no grace pass while healthy", armed is False)

    g.note_firestore(reads=FREE + 1_000_000)
    check("frozen", g.level is Level.FROZEN)

    with g.final_flush() as armed:
        check("freezing armed one grace pass", armed is True)
        check("writes are permitted inside the hatch",
              raises(lambda: g.require_firebase()) is False)
        check("even uploads are permitted inside the hatch",
              raises(lambda: g.require_firebase(upload=True)) is False)
    check("hatch closes on exit", raises(lambda: g.require_firebase()) is True)

    with g.final_flush() as armed:
        check("the grace pass is one-shot", armed is False)
        check("and does not permit writes", raises(lambda: g.require_firebase()) is True)

    # An exception inside the hatch must still close it.
    g2 = fresh()
    g2.note_firestore(reads=FREE + 1_000_000)
    assert g2.level is Level.FROZEN
    try:
        with g2.final_flush():
            raise RuntimeError("flush blew up")
    except RuntimeError:
        pass
    check("hatch closes even when the flush raises",
          raises(lambda: g2.require_firebase()) is True)


# ── [D] alerts ───────────────────────────────────────────────────────────────
print("\n[D] Threshold crossings are queued for cogs/costwatch to DM")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=FREE + 500_000)
    check("nothing is queued until the level is read (ops read it constantly)",
          g._pending_alerts == [])
    _ = g.level
    alerts = g.drain_alerts()
    check("one WARN alert queued", len(alerts) == 1 and alerts[0]["level"] == "warning",
          alerts)
    check("alert carries spend and budget",
          alerts and abs(alerts[0]["usd"] - 0.30) < 1e-9 and alerts[0]["budget"] == 0.60)
    check("draining empties the queue", g.drain_alerts() == [])

    _ = g.level
    check("no repeat alert for a level already announced", g.drain_alerts() == [])

    g.note_firestore(reads=500_000)
    _ = g.level
    alerts = g.drain_alerts()
    kinds = [a["level"] for a in alerts]
    check("skipping a rung still announces the rung reached",
          kinds == ["frozen"], kinds)
    check("alert names where it came from", alerts[0]["previous"] == "warning",
          alerts[0]["previous"])


# ── [E] recovery ─────────────────────────────────────────────────────────────
print("\n[E] Raising the budget brings the ladder back down (and re-arms)")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=FREE + 1_000_000)
    check("frozen", g.level is Level.FROZEN)
    g.drain_alerts()

with tuned(**{**PRICING, "FIREBASE_MONTHLY_BUDGET_USD": 6.0}):
    check("10× the budget → back to NORMAL", g.level is Level.NORMAL, g.level.label)
    check("ops allowed again", raises(lambda: g.require_firebase()) is False)
    check("recovery is not announced as a crossing",
          all(a["level"] != "frozen" for a in g.drain_alerts()))

with tuned(**PRICING):
    check("dropping the budget freezes again", g.level is Level.FROZEN)
    alerts = g.drain_alerts()
    check("the re-crossing is announced again",
          len(alerts) == 1 and alerts[0]["level"] == "frozen", alerts)
    with g.final_flush() as armed:
        check("a new freeze re-arms the grace pass", armed is True)


# ── [F] the killswitch ───────────────────────────────────────────────────────
print("\n[F] COST_GUARD_ENABLED off, and budget<=0 (unlimited)")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=FREE + 10_000_000)      # ~$6 against a $0.60 budget
    check("wildly over budget → FROZEN", g.level is Level.FROZEN)

with tuned(**{**PRICING, "COST_GUARD_ENABLED": False}):
    check("guard off → NORMAL despite the spend", g.level is Level.NORMAL, g.level.label)
    check("guard off → nothing is refused", raises(lambda: g.require_firebase(upload=True)) is False)
    check("guard off → Gemini is allowed too", g.gemini_ok is True)
    check("guard off → the meter still counts",
          g.snapshot()["firebase"]["usd"] > 5.0, g.snapshot()["firebase"]["usd"])
    check("snapshot admits the guard is off", g.snapshot()["enabled"] is False)

with tuned(**{**PRICING, "FIREBASE_MONTHLY_BUDGET_USD": 0.0}):
    check("budget 0 means unlimited, not zero-tolerance", g.level is Level.NORMAL)
    check("unlimited → nothing refused", raises(lambda: g.require_firebase(upload=True)) is False)
    check("snapshot flags unlimited", g.snapshot()["firebase"]["unlimited"] is True)


# ── [G] Gemini ───────────────────────────────────────────────────────────────
print("\n[G] The Gemini budget is soft — it degrades, it never raises")
class _Usage:
    def __init__(self, i, o):
        self.prompt_token_count, self.candidates_token_count = i, o

with tuned(**{**PRICING, "GEMINI_MONTHLY_BUDGET_USD": 1.0,
              "GEMINI_INPUT_USD_PER_1M": 0.10, "GEMINI_OUTPUT_USD_PER_1M": 0.40}):
    g = fresh()
    check("fresh: gemini_ok", g.gemini_ok is True)
    g.record_gemini(_Usage(1_000_000, 1_000_000))     # $0.50
    check("half spent: still ok", g.gemini_ok is True)
    check("cost is priced per token", abs(g.snapshot()["gemini"]["usd"] - 0.50) < 1e-9,
          g.snapshot()["gemini"]["usd"])
    g.record_gemini(_Usage(1_000_000, 1_000_000))     # $1.00
    check("budget spent: gemini_ok False", g.gemini_ok is False)
    alerts = g.drain_alerts()
    check("one gemini alert", len(alerts) == 1 and alerts[0]["kind"] == "gemini", alerts)
    check("no repeat gemini alert", (g.gemini_ok, g.drain_alerts()) == (False, []))
    check("a spent Gemini budget does NOT touch the Firebase ladder",
          g.level is Level.NORMAL and raises(lambda: g.require_firebase()) is False)
    g.record_gemini(None)
    check("record_gemini(None) is a no-op", abs(g.snapshot()["gemini"]["usd"] - 1.0) < 1e-9)


# ── [H] the free tier ────────────────────────────────────────────────────────
print("\n[H] The free tier is a DAILY allowance, applied day by day")
with tuned(**PRICING):
    g = fresh()
    g._daily = {"2026-08-01": {"reads": FREE}, "2026-08-02": {"reads": FREE}}
    check("two full free days cost nothing",
          abs(g.snapshot()["firebase"]["usd"]) < 1e-9, g.snapshot()["firebase"]["usd"])
    g._daily = {"2026-08-01": {"reads": 2 * FREE}}
    check("the same reads in ONE day are half billable",
          abs(g.snapshot()["firebase"]["usd"] - 0.03) < 1e-9, g.snapshot()["firebase"]["usd"])
    check("a month-total free tier would have said $0 here — it does not",
          g.snapshot()["firebase"]["usd"] > 0)

    g = fresh()
    GB = cost_guard._GB
    g._daily = {"2026-08-01": {"egress_bytes": 2 * GB, "ingress_bytes": 5 * GB}}
    lines = {l["label"]: l for l in g.snapshot()["firebase"]["lines"]}
    check("1 GB/day egress is free, the second GB is billed",
          abs(lines["Storage download"]["usd"] - 0.12) < 1e-9, lines["Storage download"]["usd"])
    check("uploads (ingress) are priced at zero",
          abs(lines["Storage upload"]["usd"]) < 1e-12, lines["Storage upload"]["usd"])

    g = fresh()
    g._auth_stored_bytes = int(15 * GB)
    line = [l for l in g.snapshot()["firebase"]["lines"] if l.get("at_rest")][0]
    check("bytes at rest: 5 GB free, the rest prorated by month elapsed",
          0 < line["usd"] <= 10 * 0.026 + 1e-9, line["usd"])
    check("projected month cost is the un-prorated figure",
          abs(g.snapshot()["storage"]["projected_month_usd"] - 10 * 0.026) < 1e-9,
          g.snapshot()["storage"]["projected_month_usd"])


# ── [I] tier 1 ───────────────────────────────────────────────────────────────
print("\n[I] Cloud Monitoring is adopted as the baseline; local counting continues")
class _Snap:
    def __init__(self, ok=True, daily=None, stored=0, error=None):
        self.ok, self.daily, self.stored_bytes = ok, daily or {}, stored
        self.error, self.fetched_at = error, 1234.0

with tuned(**PRICING):
    g = fresh()
    today = cost_guard._pacific_day()
    g.note_firestore(reads=100_000)
    g.note_storage(upload=999)
    g.ingest_usage(_Snap(daily={today: {"reads": 200_000, "egress_bytes": 5_000_000}},
                         stored=7 * cost_guard._GB))
    eff = g._effective_daily_locked()
    check("Google's figure replaces ours, not adds to it",
          eff[today]["reads"] == 200_000, eff[today]["reads"])
    check("egress we never saw is picked up",
          eff[today]["egress_bytes"] == 5_000_000, eff[today]["egress_bytes"])
    check("locally-known uploads survive the adoption (Monitoring omits ingress)",
          eff[today].get("ingress_bytes") == 999, eff[today].get("ingress_bytes"))
    check("bytes at rest come from tier 1 only",
          g.snapshot()["storage"]["stored_bytes"] == 7 * cost_guard._GB)

    g.note_firestore(reads=30_000)
    eff = g._effective_daily_locked()
    check("post-poll work is added on top of the baseline",
          eff[today]["reads"] == 230_000, eff[today]["reads"])

    snap = g.snapshot()
    check("drift is reported, not hidden",
          snap["metrics"]["drift"]["egress_bytes"] == 5_000_000,
          snap["metrics"]["drift"])
    check("metrics report as healthy", snap["metrics"]["ok"] is True)

    g.ingest_usage(_Snap(ok=False, error="403 permission denied"))
    snap = g.snapshot()
    check("a failed poll records the error", snap["metrics"]["error"] == "403 permission denied")
    check("a failed poll does NOT wipe the last good baseline",
          g._effective_daily_locked()[today]["reads"] == 230_000)
    check("...and a metering failure reads as unhealthy, not as zero usage",
          snap["metrics"]["ok"] is False and snap["firebase"]["usd"] > 0)


# ── [J] tier 2 ───────────────────────────────────────────────────────────────
print("\n[J] The BigQuery invoice is display only — it must never move the ladder")
class _Bill:
    ok = True
    invoice_month = "202608"
    currency = "USD"
    total_usd = 9999.0
    gross_usd = 10000.0
    credits_usd = -1.0
    services = [{"service": "Cloud Firestore", "usd": 9999.0}]
    table = "proj.ds.gcp_billing_export_v1"
    fetched_at = 1234.0
    bytes_processed = 10_000_000

with tuned(**PRICING):
    g = fresh()
    g.ingest_billing(_Bill())
    check("a $9999 invoice does not freeze anything", g.level is Level.NORMAL, g.level.label)
    check("...and does not raise", raises(lambda: g.require_firebase()) is False)
    snap = g.snapshot()
    check("the invoice is shown", snap["billed"]["total_usd"] == 9999.0)
    check("our own estimate is untouched by it", abs(snap["firebase"]["usd"]) < 1e-12)

    class _BadBill:
        ok = False
        error = "403 bigquery.jobs.create denied"
    g.ingest_billing(_BadBill())
    check("a billing failure is recorded", g.snapshot()["billed"]["error"].startswith("403"))
    check("...and keeps the last good invoice", g.snapshot()["billed"]["total_usd"] == 9999.0)


# ── [K] signed URLs ──────────────────────────────────────────────────────────
print("\n[K] Signed URLs: counted, never priced")
with tuned(**PRICING):
    g = fresh()
    for _ in range(5):
        g.note_signed_url()
    snap = g.snapshot()
    check("the count is kept", snap["metrics"]["signed_urls"] == 5)
    check("no bytes are invented for them", abs(snap["firebase"]["usd"]) < 1e-12)
    g.note_signed_url(size_bytes=2 * cost_guard._GB)
    check("a caller that knows the size gets it counted as egress",
          g._sum(g._daily, "egress_bytes") == 2 * cost_guard._GB)


# ── [L] rollover ─────────────────────────────────────────────────────────────
print("\n[L] Month rollover archives the closing figures and resets")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=FREE + 1_000_000)
    check("frozen before rollover", g.level is Level.FROZEN)
    g._month = "1999-12"                      # pretend the month just turned
    check("the new month starts NORMAL", g.level is Level.NORMAL, g.level.label)
    check("tallies are cleared", g._sum(g._daily, "reads") == 0)
    check("announcements re-arm", g._announced == set())
    hist = g.history()
    check("the closing month is archived", len(hist) == 1 and hist[0]["month"] == "1999-12",
          hist)
    check("the archived record keeps the spend",
          hist and abs(hist[0]["firebase_usd"] - 0.60) < 1e-6,
          hist[0]["firebase_usd"] if hist else None)
    check("the archive is local, never Firestore",
          os.path.exists(cost_guard._HISTORY_PATH))


# ── [M] persistence ──────────────────────────────────────────────────────────
print("\n[M] The meter survives a restart (it must outlive the thing it meters)")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=123_456, writes=78, deletes=9)
    g.record_gemini(_Usage(1_000_000, 0))
    g.flush()
    again = cost_guard._CostGuard()
    check("reads reload", again._sum(again._daily, "reads") == 123_456)
    check("writes reload", again._sum(again._daily, "writes") == 78)
    check("gemini spend reloads", abs(again._gemini_usd - 0.10) < 1e-9, again._gemini_usd)

    # A state file written before the ladder existed: flat totals, no per-day map.
    with open(cost_guard._STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"month": cost_guard._month_key(), "gemini_usd": 0.25,
                   "reads": 900_000, "writes": 10, "dl_bytes": 50, "ul_bytes": 60}, fh)
    legacy = cost_guard._CostGuard()
    check("a pre-ladder state file is folded into today, not discarded",
          legacy._sum(legacy._daily, "reads") == 900_000)
    check("...including its Gemini spend", abs(legacy._gemini_usd - 0.25) < 1e-9)

    # A file from a month that has already ended must not re-arm a spent budget
    # or resurrect its counters.
    with open(cost_guard._STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"month": "1999-12", "gemini_usd": 99.0,
                   "daily": {"1999-12-31": {"reads": 10_000_000}}}, fh)
    stale = cost_guard._CostGuard()
    check("a stale month starts clean",
          stale._sum(stale._daily, "reads") == 0 and stale._gemini_usd == 0.0)


# ── [N] the proxies ──────────────────────────────────────────────────────────
print("\n[N] data/firebase_guard.py — metering and gating the real Firestore/Storage handles")
from data import firebase_guard as fg  # noqa: E402


class DocumentReference:
    def __init__(self, name="doc"):
        self.name = name
    def get(self): return {"id": self.name}
    def set(self, data): return "set"
    def update(self, data): return "updated"
    def delete(self): return "deleted"


class CollectionReference:
    def document(self, name="doc"): return DocumentReference(name)
    def get(self): return [1, 2, 3]
    def stream(self): return iter([1, 2, 3, 4])
    def add(self, data): return ("ts", DocumentReference("new"))


class WriteBatch:
    def __init__(self): self.saw = []
    def set(self, ref, data): self.saw.append(ref)
    def commit(self): return len(self.saw)


class Client:
    def collection(self, name): return CollectionReference()
    def batch(self): return WriteBatch()


class Blob:
    def __init__(self): self.content_type = None
    def upload_from_string(self, data, content_type=None): return "uploaded"
    def download_as_bytes(self): return b"x" * 4096
    def generate_signed_url(self, **kw): return "https://storage.example/signed"
    def delete(self): return "gone"


class Bucket:
    def blob(self, name): return Blob()


@contextmanager
def proxied(g):
    """Point the proxies at the guard under test instead of the live singleton."""
    real = fg.guard
    fg.guard = g
    try:
        yield fg.wrap_firestore(Client()), fg.wrap_bucket(Bucket())
    finally:
        fg.guard = real


with tuned(**PRICING):
    g = fresh()
    with proxied(g) as (db, bucket):
        db.collection("users").document("42").get()
        check("a document get counts one read", g._sum(g._daily, "reads") == 1)
        check("the chain stays wrapped (collection→document)",
              isinstance(db.collection("u"), fg._GuardedRef))

        db.collection("users").get()
        check("a query get counts every doc it returned", g._sum(g._daily, "reads") == 4)

        list(db.collection("users").stream())
        check("a consumed stream counts its docs", g._sum(g._daily, "reads") == 8)

        db.collection("users").document("42").set({"xp": 1})
        db.collection("users").document("42").update({"xp": 2})
        db.collection("users").add({"xp": 3})
        check("set/update/add each count a write", g._sum(g._daily, "writes") == 3)
        check("add() keeps the returned reference guarded",
              isinstance(db.collection("u").add({})[1], fg._GuardedRef))

        db.collection("users").document("42").delete()
        check("delete counts a delete", g._sum(g._daily, "deletes") == 1)

        batch = db.batch()
        ref = db.collection("users").document("42")
        before = g._sum(g._daily, "writes")
        batch.set(ref, {"xp": 4})
        at_set = g._sum(g._daily, "writes")
        batch.commit()
        after_commit = g._sum(g._daily, "writes")
        raw = fg._unwrap(batch)
        check("batch.set() receives the REAL reference, not the proxy",
              raw.saw and not isinstance(raw.saw[0], fg._GuardedRef))
        check("a batched write is counted at set()", at_set - before == 1, (before, at_set))
        check("...and NOT counted again at commit()", after_commit == at_set,
              (at_set, after_commit))

        b = bucket.blob("craft/x.craft")
        b.content_type = "application/json"     # attribute writes must reach the blob
        check("blob attribute assignment reaches the real blob",
              object.__getattribute__(b, "_blob").content_type == "application/json")
        check("...and reads back through the proxy", b.content_type == "application/json")
        b.upload_from_string("y" * 500)
        check("an upload counts its bytes", g._sum(g._daily, "ingress_bytes") == 500)
        b.download_as_bytes()
        check("a download counts its bytes", g._sum(g._daily, "egress_bytes") == 4096)
        b.generate_signed_url(expiration=60)
        check("issuing a signed URL is counted", g._sum(g._daily, "signed_urls") == 1)

    # Now the same handles under a spent budget.
    g = fresh()
    g.note_firestore(reads=FREE + 800_000)      # DEGRADED
    with proxied(g) as (db, bucket):
        check("DEGRADED: reads still work",
              raises(lambda: db.collection("u").document("1").get()) is False)
        check("DEGRADED: writes still work",
              raises(lambda: db.collection("u").document("1").set({})) is False)
        check("DEGRADED: downloads still work",
              raises(lambda: bucket.blob("x").download_as_bytes()) is False)
        check("DEGRADED: uploads are refused",
              raises(lambda: bucket.blob("x").upload_from_string("z")) is True)

    g.note_firestore(reads=200_000)             # FROZEN
    with proxied(g) as (db, bucket):
        check("FROZEN: get raises", raises(lambda: db.collection("u").document("1").get()) is True)
        check("FROZEN: set raises", raises(lambda: db.collection("u").document("1").set({})) is True)
        check("FROZEN: delete raises",
              raises(lambda: db.collection("u").document("1").delete()) is True)
        check("FROZEN: stream raises",
              raises(lambda: list(db.collection("u").stream())) is True)
        check("FROZEN: batch.commit raises", raises(lambda: db.batch().commit()) is True)
        check("FROZEN: upload raises",
              raises(lambda: bucket.blob("x").upload_from_string("z")) is True)
        check("FROZEN: download raises",
              raises(lambda: bucket.blob("x").download_as_bytes()) is True)
        # Documented gap, not a pass/fail on correctness: signing is local, so it
        # is not gated — a frozen bot can still hand out URLs whose egress bills.
        signed_ok = raises(lambda: bucket.blob("x").generate_signed_url()) is False
        print(f"  NOTE  signed-URL issuing is NOT gated at FROZEN "
              f"({'still allowed' if signed_ok else 'refused'}) — egress it "
              f"authorises is billed but unmetered here")


# ── [O] the flush path ───────────────────────────────────────────────────────
print("\n[O] data/store.py's save() shape: buffered XP survives the freeze")
with tuned(**PRICING):
    g = fresh()
    g.note_firestore(reads=FREE + 1_000_000)
    assert g.level is Level.FROZEN
    with proxied(g) as (db, bucket):
        def save():
            """The same shape as data/store.py::save()."""
            with g.final_flush():
                batch = db.batch()
                for uid in ("1", "2", "3"):
                    batch.set(db.collection("users").document(uid), {"xp": 1})
                batch.commit()
            return True
        ok = True
        try:
            save()
        except FirebaseBudgetExceeded:
            ok = False
        check("the first flush after freezing gets through", ok is True)
        check("its writes were metered", g._sum(g._daily, "writes") == 3)

        second = True
        try:
            save()
        except FirebaseBudgetExceeded:
            second = False
        check("the next flush is refused (the stop takes hold)", second is False)


# ── the live meter must be untouched ─────────────────────────────────────────
print("\n[live] The real meter was not disturbed")
_after = _live_totals()
check("data/cost_state.json still exists", _after is not None)
if _REAL_BEFORE and _after:
    check("the live month was not rolled over", _after[0] == _REAL_BEFORE[0],
          f"{_REAL_BEFORE[0]} → {_after[0]}")
    check("live tallies were not reset or rewound",
          _after[1] >= _REAL_BEFORE[1] and _after[2] >= _REAL_BEFORE[2]
          and _after[3] >= _REAL_BEFORE[3] - 1e-9,
          f"{_REAL_BEFORE} → {_after}")
check("no history file was created next to it",
      not os.path.exists(os.path.join(BOT, "data", "cost_history.jsonl")))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
