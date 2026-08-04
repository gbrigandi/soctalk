"""ProvisioningWorker semantics — queue claim/reclaim/lifecycle.

Uses the real Postgres harness (same as test_provisioning_controller.py)
so the claim path exercises ``SELECT ... FOR UPDATE SKIP LOCKED`` against
the real engine and the partial unique index on ``provisioning_jobs``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from soctalk.core.provisioning.worker import ProvisioningWorker
from soctalk.core.tenancy.models import (
    BrandingConfig,
    IntegrationConfig,
    Organization,
    ProvisioningJob,
    Tenant,
    TenantState,
)


SKIP_INTEGRATION = os.getenv("SKIP_INTEGRATION", "0") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        SKIP_INTEGRATION,
        reason="SKIP_INTEGRATION set; worker tests need Postgres",
    ),
]


@pytest.fixture(autouse=True)
def _stub_k8s_client(monkeypatch):
    """Keep worker tests cluster-free.

    ``TenantController.__init__`` eagerly builds a Kubernetes client via
    ``new_k8s_client()`` (in-cluster config, falling back to kubeconfig).
    Worker tests exercise queue claim/dispatch mechanics only and never
    talk to a real cluster; CI runs the integration suite against Postgres
    with no Kubernetes. Stub the factory so constructing a controller in
    ``_dispatch`` never depends on a loadable kubeconfig.
    """
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "soctalk.core.provisioning.controller.new_k8s_client",
        lambda: MagicMock(name="StubK8sClient"),
    )


def _mssp_url() -> str:
    return os.getenv(
        "DATABASE_URL_MSSP",
        "postgresql+asyncpg://soctalk_mssp:soctalk_mssp@localhost:5444/soctalk",
    )


def _admin_url() -> str:
    return os.getenv(
        "DATABASE_URL_ADMIN",
        "postgresql+asyncpg://soctalk_admin:soctalk_admin@localhost:5444/soctalk",
    )


@pytest_asyncio.fixture
async def admin_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_admin_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def mssp_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_mssp_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_tenant(
    mssp_sessionmaker, admin_session: AsyncSession
) -> Tenant:
    await admin_session.execute(
        text(
            "TRUNCATE tenant_lifecycle_events, integration_configs, "
            "branding_configs, tenant_secrets, provisioning_jobs, "
            "tenants, organizations CASCADE"
        )
    )
    await admin_session.commit()

    async with mssp_sessionmaker() as session:
        org = Organization(
            mssp_id=uuid4(), mssp_name="Worker Test", slug="worker-test",
            install_id=uuid4(), install_label="test",
        )
        session.add(org)
        await session.flush()
        tenant = Tenant(
            slug=f"wrk{uuid4().hex[:6]}",
            display_name="Worker Test",
            state=TenantState.PENDING.value,
            profile="poc",
            organization_id=org.id,
        )
        session.add(tenant)
        await session.flush()
        session.add_all([
            IntegrationConfig(tenant_id=tenant.id),
            BrandingConfig(tenant_id=tenant.id, app_name="W"),
        ])
        await session.commit()
        return tenant


# ---------------------------------------------------------------------------
# Stale-claim reclaim
# ---------------------------------------------------------------------------


async def test_reclaim_flips_stale_in_flight_back_to_pending(
    mssp_sessionmaker, seeded_tenant: Tenant
):
    """A worker crash leaves an ``in_flight`` row with a claimed_at older
    than ``stale_claim_seconds``. The next reclaim pass must flip it
    back to ``pending`` so a new worker can claim it.
    """
    async with mssp_sessionmaker() as s:
        # Fresh in_flight job claimed 2 hours ago by a ghost worker.
        job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.provision",
            status="in_flight",
            claimed_at=datetime.utcnow() - timedelta(hours=2),
            claimed_by="dead-worker",
            attempts=1,
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    worker = ProvisioningWorker(
        mssp_sessionmaker,
        stale_claim_seconds=60,
        reclaim_interval_seconds=0,
    )
    # Drive one reclaim pass.
    await worker._maybe_reclaim_stale()

    async with mssp_sessionmaker() as s:
        row = (
            await s.execute(
                select(ProvisioningJob).where(ProvisioningJob.id == job_id)
            )
        ).scalar_one()
    assert row.status == "pending"
    assert row.claimed_at is None
    assert row.claimed_by is None
    assert row.last_error == "reclaimed after stale claim"
    # Attempts is preserved — stale reclaim counts toward retry budget.
    assert row.attempts == 1


async def test_reclaim_leaves_recent_in_flight_alone(
    mssp_sessionmaker, seeded_tenant: Tenant
):
    """A job claimed seconds ago is still the active worker's — hands off."""
    async with mssp_sessionmaker() as s:
        job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.provision",
            status="in_flight",
            claimed_at=datetime.utcnow() - timedelta(seconds=5),
            claimed_by="me",
            attempts=1,
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    worker = ProvisioningWorker(
        mssp_sessionmaker,
        stale_claim_seconds=60,  # 60s threshold, row is 5s old
        reclaim_interval_seconds=0,
    )
    await worker._maybe_reclaim_stale()

    async with mssp_sessionmaker() as s:
        row = (
            await s.execute(
                select(ProvisioningJob).where(ProvisioningJob.id == job_id)
            )
        ).scalar_one()
    assert row.status == "in_flight"
    assert row.claimed_by == "me"


async def test_reclaim_interval_rate_limits_passes(
    mssp_sessionmaker, seeded_tenant: Tenant
):
    """Two back-to-back ``_maybe_reclaim_stale`` calls: second is a no-op."""
    worker = ProvisioningWorker(
        mssp_sessionmaker,
        stale_claim_seconds=60,
        reclaim_interval_seconds=300,  # 5 min — tight enough we can observe
    )
    await worker._maybe_reclaim_stale()
    first_tick = worker._last_reclaim_at
    await worker._maybe_reclaim_stale()
    # Second call short-circuits — timestamp unchanged.
    assert worker._last_reclaim_at == first_tick


# ---------------------------------------------------------------------------
# Claim + run dispatch
# ---------------------------------------------------------------------------


async def test_claim_skips_rows_whose_next_attempt_is_in_the_future(
    mssp_sessionmaker, seeded_tenant: Tenant
):
    """Failed jobs sit in ``pending`` with a future next_attempt_at after
    backoff; the claim query must respect it.
    """
    async with mssp_sessionmaker() as s:
        future = datetime.utcnow() + timedelta(minutes=10)
        past_job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.provision",
            status="pending",
            next_attempt_at=future,
        )
        s.add(past_job)
        await s.commit()

    worker = ProvisioningWorker(mssp_sessionmaker)
    async with mssp_sessionmaker() as s:
        claimed = await worker._claim(s)
    assert claimed is None


# ---------------------------------------------------------------------------
# Terminal-tenant guard + job timeout
# ---------------------------------------------------------------------------


async def test_run_job_abandons_terminal_tenant(
    mssp_sessionmaker, admin_session: AsyncSession, seeded_tenant: Tenant
):
    """A queued job whose tenant was archived after enqueue must be failed
    terminally without invoking the controller — running it would at best
    fail lifecycle assertions and at worst hang the single-coroutine queue
    (the exact failure observed on demo.soctalk.ai, 2026-06-11).
    """
    await admin_session.execute(
        text("UPDATE tenants SET state = 'archived' WHERE id = :tid"),
        {"tid": str(seeded_tenant.id)},
    )
    await admin_session.commit()

    async with mssp_sessionmaker() as s:
        job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.provision",
            status="in_flight",
            claimed_at=datetime.utcnow(),
            claimed_by="me",
            attempts=1,
        )
        s.add(job)
        await s.commit()
        job_id = job.id
        job_detached = job

    worker = ProvisioningWorker(mssp_sessionmaker)
    await worker._run_job(job_detached)

    async with mssp_sessionmaker() as s:
        row = (
            await s.execute(
                select(ProvisioningJob).where(ProvisioningJob.id == job_id)
            )
        ).scalar_one()
    # Terminal failure regardless of remaining retry budget.
    assert row.status == "failed"
    assert "terminal state" in (row.last_error or "")


async def test_run_job_times_out_and_requeues(
    mssp_sessionmaker, seeded_tenant: Tenant, monkeypatch
):
    """A dispatch that hangs past ``job_timeout_seconds`` is cancelled and
    the job re-enters the retry/backoff path instead of wedging the queue.
    """
    async with mssp_sessionmaker() as s:
        job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.provision",
            status="in_flight",
            claimed_at=datetime.utcnow(),
            claimed_by="me",
            attempts=1,
        )
        s.add(job)
        await s.commit()
        job_id = job.id
        job_detached = job

    worker = ProvisioningWorker(mssp_sessionmaker, job_timeout_seconds=0.05)

    async def _hang(_job):
        await asyncio.sleep(60)

    monkeypatch.setattr(worker, "_dispatch", _hang)
    await worker._run_job(job_detached)

    async with mssp_sessionmaker() as s:
        row = (
            await s.execute(
                select(ProvisioningJob).where(ProvisioningJob.id == job_id)
            )
        ).scalar_one()
    # attempts=1 < max_attempts, so the timeout lands it back in pending
    # with backoff — not failed, not stuck in_flight.
    assert row.status == "pending"
    assert "timed out" in (row.last_error or "")
    assert row.claimed_at is None


async def test_run_forever_survives_iteration_error(
    mssp_sessionmaker, seeded_tenant: Tenant
):
    """A transient error inside the loop must NOT kill the worker — it
    logs and keeps polling. Regression for the demo-box wedge where one
    unhandled error froze the whole provisioning queue until a pod
    restart (jobs piled up `pending` with zero `in_flight`).
    """
    worker = ProvisioningWorker(mssp_sessionmaker, poll_interval=0.01)
    calls = {"n": 0}

    async def flaky_claim() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB blip")
        if calls["n"] >= 3:
            await worker.stop()  # let the loop exit cleanly
        return False

    # No-op the reclaim so we isolate the claim path.
    async def _noop():
        return None

    worker._maybe_reclaim_stale = _noop  # type: ignore[assignment]
    worker._claim_and_run_one = flaky_claim  # type: ignore[assignment]

    # If the error escaped, run_forever would raise here. It must not.
    await asyncio.wait_for(worker.run_forever(), timeout=5)
    # Proves it kept looping past the error (>=3 iterations, not 1).
    assert calls["n"] >= 3


# ---------------------------------------------------------------------------
# tenant.reconcile dispatch (tenant.llm.reconcile-active)
# ---------------------------------------------------------------------------


async def test_run_job_dispatches_tenant_reconcile_to_controller(
    mssp_sessionmaker, seeded_tenant: Tenant, monkeypatch
):
    """A claimed job with kind='tenant.reconcile' must reach
    ``TenantController.reconcile`` (not provision/decommission), and the
    job lands in 'succeeded' on a clean run.
    """
    from soctalk.core.provisioning.controller import TenantController

    called: dict = {}

    async def fake_reconcile(self, tenant_id, *, actor_id=None):
        called["tenant_id"] = tenant_id
        called["actor_id"] = actor_id

    monkeypatch.setattr(TenantController, "reconcile", fake_reconcile)

    async with mssp_sessionmaker() as s:
        job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.reconcile",
            status="pending",
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    worker = ProvisioningWorker(mssp_sessionmaker, worker_id="test-worker")
    async with mssp_sessionmaker() as s:
        claimed = await worker._claim(s)
    assert claimed is not None and claimed.id == job_id

    await worker._run_job(claimed)

    assert called["tenant_id"] == seeded_tenant.id
    assert called["actor_id"] == "worker:test-worker"

    async with mssp_sessionmaker() as s:
        row = (
            await s.execute(
                select(ProvisioningJob).where(ProvisioningJob.id == job_id)
            )
        ).scalar_one()
    assert row.status == "succeeded"
    assert row.last_error is None


async def test_run_job_reconcile_failure_records_backoff(
    mssp_sessionmaker, seeded_tenant: Tenant, monkeypatch
):
    """A reconcile failure goes through the same _record_failure path as
    every other kind: status back to 'pending', last_error captured,
    next_attempt_at pushed into the future (capped exponential backoff),
    claim released.
    """
    from soctalk.core.provisioning.controller import (
        ProvisionError,
        TenantController,
    )

    async def failing_reconcile(self, tenant_id, *, actor_id=None):
        raise ProvisionError("helm upgrade failed: boom", step="helm_apply_tenant")

    monkeypatch.setattr(TenantController, "reconcile", failing_reconcile)

    async with mssp_sessionmaker() as s:
        job = ProvisioningJob(
            tenant_id=seeded_tenant.id,
            kind="tenant.reconcile",
            status="pending",
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    worker = ProvisioningWorker(
        mssp_sessionmaker, worker_id="test-worker", backoff_base=30.0
    )
    async with mssp_sessionmaker() as s:
        claimed = await worker._claim(s)
    assert claimed is not None

    before = datetime.utcnow()
    await worker._run_job(claimed)

    async with mssp_sessionmaker() as s:
        row = (
            await s.execute(
                select(ProvisioningJob).where(ProvisioningJob.id == job_id)
            )
        ).scalar_one()
    # attempts=1 < max_attempts → retriable: back to pending with backoff.
    assert row.status == "pending"
    assert row.attempts == 1
    assert "boom" in row.last_error
    assert row.claimed_at is None
    assert row.claimed_by is None
    assert row.next_attempt_at.replace(tzinfo=None) > before


# ---------------------------------------------------------------------------
# Drift sweep (issue #104): sync_state runs for live tenants
# ---------------------------------------------------------------------------


async def _add_tenant(mssp_sessionmaker, org_id, state) -> Tenant:
    async with mssp_sessionmaker() as session:
        t = Tenant(
            slug=f"wrk{uuid4().hex[:6]}",
            display_name="Sweep",
            state=state,
            profile="poc",
            organization_id=org_id,
        )
        session.add(t)
        await session.commit()
        return t


async def _set_state(mssp_sessionmaker, tenant_id, state):
    async with mssp_sessionmaker() as session:
        await session.execute(
            text("UPDATE tenants SET state=:s WHERE id=:i"),
            {"s": state, "i": str(tenant_id)},
        )
        await session.commit()


async def test_drift_sweep_reconciles_only_live_tenants(
    mssp_sessionmaker, seeded_tenant: Tenant, monkeypatch
):
    # active + degraded get swept; archived (terminal) and provisioning
    # (transitional) do not.
    from soctalk.core.provisioning.controller import TenantController

    await _set_state(mssp_sessionmaker, seeded_tenant.id, TenantState.ACTIVE.value)
    degraded = await _add_tenant(
        mssp_sessionmaker, seeded_tenant.organization_id, TenantState.DEGRADED.value
    )
    archived = await _add_tenant(
        mssp_sessionmaker, seeded_tenant.organization_id, TenantState.ARCHIVED.value
    )
    provisioning = await _add_tenant(
        mssp_sessionmaker, seeded_tenant.organization_id,
        TenantState.PROVISIONING.value,
    )

    swept: list = []

    async def _record(self, tenant_id, *, actor_id=None):
        swept.append(tenant_id)

    monkeypatch.setattr(TenantController, "sync_state", _record)

    worker = ProvisioningWorker(mssp_sessionmaker)
    await worker._maybe_drift_sweep()

    assert set(swept) == {seeded_tenant.id, degraded.id}
    assert archived.id not in swept
    assert provisioning.id not in swept


async def test_drift_sweep_is_rate_limited(
    mssp_sessionmaker, seeded_tenant: Tenant, monkeypatch
):
    from soctalk.core.provisioning.controller import TenantController

    await _set_state(mssp_sessionmaker, seeded_tenant.id, TenantState.ACTIVE.value)
    calls = {"n": 0}

    async def _count(self, tenant_id, *, actor_id=None):
        calls["n"] += 1

    monkeypatch.setattr(TenantController, "sync_state", _count)

    worker = ProvisioningWorker(
        mssp_sessionmaker, drift_sweep_interval_seconds=3600
    )
    await worker._maybe_drift_sweep()
    await worker._maybe_drift_sweep()  # within interval -> no-op

    assert calls["n"] == 1


async def test_drift_sweep_skips_one_bad_tenant_and_continues(
    mssp_sessionmaker, seeded_tenant: Tenant, monkeypatch
):
    # A per-tenant sync_state failure is logged and skipped, not fatal.
    from soctalk.core.provisioning.controller import TenantController

    await _set_state(mssp_sessionmaker, seeded_tenant.id, TenantState.ACTIVE.value)
    other = await _add_tenant(
        mssp_sessionmaker, seeded_tenant.organization_id, TenantState.ACTIVE.value
    )
    seen: list = []

    async def _maybe_boom(self, tenant_id, *, actor_id=None):
        seen.append(tenant_id)
        if tenant_id == seeded_tenant.id:
            raise RuntimeError("boom")

    monkeypatch.setattr(TenantController, "sync_state", _maybe_boom)

    worker = ProvisioningWorker(mssp_sessionmaker)
    await worker._maybe_drift_sweep()  # must not raise

    assert set(seen) == {seeded_tenant.id, other.id}
