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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.cost import get_tenant_daily_spend
from soctalk.core.ir.policies import (
    RUN_TOKEN_BUDGET_KEY,
    delete_tenant_policy,
    resolve_run_token_budget,
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
