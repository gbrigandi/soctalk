"""Runs-worker-facing internal API.

The per-tenant L2 runs-worker calls these endpoints to claim active
investigation_runs, extend the lease while a graph invocation is in flight, and
post the terminal result. L1 owns workflow state; the worker only
executes locally and reports back over this protocol.

Authentication: tenant-bound runs-worker token signed by SocTalk.
Same signing key as the adapter token, distinguished by user_type +
scope claims.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.tenancy.context import tenant_context

logger = structlog.get_logger()

router = APIRouter(prefix="/api/internal/worker", tags=["internal-worker"])

LEASE_TTL_SECONDS = 60

# How long a transient-failed run waits before it becomes claimable again.
# Long enough to let a cold serverless endpoint finish warming; short enough
# that the alert is not stuck for minutes. Multiplied by the attempt count for
# a gentle backoff. Env-tunable so operators can widen the window to cover a
# cold start that includes a model download (no network volume).
def _env_positive_int(name: str, default: int, minimum: int = 1) -> int:
    """Parse an int env var, tolerating garbage and clamping to >= minimum.

    A raw int() here would (a) crash the router import on a non-int value like
    "15s"/"" and (b) accept 0/-5, which pushes not_before to now-or-past and
    makes a transient-released run instantly re-claimable — hammering a cold
    backend and burning the attempt cap with no warm-up window.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw.strip()))
    except (ValueError, AttributeError):
        logger.warning("bad_env_int name=%s value=%r -> default=%d", name, raw, default)
        return default


RETRY_BACKOFF_SECONDS = _env_positive_int("SOCTALK_RUN_RETRY_BACKOFF_SECONDS", 15)


async def _record_verdict_memo(
    db, tenant_id, investigation_id, *, decision: str, confidence: float
) -> None:
    """Cache the run's verdict keyed on the investigation's alert shape
    (issue #29). Pulls the shape from the primary alert's latest source
    event; no-op if there's no template to key on."""
    from soctalk.core.ir.memoization import record_verdict, shape_key

    row = (
        await db.execute(
            text(
                """
                SELECT a.source AS source, se.decoder AS decoder,
                       se.template_hash AS template_hash,
                       se.template_version AS template_version
                FROM alerts a
                LEFT JOIN LATERAL (
                    SELECT decoder, template_hash, template_version
                    FROM alert_source_events
                    WHERE alert_id = a.id
                    ORDER BY (template_hash IS NOT NULL) DESC, ingested_at DESC
                    LIMIT 1
                ) se ON true
                WHERE a.investigation_id = :c
                ORDER BY a.severity DESC, a.first_event_at DESC
                LIMIT 1
                """
            ),
            {"c": str(investigation_id)},
        )
    ).mappings().first()
    if row is None:
        return
    key = shape_key(
        source=row["source"], decoder=row["decoder"],
        template_hash=row["template_hash"], template_version=row["template_version"],
    )
    if key is None:
        return
    await record_verdict(
        db, tenant_id=tenant_id, key=key, decision=decision,
        confidence=confidence, template_hash=row["template_hash"],
    )



# Tenant daily spend cap helpers moved to ``soctalk.core.cost`` so the
# chat handler can enforce the same ceiling. Re-export for back-compat.
from soctalk.core.cost import (  # noqa: E402
    assert_tenant_daily_cap_ok,
)


def _db(request: Request) -> AsyncSession:
    session = getattr(request.state, "db", None)
    if session is None:
        raise HTTPException(500, "db session not attached to request")
    return session


async def close_fp_floor_veto(
    db: AsyncSession, *, tenant_id: UUID, investigation_id: UUID
) -> str | None:
    """Server-side safety floor for a worker ``close_fp`` (issues #43/#46).

    Returns the veto reason (kill switch, active key-sharing sibling, or spent
    close-volume cap) with the audit row written, or None when the close may
    commit. Only a still-active investigation can be vetoed into an escalate: if
    an analyst closed/merged the case while the run was in flight, the close path
    already no-ops via its ``WHERE status = 'active'`` guard, and converting it
    to an escalate would create a pending review on a closed investigation.
    Extracted so the veto logic is integration-testable without the HTTP/JWT
    plumbing.
    """
    from soctalk.core.ir.correlation import (
        find_other_active_investigation_sharing_keys,
    )
    from soctalk.core.ir.events import canonical_json
    from soctalk.core.ir.policies import effective_policy
    from soctalk.core.ir.triage import volume_cap_exceeded
    from soctalk.core.observability.audit import log_audit
    from soctalk.triage_policy.floor import (
        FLOOR_AUDIT_ACTION,
        VETO_ACTIVE_INCIDENT,
        VETO_KILL_SWITCH,
        VETO_VOLUME_CAP,
        auto_close_killed,
    )

    inv_status = (
        await db.execute(
            text("SELECT status FROM investigations WHERE id = :c AND tenant_id = :t"),
            {"c": str(investigation_id), "t": str(tenant_id)},
        )
    ).scalar_one_or_none()
    if inv_status != "active":
        return None

    policy = await effective_policy(db, tenant_id)
    veto: str | None = None
    detail: dict[str, Any] = {}
    if auto_close_killed(policy):
        veto = VETO_KILL_SWITCH
    else:
        other = await find_other_active_investigation_sharing_keys(
            db, tenant_id=tenant_id, investigation_id=investigation_id
        )
        if other is not None:
            veto = VETO_ACTIVE_INCIDENT
            detail["active_incident"] = str(other)
        elif await volume_cap_exceeded(db, tenant_id=tenant_id, policy=policy):
            veto = VETO_VOLUME_CAP
    if veto is None:
        return None

    await log_audit(
        db,
        action=FLOOR_AUDIT_ACTION,
        actor_principal="system",
        actor_id="worker_runs",
        tenant_id=tenant_id,
        resource_type="investigation",
        resource_id=str(investigation_id),
        # canonical_json: the same compact serializer the ingest plane uses, so
        # string-based audit filters match vetoes from every plane.
        notes=canonical_json({"veto": veto, "blocked": "worker_close_fp", **detail}),
    )
    return veto


def _verify_worker_jwt(request: Request) -> UUID:
    from soctalk.core.tenancy.auth import verify_worker_token

    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "worker JWT required")
    token = auth.split(" ", 1)[1].strip()
    identity = verify_worker_token(token)
    if identity is None:
        raise HTTPException(401, "invalid worker token")
    if identity.tenant_id is None:
        raise HTTPException(400, "worker token missing tenant_id")
    return identity.tenant_id


class ClaimedRun(BaseModel):
    run_id: UUID
    investigation_id: UUID
    tokens_used: int
    tokens_budget: int
    dollars_used: float = 0.0
    dollars_budget: float = 0.0
    lease_id: UUID
    lease_expires_at: datetime
    # Rates this run is priced at, resolved and frozen at run creation (#125).
    # Optional so a worker built against this model keeps working against an
    # API that predates it, and so a run created before the column existed
    # claims cleanly and prices the legacy way.
    price_snapshot: dict[str, Any] | None = None
    # ``alert`` is the primary (highest-severity) alert — kept for backward
    # compat. ``alerts`` is the full correlated set (issue #26): one run
    # reasons over every alert #27 grouped onto the investigation.
    alert: dict[str, Any]
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    # Store-primary authorization context (epic): the tenant's durable facts bound to this
    # alert's activity, built at claim time. None when no activity is extractable or the store
    # is empty; the worker then falls back to the fixture/claim authorization path.
    authorization_context: dict[str, Any] | None = None


class WorkerHeartbeatPayload(BaseModel):
    lease_id: UUID
    tokens_used: int = Field(ge=0)
    # ``None`` preserves the stored value (see CompletePayload).
    dollars_used: float | None = Field(default=None, ge=0.0)


class WorkerEventItem(BaseModel):
    kind: str = Field(max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: str = Field(default="mssp_only", max_length=32)
    client_ord: int = Field(ge=1)
    occurred_at: str | None = Field(default=None, max_length=64)


class WorkerEventsPayload(BaseModel):
    lease_id: UUID
    events: list[WorkerEventItem] = Field(default_factory=list, max_length=200)


class ReleasePayload(BaseModel):
    """Release a run back to the queue for a bounded retry on the SAME run_id
    after a TRANSIENT provider failure (e.g. a cold serverless endpoint). Not
    a terminal state: the run stays ``active`` unless the attempt cap is hit,
    in which case the server terminalizes it to ``failed`` itself."""

    lease_id: UUID
    error_category: str = Field(max_length=64)
    tokens_used: int = Field(default=0, ge=0)
    dollars_used: float | None = Field(default=None, ge=0.0)


class CompletePayload(BaseModel):
    lease_id: UUID
    status: str = Field(pattern=r"^(completed|halted_budget|failed)$")
    tokens_used: int = Field(ge=0)
    # ``None`` (or absent) means "the caller didn't track $; preserve
    # whatever the DB has". Important during mixed-version rolling
    # upgrades where the prior heartbeat may have persisted real spend.
    dollars_used: float | None = Field(default=None, ge=0.0)
    last_error: str | None = Field(default=None, max_length=4096)
    disposition: str | None = Field(
        default=None, pattern=r"^(close_fp|escalate|leave_open)$"
    )
    verdict_summary: str | None = Field(default=None, max_length=1024)
    verdict_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    findings: list[str] = Field(default_factory=list)
    enrichments: dict[str, Any] = Field(default_factory=dict)


@router.post("/runs/claim", response_model=ClaimedRun | None)
async def claim_run(request: Request) -> ClaimedRun | None:
    """Claim the oldest active run for the caller's tenant.

    Returns ``null`` (HTTP 200) when nothing is available — keeps the
    worker loop simple. Uses ``FOR UPDATE SKIP LOCKED`` so multiple
    replicas can poll without contention.
    """
    tenant_id = _verify_worker_jwt(request)
    worker_label = request.headers.get("X-Worker-Id") or "runs-worker"
    db = _db(request)

    async with tenant_context(db, tenant_id):
        # Circuit breaker: refuse to claim new runs once the tenant has
        # blown through its rolling 24h spend ceiling. Shared with the
        # chat path via ``soctalk.core.cost.assert_tenant_daily_cap_ok``
        # so a busy chat session can't dodge the worker's cap and a
        # flood of runs can't dodge the chat handler's cap.
        if await assert_tenant_daily_cap_ok(
            db, tenant_id, source="worker_claim"
        ) is None:
            return None

        row = (
            await db.execute(
                text(
                    """
                    SELECT r.id, r.investigation_id, r.tokens_used, r.tokens_budget,
                           r.dollars_used, r.dollars_budget, r.price_snapshot
                    FROM investigation_runs r
                    JOIN investigations i ON i.id = r.investigation_id
                                         AND i.tenant_id = r.tenant_id
                    WHERE r.tenant_id = :t
                      AND r.status = 'active'
                      AND r.not_before <= now()
                      AND (r.claimed_at IS NULL
                           OR r.lease_expires_at < now())
                      -- Don't claim a run whose investigation was closed out
                      -- from under it (e.g. merged away by an analyst,
                      -- review finding #4).
                      AND i.status = 'active'
                    ORDER BY r.started_at ASC
                    FOR UPDATE OF r SKIP LOCKED
                    LIMIT 1
                    """
                ),
                {"t": str(tenant_id)},
            )
        ).mappings().first()
        if row is None:
            return None

        lease_id = uuid4()
        lease_expires = datetime.now(UTC) + timedelta(
            seconds=LEASE_TTL_SECONDS
        )
        await db.execute(
            text(
                """
                UPDATE investigation_runs
                   SET claimed_by = :w,
                       claimed_at = now(),
                       lease_id = :lid,
                       lease_expires_at = :exp
                 WHERE id = :id
                """
            ),
            {
                "w": worker_label[:64],
                "lid": str(lease_id),
                "exp": lease_expires,
                "id": str(row["id"]),
            },
        )

        alert = (
            await db.execute(
                text(
                    """
                    SELECT a.id, a.source, a.rule_id, a.severity, a.signature,
                           a.source_event_ids, a.asset_ids, a.initial_iocs,
                           a.ai_assessment, a.description,
                           se.mitre AS mitre, se.rule_groups AS rule_groups,
                           se.entities AS entities
                    FROM alerts a
                    LEFT JOIN LATERAL (
                        SELECT mitre, rule_groups, entities
                        FROM alert_source_events
                        WHERE alert_id = a.id
                        -- Prefer a source event that actually carries rule
                        -- semantics: a later empty/coalesced v1 event must
                        -- not hide the MITRE/entities of an earlier one.
                        ORDER BY (mitre <> '{}'::jsonb OR entities <> '[]'::jsonb) DESC,
                                 ingested_at DESC
                        LIMIT 1
                    ) se ON true
                    WHERE a.investigation_id = :c
                    ORDER BY a.severity DESC, a.first_event_at DESC
                    """
                ),
                {"c": str(row["investigation_id"])},
            )
        ).mappings().all()
        if not alert:
            return ClaimedRun(
                run_id=row["id"],
                investigation_id=row["investigation_id"],
                tokens_used=row["tokens_used"],
                tokens_budget=row["tokens_budget"],
                dollars_used=float(row["dollars_used"] or 0.0),
                dollars_budget=float(row["dollars_budget"] or 0.0),
                lease_id=lease_id,
                lease_expires_at=lease_expires,
                price_snapshot=row["price_snapshot"],
                alert={
                    "id": str(row["id"]),
                    "rule": {"id": "n/a", "level": 0},
                },
            )

        alert_payloads = [
            {
                "id": str(a["id"]),
                # Which adapter produced this alert. Rule semantics are
                # source-specific (a Wazuh level 13 is not an identity-feed 13),
                # so anything downstream that reads severity must know the source.
                "adapter_source": a["source"] or "wazuh",
                "rule": {"id": a["rule_id"] or "?", "level": int(a["severity"] or 0)},
                "signature": a["signature"],
                # #17 fix 3: prefer the dedicated description column; fall back
                # to ai_assessment for rows written before v1_0018.
                "description": a["description"] or a["ai_assessment"],
                "source_event_ids": list(a["source_event_ids"] or []),
                "asset_ids": list(a["asset_ids"] or []),
                "initial_iocs": list(a["initial_iocs"] or []),
                # #17 fix 2/T6: rule semantics from the evidence store.
                "mitre": a["mitre"] or {},
                "rule_groups": list(a["rule_groups"] or []),
                "entities": list(a["entities"] or []),
            }
            for a in alert
        ]

        # Store-primary authorization: bind the tenant's durable facts to this alert's
        # activity now (the worker has no DB). Guarded — a failure never blocks a claim.
        authz_ctx: dict[str, Any] | None = None
        try:
            from soctalk.core.ir.authz_shadow import authorization_context_for_alert

            authz_ctx = await authorization_context_for_alert(
                db,
                tenant_id=tenant_id,
                source=alert[0]["source"],
                rule_id=alert[0]["rule_id"],
                entities=list(alert[0]["entities"] or []),
                ts=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 — authz load must never block a claim
            logger.warning("authz_context_load_failed", error=str(exc))

    return ClaimedRun(
        run_id=row["id"],
        investigation_id=row["investigation_id"],
        tokens_used=row["tokens_used"],
        tokens_budget=row["tokens_budget"],
        dollars_used=float(row["dollars_used"] or 0.0),
        dollars_budget=float(row["dollars_budget"] or 0.0),
        lease_id=lease_id,
        lease_expires_at=lease_expires,
        price_snapshot=row["price_snapshot"],
        alert=alert_payloads[0],
        alerts=alert_payloads,
        authorization_context=authz_ctx,
    )


@router.post("/runs/{run_id}/heartbeat")
async def heartbeat_run(
    run_id: UUID, payload: WorkerHeartbeatPayload, request: Request
) -> dict[str, Any]:
    """Extend the lease and persist current ``tokens_used`` + ``dollars_used``.

    Persisting both at heartbeat time is what keeps the per-run dollar
    cap honest across worker crashes: if the lease expires mid-run and
    a different worker picks the run back up, it must see the spend
    that was already incurred or it can blow through the cap a second
    time at no further cost to the runaway customer.
    """
    tenant_id = _verify_worker_jwt(request)
    db = _db(request)
    new_expiry = datetime.now(UTC) + timedelta(
        seconds=LEASE_TTL_SECONDS
    )
    async with tenant_context(db, tenant_id):
        result = await db.execute(
            text(
                """
                UPDATE investigation_runs
                   SET tokens_used = :u,
                       dollars_used = COALESCE(:d, dollars_used),
                       lease_expires_at = :exp
                 WHERE id = :id
                   AND tenant_id = :t
                   AND lease_id = :lid
                   AND status = 'active'
                """
            ),
            {
                "u": payload.tokens_used,
                "d": payload.dollars_used,
                "exp": new_expiry,
                "id": str(run_id),
                "t": str(tenant_id),
                "lid": str(payload.lease_id),
            },
        )
        if result.rowcount == 0:
            raise HTTPException(409, "lease expired or run not active")
    return {"ok": True, "lease_expires_at": new_expiry.isoformat()}


@router.post("/runs/{run_id}/events")
async def append_run_events(
    run_id: UUID, payload: WorkerEventsPayload, request: Request
) -> dict[str, Any]:
    """Append replay beats emitted by the graph plane (issue #72, Phase 0).

    Lease-checked with the same ownership predicate as heartbeat: a batch
    from a lost lease gets a 409 and the worker stops emitting (the
    reclaiming worker re-emits; per-lease idempotency keys dedupe). The
    server assigns ``seq`` via ``append_event``; ``client_ord`` only
    orders the batch and keys idempotency. Invalid kinds/visibilities are
    skipped with a log, never a 4xx — replay beats are enrichment and one
    bad item must not poison the batch.
    """
    from soctalk.core.ir.events import (
        RUN_SCOPED_WORKER_EVENTS,
        EventKind,
        append_event,
        run_scoped_event_idempotency_key,
        worker_event_idempotency_key,
    )
    from soctalk.core.ir.models import Visibility

    tenant_id = _verify_worker_jwt(request)
    db = _db(request)
    valid_visibilities = {v.value for v in Visibility}
    appended = 0
    async with tenant_context(db, tenant_id):
        row = (
            await db.execute(
                text(
                    """
                    SELECT investigation_id FROM investigation_runs
                     WHERE id = :id
                       AND tenant_id = :t
                       AND lease_id = :lid
                       AND status = 'active'
                    """
                ),
                {"id": str(run_id), "t": str(tenant_id), "lid": str(payload.lease_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(409, "lease expired or run not active")
        investigation_id = row["investigation_id"]

        for item in sorted(payload.events, key=lambda e: e.client_ord):
            try:
                kind = EventKind(item.kind)
            except ValueError:
                logger.warning(
                    "worker_event_unknown_kind", run_id=str(run_id), kind=item.kind
                )
                continue
            visibility = (
                item.visibility
                if item.visibility in valid_visibilities
                else Visibility.MSSP_ONLY.value
            )
            if kind in RUN_SCOPED_WORKER_EVENTS:
                # At-most-once per run: a reclaimed run re-runs the graph and
                # can re-cross the same threshold, but the beat is a per-run
                # fact — dedupe it independently of the (new) lease. (#103)
                key = run_scoped_event_idempotency_key(run_id=run_id, kind=kind)
            else:
                key = worker_event_idempotency_key(
                    run_id=run_id, lease_id=payload.lease_id, client_ord=item.client_ord
                )
            await append_event(
                db,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                run_id=run_id,
                kind=kind,
                payload={**item.payload, "occurred_at": item.occurred_at},
                visibility=visibility,
                idempotency_key=key,
                producer="runs_worker",
            )
            appended += 1
    return {"ok": True, "appended": appended}


@router.post("/runs/{run_id}/release")
async def release_run(
    run_id: UUID, payload: ReleasePayload, request: Request
) -> dict[str, Any]:
    """Release a leased run for a bounded retry after a TRANSIENT failure.

    Lease-checked exactly like complete/heartbeat (only the current lease
    holder can act). While under the attempt cap the run stays ``active``,
    its lease is cleared, ``attempts`` is bumped, ``not_before`` is pushed out
    by a per-attempt backoff, and it becomes claimable again on the SAME
    ``run_id`` — so no completed-run side effect (response dispatch, HIL
    review, memoization, follow-up runs; all keyed on run_id) ever replays.

    Once ``attempts`` reaches ``max_attempts`` the release terminalizes the
    run to ``failed`` instead, so a permanently-cold backend can't retry
    forever. The persisted ``dollars_used`` is preserved (COALESCE) so the
    per-run cap stays honest across retries, same contract as heartbeat.
    """
    tenant_id = _verify_worker_jwt(request)
    db = _db(request)
    async with tenant_context(db, tenant_id):
        row = (
            await db.execute(
                text(
                    """
                    UPDATE investigation_runs
                       SET attempts = attempts + 1,
                           last_error_category = :cat,
                           last_error = :err,
                           tokens_used = :u,
                           dollars_used = COALESCE(:d, dollars_used),
                           -- terminalize on the last attempt, else keep active
                           status = CASE WHEN attempts + 1 >= max_attempts
                                         THEN 'failed' ELSE 'active' END,
                           ended_at = CASE WHEN attempts + 1 >= max_attempts
                                           THEN now() ELSE ended_at END,
                           -- release the lease either way
                           lease_id = NULL,
                           lease_expires_at = NULL,
                           claimed_at = NULL,
                           claimed_by = NULL,
                           -- gentle per-attempt backoff before reclaim
                           -- (no-op once failed); scales with the new attempt count
                           not_before = now()
                               + make_interval(secs => :backoff * (attempts + 1))
                     WHERE id = :id
                       AND tenant_id = :t
                       AND lease_id = :lid
                       AND status = 'active'
                    RETURNING attempts, max_attempts, status
                    """
                ),
                {
                    "cat": payload.error_category[:64],
                    "err": f"transient:{payload.error_category}"[:4096],
                    "u": payload.tokens_used,
                    "d": payload.dollars_used,
                    "backoff": RETRY_BACKOFF_SECONDS,
                    "id": str(run_id),
                    "t": str(tenant_id),
                    "lid": str(payload.lease_id),
                },
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(409, "lease expired or run not active")
    terminal = row["status"] == "failed"
    logger.info(
        "run_released" if not terminal else "run_retry_exhausted",
        run_id=str(run_id),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        category=payload.error_category,
    )
    return {
        "ok": True,
        "retrying": not terminal,
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
    }


@router.post("/runs/{run_id}/complete")
async def complete_run(
    run_id: UUID, payload: CompletePayload, request: Request
) -> dict[str, Any]:
    """Mark the run terminal and optionally apply the verdict to the investigation.

    Idempotent on (run_id, lease_id). When ``disposition`` is supplied
    the parent ``cases`` row transitions atomically with the case_run:

      - ``close_fp`` → ``cases.status='auto_closed_fp'``, ``closed_at=now()``
      - ``escalate`` → leaves ``cases.status='active'``, sets ``severity``
        to at least 12 so the row sorts to the top of the MSSP queue
      - ``leave_open`` (or omitted) → no change to the investigation
    """
    from soctalk.core.ir import replay_events
    from soctalk.core.ir.events import append_event as _append_ir_event

    tenant_id = _verify_worker_jwt(request)
    db = _db(request)
    investigation_id: UUID | None = None
    case_changed = False
    async with tenant_context(db, tenant_id):
        row = (
            await db.execute(
                text(
                    """
                    UPDATE investigation_runs
                       SET status = :s,
                           tokens_used = :u,
                           dollars_used = COALESCE(:d, dollars_used),
                           last_error = :e,
                           ended_at = now(),
                           lease_id = NULL,
                           lease_expires_at = NULL,
                           claimed_at = NULL,
                           claimed_by = NULL
                     WHERE id = :id
                       AND tenant_id = :t
                       AND lease_id = :lid
                       AND status = 'active'
                    RETURNING investigation_id
                    """
                ),
                {
                    "s": payload.status,
                    "u": payload.tokens_used,
                    "d": payload.dollars_used,
                    "e": payload.last_error,
                    "id": str(run_id),
                    "t": str(tenant_id),
                    "lid": str(payload.lease_id),
                },
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(409, "lease expired or run not active")
        investigation_id = row["investigation_id"]

        # Safety floor, server side (issue #43): a ``close_fp`` must not commit
        # while another ACTIVE investigation shares an attach-eligible entity
        # with this one — the same active-incident veto the ingest plane
        # enforces, applied to the worker's close disposition where only L1
        # has the data to check it. The close becomes an escalate (audited),
        # never a silent drop.
        effective_disposition = payload.disposition
        floor_veto: str | None = None
        if payload.status == "completed" and payload.disposition == "close_fp":
            floor_veto = await close_fp_floor_veto(
                db, tenant_id=tenant_id, investigation_id=investigation_id
            )
            if floor_veto is not None:
                effective_disposition = "escalate"
                logger.warning(
                    "close_floor_veto",
                    run_id=str(run_id),
                    investigation_id=str(investigation_id),
                    veto=floor_veto,
                )
            # Replay beat (#72): the server-side floor is the authoritative
            # last gate — record its ruling (pass AND veto) in the same
            # transaction that acts on it. The worker cannot emit this one:
            # it runs after the graph finished.
            _floor_ev = replay_events.guard_evaluated(
                stage="server_floor",
                decision_in="close_fp",
                decision_out=effective_disposition,
                effect="pass" if floor_veto is None else "override",
                fired=[floor_veto] if floor_veto else [],
            )
            await _append_ir_event(
                db,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                run_id=run_id,
                kind=_floor_ev.kind,
                payload=_floor_ev.payload,
                visibility=_floor_ev.visibility,
                producer="complete_run",
            )

        if payload.status == "completed" and effective_disposition == "close_fp":
            # Write the reopen signature/window alongside the close so an
            # LLM-dismissed FP stays resurrectable by _check_and_reopen()
            # when the same entities show up again — same contract as the
            # rules-based auto_close_alert() path. Same transaction as the
            # close update; the WHERE status='active' guard means we never
            # stamp reopen fields onto an investigation someone else closed.
            from soctalk.core.ir.policies import effective_policy
            from soctalk.core.ir.triage import build_reopen_fields_for_investigation

            policy = await effective_policy(db, tenant_id)
            reopen_sig, reopen_until = await build_reopen_fields_for_investigation(
                db,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                reopen_window_days=policy.get("reopen_window_days", 30),
            )
            r = await db.execute(
                text(
                    """
                    UPDATE investigations
                       SET status = 'auto_closed_fp',
                           closed_at = now(),
                           close_reason = COALESCE(:reason, close_reason),
                           reopen_signature = CAST(:sig AS JSONB),
                           reopen_window_until = :reopen_until,
                           updated_at = now()
                     WHERE id = :c
                       AND tenant_id = :t
                       AND status = 'active'
                    """
                ),
                {
                    "reason": payload.verdict_summary,
                    "sig": reopen_sig,
                    "reopen_until": reopen_until,
                    "c": str(investigation_id),
                    "t": str(tenant_id),
                },
            )
            case_changed = (r.rowcount or 0) > 0

            # Persist the graph-plane triage policy audit trail for closes (issue #43):
            # escalates carry it into pending_reviews via enrichments, but a close
            # has no review row — without this, a deterministic operational close
            # would leave only free-text close_reason behind.
            _enr = payload.enrichments or {}
            triage_policy_audit = _enr.get("triage_policy_audit") or _enr.get("playbook_audit")
            if case_changed and triage_policy_audit:
                import json as _json

                from soctalk.core.observability.audit import log_audit
                from soctalk.triage_policy.floor import TRIAGE_POLICY_AUDIT_ACTION

                await log_audit(
                    db,
                    action=TRIAGE_POLICY_AUDIT_ACTION,
                    actor_principal="system",
                    actor_id="worker_runs",
                    tenant_id=tenant_id,
                    resource_type="investigation",
                    resource_id=str(investigation_id),
                    notes=_json.dumps(triage_policy_audit[:5], default=str)[:4096],
                )
            if case_changed:
                # Replay terminal beat (#72): the ending, recorded by the
                # plane that decided it, same transaction as the close. Path
                # taxonomy feeds the fleet-day aggregate.
                _path = (
                    "operational"
                    if any(
                        (a or {}).get("effect") == "deterministic_disposition"
                        for a in (triage_policy_audit or [])
                    )
                    else "reasoning"
                )
                _closed_ev = replay_events.case_closed(
                    path=_path,
                    reason=payload.verdict_summary,
                    run_id=str(run_id),
                )
                await _append_ir_event(
                    db,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    run_id=run_id,
                    kind=_closed_ev.kind,
                    payload=_closed_ev.payload,
                    visibility=_closed_ev.visibility,
                    producer="complete_run",
                )
        elif payload.status == "completed" and effective_disposition == "escalate":
            # Event-sourced HIL request: appends the canonical event
            # AND performs the V1-schema side effects (severity bump +
            # pending_reviews queue row) in the same transaction. See
            # ``soctalk.core.ir.review_events`` for the why behind
            # not using the legacy projector.
            from soctalk.core.ir.review_events import (
                record_human_review_requested,
            )

            # A floor-vetoed close arrives here with a close-shaped verdict
            # summary — say so, or the reviewer reads a "close as FP"
            # recommendation on an escalated case with no explanation.
            reason = payload.verdict_summary
            if payload.disposition == "close_fp":
                reason = (
                    f"[SAFETY FLOOR: {floor_veto} — worker close_fp overridden] "
                    f"{reason or ''}"
                ).strip()[:1024]
            await record_human_review_requested(
                db,
                investigation_id=investigation_id,
                tenant_id=tenant_id,
                reason=reason,
                verdict_decision="escalate",
                verdict_confidence=payload.verdict_confidence,
                findings=payload.findings,
                enrichments=payload.enrichments,
            )
            case_changed = True

        # Verdict memoization write (issue #29): cache this run's verdict
        # keyed on the investigation's alert shape, so a future recurrence
        # of the same shape can be closed by reference. Only meaningful when
        # the run produced a disposition + confidence.
        if (
            payload.status == "completed"
            and effective_disposition in ("close_fp", "escalate")
            and payload.verdict_confidence is not None
        ):
            # Memoize the EFFECTIVE disposition: a floor-vetoed close records
            # ``escalate`` so the shape can never close-by-reference later.
            await _record_verdict_memo(
                db, tenant_id, investigation_id,
                decision="close" if effective_disposition == "close_fp" else "escalate",
                confidence=float(payload.verdict_confidence),
            )

        # Response-playbook dispatch (issue #49): match + enqueue response
        # actions in THIS transaction — only here is the disposition post-floor
        # and committed-with. Under a SAVEPOINT so a dispatch failure can never
        # poison the completion (an aborted subtransaction would otherwise fail
        # every later statement and roll back the close itself).
        # ``case_changed`` gates dispatch on the disposition having actually
        # taken effect (Codex #49 High-1): an analyst who closed/merged the
        # investigation mid-run makes the close update a no-op, and a response
        # action must not fire on a state transition that never happened.
        if (
            payload.status == "completed"
            and case_changed
            and effective_disposition in ("close_fp", "escalate")
        ):
            try:
                from soctalk.response.dispatch import dispatch_for_completed_run

                async with db.begin_nested():
                    await dispatch_for_completed_run(
                        db,
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        run_id=run_id,
                        worker_disposition=payload.disposition,
                        effective_disposition=effective_disposition,
                        server_floor_veto=floor_veto,
                        verdict_summary=payload.verdict_summary,
                        verdict_confidence=payload.verdict_confidence,
                        enrichments=payload.enrichments,
                    )
            except Exception as exc:  # noqa: BLE001 — never fail a completion
                logger.warning(
                    "response_dispatch_failed",
                    run_id=str(run_id),
                    investigation_id=str(investigation_id),
                    error=str(exc)[:300],
                )

        # Follow-up run (review finding #2): if alerts correlated onto this
        # investigation WHILE the run was executing, they were invisible to
        # the snapshot the graph reasoned over. If the investigation is still
        # active (this run didn't close it) and evidence arrived, start a
        # fresh run over the now-complete alert set and clear the flag. The
        # just-completed run is terminal, so the single-active-run index
        # permits the new one.
        if payload.status == "completed":
            from soctalk.core.ir.runtime import start_run

            fu = (await db.execute(
                text("SELECT has_new_evidence, status FROM investigations "
                     "WHERE id = :c AND tenant_id = :t"),
                {"c": str(investigation_id), "t": str(tenant_id)},
            )).mappings().first()
            if fu and fu["has_new_evidence"] and fu["status"] == "active":
                await db.execute(
                    text("UPDATE investigations SET has_new_evidence = false "
                         "WHERE id = :c AND tenant_id = :t"),
                    {"c": str(investigation_id), "t": str(tenant_id)},
                )
                await start_run(db, tenant_id, investigation_id)
    logger.info(
        "case_run_completed",
        run_id=str(run_id),
        investigation_id=str(investigation_id) if investigation_id else None,
        tenant_id=str(tenant_id),
        status=payload.status,
        tokens_used=payload.tokens_used,
        disposition=payload.disposition,
        effective_disposition=effective_disposition,
        case_changed=case_changed,
    )
    return {"ok": True, "case_changed": case_changed}


async def reap_expired_leases(db: AsyncSession) -> int:
    """Reset stale claims so a crashed worker doesn't strand a run.

    Called periodically by the L1 lease-reaper background task.
    """
    result = await db.execute(
        text(
            """
            UPDATE investigation_runs
               SET claimed_by = NULL,
                   claimed_at = NULL,
                   lease_id = NULL,
                   lease_expires_at = NULL
             WHERE status = 'active'
               AND lease_expires_at IS NOT NULL
               AND lease_expires_at < now()
            """
        )
    )
    return result.rowcount or 0
