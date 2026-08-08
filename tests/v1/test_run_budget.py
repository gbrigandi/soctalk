"""Per-tenant run token budget: resolution, run-creation stamping, immutability (#103)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.api.run_budget import (
    RunBudgetUpdate,
    get_run_budget,
    update_run_budget,
)
from soctalk.core.ir.policies import (
    RUN_TOKEN_BUDGET_KEY,
    delete_tenant_policy,
    resolve_run_token_budget,
    run_token_budget_default,
    set_tenant_policy,
)
from soctalk.core.ir.runtime import start_run


def _req(session: AsyncSession):
    """Minimal request the handlers need: request.state.db + .user_identity."""
    ident = {
        "user_id": str(uuid4()),
        "email": "admin@mssp.example",
        "user_type": "mssp",
        "role": "mssp_admin",
        "tenant_id": None,
    }
    return SimpleNamespace(state=SimpleNamespace(db=session, user_identity=ident))

SKIP_INTEGRATION = os.getenv("SKIP_INTEGRATION", "0") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(SKIP_INTEGRATION, reason="needs Postgres"),
]


async def _mk_investigation(s: AsyncSession, tenant_id) -> str:
    cid = str(uuid4())
    await s.execute(
        text(
            "INSERT INTO investigations (id, tenant_id, short_id, title, status, "
            "severity, opened_at, visibility) "
            "VALUES (:id, :t, :sid, 'x', 'active', 5, now(), 'mssp_only')"
        ),
        {"id": cid, "t": str(tenant_id), "sid": f"S-{cid[:6]}"},
    )
    return cid


async def _budget(s: AsyncSession, run_id) -> int:
    return int(
        (
            await s.execute(
                text("SELECT tokens_budget FROM investigation_runs WHERE id = :r"),
                {"r": str(run_id)},
            )
        ).scalar_one()
    )


async def test_run_uses_install_default_when_no_override(
    mssp_session: AsyncSession, seed_two_tenants
):
    a, _ = seed_two_tenants
    cid = await _mk_investigation(mssp_session, a.tenant_id)
    rid = await start_run(mssp_session, a.tenant_id, cid, settle_seconds=0)
    await mssp_session.commit()
    assert await _budget(mssp_session, rid) == run_token_budget_default()


async def test_override_applies_to_new_run_and_is_immutable_for_existing(
    mssp_session: AsyncSession, seed_two_tenants
):
    a, _ = seed_two_tenants
    # First run at the install default.
    c1 = await _mk_investigation(mssp_session, a.tenant_id)
    r1 = await start_run(mssp_session, a.tenant_id, c1, settle_seconds=0)
    await mssp_session.commit()
    b1 = await _budget(mssp_session, r1)

    # Set a lower per-tenant override, then create a second run.
    await set_tenant_policy(mssp_session, a.tenant_id, RUN_TOKEN_BUDGET_KEY, 40_000)
    await mssp_session.commit()
    c2 = await _mk_investigation(mssp_session, a.tenant_id)
    r2 = await start_run(mssp_session, a.tenant_id, c2, settle_seconds=0)
    await mssp_session.commit()

    assert await _budget(mssp_session, r2) == 40_000, "new run picks up the override"
    assert await _budget(mssp_session, r1) == b1, (
        "existing run's stamped budget must not change when the override changes"
    )


async def test_override_above_install_cap_is_clamped_at_resolve(
    mssp_session: AsyncSession, seed_two_tenants, monkeypatch
):
    a, _ = seed_two_tenants
    monkeypatch.setenv("SOCTALK_RUN_TOKEN_BUDGET_MAX", "25000")
    await set_tenant_policy(mssp_session, a.tenant_id, RUN_TOKEN_BUDGET_KEY, 5_000_000)
    await mssp_session.commit()
    assert await resolve_run_token_budget(mssp_session, a.tenant_id) == 25000


async def test_clearing_override_reverts_to_install_default(
    mssp_session: AsyncSession, seed_two_tenants
):
    a, _ = seed_two_tenants
    await set_tenant_policy(mssp_session, a.tenant_id, RUN_TOKEN_BUDGET_KEY, 40_000)
    await mssp_session.commit()
    assert await resolve_run_token_budget(mssp_session, a.tenant_id) == 40_000

    await delete_tenant_policy(mssp_session, a.tenant_id, RUN_TOKEN_BUDGET_KEY)
    await mssp_session.commit()
    assert await resolve_run_token_budget(mssp_session, a.tenant_id) == run_token_budget_default()


# --- API handler behaviour (validation, clear-deletes, audit, 404) ----------


async def test_api_set_clear_and_audit(mssp_session: AsyncSession, seed_two_tenants):
    a, _ = seed_two_tenants
    req = _req(mssp_session)

    # Set an override -> effective reflects it.
    view = await update_run_budget(a.tenant_id, RunBudgetUpdate(override=40_000), req)
    assert view.tenant_override == 40_000
    assert view.effective == 40_000

    # An audit row was written with before/after.
    n = (
        await mssp_session.execute(
            text(
                "SELECT count(*)::int FROM audit_log "
                "WHERE action = 'tenant.run_budget.update' AND tenant_id = :t"
            ),
            {"t": str(a.tenant_id)},
        )
    ).scalar_one()
    assert n >= 1

    # Clear (override=null) DELETES the policy row -> revert to install default.
    cleared = await update_run_budget(a.tenant_id, RunBudgetUpdate(override=None), req)
    assert cleared.tenant_override is None
    assert cleared.effective == run_token_budget_default()
    remaining = (
        await mssp_session.execute(
            text(
                "SELECT count(*)::int FROM tenant_policies "
                "WHERE tenant_id = :t AND key = :k"
            ),
            {"t": str(a.tenant_id), "k": RUN_TOKEN_BUDGET_KEY},
        )
    ).scalar_one()
    assert remaining == 0


async def test_api_empty_patch_does_not_clear(mssp_session: AsyncSession, seed_two_tenants):
    a, _ = seed_two_tenants
    req = _req(mssp_session)
    await update_run_budget(a.tenant_id, RunBudgetUpdate(override=40_000), req)
    # An empty body {} must NOT silently clear the override.
    with pytest.raises(HTTPException) as ei:
        await update_run_budget(a.tenant_id, RunBudgetUpdate(), req)
    assert ei.value.status_code == 422
    # Override still set.
    assert (await get_run_budget(a.tenant_id, req)).tenant_override == 40_000


async def test_api_validation_bounds(mssp_session: AsyncSession, seed_two_tenants, monkeypatch):
    a, _ = seed_two_tenants
    req = _req(mssp_session)
    monkeypatch.setenv("SOCTALK_RUN_TOKEN_BUDGET_MAX", "100000")
    # Above the cap -> 422.
    with pytest.raises(HTTPException) as e1:
        await update_run_budget(a.tenant_id, RunBudgetUpdate(override=200_000), req)
    assert e1.value.status_code == 422
    # Below the floor -> 422.
    with pytest.raises(HTTPException) as e2:
        await update_run_budget(a.tenant_id, RunBudgetUpdate(override=999), req)
    assert e2.value.status_code == 422


def test_api_rejects_non_int_override():
    # StrictInt: a numeric string / bool / float is rejected at parse time.
    for bad in ("40000", 4.5, True):
        with pytest.raises(ValidationError):
            RunBudgetUpdate(override=bad)


async def test_api_unknown_tenant_404(mssp_session: AsyncSession):
    req = _req(mssp_session)
    with pytest.raises(HTTPException) as ei:
        await get_run_budget(uuid4(), req)
    assert ei.value.status_code == 404
