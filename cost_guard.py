"""
cost_guard.py – Spend tracking and the spending brake for the paid Google services.

The bot depends on two metered Google products:

  • Gemini   – AI screenshot / mission / contract analysis.
  • Firebase – Firestore (every XP write, every contract) + Storage (craft files).

THREE TIERS, FOR THREE DIFFERENT JOBS
-------------------------------------
No single source is both fast enough to brake on and accurate enough to trust,
so there are three, and each is used only for what it is good at.

Tier 0 is the in-process estimate: data/firebase_guard.py counts operations and
bytes as the bot performs them. It is instant, and that is the only property a
brake really needs — a runaway retry loop has to be stopped in seconds. What it
cannot do is be right. It never sees a signed-URL download (those go straight
from the client to GCS), it cannot see bytes at rest, and it is blind to any
usage that isn't this process.

Tier 1 is Cloud Monitoring (data/gcp_metrics.py): Google's own measurements, so
it sees all three of those. It lags a few minutes, which makes it useless as a
trigger and ideal as the truth.

Tier 2 is the BigQuery billing export (data/gcp_billing.py): actual billed
dollars, net of free-tier credits, hours behind. It is the invoice, and it is
DISPLAY ONLY — `ingest_billing` writes somewhere the ladder never reads. A brake
fed by it would let a runaway spend for a whole export cycle first.

So tier 1 does not replace tier 0, it *corrects* it. `ingest_usage` adopts the
authoritative month-to-date counts as a new baseline and the local counters keep
running on top of it, so the effective figure is "Google's number, plus whatever
we've done since the last poll". The gap between the two tiers is kept and
reported as `drift`: it is the size of everything the wrapper cannot see, which
is a diagnostic worth having rather than an error to be hidden. Tier 2 sits
beside both as the answer key — where it disagrees with our modelled cost, the
error is in our price constants, and that gap is worth being able to see.

THE LADDER
----------
Enforcement used to be a single wall — under budget everything worked, over it
every Firestore call raised. That fails badly in practice: it takes the whole bot
down at once, with no warning beforehand and no way to run in a reduced mode.
Instead there are four levels (`Level`), driven by the fraction of budget spent:

  NORMAL    – nothing.
  WARN      – the owner is told, once. Everything still works.
  DEGRADED  – Storage *uploads* are refused; reads, downloads and all of
              Firestore keep working. Uploads are the expensive half and the
              most deferrable, so the bot stays fully usable and only new craft
              files are turned away.
  FROZEN    – the old hard stop: every Firestore and Storage operation raises
              `FirebaseBudgetExceeded`, after buffered writes are flushed.

Gemini is unchanged and always soft: over budget, `gemini_ok` is False and every
call site falls back to its heuristic.

STATE
-----
Persisted to LOCAL files, never to Firestore — the meter must keep working when
Firebase is the thing being cut off, and metering must not itself cost Firestore
operations. `data/cost_state.json` holds the live month; on rollover the closing
figures are appended to `data/cost_history.jsonl` so month-over-month growth is
visible before it becomes a surprise (the old version simply erased them).

The guard imports nothing from the bot except `settings`, so it is safe to
import from low-level modules like data/store.py without a circular import.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from calendar import monthrange
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import IntEnum
from zoneinfo import ZoneInfo

import settings

log = logging.getLogger(__name__)

# Live next to the other local-cache files (data/users.json).
_STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "cost_state.json")
_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "data", "cost_history.jsonl")

# Don't rewrite the file on every single Firestore op (the XP path is hot);
# flush at most this often. State is also flushed whenever a level changes.
_PERSIST_INTERVAL = 15.0

_GB = 1_073_741_824

# Google's free-tier daily quotas reset at midnight US/Pacific, not UTC. The
# month rolls over in UTC (that is the billing period) but the daily allowance
# does not, so the two calendars are deliberately kept apart. Defined here rather
# than in data/gcp_metrics.py because this module is the lower of the two —
# data/firebase_guard.py imports it on the hot path, and it must stay importable
# with nothing but `settings`.
FREE_TIER_TZ = ZoneInfo("America/Los_Angeles")


class Level(IntEnum):
    """How much of the budget is gone, and therefore how much still runs."""

    NORMAL = 0
    WARN = 1
    DEGRADED = 2
    FROZEN = 3

    @property
    def label(self) -> str:
        return {0: "normal", 1: "warning", 2: "degraded", 3: "frozen"}[int(self)]


class FirebaseBudgetExceeded(RuntimeError):
    """Raised by Firestore/Storage wrappers once the Firebase budget is spent."""


def _month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def _pacific_day(ts: float | None = None) -> str:
    """The calendar day a usage figure counts against for free-tier purposes.

    Firebase's daily free quotas reset at midnight US/Pacific, not UTC. Bucketing
    by the wrong calendar shifts a chunk of every day's usage into its neighbour,
    which matters precisely when usage is near the daily allowance — the moment
    the number has to be right."""
    when = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return when.astimezone(FREE_TIER_TZ).strftime("%Y-%m-%d")


def _month_progress() -> float:
    """Fraction of the current UTC month elapsed, in (0, 1]. Used to prorate the
    at-rest storage charge, which is billed per GB-month."""
    now = datetime.now(timezone.utc)
    days_in_month = monthrange(now.year, now.month)[1]
    elapsed = (now.day - 1) + (now.hour * 3600 + now.minute * 60 + now.second) / 86400
    return max(elapsed / days_in_month, 1e-6)


class _CostGuard:
    def __init__(self) -> None:
        # Reentrant: the public properties call each other (level → firebase_ok →
        # _firebase_usd) and a plain Lock would deadlock on the nesting.
        self._lock = threading.RLock()
        self._month = _month_key()

        # ── Tier 0: what this process has done, per Pacific calendar day.
        # Per-day rather than a running total so the daily free tier can be
        # applied day by day, exactly as Google applies it.
        self._daily: dict[str, dict[str, int]] = {}
        self._gemini_usd = 0.0

        # ── Tier 1: Google's month-to-date figures, and the tier-0 totals at the
        # moment we adopted them. Everything since is added from tier 0.
        self._auth_daily: dict[str, dict[str, int]] = {}
        self._auth_stored_bytes = 0
        self._auth_at = 0.0
        self._auth_error: str | None = None
        self._baseline: dict[str, int] = {}

        # ── Tier 2: the invoice. Display only — never consulted by the ladder.
        self._billed: dict | None = None
        self._billed_error: str | None = None

        self._level = Level.NORMAL
        self._gemini_blocked = False
        self._announced: set[str] = set()
        self._pending_alerts: list[dict] = []
        self._last_persist = 0.0

        # One-shot permission to write after freezing, so the minutes of XP and
        # balance sitting in store's memory buffer reach Firestore instead of
        # dying with the process. Armed on the transition to FROZEN, consumed by
        # the first `final_flush` that follows. See that method.
        self._flush_grace = False
        self._flush_active = threading.local()
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(_STATE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("month") != self._month:
            return  # stale month → start fresh
        self._gemini_usd = float(data.get("gemini_usd", 0.0))
        self._daily = {k: dict(v) for k, v in (data.get("daily") or {}).items()}
        self._auth_daily = {k: dict(v) for k, v in (data.get("auth_daily") or {}).items()}
        self._auth_stored_bytes = int(data.get("auth_stored_bytes", 0))
        self._auth_at = float(data.get("auth_at", 0.0))
        self._baseline = dict(data.get("baseline") or {})
        self._billed = data.get("billed") or None
        self._announced = set(data.get("announced") or [])

        # A pre-ladder state file has flat totals and no per-day breakdown. Fold
        # them into today's bucket rather than discarding them: losing a month's
        # accumulated spend on upgrade would silently re-arm a budget that was
        # nearly gone.
        if not self._daily and any(k in data for k in ("reads", "writes", "deletes")):
            self._daily[_pacific_day()] = {
                "reads": int(data.get("reads", 0)),
                "writes": int(data.get("writes", 0)),
                "deletes": int(data.get("deletes", 0)),
                "egress_bytes": int(data.get("dl_bytes", 0)),
                "ingress_bytes": int(data.get("ul_bytes", 0)),
            }

    def _persist_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_persist) < _PERSIST_INTERVAL:
            return
        self._last_persist = now
        payload = self._state_payload_locked()
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            tmp = _STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, _STATE_PATH)
        except OSError as exc:
            log.warning("cost_guard: could not persist state: %s", exc)

    def _state_payload_locked(self) -> dict:
        return {
            "month": self._month,
            "gemini_usd": round(self._gemini_usd, 6),
            "firebase_usd": round(self._firebase_usd_locked(), 6),
            "daily": self._daily,
            "auth_daily": self._auth_daily,
            "auth_stored_bytes": self._auth_stored_bytes,
            "auth_at": self._auth_at,
            "baseline": self._baseline,
            "billed": self._billed,
            "announced": sorted(self._announced),
            "level": self._level.label,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def flush(self) -> None:
        """Force-write the current tallies (call on shutdown)."""
        with self._lock:
            self._persist_locked(force=True)

    # ── usage bookkeeping ────────────────────────────────────────────────────
    def _bump_locked(self, key: str, amount: int) -> None:
        day = _pacific_day()
        bucket = self._daily.setdefault(day, {})
        bucket[key] = bucket.get(key, 0) + amount

    @staticmethod
    def _sum(daily: dict[str, dict[str, int]], key: str) -> int:
        return sum(day.get(key, 0) for day in daily.values())

    def _effective_daily_locked(self) -> dict[str, dict[str, int]]:
        """Google's numbers where we have them, plus everything since the poll.

        The authoritative snapshot covers the month up to roughly the last poll;
        the local counters have kept running since. `_baseline` is what tier 0
        read at the moment tier 1 was adopted, so the difference is precisely the
        un-audited tail. The few minutes Monitoring lags may therefore be counted
        twice — deliberately, since for a brake the safe rounding direction is
        up, and over a month it is noise.
        """
        if not self._auth_daily:
            return self._daily

        merged = {day: dict(vals) for day, vals in self._auth_daily.items()}
        for key in ("reads", "writes", "deletes", "egress_bytes", "ingress_bytes"):
            tail = self._sum(self._daily, key) - int(self._baseline.get(key, 0))
            if tail <= 0:
                continue
            today = merged.setdefault(_pacific_day(), {})
            today[key] = today.get(key, 0) + tail
        return merged

    # ── derived cost ─────────────────────────────────────────────────────────
    def _priced_lines_locked(self) -> list[dict]:
        """Per-component cost, with the daily free tier applied day by day."""
        daily = self._effective_daily_locked()

        specs = (
            # key,           label,               free/day,                                    unit price, per
            ("reads",        "Firestore reads",   settings.FREE_FIRESTORE_READS_PER_DAY,
             settings.FIRESTORE_READ_USD_PER_100K, 100_000),
            ("writes",       "Firestore writes",  settings.FREE_FIRESTORE_WRITES_PER_DAY,
             settings.FIRESTORE_WRITE_USD_PER_100K, 100_000),
            ("deletes",      "Firestore deletes", settings.FREE_FIRESTORE_DELETES_PER_DAY,
             settings.FIRESTORE_DELETE_USD_PER_100K, 100_000),
            ("egress_bytes", "Storage download",  int(settings.FREE_STORAGE_EGRESS_GB_PER_DAY * _GB),
             settings.STORAGE_DOWNLOAD_USD_PER_GB, _GB),
            ("ingress_bytes", "Storage upload",   0,
             settings.STORAGE_UPLOAD_USD_PER_GB, _GB),
        )

        lines: list[dict] = []
        for key, label, free_per_day, price, per in specs:
            used = 0
            billable = 0
            for day_vals in daily.values():
                amount = int(day_vals.get(key, 0))
                used += amount
                billable += max(0, amount - free_per_day)
            lines.append({
                "label": label,
                "used": used,
                "billable": billable,
                "usd": billable / per * price,
                "bytes": key.endswith("_bytes"),
            })

        # Bytes at rest: a standing level, billed per GB-month. Charge only the
        # part of the month that has actually elapsed, so the brake reacts to
        # spend incurred rather than to spend forecast.
        #
        # This figure (like the egress one) is PROJECT-WIDE, not just the app's
        # Firebase bucket: the Monitoring query reduces across every bucket,
        # because the bill does too — this project also carries four
        # Cloud-Functions buckets. Deliberately not filtered to one bucket, since
        # a brake that under-counts is worse than one that reads slightly high.
        # It also includes soft-deleted objects, which GCS bills for the length
        # of the bucket's soft-delete retention (7 days here) after deletion.
        stored_free = settings.FREE_STORAGE_STORED_GB * _GB
        billable_stored = max(0, self._auth_stored_bytes - stored_free)
        lines.append({
            "label": "Storage at rest (all buckets)",
            "used": self._auth_stored_bytes,
            "billable": billable_stored,
            "usd": billable_stored / _GB * settings.STORAGE_STORED_USD_PER_GB_MONTH * _month_progress(),
            "bytes": True,
            "at_rest": True,
        })
        return lines

    def _firebase_usd_locked(self) -> float:
        return sum(line["usd"] for line in self._priced_lines_locked())

    def _rollover_locked(self) -> None:
        """Reset tallies if the UTC month changed, keeping the closing figures."""
        current = _month_key()
        if current == self._month:
            return

        # Archive before erasing — this is the month-over-month trend that makes
        # a rising bill visible while it is still small.
        try:
            record = self._state_payload_locked()
            record["closed_at"] = datetime.now(timezone.utc).isoformat()
            with open(_HISTORY_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("cost_guard: could not append history: %s", exc)

        log.info("cost_guard: new month %s — resetting spend tallies", current)
        self._month = current
        self._gemini_usd = 0.0
        self._daily = {}
        self._auth_daily = {}
        self._auth_stored_bytes = 0
        self._auth_at = 0.0
        self._baseline = {}
        self._billed = None
        self._billed_error = None
        self._level = Level.NORMAL
        self._gemini_blocked = False
        self._announced = set()
        self._persist_locked(force=True)

    # ── the ladder ───────────────────────────────────────────────────────────
    def _level_locked(self) -> Level:
        if not settings.COST_GUARD_ENABLED:
            return Level.NORMAL
        budget = settings.FIREBASE_MONTHLY_BUDGET_USD
        if budget <= 0:
            return Level.NORMAL  # unlimited
        fraction = self._firebase_usd_locked() / budget
        if fraction >= 1.0:
            return Level.FROZEN
        if fraction >= settings.COST_DEGRADE_FRACTION:
            return Level.DEGRADED
        if fraction >= settings.COST_WARN_FRACTION:
            return Level.WARN
        return Level.NORMAL

    def _refresh_level_locked(self) -> Level:
        """Recompute the level and queue an alert if it moved."""
        self._rollover_locked()
        new = self._level_locked()
        if new == self._level:
            return new

        previous, self._level = self._level, new
        spent = self._firebase_usd_locked()
        budget = settings.FIREBASE_MONTHLY_BUDGET_USD
        if new is Level.FROZEN:
            # Arm the escape hatch before anything can be refused.
            self._flush_grace = True
        if new > previous and new.label not in self._announced:
            self._announced.add(new.label)
            self._pending_alerts.append({
                "kind": "level",
                "level": new.label,
                "previous": previous.label,
                "usd": spent,
                "budget": budget,
                "month": self._month,
            })
            logger = log.error if new is Level.FROZEN else log.warning
            logger("cost_guard: Firebase spend %s → %s ($%.4f / $%.2f)",
                   previous.label, new.label, spent, budget)
        elif new < previous:
            # Recovered (a budget was raised, or Monitoring corrected us down).
            # Re-arm the announcement so the next crossing is reported again.
            self._announced.discard(previous.label)
            log.info("cost_guard: Firebase spend recovered %s → %s ($%.4f / $%.2f)",
                     previous.label, new.label, spent, budget)
        self._persist_locked(force=True)
        return new

    @property
    def level(self) -> Level:
        with self._lock:
            return self._refresh_level_locked()

    # ── status (read by call sites) ──────────────────────────────────────────
    @property
    def gemini_ok(self) -> bool:
        if not settings.COST_GUARD_ENABLED:
            return True
        budget = settings.GEMINI_MONTHLY_BUDGET_USD
        if budget <= 0:
            return True  # unlimited
        with self._lock:
            self._rollover_locked()
            over = self._gemini_usd >= budget
            if over and not self._gemini_blocked:
                self._gemini_blocked = True
                self._pending_alerts.append({
                    "kind": "gemini",
                    "usd": self._gemini_usd,
                    "budget": budget,
                    "month": self._month,
                })
                log.warning(
                    "cost_guard: Gemini budget hit ($%.4f / $%.2f) — AI degraded "
                    "to fallbacks until %s rolls over.",
                    self._gemini_usd, budget, self._month,
                )
            elif not over:
                self._gemini_blocked = False
            return not over

    @property
    def firebase_ok(self) -> bool:
        """False once the hard stop is in force. Kept for call sites that only
        care about the wall; `level` is the finer-grained question."""
        return self.level < Level.FROZEN

    @property
    def degraded(self) -> bool:
        """True once discretionary work should be skipped, before the hard stop."""
        return self.level >= Level.DEGRADED

    @contextmanager
    def final_flush(self):
        """Permit one last write pass after the budget froze.

        A hard stop that simply refuses everything does not only stop new spend —
        it strands whatever `data/store.py` has buffered in memory, because the
        flush is itself a Firestore write. Left that way the freeze quietly
        converts "we stopped spending" into "we lost the last few minutes of
        everyone's XP and balance", and a freeze lasting until the 1st guarantees
        a restart happens first.

        So freezing arms exactly one grace pass. Yields True if this call
        consumed it (the caller is writing under the hatch), False otherwise —
        in which case normal gating applies and the caller behaves as usual.
        """
        with self._lock:
            armed = self._flush_grace
            if armed:
                self._flush_grace = False
                log.warning("cost_guard: frozen — allowing one final flush of "
                            "buffered writes before the stop takes hold.")
        if not armed:
            yield False
            return
        self._flush_active.on = True
        try:
            yield True
        finally:
            self._flush_active.on = False

    def require_firebase(self, upload: bool = False) -> None:
        """Raise if this operation is not permitted at the current level.

        `upload=True` marks the expensive, deferrable half — new bytes into
        Storage. Those are refused one rung early (DEGRADED), which keeps the bot
        wholly usable on a stretched budget: everything reads, downloads and
        persists, and only new craft-file uploads are turned away.
        """
        if getattr(self._flush_active, "on", False):
            return  # inside the post-freeze grace pass
        level = self.level
        if level is Level.FROZEN:
            raise FirebaseBudgetExceeded(
                "Firebase monthly budget exceeded; Firestore and Storage are "
                "paused until the budget resets on the 1st."
            )
        if upload and level is Level.DEGRADED:
            raise FirebaseBudgetExceeded(
                "Firebase spending is close to its monthly budget, so new file "
                "uploads are paused. Everything else still works."
            )

    # ── recording usage (tier 0) ─────────────────────────────────────────────
    def record_gemini(self, usage) -> None:
        """Add the cost of one Gemini call from its response.usage_metadata."""
        if usage is None:
            return
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        cost = (
            in_tok / 1_000_000 * settings.GEMINI_INPUT_USD_PER_1M
            + out_tok / 1_000_000 * settings.GEMINI_OUTPUT_USD_PER_1M
        )
        with self._lock:
            self._rollover_locked()
            self._gemini_usd += cost
            self._persist_locked(force=True)  # rare event — persist immediately

    def note_firestore(self, reads: int = 0, writes: int = 0, deletes: int = 0) -> None:
        if not (reads or writes or deletes):
            return
        with self._lock:
            self._rollover_locked()
            if reads:
                self._bump_locked("reads", reads)
            if writes:
                self._bump_locked("writes", writes)
            if deletes:
                self._bump_locked("deletes", deletes)
            self._persist_locked()

    def note_storage(self, download: int = 0, upload: int = 0) -> None:
        if not (download or upload):
            return
        with self._lock:
            self._rollover_locked()
            if download:
                self._bump_locked("egress_bytes", download)
            if upload:
                self._bump_locked("ingress_bytes", upload)
            self._persist_locked()

    def note_signed_url(self, size_bytes: int = 0) -> None:
        """Record that a direct-download URL was handed out.

        A signed or public URL is fetched by the client straight from GCS, so
        those bytes leave the bucket without ever passing through this process.
        Only the *count* is recorded, and it is never priced: the object's size
        is not known here without a metadata round-trip that would itself cost an
        operation, and a guessed byte figure would corrupt the estimate this
        exists to protect. Cloud Monitoring reports the real egress on the next
        poll — this counter is what makes the gap between the two legible, and
        `size_bytes` is accepted for the few callers that genuinely know it.
        """
        with self._lock:
            self._rollover_locked()
            self._bump_locked("signed_urls", 1)
            if size_bytes > 0:
                self._bump_locked("egress_bytes", int(size_bytes))
            self._persist_locked()

    # ── ingesting the truth (tier 1) ─────────────────────────────────────────
    def ingest_usage(self, snap) -> None:
        """Adopt a Cloud Monitoring snapshot as the new authoritative baseline."""
        with self._lock:
            self._rollover_locked()
            if not getattr(snap, "ok", False):
                self._auth_error = getattr(snap, "error", "unknown")
                return

            self._auth_error = None
            self._auth_at = getattr(snap, "fetched_at", time.time())
            self._auth_stored_bytes = int(getattr(snap, "stored_bytes", 0) or 0)
            self._auth_daily = {
                day: {
                    "reads": int(vals.get("reads", 0)),
                    "writes": int(vals.get("writes", 0)),
                    "deletes": int(vals.get("deletes", 0)),
                    "egress_bytes": int(vals.get("egress_bytes", 0)),
                }
                for day, vals in (getattr(snap, "daily", None) or {}).items()
            }
            # Uploads: Monitoring's received_bytes_count is not collected here
            # (ingress is free on every tier we price), so the local figure is
            # carried through rather than being zeroed by the adoption.
            for day, vals in self._daily.items():
                if vals.get("ingress_bytes"):
                    self._auth_daily.setdefault(day, {})["ingress_bytes"] = vals["ingress_bytes"]

            # Freeze tier 0's totals: everything counted after this point is the
            # un-audited tail added on top of Google's figures.
            self._baseline = {
                key: self._sum(self._daily, key)
                for key in ("reads", "writes", "deletes", "egress_bytes", "ingress_bytes")
            }
            self._refresh_level_locked()
            self._persist_locked(force=True)

    def note_metrics_error(self, error: str | None) -> None:
        with self._lock:
            self._auth_error = error

    # ── the receipt (tier 2) ─────────────────────────────────────────────────
    def ingest_billing(self, snap) -> None:
        """Record what Google actually charged. DISPLAY ONLY.

        Deliberately kept out of `_level_locked` and out of `_priced_lines_locked`.
        The billing export lands a few times a day, so a brake driven by it would
        let a runaway spend for a whole export cycle before it could see the
        problem — the exact failure the instant tier-0 meter exists to prevent.
        Its value is being *right*: free-tier allowances arrive as negative
        credits, so this is the invoice rather than a model of one, and the gap
        between it and our estimate is the error in our own price constants.
        """
        with self._lock:
            self._rollover_locked()
            if not getattr(snap, "ok", False):
                self._billed_error = getattr(snap, "error", "unknown")
                return
            self._billed_error = None
            self._billed = {
                "invoice_month": snap.invoice_month,
                "currency": snap.currency,
                "total_usd": snap.total_usd,
                "gross_usd": snap.gross_usd,
                "credits_usd": snap.credits_usd,
                "services": snap.services,
                "table": snap.table,
                "fetched_at": snap.fetched_at,
                "bytes_processed": snap.bytes_processed,
            }
            self._persist_locked(force=True)

    def note_billing_error(self, error: str | None) -> None:
        with self._lock:
            self._billed_error = error

    # ── alerts ───────────────────────────────────────────────────────────────
    def drain_alerts(self) -> list[dict]:
        """Take the queued threshold crossings. Queued rather than dispatched
        because a level can change on any thread inside a hot Firestore call,
        which is no place to be touching Discord's event loop."""
        with self._lock:
            alerts, self._pending_alerts = self._pending_alerts, []
            return alerts

    # ── reporting ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Everything the admin console and /costs render."""
        with self._lock:
            level = self._refresh_level_locked()
            lines = self._priced_lines_locked()
            firebase_usd = sum(line["usd"] for line in lines)
            g_budget = settings.GEMINI_MONTHLY_BUDGET_USD
            f_budget = settings.FIREBASE_MONTHLY_BUDGET_USD

            # Drift: how much of the real usage the in-process meter misses.
            # Mostly signed-URL egress, so a large number here is informative
            # rather than alarming — it is the blind spot being measured.
            local_total = {k: self._sum(self._daily, k)
                           for k in ("reads", "writes", "deletes", "egress_bytes")}
            auth_total = {k: self._sum(self._auth_daily, k)
                          for k in ("reads", "writes", "deletes", "egress_bytes")}

            stored_gb = self._auth_stored_bytes / _GB
            return {
                "enabled": settings.COST_GUARD_ENABLED,
                "month": self._month,
                "level": level.label,
                "thresholds": {
                    "warn": settings.COST_WARN_FRACTION,
                    "degrade": settings.COST_DEGRADE_FRACTION,
                },
                "gemini": {
                    "usd": self._gemini_usd,
                    "budget": g_budget,
                    "ok": self._gemini_usd < g_budget if g_budget > 0 else True,
                    "unlimited": g_budget <= 0,
                },
                "firebase": {
                    "usd": firebase_usd,
                    "budget": f_budget,
                    "ok": level < Level.FROZEN,
                    "unlimited": f_budget <= 0,
                    "fraction": (firebase_usd / f_budget) if f_budget > 0 else 0.0,
                    "lines": lines,
                },
                "storage": {
                    "stored_bytes": self._auth_stored_bytes,
                    "stored_gb": stored_gb,
                    "free_gb": settings.FREE_STORAGE_STORED_GB,
                    # The full-month charge this standing total implies, as
                    # opposed to the prorated part already incurred. Storage at
                    # rest is the one line no brake can stop.
                    "projected_month_usd": max(0.0, stored_gb - settings.FREE_STORAGE_STORED_GB)
                    * settings.STORAGE_STORED_USD_PER_GB_MONTH,
                },
                "metrics": {
                    "enabled": settings.COST_METRICS_ENABLED,
                    "ok": bool(self._auth_daily) and self._auth_error is None,
                    "error": self._auth_error,
                    "fetched_at": self._auth_at,
                    "authoritative": auth_total,
                    "local": local_total,
                    "drift": {
                        k: auth_total[k] - local_total[k] for k in auth_total
                    },
                    # Direct-download URLs handed out this month. Their bytes are
                    # in the authoritative egress figure and in none of the local
                    # one, so this is the count that explains the egress drift.
                    "signed_urls": self._sum(self._daily, "signed_urls"),
                },
                # Tier 2: what Google billed. Reported beside our estimate, never
                # folded into it — see ingest_billing.
                "billed": {
                    "enabled": settings.COST_BILLING_ENABLED,
                    "ok": self._billed is not None and self._billed_error is None,
                    "error": self._billed_error,
                    "dataset": settings.COST_BILLING_DATASET,
                    **(self._billed or {}),
                },
            }

    def history(self, limit: int = 12) -> list[dict]:
        """Closing figures for previous months, newest first."""
        try:
            with open(_HISTORY_PATH, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            return []
        return rows[-limit:][::-1]


guard = _CostGuard()
