"""
data/gcp_billing.py – Tier 2: what Google actually charged.

The third and last source in the cost stack, and the only one denominated in
money rather than usage:

  • TIER 0 (cost_guard's own counters) — instant, an estimate, blind to anything
    that doesn't pass through this process. Drives the brake.
  • TIER 1 (data/gcp_metrics.py) — Cloud Monitoring. Google's usage figures, a
    few minutes behind. Corrects tier 0 and closes its blind spots.
  • TIER 2 (this file) — the BigQuery billing export. Actual billed dollars,
    net of free-tier credits, hours behind.

WHY THIS IS NOT ALLOWED TO DRIVE THE BRAKE
The export lands a few times a day. A brake fed by it would let a runaway spend
for a full export cycle before it could even see the problem — which is the
whole reason tiers 0 and 1 exist. So `ingest_billing` deliberately writes to a
display-only field: `cost_guard._billed`, which `snapshot()` reports and
`_level_locked()` never reads. Tier 2 is the receipt, not the trigger.

What it is uniquely good for is being *right*. Free-tier allowances arrive as
negative `credits` rows, so `cost + credits` is the true bill — no modelling of
daily quotas, no price constants, no guessing at regional rates. Where the
Costs tab shows a tier-1 estimate next to a tier-2 figure, the gap between them
is the error in our own pricing table, which is worth seeing.

SETUP (both are one-time, and until they exist this degrades to `ok=False`)
  1. Billing → Billing export → BigQuery export → Standard usage cost, pointed
     at a dataset. Data starts accruing from that moment; it never backfills.
  2. The service account needs `roles/bigquery.dataViewer` on the dataset and
     `roles/bigquery.jobUser` on the project. The Firebase key has neither by
     default — the Monitoring grant does not cover BigQuery.

Like data/gcp_metrics.py this speaks REST via google-auth + aiohttp rather than
pulling in `google-cloud-bigquery`, so it adds nothing to requirements.txt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

import settings
from config import cfg

log = logging.getLogger(__name__)

_API_ROOT = "https://bigquery.googleapis.com/bigquery/v2"
_SCOPES = ["https://www.googleapis.com/auth/bigquery.readonly"]

# The export creates one table per billing account, named for it with the dashes
# turned into underscores. Discovered rather than configured: the id is already
# knowable from the dataset, and one less thing in .env to get wrong.
_TABLE_RE = re.compile(r"^gcp_billing_export_v1_[A-Z0-9_]+$")

# A table identifier cannot be a query parameter, so it is interpolated — and
# therefore has to be proven safe first. Only ever built from names BigQuery
# itself handed us, but validated anyway: an interpolated identifier that is
# merely *probably* safe is how injection gets in.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class BillingSnapshot:
    """Month-to-date charges as Google billed them."""

    ok: bool = False
    error: str | None = None
    fetched_at: float = 0.0
    invoice_month: str = ""
    currency: str = "USD"
    # Net of credits — i.e. the number that lands on the invoice.
    total_usd: float = 0.0
    gross_usd: float = 0.0
    credits_usd: float = 0.0
    # Per-service net cost, largest first.
    services: list[dict] = field(default_factory=list)
    table: str | None = None
    bytes_processed: int = 0


class _BillingClient:
    """Queries the billing export. Never raises outward."""

    def __init__(self) -> None:
        self._creds = None
        self._project: str | None = None
        self._table: str | None = None
        self._location: str | None = None
        self._disabled_reason: str | None = None

    # ── credentials ──────────────────────────────────────────────────────────
    def _load_creds_blocking(self):
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

    # ── discovery ────────────────────────────────────────────────────────────
    async def _resolve_table(self, session: aiohttp.ClientSession, token: str,
                             project: str, dataset: str) -> tuple[str, str]:
        """Find the export table and the dataset's location.

        The location is not optional trivia: a query job runs in the region of
        the data it reads, and BigQuery rejects the job outright if the two
        disagree. This dataset can be anywhere the owner picked, so it is read
        rather than assumed.
        """
        if self._table and self._location:
            return self._table, self._location

        headers = {"Authorization": f"Bearer {token}"}
        meta_url = f"{_API_ROOT}/projects/{project}/datasets/{dataset}"
        async with session.get(meta_url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    (body.get("error", {}).get("message") or f"HTTP {resp.status}")[:300])
            location = body.get("location") or "US"

        async with session.get(f"{meta_url}/tables", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    (body.get("error", {}).get("message") or f"HTTP {resp.status}")[:300])

        names = [t.get("tableReference", {}).get("tableId", "") for t in body.get("tables", [])]
        matches = [n for n in names if _TABLE_RE.match(n)]
        if not matches:
            # Standard state for the first few hours after enabling the export.
            raise RuntimeError(
                "no gcp_billing_export_v1_* table in the dataset yet — the export "
                "was probably enabled recently; the first load takes a few hours "
                f"(saw: {', '.join(names) or 'no tables at all'})"
            )
        # More than one billing account exporting here would be unusual; take the
        # first by name so repeated polls at least stay consistent.
        self._table, self._location = sorted(matches)[0], location
        return self._table, self._location

    # ── the query ────────────────────────────────────────────────────────────
    async def fetch(self) -> BillingSnapshot:
        snap = BillingSnapshot(fetched_at=time.time())

        if self._disabled_reason:
            snap.error = self._disabled_reason
            return snap

        dataset = settings.COST_BILLING_DATASET
        if not dataset:
            snap.error = "COST_BILLING_DATASET is not set"
            return snap
        if not _IDENT_RE.match(dataset):
            snap.error = f"COST_BILLING_DATASET is not a valid dataset id: {dataset!r}"
            return snap

        try:
            token, project = await asyncio.to_thread(self._load_creds_blocking)
        except Exception as exc:
            snap.error = f"credentials unavailable: {exc}"
            return snap
        if not project:
            snap.error = "service account JSON has no project_id"
            return snap

        month = datetime.now(timezone.utc).strftime("%Y%m")
        snap.invoice_month = month

        try:
            async with aiohttp.ClientSession() as session:
                table, location = await self._resolve_table(session, token, project, dataset)
                snap.table = table
                if not _IDENT_RE.match(table):  # belt and braces before interpolation
                    raise RuntimeError(f"refusing to query unsafe table id {table!r}")

                rows, bytes_processed, currency = await self._run_query(
                    session, token, project, dataset, table, location, month)
        except Exception as exc:
            msg = str(exc)
            if "Access Denied" in msg or "PERMISSION_DENIED" in msg or "403" in msg:
                self._disabled_reason = (
                    "BigQuery denied access — grant the service account "
                    "roles/bigquery.dataViewer on the dataset and "
                    "roles/bigquery.jobUser on the project"
                )
                snap.error = self._disabled_reason
                log.error("gcp_billing: %s", self._disabled_reason)
            else:
                snap.error = msg
            return snap

        snap.bytes_processed = bytes_processed
        snap.currency = currency or "USD"
        for row in rows:
            snap.services.append(row)
            snap.gross_usd += row["gross"]
            snap.credits_usd += row["credits"]
            snap.total_usd += row["net"]
        snap.services.sort(key=lambda r: r["net"], reverse=True)
        snap.ok = True
        return snap

    async def _run_query(self, session: aiohttp.ClientSession, token: str, project: str,
                         dataset: str, table: str, location: str,
                         month: str) -> tuple[list[dict], int, str]:
        """Month-to-date net cost per service.

        `invoice.month` is the canonical way to scope a billing month, and it is
        used instead of a `_PARTITIONTIME` predicate on purpose. Partition
        pruning would save nothing here — every query on a table this small bills
        the 10 MB floor regardless — while a `_PARTITIONTIME` filter fails the
        whole query outright on a table that isn't ingestion-partitioned. The
        seatbelt that actually matters is `maximumBytesBilled` below, which caps
        the damage a wrong predicate can do rather than trusting it not to be
        wrong.

        Credits carry the free tier as negative amounts, so `cost + credits` is
        the real invoice line — that is the whole reason this tier is worth
        having next to our own modelled estimate.
        """
        sql = f"""
            SELECT
              service.description AS service,
              SUM(cost) AS gross,
              SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
              ANY_VALUE(currency) AS currency
            FROM `{project}.{dataset}.{table}`
            WHERE invoice.month = @month
            GROUP BY service
        """
        payload = {
            "query": sql,
            "useLegacySql": False,
            "parameterMode": "NAMED",
            "queryParameters": [{
                "name": "month",
                "parameterType": {"type": "STRING"},
                "parameterValue": {"value": month},
            }],
            # Hard ceiling: the job is rejected rather than run if it would scan
            # more than this. A billing export for one project is megabytes, so
            # anything approaching the cap means the query is wrong.
            "maximumBytesBilled": str(int(settings.COST_BILLING_MAX_BYTES)),
            "timeoutMs": 30_000,
            "location": location,
        }
        url = f"{_API_ROOT}/projects/{project}/queries"
        async with session.post(url, json=payload,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    (body.get("error", {}).get("message") or f"HTTP {resp.status}")[:300])

        if not body.get("jobComplete"):
            # Tiny table, 30s budget — this should not happen, and inventing a
            # polling loop for a case that means something is badly wrong would
            # only hide it.
            raise RuntimeError("query did not complete within the timeout")

        bytes_processed = int(body.get("totalBytesProcessed") or 0)
        fields = [f["name"] for f in body.get("schema", {}).get("fields", [])]
        rows: list[dict] = []
        currency = "USD"
        for row in body.get("rows", []):
            values = dict(zip(fields, (c.get("v") for c in row.get("f", []))))
            gross = float(values.get("gross") or 0.0)
            credits = float(values.get("credits") or 0.0)
            currency = values.get("currency") or currency
            rows.append({
                "service": values.get("service") or "(unknown)",
                "gross": gross,
                "credits": credits,
                "net": gross + credits,
            })
        return rows, bytes_processed, currency

    def reset_disabled(self) -> None:
        """Clear a sticky permission failure and re-discover the table, so a
        fresh IAM grant (or the export's first load) is picked up without a
        restart — the admin console's retry button."""
        self._disabled_reason = None
        self._table = None
        self._location = None


client = _BillingClient()


async def fetch_billing() -> BillingSnapshot:
    """Month-to-date billed cost. Never raises."""
    if not settings.COST_BILLING_ENABLED:
        return BillingSnapshot(error="BigQuery billing export polling is disabled in settings")
    try:
        return await client.fetch()
    except Exception as exc:  # pragma: no cover - belt and braces
        log.warning("gcp_billing: unexpected failure: %s", exc)
        return BillingSnapshot(error=str(exc))
