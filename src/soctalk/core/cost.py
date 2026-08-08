"""Shared tenant-level cost-cap accounting.

Both the worker-claim path (``worker_runs.claim_run``) and the chat
turn handler (``core/api/chat.messages_post``) need to refuse new work
once the tenant has blown through its rolling 24h spend ceiling. The
query unions two cost sources:

* ``investigation_runs`` — LLM spend incurred by the worker on the
  supervisor/verdict loop. Windowed by ``COALESCE(ended_at,
  lease_expires_at, claimed_at, started_at)`` so long-running active
  runs (whose heartbeat refreshes ``lease_expires_at``) stay in window.
* ``chat_messages`` — LLM spend incurred by the chat agent. Windowed
  by ``created_at`` (point-in-time; no lifecycle columns).

A single source of truth means a busy chat session can't blow past the
runs-worker's cap and a flood of runs can't blow past the chat
handler's cap.

Defaults pull from env so operators can tune per cluster without a
schema change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# When "today" starts
# ---------------------------------------------------------------------------
#
# The ceilings are CALENDAR-DAY, not a sliding 24h sum. A rolling window never
# fully clears -- each charge ages out on its own anniversary -- so a tenant
# that blew its ceiling at 14:00 is still partly blocked at 13:00 the next day,
# and there is no moment an operator can point at and say "it resets then".
# A calendar day resets in one step at local midnight, which is what "daily
# budget" means to the person paying it.
#
# The zone matters for an MSSP whose customers span regions: midnight has to be
# the customer's midnight, not the cluster's. Install default via env, per
# tenant via the same policy mechanism as the ceilings themselves.

BUDGET_DAY_TZ_KEY = "budget_day_timezone"


def install_budget_day_timezone() -> str:
    """Install default zone for the day boundary. UTC unless configured."""
    raw = (os.getenv("SOCTALK_BUDGET_DAY_TIMEZONE") or "").strip()
    return raw if _is_valid_timezone(raw) else "UTC"


def _is_valid_timezone(name: str) -> bool:
    """True when Postgres would accept this zone name.

    Checked in Python rather than by asking the database, because an invalid
    zone makes the spend query itself raise -- and a cap that errors is a cap
    that stops all triage.
    """
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


async def resolve_budget_day_timezone(db: AsyncSession, tenant_id: UUID) -> str:
    """The zone whose midnight resets this tenant's daily ceilings."""
    default = install_budget_day_timezone()
    try:
        from soctalk.core.ir.policies import effective_policy

        eff = await effective_policy(db, tenant_id)
    except Exception:  # noqa: BLE001
        return default
    raw = eff.get(BUDGET_DAY_TZ_KEY)
    if raw is None:
        return default
    if not isinstance(raw, str) or not _is_valid_timezone(raw.strip()):
        logger.warning(
            "budget_day_timezone_invalid", tenant_id=str(tenant_id), value=str(raw)[:40]
        )
        return default
    return raw.strip()


def day_window(tz_name: str) -> tuple[datetime, datetime]:
    """(start, end) of the current calendar day in ``tz_name``, as UTC instants.

    ``end`` is when the ceiling next resets, which is the thing an operator
    staring at a blocked queue actually wants to know.
    """
    zone = ZoneInfo(tz_name) if _is_valid_timezone(tz_name) else ZoneInfo("UTC")
    local_now = datetime.now(zone)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Add a day on the LOCAL clock, so a DST transition shortens or lengthens
    # the day rather than landing an hour off.
    next_local = (start_local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start_local.astimezone(UTC), next_local.astimezone(UTC)


def tenant_daily_token_cap() -> int:
    """Per-tenant rolling 24h token ceiling. Default 10M."""
    raw = os.getenv("SOCTALK_TENANT_DAILY_TOKEN_CAP", "")
    try:
        v = int(raw) if raw else 10_000_000
    except ValueError:
        v = 10_000_000
    return v if v > 0 else 10_000_000


def tenant_daily_dollar_cap() -> float:
    """Per-tenant rolling 24h dollar ceiling. Default $50."""
    raw = os.getenv("SOCTALK_TENANT_DAILY_DOLLAR_CAP", "")
    try:
        v = float(raw) if raw else 50.0
    except ValueError:
        v = 50.0
    return v if v > 0 else 50.0


@dataclass(frozen=True, slots=True)
class TenantDailySpend:
    tokens: int
    dollars: float

    @property
    def token_cap_hit(self) -> bool:
        return self.tokens >= tenant_daily_token_cap()

    @property
    def dollar_cap_hit(self) -> bool:
        return self.dollars >= tenant_daily_dollar_cap()

    @property
    def cap_hit(self) -> bool:
        return self.token_cap_hit or self.dollar_cap_hit


_DAILY_SPEND_SQL = """
    SELECT COALESCE(SUM(s.tokens), 0)::bigint AS tokens,
           COALESCE(SUM(s.dollars), 0)::float AS dollars
    FROM (
        SELECT tokens_used::bigint AS tokens,
               dollars_used        AS dollars
          FROM investigation_runs
         WHERE tenant_id = :t
           AND COALESCE(ended_at, lease_expires_at, claimed_at, started_at)
               >= (date_trunc('day', now() AT TIME ZONE :tz) AT TIME ZONE :tz)
        UNION ALL
        SELECT (COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0))::bigint AS tokens,
               COALESCE(dollars, 0.0)                                     AS dollars
          FROM chat_messages
         WHERE tenant_id = :t
           AND created_at >= (date_trunc('day', now() AT TIME ZONE :tz) AT TIME ZONE :tz)
    ) s
"""


async def get_tenant_daily_spend(
    db: AsyncSession, tenant_id: UUID
) -> TenantDailySpend:
    """Run the unified daily-cap query against the given session.

    Caller chooses the session — usually the request-bound session that
    already has the right RLS scope. ``tenant_id`` is the data column
    filter; RLS still applies on top, so tenant-scoped sessions only
    sum their own tenant's rows even if a caller passed a different
    UUID (defence in depth).
    """
    row = (
        await db.execute(
            text(_DAILY_SPEND_SQL),
            {"t": str(tenant_id), "tz": await resolve_budget_day_timezone(db, tenant_id)},
        )
    ).mappings().first()
    if row is None:
        return TenantDailySpend(tokens=0, dollars=0.0)
    return TenantDailySpend(
        tokens=int(row["tokens"] or 0),
        dollars=float(row["dollars"] or 0.0),
    )


async def assert_tenant_daily_cap_ok(
    db: AsyncSession, tenant_id: UUID, *, source: str
) -> TenantDailySpend | None:
    """Return the spend snapshot if under cap, ``None`` if over.

    Caller decides what to do on ``None`` (worker returns 200/null;
    chat returns 429). ``source`` is logged so we can tell which path
    tripped the breaker.
    """
    status = await get_tenant_daily_status(db, tenant_id)
    if status.cap_hit:
        logger.warning(
            "tenant_daily_cap_hit",
            source=source,
            tenant_id=str(tenant_id),
            tokens_24h=status.spend.tokens,
            token_cap=status.caps.tokens,
            dollars_24h=round(status.spend.dollars, 4),
            dollar_cap=status.caps.dollars,
            reason=status.reason,
        )
        return None
    return status.spend


# ---------------------------------------------------------------------------
# MSSP-user-per-day cap (fleet-scope conversations)
# ---------------------------------------------------------------------------
#
# Fleet-scope conversations have ``chat_messages.tenant_id IS NULL`` and
# therefore fall out of the tenant cap entirely. We add a parallel cap
# bound to the MSSP user so a busy fleet session can't be a budget
# side-door. Window + units mirror the tenant cap.


def mssp_user_daily_token_cap() -> int:
    raw = os.getenv("SOCTALK_MSSP_USER_DAILY_TOKEN_CAP", "")
    try:
        v = int(raw) if raw else 10_000_000
    except ValueError:
        v = 10_000_000
    return v if v > 0 else 10_000_000


def mssp_user_daily_dollar_cap() -> float:
    raw = os.getenv("SOCTALK_MSSP_USER_DAILY_DOLLAR_CAP", "")
    try:
        v = float(raw) if raw else 50.0
    except ValueError:
        v = 50.0
    return v if v > 0 else 50.0


@dataclass(frozen=True, slots=True)
class MsspUserDailySpend:
    tokens: int
    dollars: float

    @property
    def token_cap_hit(self) -> bool:
        return self.tokens >= mssp_user_daily_token_cap()

    @property
    def dollar_cap_hit(self) -> bool:
        return self.dollars >= mssp_user_daily_dollar_cap()

    @property
    def cap_hit(self) -> bool:
        return self.token_cap_hit or self.dollar_cap_hit


_MSSP_USER_DAILY_SPEND_SQL = """
    SELECT COALESCE(SUM((COALESCE(m.tokens_in, 0)
                       + COALESCE(m.tokens_out, 0))::bigint), 0)::bigint AS tokens,
           COALESCE(SUM(COALESCE(m.dollars, 0.0))::float, 0.0)::float    AS dollars
      FROM chat_messages m
      JOIN conversations c ON c.id = m.conversation_id
     WHERE c.scope = 'mssp_fleet'
       AND c.created_by_user_id = :u
       AND m.created_at >= (date_trunc('day', now() AT TIME ZONE :tz) AT TIME ZONE :tz)
"""


async def get_mssp_user_daily_spend(
    db: AsyncSession, user_id: UUID
) -> MsspUserDailySpend:
    row = (
        await db.execute(
            text(_MSSP_USER_DAILY_SPEND_SQL),
            # The fleet-scope cap is not tenant-scoped, so it uses the install
            # zone; there is no single customer whose midnight would apply.
            {"u": str(user_id), "tz": install_budget_day_timezone()},
        )
    ).mappings().first()
    if row is None:
        return MsspUserDailySpend(tokens=0, dollars=0.0)
    return MsspUserDailySpend(
        tokens=int(row["tokens"] or 0),
        dollars=float(row["dollars"] or 0.0),
    )


async def assert_mssp_user_daily_cap_ok(
    db: AsyncSession, user_id: UUID, *, source: str
) -> MsspUserDailySpend | None:
    spend = await get_mssp_user_daily_spend(db, user_id)
    if spend.cap_hit:
        logger.warning(
            "mssp_user_daily_cap_hit",
            source=source,
            user_id=str(user_id),
            tokens_24h=spend.tokens,
            token_cap=mssp_user_daily_token_cap(),
            dollars_24h=round(spend.dollars, 4),
            dollar_cap=mssp_user_daily_dollar_cap(),
        )
        return None
    return spend


# ---------------------------------------------------------------------------
# Per-tenant 24h ceilings (#129)
# ---------------------------------------------------------------------------
#
# The env values above stay meaningful as the INSTALL default. What they were
# not is per-tenant: every other budget in the system is, so an MSSP could not
# give one customer more headroom without raising it for all of them.

DAILY_TOKEN_CAP_KEY = "max_tokens_per_24h"
DAILY_DOLLAR_CAP_KEY = "max_dollars_per_24h"


def tenant_daily_token_cap_max() -> int:
    """Ceiling a tenant override cannot exceed. Generous unless configured."""
    raw = os.getenv("SOCTALK_TENANT_DAILY_TOKEN_CAP_MAX", "")
    try:
        v = int(raw) if raw else 0
    except ValueError:
        v = 0
    return v if v > 0 else 10_000_000_000


def tenant_daily_dollar_cap_max() -> float:
    raw = os.getenv("SOCTALK_TENANT_DAILY_DOLLAR_CAP_MAX", "")
    try:
        v = float(raw) if raw else 0.0
    except ValueError:
        v = 0.0
    return v if v > 0 else 100_000.0


@dataclass(frozen=True, slots=True)
class DailyCaps:
    tokens: int
    dollars: float


async def resolve_tenant_daily_caps(db: AsyncSession, tenant_id: UUID) -> DailyCaps:
    """Effective 24h ceilings: env install default -> tenant override -> clamp.

    Never raises. A cap that cannot be resolved falls back to the install
    default rather than leaving the tenant uncapped, because failing open on a
    spend ceiling is the expensive direction.
    """
    from soctalk.core.ir.policies import effective_policy

    tokens = tenant_daily_token_cap()
    dollars = tenant_daily_dollar_cap()
    try:
        eff = await effective_policy(db, tenant_id)
    except Exception:  # noqa: BLE001
        logger.warning("tenant_daily_caps_unresolved", tenant_id=str(tenant_id))
        return DailyCaps(tokens=tokens, dollars=dollars)

    raw_tokens = eff.get(DAILY_TOKEN_CAP_KEY)
    if raw_tokens is not None:
        try:
            v = int(raw_tokens)
            if v > 0:
                tokens = min(v, tenant_daily_token_cap_max())
        except (TypeError, ValueError):
            logger.warning("tenant_daily_token_cap_unreadable", value=raw_tokens)

    raw_dollars = eff.get(DAILY_DOLLAR_CAP_KEY)
    if raw_dollars is not None:
        try:
            v = float(raw_dollars)
            if v > 0 and v == v and v != float("inf"):
                dollars = min(v, tenant_daily_dollar_cap_max())
        except (TypeError, ValueError):
            logger.warning("tenant_daily_dollar_cap_unreadable", value=raw_dollars)

    return DailyCaps(tokens=tokens, dollars=dollars)


@dataclass(frozen=True, slots=True)
class DailyCapStatus:
    """Spend against the effective ceilings, with the headroom left.

    Exists because a tripped 24h cap is otherwise invisible: the worker claim
    simply returns nothing, which is indistinguishable from an idle queue.
    """

    spend: TenantDailySpend
    caps: DailyCaps
    # Zone whose midnight resets this, and the next reset instant. Defaults
    # keep older callers constructing this with two arguments working.
    timezone: str = "UTC"
    resets_at: datetime | None = None

    @property
    def token_cap_hit(self) -> bool:
        return self.spend.tokens >= self.caps.tokens

    @property
    def dollar_cap_hit(self) -> bool:
        return self.spend.dollars >= self.caps.dollars

    @property
    def cap_hit(self) -> bool:
        return self.token_cap_hit or self.dollar_cap_hit

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.caps.tokens - self.spend.tokens)

    @property
    def dollars_remaining(self) -> float:
        return max(0.0, self.caps.dollars - self.spend.dollars)

    @property
    def reason(self) -> str | None:
        """Which dimension tripped, for an operator reading it cold."""
        if self.token_cap_hit:
            return f"tokens {self.spend.tokens}/{self.caps.tokens} today"
        if self.dollar_cap_hit:
            return f"spend ${self.spend.dollars:.4f}/${self.caps.dollars:.2f} today"
        return None


async def get_tenant_daily_status(
    db: AsyncSession, tenant_id: UUID
) -> DailyCapStatus:
    """Spend and the ceilings it is measured against, in one read."""
    tz = await resolve_budget_day_timezone(db, tenant_id)
    return DailyCapStatus(
        spend=await get_tenant_daily_spend(db, tenant_id),
        caps=await resolve_tenant_daily_caps(db, tenant_id),
        timezone=tz,
        resets_at=day_window(tz)[1],
    )
