"""Per-tenant Agent Run token budget (#103).

A dedicated, DB-resolved budget resource. Unlike the legacy
``token_budget_per_run`` on the LLM-config surface (which rendered to worker
env and needed a helm rollout), the override set here is resolved at run
creation and takes effect immediately with NO worker rollout. Storage is a
``tenant_policies`` override on ``max_tokens_per_investigation``; the resolver
(:func:`core.ir.policies.resolve_run_token_budget`) applies install default ->
tenant override, clamped to the install cap.

Routes:
  GET   /api/mssp/tenants/{tenant_id}/run-budget   (mssp admin/platform + analyst read)
  PATCH /api/mssp/tenants/{tenant_id}/run-budget   (mssp admin/platform write)
  GET   /api/tenant/run-budget                     (tenant admin/analyst, read-only)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictFloat, StrictInt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.cost import (
    DAILY_DOLLAR_CAP_KEY,
    DAILY_TOKEN_CAP_KEY,
    get_tenant_daily_status,
    tenant_daily_dollar_cap,
    tenant_daily_dollar_cap_max,
    tenant_daily_token_cap,
    tenant_daily_token_cap_max,
)
from soctalk.core.ir.events import EventKind, append_event
from soctalk.core.ir.policies import (
    RUN_DOLLAR_BUDGET_KEY,
    RUN_TOKEN_BUDGET_KEY,
    delete_tenant_policy,
    resolve_run_dollar_budget,
    resolve_run_token_budget,
    run_dollar_budget_default,
    run_dollar_budget_max,
    run_token_budget_default,
    run_token_budget_max,
    set_tenant_policy,
    tenant_policies,
)
from soctalk.core.observability.audit import log_audit
from soctalk.core.tenancy.auth import current_identity
from soctalk.core.tenancy.context import tenant_context
from soctalk.core.tenancy.decorators import require_role, require_tenant_role
from soctalk.core.tenancy.models import Role, Tenant

router = APIRouter(prefix="/api/mssp/tenants", tags=["mssp-run-budget"])
tenant_router = APIRouter(prefix="/api/tenant", tags=["tenant-run-budget"])

# Lower bound for a sane override (mirrors the legacy field's ge=1000): a budget
# too small to complete a single triage node is a foot-gun, not a policy.
MIN_BUDGET = 1000


def _db(request: Request) -> AsyncSession:
    session = getattr(request.state, "db", None)
    if session is None:
        raise HTTPException(500, "no db session on request")
    return session


class RunBudgetView(BaseModel):
    """The MSSP and tenant read shape. ``effective`` is what a new run gets.

    Token fields keep their original names so existing clients keep working;
    dollars and the 24h ceilings are additive (#128, #129).
    """

    install_default: int
    install_max: int
    tenant_override: int | None
    effective: int
    spend_24h_tokens: int

    # Per-run dollar ceiling, same shape as the token one.
    dollar_install_default: float = 5.0
    dollar_install_max: float = 1_000.0
    dollar_tenant_override: float | None = None
    dollar_effective: float = 5.0

    # Rolling 24h ceilings and what is left of them. Without these a tripped
    # cap is indistinguishable from an idle queue.
    spend_24h_dollars: float = 0.0
    daily_token_cap: int = 0
    daily_dollar_cap: float = 0.0
    daily_tokens_remaining: int = 0
    daily_dollars_remaining: float = 0.0
    daily_cap_hit: bool = False
    daily_cap_reason: str | None = None
    # When the daily ceilings next reset, and the zone that decides it. The
    # thing an operator staring at a blocked queue needs is a time, not
    # "eventually".
    daily_resets_at: str | None = None
    daily_timezone: str = "UTC"
    daily_token_install_default: int = 0
    daily_dollar_install_default: float = 0.0
    daily_token_max: int = 0
    daily_dollar_max: float = 0.0
    daily_token_override: int | None = None
    daily_dollar_override: float | None = None


class RunBudgetUpdate(BaseModel):
    """Tri-state per field: present = set, present-as-null = clear, absent = unchanged.

    ``override`` is the original token-only field, kept as an alias for one
    release. With two dimensions, "absent means 422" per field stops working
    (you must be able to change one without touching the other), so the
    handler requires that AT LEAST ONE field be present instead — an empty
    body still cannot silently clear anything (Codex review, finding 7).
    """

    override: StrictInt | None = None
    token_override: StrictInt | None = None
    # StrictFloat, not float: bool is a subclass of int, so a JSON `true`
    # would coerce to a $1.00 ceiling (Codex review round 2, finding 4).
    # StrictInt already gives the token fields this property.
    dollar_override: StrictFloat | StrictInt | None = None
    # Rolling 24h ceilings (#129). Same tri-state, same clamping.
    daily_token_override: StrictInt | None = None
    daily_dollar_override: StrictFloat | StrictInt | None = None


async def _assert_tenant_exists(db: AsyncSession, tenant_id: UUID) -> None:
    row = (
        await db.execute(select(Tenant.id).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "tenant not found")


async def _override(db: AsyncSession, tenant_id: UUID) -> int | None:
    pol = await tenant_policies(db, tenant_id)
    v = pol.get(RUN_TOKEN_BUDGET_KEY)
    return int(v) if v is not None else None


async def _daily_overrides(
    db: AsyncSession, tenant_id: UUID
) -> tuple[int | None, float | None]:
    """The tenant's own 24h ceilings, where set."""
    pol = await tenant_policies(db, tenant_id)
    tok = pol.get(DAILY_TOKEN_CAP_KEY)
    dol = pol.get(DAILY_DOLLAR_CAP_KEY)
    try:
        tok_v = int(tok) if tok is not None else None
    except (TypeError, ValueError):
        tok_v = None
    try:
        dol_v = float(dol) if dol is not None else None
    except (TypeError, ValueError):
        dol_v = None
    return tok_v, dol_v


async def _dollar_override(db: AsyncSession, tenant_id: UUID) -> float | None:
    pol = await tenant_policies(db, tenant_id)
    v = pol.get(RUN_DOLLAR_BUDGET_KEY)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _view(db: AsyncSession, tenant_id: UUID) -> RunBudgetView:
    status = await get_tenant_daily_status(db, tenant_id)
    daily_tok, daily_dol = await _daily_overrides(db, tenant_id)
    return RunBudgetView(
        install_default=run_token_budget_default(),
        install_max=run_token_budget_max(),
        tenant_override=await _override(db, tenant_id),
        effective=await resolve_run_token_budget(db, tenant_id),
        spend_24h_tokens=status.spend.tokens,
        dollar_install_default=run_dollar_budget_default(),
        dollar_install_max=run_dollar_budget_max(),
        dollar_tenant_override=await _dollar_override(db, tenant_id),
        dollar_effective=await resolve_run_dollar_budget(db, tenant_id),
        spend_24h_dollars=round(status.spend.dollars, 6),
        daily_token_cap=status.caps.tokens,
        daily_dollar_cap=status.caps.dollars,
        daily_tokens_remaining=status.tokens_remaining,
        daily_dollars_remaining=round(status.dollars_remaining, 6),
        daily_cap_hit=status.cap_hit,
        daily_cap_reason=status.reason,
        daily_resets_at=status.resets_at.isoformat() if status.resets_at else None,
        daily_timezone=status.timezone,
        daily_token_install_default=tenant_daily_token_cap(),
        daily_dollar_install_default=tenant_daily_dollar_cap(),
        daily_token_max=tenant_daily_token_cap_max(),
        daily_dollar_max=tenant_daily_dollar_cap_max(),
        daily_token_override=daily_tok,
        daily_dollar_override=daily_dol,
    )


@router.get(
    "/{tenant_id}/run-budget",
    response_model=RunBudgetView,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN, Role.ANALYST))],
)
async def get_run_budget(tenant_id: UUID, request: Request) -> RunBudgetView:
    db = _db(request)
    await _assert_tenant_exists(db, tenant_id)
    async with tenant_context(db, tenant_id):
        return await _view(db, tenant_id)


@router.patch(
    "/{tenant_id}/run-budget",
    response_model=RunBudgetView,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))],
)
async def update_run_budget(
    tenant_id: UUID, payload: RunBudgetUpdate, request: Request
) -> RunBudgetView:
    db = _db(request)
    identity = current_identity(request)
    setf = payload.model_fields_set

    # ``override`` is the legacy token-only name; ``token_override`` is the
    # explicit one. Both may not disagree in the same request.
    if "override" in setf and "token_override" in setf:
        raise HTTPException(
            422, "send either 'override' (legacy) or 'token_override', not both"
        )
    token_present = "override" in setf or "token_override" in setf
    dollar_present = "dollar_override" in setf
    daily_token_present = "daily_token_override" in setf
    daily_dollar_present = "daily_dollar_override" in setf
    if not any(
        (token_present, dollar_present, daily_token_present, daily_dollar_present)
    ):
        raise HTTPException(
            422,
            "body must include at least one of 'token_override', 'dollar_override', "
            "'daily_token_override' or 'daily_dollar_override' (a value to set, or "
            "null to clear)",
        )

    token_value = payload.token_override if "token_override" in setf else payload.override
    dollar_value = payload.dollar_override
    daily_token_value = payload.daily_token_override
    daily_dollar_value = payload.daily_dollar_override

    await _assert_tenant_exists(db, tenant_id)

    token_cap = run_token_budget_max()
    if token_present and token_value is not None:
        if token_value < MIN_BUDGET:
            raise HTTPException(422, f"token_override must be >= {MIN_BUDGET}")
        if token_value > token_cap:
            raise HTTPException(
                422,
                f"token_override {token_value} must not exceed the install cap "
                f"({token_cap})",
            )
    dollar_cap = run_dollar_budget_max()
    if dollar_present and dollar_value is not None:
        # NaN fails every comparison, so reject it explicitly rather than
        # letting it through as a ceiling that can never be hit.
        if dollar_value != dollar_value:
            raise HTTPException(422, "dollar_override must be a number")
        if dollar_value <= 0:
            raise HTTPException(422, "dollar_override must be > 0")
        if dollar_value > dollar_cap:
            raise HTTPException(
                422,
                f"dollar_override {dollar_value} must not exceed the install cap "
                f"({dollar_cap})",
            )

    # The 24h ceilings clamp against their own install maxima. A ceiling of
    # zero would stop all work with no way to tell it from "unset", so it is
    # rejected rather than stored.
    if daily_token_present and daily_token_value is not None:
        cap = tenant_daily_token_cap_max()
        if daily_token_value <= 0:
            raise HTTPException(422, "daily_token_override must be > 0")
        if daily_token_value > cap:
            raise HTTPException(
                422,
                f"daily_token_override {daily_token_value} must not exceed the "
                f"install cap ({cap})",
            )
    if daily_dollar_present and daily_dollar_value is not None:
        cap_d = tenant_daily_dollar_cap_max()
        if daily_dollar_value != daily_dollar_value:
            raise HTTPException(422, "daily_dollar_override must be a number")
        if daily_dollar_value <= 0:
            raise HTTPException(422, "daily_dollar_override must be > 0")
        if daily_dollar_value > cap_d:
            raise HTTPException(
                422,
                f"daily_dollar_override {daily_dollar_value} must not exceed the "
                f"install cap ({cap_d})",
            )

    async with tenant_context(db, tenant_id):
        before = await _view(db, tenant_id)
        if token_present:
            if token_value is None:
                await delete_tenant_policy(db, tenant_id, RUN_TOKEN_BUDGET_KEY)
            else:
                await set_tenant_policy(
                    db, tenant_id, RUN_TOKEN_BUDGET_KEY, token_value
                )
        if dollar_present:
            if dollar_value is None:
                await delete_tenant_policy(db, tenant_id, RUN_DOLLAR_BUDGET_KEY)
            else:
                await set_tenant_policy(
                    db, tenant_id, RUN_DOLLAR_BUDGET_KEY, dollar_value
                )
        if daily_token_present:
            if daily_token_value is None:
                await delete_tenant_policy(db, tenant_id, DAILY_TOKEN_CAP_KEY)
            else:
                await set_tenant_policy(
                    db, tenant_id, DAILY_TOKEN_CAP_KEY, daily_token_value
                )
        if daily_dollar_present:
            if daily_dollar_value is None:
                await delete_tenant_policy(db, tenant_id, DAILY_DOLLAR_CAP_KEY)
            else:
                await set_tenant_policy(
                    db, tenant_id, DAILY_DOLLAR_CAP_KEY, daily_dollar_value
                )
        after = await _view(db, tenant_id)
        await log_audit(
            db,
            action="tenant.run_budget.update",
            actor_principal=identity.user_type,
            actor_id=str(identity.user_id),
            tenant_id=tenant_id,
            resource_type="run_budget",
            resource_id=str(tenant_id),
            before={
                "override": before.tenant_override,
                "effective": before.effective,
                "dollar_override": before.dollar_tenant_override,
                "dollar_effective": before.dollar_effective,
                "daily_token_cap": before.daily_token_cap,
                "daily_dollar_cap": before.daily_dollar_cap,
            },
            after={
                "override": after.tenant_override,
                "effective": after.effective,
                "dollar_override": after.dollar_tenant_override,
                "dollar_effective": after.dollar_effective,
                "daily_token_cap": after.daily_token_cap,
                "daily_dollar_cap": after.daily_dollar_cap,
            },
        )
    return after


@tenant_router.get(
    "/run-budget",
    response_model=RunBudgetView,
    # NOT a bare require_tenant_role(): that defaults to allowing
    # customer_viewer, and this view carries 24h spend, remaining budget and
    # the effective ceilings. The investigations bridge deliberately blanks
    # every cost and run field for a customer audience, so exposing the same
    # numbers here would route around that decision (Codex review round 3,
    # finding 2). /api/tenant/llm is already pinned this way.
    dependencies=[
        Depends(require_tenant_role(Role.TENANT_ADMIN, Role.TENANT_MANAGER, Role.TENANT_ANALYST))
    ],
)
async def get_tenant_run_budget(request: Request) -> RunBudgetView:
    identity = current_identity(request)
    if identity.tenant_id is None:
        raise HTTPException(403, "tenant scope required")
    db = _db(request)
    async with tenant_context(db, identity.tenant_id):
        return await _view(db, identity.tenant_id)


# ---------------------------------------------------------------------------
# Unlocking a budget-halted run (#127)
# ---------------------------------------------------------------------------
#
# A run that exceeds its per-run cap ends ``halted_budget``, and that status
# sits inside ``uq_investigation_runs_single_active``. So the investigation
# cannot get a new run either: one halt removes it from triage permanently.
# The automatic follow-up ``start_run`` fires only on ``completed``, so not
# even new evidence restarts it. Before this endpoint the only recovery was a
# direct database UPDATE.


class RunUnlockRequest(BaseModel):
    """New ceilings for the resumed run. Absent means keep the current one."""

    # Strict for the same reason as RunBudgetUpdate: this is a money field, and
    # a JSON `true` coercing to a $1.00 ceiling is not a rounding error
    # (Codex review round 3, finding 5).
    dollar_budget: StrictFloat | StrictInt | None = None
    token_budget: StrictInt | None = None


class RunUnlockResult(BaseModel):
    run_id: UUID
    status: str
    tokens_used: int
    tokens_budget: int
    dollars_used: float
    dollars_budget: float
    # Set when the tenant is over its rolling 24h ceiling: the run is active
    # again but the worker will not claim it until the window clears, and an
    # operator who is not told that will think the unlock silently failed.
    warning: str | None = None


@router.post(
    "/{tenant_id}/runs/{run_id}/unlock",
    response_model=RunUnlockResult,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))],
)
async def unlock_run(
    tenant_id: UUID, run_id: UUID, payload: RunUnlockRequest, request: Request
) -> RunUnlockResult:
    """Resume a budget-halted run, optionally raising its ceiling first.

    Raising and resuming happen in one transaction because they are one
    decision: spend is NOT reset, so resuming without a higher ceiling just
    re-halts on the first budget check and teaches the operator nothing.

    An unlock is an operator decision, not a retry, so ``attempts`` is left
    alone — it must not consume the re-triage budget.
    """
    db = _db(request)
    identity = current_identity(request)
    await _assert_tenant_exists(db, tenant_id)

    async with tenant_context(db, tenant_id):
        # Lock the run and its investigation together. The claim loop takes
        # ``FOR UPDATE SKIP LOCKED`` over active runs, so holding the row here
        # keeps a worker from claiming mid-update, and locking the
        # investigation keeps it from being closed out underneath us.
        row = (
            await db.execute(
                text(
                    """
                    SELECT r.id, r.status, r.tokens_used, r.tokens_budget,
                           r.dollars_used, r.dollars_budget, r.investigation_id,
                           i.status AS investigation_status
                      FROM investigation_runs r
                      JOIN investigations i ON i.id = r.investigation_id
                                           AND i.tenant_id = r.tenant_id
                     WHERE r.id = :r AND r.tenant_id = :t
                       FOR UPDATE OF r, i
                    """
                ),
                {"r": str(run_id), "t": str(tenant_id)},
            )
        ).mappings().first()

        if row is None:
            raise HTTPException(404, "run not found")
        if row["status"] != "halted_budget":
            raise HTTPException(
                409,
                f"run is {row['status']}, not halted_budget; only a budget-halted "
                "run can be unlocked",
            )
        if row["investigation_status"] != "active":
            raise HTTPException(
                409,
                f"investigation is {row['investigation_status']}; resuming a run "
                "on a closed investigation would re-open triage on it",
            )

        dollars_used = float(row["dollars_used"] or 0.0)
        tokens_used = int(row["tokens_used"] or 0)
        dollar_cap = run_dollar_budget_max()
        token_cap = run_token_budget_max()

        new_dollars = (
            float(payload.dollar_budget)
            if payload.dollar_budget is not None
            else float(row["dollars_budget"] or 0.0)
        )
        # NaN fails every comparison below, so without this it would slip past
        # the cap and the exceeds-spend check and land in the column as a
        # ceiling that can never be reached (Codex review, finding 6).
        if new_dollars != new_dollars or new_dollars in (
            float("inf"),
            float("-inf"),
        ):
            raise HTTPException(422, "dollar_budget must be a finite number")
        new_tokens = (
            int(payload.token_budget)
            if payload.token_budget is not None
            else int(row["tokens_budget"] or 0)
        )
        if new_dollars > dollar_cap:
            raise HTTPException(
                422, f"dollar_budget must not exceed the install cap ({dollar_cap})"
            )
        if new_tokens > token_cap:
            raise HTTPException(
                422, f"token_budget must not exceed the install cap ({token_cap})"
            )
        # Strictly greater: ``over_budget`` halts at >=, so an equal ceiling
        # re-halts immediately.
        if new_dollars <= dollars_used:
            raise HTTPException(
                422,
                f"dollar_budget must exceed the ${dollars_used:.6f} already spent, "
                "or the run halts again on its first budget check",
            )
        if new_tokens <= tokens_used:
            raise HTTPException(
                422,
                f"token_budget must exceed the {tokens_used} tokens already spent, "
                "or the run halts again on its first budget check",
            )

        updated = await db.execute(
            text(
                """
                UPDATE investigation_runs
                   SET status = 'active',
                       tokens_budget = :tb,
                       dollars_budget = :db,
                       -- complete_run stamps ended_at even for a halt, and the
                       -- 24h spend window keys on COALESCE(ended_at, ...), so a
                       -- resumed run that kept it would have its spend aged out
                       -- by when it halted rather than when it ran.
                       ended_at = NULL,
                       claimed_at = NULL,
                       claimed_by = NULL,
                       lease_id = NULL,
                       lease_expires_at = NULL,
                       not_before = now(),
                       last_error = NULL
                 WHERE id = :r AND tenant_id = :t AND status = 'halted_budget'
                """
            ),
            {"r": str(run_id), "t": str(tenant_id), "tb": new_tokens, "db": new_dollars},
        )
        if updated.rowcount != 1:
            # Someone changed the status between the lock and the write.
            raise HTTPException(409, "run changed state during unlock; retry")

        await append_event(
            db,
            tenant_id=tenant_id,
            investigation_id=row["investigation_id"],
            run_id=run_id,
            kind=EventKind.BUDGET_UNLOCK,
            payload={
                "actor": str(identity.user_id),
                "tokens_used": tokens_used,
                "dollars_used": round(dollars_used, 6),
                "tokens_budget": new_tokens,
                "dollars_budget": new_dollars,
            },
            producer="run_budget.unlock",
        )
        await log_audit(
            db,
            action="tenant.run.unlock",
            actor_principal=identity.user_type,
            actor_id=str(identity.user_id),
            tenant_id=tenant_id,
            resource_type="investigation_run",
            resource_id=str(run_id),
            before={
                "status": "halted_budget",
                "tokens_budget": int(row["tokens_budget"] or 0),
                "dollars_budget": float(row["dollars_budget"] or 0.0),
            },
            after={
                "status": "active",
                "tokens_budget": new_tokens,
                "dollars_budget": new_dollars,
            },
        )

        # The per-run cap is not the only thing that can stop this run. Resolve
        # the ceilings rather than reading TenantDailySpend.cap_hit, which only
        # knows the install env values and would miss a per-tenant override
        # (#129) — the exact case where the operator most needs telling.
        status = await get_tenant_daily_status(db, tenant_id)
        warning = None
        if status.cap_hit:
            warning = (
                f"tenant is over its rolling 24h ceiling ({status.reason}), so the "
                "worker will not claim this run until the window clears"
            )

    return RunUnlockResult(
        run_id=run_id,
        status="active",
        tokens_used=tokens_used,
        tokens_budget=new_tokens,
        dollars_used=dollars_used,
        dollars_budget=new_dollars,
        warning=warning,
    )
