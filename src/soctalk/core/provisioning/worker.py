"""Provisioning worker.

Claims rows from ``provisioning_jobs``, instantiates :class:`TenantController`,
and drives ``provision``, ``reconcile``, or ``decommission`` to completion.
One worker per
replica; rows are claim-locked via ``SELECT ... FOR UPDATE SKIP LOCKED`` so
multiple replicas can run safely in parallel.

Not an investigation-domain outbox. Kept in the provisioning package because the
only thing it knows how to drive is ``TenantController``.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from soctalk.core.provisioning.controller import (
    ProvisionError,
    TenantController,
    TenantLifecycleError,
)
from soctalk.core.tenancy.models import ProvisioningJob, Tenant, TenantState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()


DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_STALE_CLAIM_SECONDS = 900.0  # 15 min — longer than wazuh readiness
DEFAULT_RECLAIM_INTERVAL_SECONDS = 60.0
# Drift sweep: reconcile every live tenant's DB state against its actual
# Kubernetes workloads (issue #104). Job-driven provisioning never notices an
# out-of-band namespace deletion or a stuck ``degraded`` tenant, so a periodic
# sweep runs sync_state() for each active/degraded tenant.
DEFAULT_DRIFT_SWEEP_INTERVAL_SECONDS = 300.0
# Hard cap on a single job execution. MUST stay below the stale-claim
# window: the local asyncio cancel has to fire before another worker
# reclaims the row, or the same job can end up running twice.
DEFAULT_JOB_TIMEOUT_SECONDS = 840.0

# Tenants in these states can never be provisioned or decommissioned
# again; running a queued job against one would at best fail the
# lifecycle assertions and at worst hang against deleted namespaces
# (a hung job blocks the whole queue — single coroutine per replica).
_TERMINAL_TENANT_STATES = frozenset(
    {TenantState.ARCHIVED.value, TenantState.PURGED.value}
)


class ProvisioningWorker:
    """Drives the provisioning job queue end-to-end.

    Caller owns the ``async_sessionmaker``; the worker opens one session
    per claim so transactions stay short.
    """

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession]",
        *,
        worker_id: str | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        stale_claim_seconds: float = DEFAULT_STALE_CLAIM_SECONDS,
        reclaim_interval_seconds: float = DEFAULT_RECLAIM_INTERVAL_SECONDS,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        drift_sweep_interval_seconds: float = DEFAULT_DRIFT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._sf = session_factory
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._poll_interval = poll_interval
        self._backoff_base = backoff_base
        self._stale_claim_seconds = stale_claim_seconds
        self._reclaim_interval_seconds = reclaim_interval_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._drift_sweep_interval_seconds = drift_sweep_interval_seconds
        self._stop_event = asyncio.Event()
        self._last_reclaim_at = 0.0
        self._last_drift_sweep_at = 0.0

    async def run_forever(self) -> None:
        logger.info("provisioning_worker_start", worker_id=self._worker_id)
        while not self._stop_event.is_set():
            try:
                # Reclaim before claiming: a crashed worker's in_flight row
                # may be blocking a replacement (partial unique index on the
                # active status set). Cheap — bounded by how many rows went
                # stale since last pass.
                await self._maybe_reclaim_stale()
                claimed = await self._claim_and_run_one()
                # Drift reconciliation runs only on idle cycles, so a backlog
                # of provisioning jobs always takes priority over the sweep.
                if not claimed:
                    await self._maybe_drift_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                # NEVER let a transient error (DB blip, k8s API hiccup)
                # escape the loop — that kills the worker task while the
                # API pod keeps serving, silently freezing the whole queue
                # until a manual pod restart. Log and keep polling.
                logger.exception(
                    "provisioning_worker_iteration_error",
                    worker_id=self._worker_id,
                )
                claimed = False
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stop_event.set()

    async def _maybe_reclaim_stale(self) -> None:
        """Flip ``in_flight`` rows whose claim has gone stale back to
        ``pending`` so a crashed worker doesn't strand a tenant.

        Runs at most every ``reclaim_interval_seconds`` to keep the hot
        path cheap. Does *not* reset ``attempts`` — a stale-reclaim
        counts toward the retry budget, same as any other failure.
        """
        now_monotonic = asyncio.get_event_loop().time()
        if (now_monotonic - self._last_reclaim_at) < self._reclaim_interval_seconds:
            return
        self._last_reclaim_at = now_monotonic

        cutoff = datetime.utcnow() - timedelta(seconds=self._stale_claim_seconds)
        async with self._sf() as session:
            result = await session.execute(
                update(ProvisioningJob)
                .where(ProvisioningJob.status == "in_flight")
                .where(ProvisioningJob.claimed_at < cutoff)
                .values(
                    status="pending",
                    claimed_at=None,
                    claimed_by=None,
                    last_error="reclaimed after stale claim",
                    next_attempt_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                .returning(ProvisioningJob.id)
            )
            reclaimed = result.scalars().all()
            await session.commit()

        if reclaimed:
            logger.warning(
                "provisioning_jobs_reclaimed",
                count=len(reclaimed),
                stale_after_seconds=self._stale_claim_seconds,
            )

    async def _maybe_drift_sweep(self) -> None:
        """Reconcile every live tenant's DB state against actual K8s workloads.

        Runs at most every ``drift_sweep_interval_seconds``. For each tenant in
        ``active`` or ``degraded`` (the states that must have a running data
        plane), invoke ``sync_state`` — which archives a tenant whose namespace
        has vanished, degrades one whose pods aren't Ready, and recovers a
        degraded one whose pods came back (issue #104). Job-driven provisioning
        alone never notices these; the sweep is what makes the reconciler run.
        Per-tenant failures are logged and skipped so one bad tenant never
        stalls the sweep or the queue.
        """
        now_monotonic = asyncio.get_event_loop().time()
        if (now_monotonic - self._last_drift_sweep_at) < (
            self._drift_sweep_interval_seconds
        ):
            return
        self._last_drift_sweep_at = now_monotonic

        async with self._sf() as session:
            tenant_ids = (
                (
                    await session.execute(
                        select(Tenant.id).where(
                            Tenant.state.in_(
                                (
                                    TenantState.ACTIVE.value,
                                    TenantState.DEGRADED.value,
                                    # so a committed decommission whose
                                    # namespace is already gone gets finalized.
                                    TenantState.DECOMMISSIONING.value,
                                )
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )

        for tenant_id in tenant_ids:
            try:
                async with self._sf() as session:
                    await TenantController(session).sync_state(
                        tenant_id, actor_id=f"drift-sweep:{self._worker_id}"
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "drift_sweep_tenant_failed", tenant_id=str(tenant_id)
                )

    async def _claim_and_run_one(self) -> bool:
        """Try to claim a single due job; run it to completion.

        Returns True if a job was processed (success or failure), False if
        the queue was empty.
        """
        async with self._sf() as session:
            job = await self._claim(session)
            if job is None:
                return False

        # Run outside the claim transaction: the controller manages its
        # own txn boundaries.
        await self._run_job(job)
        return True

    async def _claim(self, session: "AsyncSession") -> ProvisioningJob | None:
        """Atomically claim one due job. Returns None if nothing is due."""
        now = datetime.utcnow()
        # SKIP LOCKED lets multiple workers compete without blocking.
        result = await session.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.status == "pending")
            .where(ProvisioningJob.next_attempt_at <= now)
            .order_by(ProvisioningJob.next_attempt_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if job is None:
            return None

        job.status = "in_flight"
        job.claimed_at = now
        job.claimed_by = self._worker_id
        job.attempts += 1
        job.updated_at = now
        await session.commit()
        logger.info(
            "provisioning_job_claimed",
            job_id=str(job.id),
            tenant_id=str(job.tenant_id),
            kind=job.kind,
            attempt=job.attempts,
        )
        return job

    async def _tenant_state(self, tenant_id) -> str | None:
        async with self._sf() as session:
            return (
                await session.execute(
                    select(Tenant.state).where(Tenant.id == tenant_id)
                )
            ).scalar_one_or_none()

    async def _dispatch(self, job: ProvisioningJob) -> None:
        async with self._sf() as session:
            controller = TenantController(session)
            if job.kind == "tenant.provision":
                await controller.provision(
                    job.tenant_id,
                    actor_id=f"worker:{self._worker_id}",
                )
            elif job.kind == "tenant.reconcile":
                # Re-render + helm upgrade of an ACTIVE tenant's release
                # without lifecycle transitions (chart-affecting LLM
                # edits). Raises TenantLifecycleError for non-active
                # tenants — handled by the caller like any other failure,
                # so retry/backoff bookkeeping applies identically.
                await controller.reconcile(
                    job.tenant_id,
                    actor_id=f"worker:{self._worker_id}",
                )
            elif job.kind == "tenant.decommission":
                await controller.decommission(
                    job.tenant_id,
                    actor_id=f"worker:{self._worker_id}",
                    force=False,
                )
            else:
                raise ProvisionError(
                    f"unknown job kind: {job.kind}",
                    step="dispatch",
                )

    async def _run_job(self, job: ProvisioningJob) -> None:
        """Execute a claimed job; record outcome in a fresh session."""
        # A job may have been enqueued before its tenant was archived or
        # purged. Abandon it terminally instead of running it.
        state = await self._tenant_state(job.tenant_id)
        if state in _TERMINAL_TENANT_STATES:
            await self._record_failure(
                job.id,
                f"tenant in terminal state {state!r}; job abandoned",
                terminal=True,
            )
            logger.warning(
                "provisioning_job_abandoned_terminal_tenant",
                job_id=str(job.id),
                tenant_id=str(job.tenant_id),
                tenant_state=state,
            )
            return

        try:
            await asyncio.wait_for(
                self._dispatch(job), timeout=self._job_timeout_seconds
            )
        except asyncio.TimeoutError:
            # The controller tolerates re-entry from ``provisioning``
            # state, so a cancelled-mid-flight job is safe to retry via
            # the normal backoff path.
            await self._record_failure(
                job.id,
                f"job timed out after {self._job_timeout_seconds:.0f}s",
            )
            logger.error(
                "provisioning_job_timeout",
                job_id=str(job.id),
                tenant_id=str(job.tenant_id),
                kind=job.kind,
                timeout_seconds=self._job_timeout_seconds,
            )
            return
        except (ProvisionError, TenantLifecycleError) as e:
            await self._record_failure(job.id, str(e))
            logger.warning(
                "provisioning_job_failed",
                job_id=str(job.id),
                tenant_id=str(job.tenant_id),
                error=str(e),
                step=getattr(e, "step", None),
            )
            return
        except Exception as e:  # noqa: BLE001
            await self._record_failure(job.id, f"unexpected: {e}")
            logger.exception(
                "provisioning_job_unexpected_error",
                job_id=str(job.id),
                tenant_id=str(job.tenant_id),
            )
            return

        await self._record_success(job.id)
        logger.info(
            "provisioning_job_succeeded",
            job_id=str(job.id),
            tenant_id=str(job.tenant_id),
        )

    async def _record_success(self, job_id) -> None:
        async with self._sf() as session:
            await session.execute(
                update(ProvisioningJob)
                .where(ProvisioningJob.id == job_id)
                .values(
                    status="succeeded",
                    last_error=None,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()

    async def _record_failure(
        self, job_id, error_msg: str, *, terminal: bool = False
    ) -> None:
        async with self._sf() as session:
            # Fetch to decide terminal vs retriable.
            row = (
                await session.execute(
                    select(ProvisioningJob).where(ProvisioningJob.id == job_id)
                )
            ).scalar_one()

            values: dict = {
                "last_error": error_msg[:2000],
                "claimed_at": None,
                "claimed_by": None,
                "updated_at": datetime.utcnow(),
            }
            if terminal or row.attempts >= row.max_attempts:
                values["status"] = "failed"
                # Leave next_attempt_at untouched — nothing will run again.
            else:
                values["status"] = "pending"
                # Capped exponential backoff.
                backoff = self._backoff_base * (2 ** (row.attempts - 1))
                values["next_attempt_at"] = datetime.utcnow() + timedelta(
                    seconds=min(backoff, 1800)
                )

            await session.execute(
                update(ProvisioningJob)
                .where(ProvisioningJob.id == job_id)
                .values(**values)
            )
            await session.commit()


__all__ = ["ProvisioningWorker"]
