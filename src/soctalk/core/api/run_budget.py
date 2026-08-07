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
from pydantic import BaseModel, StrictInt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.cost import get_tenant_daily_spend
from soctalk.core.ir.events import EventKind, append_event
from soctalk.core.ir.policies import (
    RUN_TOKEN_BUDGET_KEY,
    delete_tenant_policy,
    resolve_run_token_budget,
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
    """The MSSP and tenant read shape. ``effective`` is what a new run gets."""

    install_default: int
    install_max: int
    tenant_override: int | None
    effective: int
    spend_24h_tokens: int


class RunBudgetUpdate(BaseModel):
    # Tri-state via ``model_fields_set`` (checked in the handler): ``override``
    # present as int = set; present as null = clear; ABSENT = 422 (an empty {}
    # body must not silently clear the override). StrictInt rejects bool/float/
    # numeric-string so "non-int" really means non-int.
    override: StrictInt | None = None


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


async def _view(db: AsyncSession, tenant_id: UUID) -> RunBudgetView:
    spend = await get_tenant_daily_spend(db, tenant_id)
    return RunBudgetView(
        install_default=run_token_budget_default(),
        install_max=run_token_budget_max(),
        tenant_override=await _override(db, tenant_id),
        effective=await resolve_run_token_budget(db, tenant_id),
        spend_24h_tokens=spend.tokens,
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
    if "override" not in payload.model_fields_set:
        raise HTTPException(
            422, "body must include 'override' (an int to set, or null to clear)"
        )
    await _assert_tenant_exists(db, tenant_id)
    cap = run_token_budget_max()
    if payload.override is not None:
        if payload.override < MIN_BUDGET:
            raise HTTPException(422, f"override must be >= {MIN_BUDGET}")
        if payload.override > cap:
            raise HTTPException(
                422, f"override {payload.override} must not exceed the install cap ({cap})"
            )
    async with tenant_context(db, tenant_id):
        before = await _view(db, tenant_id)
        if payload.override is None:
            await delete_tenant_policy(db, tenant_id, RUN_TOKEN_BUDGET_KEY)
        else:
            await set_tenant_policy(db, tenant_id, RUN_TOKEN_BUDGET_KEY, payload.override)
        after = await _view(db, tenant_id)
        await log_audit(
            db,
            action="tenant.run_budget.update",
            actor_principal=identity.user_type,
            actor_id=str(identity.user_id),
            tenant_id=tenant_id,
            resource_type="run_budget",
            resource_id=str(tenant_id),
            before={"override": before.tenant_override, "effective": before.effective},
            after={"override": after.tenant_override, "effective": after.effective},
        )
    return after


@tenant_router.get(
    "/run-budget",
    response_model=RunBudgetView,
    dependencies=[Depends(require_tenant_role())],  # any tenant role may read
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

    dollar_budget: float | None = None
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

        # The per-run cap is not the only thing that can stop this run.
        spend = await get_tenant_daily_spend(db, tenant_id)
        warning = None
        if spend.cap_hit:
            warning = (
                "tenant is over its rolling 24h cap, so the worker will not claim "
                "this run until the window clears"
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
