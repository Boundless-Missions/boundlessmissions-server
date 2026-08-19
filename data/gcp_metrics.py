"""
data/gcp_metrics.py – Authoritative usage figures from Cloud Monitoring.

cost_guard.py meters what the bot *does* — every Firestore call and every byte
uploaded through data/store.py. That estimate is instant, which is what makes it
usable as a breaker, but it is blind to three things by construction:

  • Egress from signed / public URLs. `store.signed_url` hands the client a link
    straight to GCS; the download is billed to us and never touches this process.
    That is the single largest and fastest-growing line item on the bill.
  • Bytes at rest. Storage is billed per GB-month on the total stored, which only
    ever goes up and which no amount of counting operations can reveal.
  • Anything that isn't the bot — scripts/backfill_*, the Firebase console, a
    second instance sharing the project.

Cloud Monitoring sees all of it, because it is metered by Google on their side.
It lags a few minutes rather than being instant, which is exactly the wrong
property for a breaker and exactly the right one for the truth. So this module
does not replace the local meter: it corrects it (see `cost_guard.ingest_usage`).

WHY RAW REST AND NOT google-cloud-monitoring
The official client would be a heavyweight new dependency, and a synchronous one
that every call would have to hop onto a thread. The Monitoring read API is a
single authenticated GET, and both halves of that are already installed:
`google-auth` (pulled in by firebase-admin) mints the token, `aiohttp` (already
a bot dependency) makes the call. So this file adds nothing to requirements.txt.

IAM
The service account in FIREBASE_CREDENTIALS needs `roles/monitoring.viewer` on
the project — Firebase does not grant it, so this is an explicit one-time grant
and until it happens every call here 403s. That is handled as a normal outcome
rather than an error: `UsageSnapshot.ok` is False, the reason is carried in
`.error`, and the cost guard keeps running on its local estimate alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

import settings
from config import cfg
from cost_guard import FREE_TIER_TZ

log = logging.getLogger(__name__)

_API_ROOT = "https://monitoring.googleapis.com/v3"
_SCOPES = ["https://www.googleapis.com/auth/monitoring.read"]

# Hourly points, bucketed into calendar days on our side. Asking Monitoring for
# daily buckets directly would align them to *now* rather than to midnight, and
# the daily free tier is a calendar-day allowance — a rolling 24h window would
# quietly mis-state it every single day.
_ALIGNMENT = "3600s"

# The metrics we bill on. `kind` picks how points combine: DELTA counters are
# summed over the window, GAUGE levels are a standing value where only the most
# recent reading means anything.
_METRICS: dict[str, tuple[str, str]] = {
    # key:              (metric.type,                                        kind)
    "reads":            ("firestore.googleapis.com/document/read_count",     "delta"),
    "writes":           ("firestore.googleapis.com/document/write_count",    "delta"),
    "deletes":          ("firestore.googleapis.com/document/delete_count",   "delta"),
    "egress_bytes":     ("storage.googleapis.com/network/sent_bytes_count",  "delta"),
    "stored_bytes":     ("storage.googleapis.com/storage/total_bytes",       "gauge"),
}


@dataclass
class UsageSnapshot:
    """Month-to-date usage as Google measured it.

    `ok` False means we have no authoritative figures right now (no credentials,
    no IAM grant, API down). Callers must treat that as "unknown", never as zero
    — a metering failure that reads as zero usage is a killswitch that never
    fires, which is the one failure mode worth designing against here.
    """

    ok: bool = False
    error: str | None = None
    fetched_at: float = 0.0
    # Month-to-date totals (UTC month, matching the billing period).
    reads: int = 0
    writes: int = 0
    deletes: int = 0
    egress_bytes: int = 0
    # Standing value, not a total: the most recent reading of bytes at rest.
    stored_bytes: int = 0
    # Per-Pacific-calendar-day totals, so the daily free tier can be applied
    # day by day instead of being smeared across the month. {"2026-08-18": {...}}
    daily: dict[str, dict[str, int]] = field(default_factory=dict)
    # Which metrics actually came back — a partial answer is still useful, but
    # the caller has to know which numbers are real.
    present: set[str] = field(default_factory=set)


class _MetricsClient:
    """Reads Cloud Monitoring for the Firebase project. Never raises outward."""

    def __init__(self) -> None:
        self._creds = None
        self._project: str | None = None
        self._lock = asyncio.Lock()
        self._disabled_reason: str | None = None

    # ── credentials ──────────────────────────────────────────────────────────
    def _load_creds_blocking(self):
        """Build (and lazily refresh) a token from the Firebase service account.

        google-auth's refresh is a blocking HTTPS call, so every caller of this
        goes through asyncio.to_thread. The credentials object caches the token
        and only actually hits the network when it has expired.
        """
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(
                cfg.FIREBASE_CREDENTIALS, scopes=_SCOPES,
            )
            self._project = self._creds.project_id
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token, self._project

    # ── one metric ───────────────────────────────────────────────────────────
    async def _series(self, session: aiohttp.ClientSession, token: str, project: str,
                      metric_type: str, kind: str,
                      start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        """Fetch one metric as (bucket_end, value) points across the window.

        Aggregation is done server-side across resources (REDUCE_SUM) so a
        project with several buckets or databases comes back as one series;
        the per-hour points are still separate so we can bucket them by day.
        """
        params = {
            "filter": f'metric.type="{metric_type}"',
            "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "aggregation.alignmentPeriod": _ALIGNMENT,
            # A DELTA counter sums within the bucket; a GAUGE is a level, and
            # summing levels across time would be meaningless, so it is averaged.
            "aggregation.perSeriesAligner": "ALIGN_SUM" if kind == "delta" else "ALIGN_MEAN",
            "aggregation.crossSeriesReducer": "REDUCE_SUM",
            "view": "FULL",
        }
        points: list[tuple[datetime, float]] = []
        url = f"{_API_ROOT}/projects/{project}/timeSeries"
        page_token: str | None = None

        # Paginate defensively. At hourly resolution a month is ~744 points and
        # fits one page, but a longer window or a multi-resource project can
        # split — and a silently truncated read would under-report usage.
        for _ in range(10):
            q = dict(params)
            if page_token:
                q["pageToken"] = page_token
            async with session.get(url, params=q,
                                   headers={"Authorization": f"Bearer {token}"},
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise RuntimeError(f"HTTP {resp.status}: {body}")
                data = await resp.json()

            for series in data.get("timeSeries", []):
                for pt in series.get("points", []):
                    value = pt.get("value", {})
                    raw = value.get("int64Value", value.get("doubleValue", 0))
                    ts = pt.get("interval", {}).get("endTime", "")
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    points.append((when, float(raw or 0)))

            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return points

    # ── the snapshot ─────────────────────────────────────────────────────────
    async def fetch(self) -> UsageSnapshot:
        """Month-to-date usage, or an `ok=False` snapshot explaining why not."""
        snap = UsageSnapshot(fetched_at=time.time())

        if self._disabled_reason:
            snap.error = self._disabled_reason
            return snap

        try:
            token, project = await asyncio.to_thread(self._load_creds_blocking)
        except Exception as exc:
            snap.error = f"credentials unavailable: {exc}"
            return snap
        if not project:
            snap.error = "service account JSON has no project_id"
            return snap

        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        try:
            async with aiohttp.ClientSession() as session:
                for key, (metric_type, kind) in _METRICS.items():
                    try:
                        points = await self._series(session, token, project,
                                                    metric_type, kind, start, now)
                    except Exception as exc:
                        # One missing metric must not sink the whole snapshot:
                        # a project that has never used Storage simply has no
                        # Storage series, and that is not an error worth losing
                        # the Firestore numbers over.
                        msg = str(exc)
                        if "403" in msg or "PERMISSION_DENIED" in msg:
                            # This one *is* fatal and worth saying once, loudly.
                            self._disabled_reason = (
                                "Cloud Monitoring denied access — grant "
                                "roles/monitoring.viewer to the service account "
                                f"in {cfg.FIREBASE_CREDENTIALS}"
                            )
                            snap.error = self._disabled_reason
                            log.error("gcp_metrics: %s", self._disabled_reason)
                            return snap
                        log.debug("gcp_metrics: %s unavailable (%s)", key, msg)
                        continue

                    snap.present.add(key)
                    if kind == "gauge":
                        # A level: only the newest reading means anything.
                        if points:
                            snap.stored_bytes = int(max(points, key=lambda p: p[0])[1])
                        continue

                    total = 0
                    for when, value in points:
                        total += int(value)
                        day = when.astimezone(FREE_TIER_TZ).strftime("%Y-%m-%d")
                        snap.daily.setdefault(day, {})
                        snap.daily[day][key] = snap.daily[day].get(key, 0) + int(value)
                    setattr(snap, key, total)
        except Exception as exc:
            snap.error = f"monitoring query failed: {exc}"
            return snap

        if not snap.present:
            snap.error = "no metrics returned (project may have no billable usage yet)"
            return snap

        snap.ok = True
        return snap

    def reset_disabled(self) -> None:
        """Clear a sticky permission failure so a fresh IAM grant is picked up
        without restarting the bot (the admin console's 'retry' button)."""
        self._disabled_reason = None


client = _MetricsClient()


async def fetch_usage() -> UsageSnapshot:
    """Month-to-date usage from Cloud Monitoring. Never raises."""
    if not settings.COST_METRICS_ENABLED:
        return UsageSnapshot(error="Cloud Monitoring polling is disabled in settings")
    try:
        return await client.fetch()
    except Exception as exc:  # pragma: no cover - belt and braces
        log.warning("gcp_metrics: unexpected failure: %s", exc)
        return UsageSnapshot(error=str(exc))
