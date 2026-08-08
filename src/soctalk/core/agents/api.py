"""L2-agent wire protocol endpoints (v0).

Same five-verb protocol soctalk-cloud exposes for L0→L1, applied one
level down: the MSSP's SocTalk (L1) is the control plane for tenant
agents running in tenant clusters (L2).

Token resolution:

- ``/register``: bootstrap token only. Burned on success.
- every other endpoint: runtime token only. Argon2-verified against
  the small set of un-revoked runtime tokens.

If you change behaviour here, update the spec first. The agent binary
is the same one that talks to L0 — protocol drift between L0↔agent and
L1↔agent would split the binary into two codebases.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from soctalk.core.agents.models import (
    AgentJob,
    AgentJobEvent,
    TenantInstallation,
    TenantInstallationBootstrapToken,
    TenantInstallationEvent,
    TenantInstallationHeartbeat,
    TenantInstallationRuntimeToken,
)
from soctalk.core.agents.tokens import hash_token, mint_token, verify_token
from soctalk.core.tenancy.context import tenant_context


logger = structlog.get_logger()
router = APIRouter(prefix="/api/agent", tags=["l2-agent"])


HEARTBEAT_INTERVAL_SECONDS = 60
CLAIM_MAX_WAIT_SECONDS = 30
CLAIM_POLL_INTERVAL_SECONDS = 1.0
# Claim older than this without a completion is treated as abandoned
# (agent crash, network partition, restart mid-job). On the next claim
# attempt it's reset back to ``pending`` so a fresh claim can pick it
# up. Must be > longest expected in-flight job duration.
STALE_CLAIM_THRESHOLD_SECONDS = 15 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Wire schemas (keep aligned with soctalk-cloud's agent.py)
# ---------------------------------------------------------------------------


class RegisterBody(BaseModel):
    cluster_label: str = Field(max_length=128)
    agent_version: str = Field(max_length=64)
    kubernetes_version: str | None = Field(default=None, max_length=64)
    node_count: int | None = Field(default=None)


class RegisterResponse(BaseModel):
    installation_id: str
    runtime_token: str
    heartbeat_interval_seconds: int
    claim_max_wait_seconds: int


class ClaimResponse(BaseModel):
    job_id: str
    kind: str
    idempotency_key: str
    spec: dict[str, Any]


class EventBody(BaseModel):
    seq: int = Field(ge=1)
    event_type: str = Field(max_length=64)
    timestamp: datetime
    step: str | None = Field(default=None, max_length=128)
    detail: dict[str, Any] = Field(default_factory=dict)


class CompleteBody(BaseModel):
    outcome: str  # 'success' | 'failed'
    error_code: str | None = None
    summary: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class HeartbeatBody(BaseModel):
    timestamp: datetime
    agent_version: str | None = None
    reported_chart_version: str | None = None
    reported_state: str | None = None


# ---------------------------------------------------------------------------
# Token extraction + lookup
# ---------------------------------------------------------------------------


def _extract_bearer(auth_header: str | None) -> str:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return auth_header.split(" ", 1)[1].strip()


async def _consume_bootstrap(
    db, plaintext: str
) -> TenantInstallationBootstrapToken:
    """Find + consume a valid bootstrap token by plaintext.

    Bootstrap tokens are single-use. Scan un-consumed/un-revoked/un-expired
    rows, argon2-verify each, and on match issue a conditional UPDATE
    (compare-and-set on ``consumed_at IS NULL``) so two concurrent
    requests racing on the same token can only have one winner — the
    other gets 0 rows back and we treat it as already-consumed.
    """
    from sqlalchemy import update as _update

    now = _now()
    rows = (
        await db.execute(
            select(TenantInstallationBootstrapToken)
            .where(TenantInstallationBootstrapToken.consumed_at.is_(None))
            .where(TenantInstallationBootstrapToken.revoked_at.is_(None))
            .where(TenantInstallationBootstrapToken.expires_at > now)
        )
    ).scalars().all()
    for row in rows:
        if not verify_token(row.token_hash, plaintext):
            continue
        result = await db.execute(
            _update(TenantInstallationBootstrapToken)
            .where(TenantInstallationBootstrapToken.id == row.id)
            .where(TenantInstallationBootstrapToken.consumed_at.is_(None))
            .where(TenantInstallationBootstrapToken.revoked_at.is_(None))
            .where(TenantInstallationBootstrapToken.expires_at > now)
            .values(consumed_at=now)
            .returning(TenantInstallationBootstrapToken.id)
        )
        if result.scalar_one_or_none() is None:
            # Lost the race — another caller consumed it microseconds ago.
            raise HTTPException(409, "bootstrap token already consumed")
        await db.refresh(row)
        return row
    raise HTTPException(401, "bootstrap token invalid or expired")


async def _resolve_runtime(
    db, plaintext: str
) -> TenantInstallationRuntimeToken:
    rows = (
        await db.execute(
            select(TenantInstallationRuntimeToken)
            .where(TenantInstallationRuntimeToken.revoked_at.is_(None))
        )
    ).scalars().all()
    for row in rows:
        if verify_token(row.token_hash, plaintext):
            row.last_used_at = _now()
            await db.flush()
            return row
    raise HTTPException(401, "runtime token invalid")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _build_install_helm_release_spec(
    db, installation: TenantInstallation,
) -> dict[str, Any]:
    """Assemble the install_helm_release job spec for an L2 tenant.

    Goes through :func:`soctalk.core.provisioning.render.render_tenant_values`
    so the values dict matches the soctalk-tenant chart contract exactly
    (``tenant.slug / msspId / installId`` present, no stray keys). Sets
    ``create_namespace=true`` — the executor treats a missing flag as
    false, and a clean L2 cluster has no tenant namespace owner.

    Secret handling: the legacy in-cluster controller materialized the
    adapter-token Secret and the LLM API key Secret via client-go before
    running Helm. In the cross-cluster (L2 agent) model we carry the
    plaintext values inside the install spec and rely on the chart to
    create the Secret objects from Helm values. Trust boundary is the
    same as the runtime token: it already transits through agent_jobs.
    Chart-side work (Secret templates + values.schema.json) is tracked
    separately; this helper pins the payload shape.

    The ``soctalkSystem`` block gives the tenant-side adapter the hint
    it needs to reach back to L1 in the cross-cluster case (URL +
    tenant's adapter token). When the tenant is co-located with L1 the
    chart falls back to its in-cluster defaults.
    """
    import os

    from soctalk.core.provisioning.render import (
        render_tenant_values,
        render_wazuh_values,
    )
    from soctalk.core.tenancy.auth import mint_adapter_token
    from soctalk.core.tenancy.models import (
        BrandingConfig,
        IntegrationConfig,
        Organization,
    )
    from soctalk.core.tenancy.models import Tenant as _Tenant

    # RLS: IntegrationConfig + BrandingConfig + Organization are tenant-scoped
    # tables. The agent-callback session has no caller-tenant context, so
    # without this wrap RLS hides the rows even though they exist.
    async with tenant_context(db, installation.tenant_id):
        tenant = (
            await db.execute(
                select(_Tenant).where(_Tenant.id == installation.tenant_id)
            )
        ).scalar_one()
        organization = (
            await db.execute(
                select(Organization).where(Organization.id == tenant.organization_id)
            )
        ).scalar_one()
        integration = (
            await db.execute(
                select(IntegrationConfig).where(
                    IntegrationConfig.tenant_id == tenant.id
                )
            )
        ).scalar_one()
        branding = (
            await db.execute(
                select(BrandingConfig).where(BrandingConfig.tenant_id == tenant.id)
            )
        ).scalar_one()

    llm_secret_name = f"tenant-{tenant.id}-llm"

    values = render_tenant_values(
        tenant=tenant,
        integration=integration,
        branding=branding,
        mssp_id=str(organization.mssp_id),
        install_id=str(organization.install_id),
        llm_secret_name=llm_secret_name,
        profile=tenant.profile,  # 'poc' | 'persistent' | 'legacy'
    )

    # Cross-cluster: tell the adapter how to reach L1 + pass its JWT.
    # Chart-side TODO: consume these values and materialize a Secret
    # + rewrite the adapter env to point at soctalkSystem.url instead
    # of the hardcoded in-cluster service DNS.
    adapter_jwt = mint_adapter_token(tenant.id)
    system_block: dict[str, Any] = {
        "url": os.getenv(
            "SOCTALK_L1_PUBLIC_URL", "http://host.docker.internal:8000"
        ),
        "adapterToken": adapter_jwt,
    }
    # Pod-level /etc/hosts for tenants whose cluster DNS can't resolve the
    # MSSP hostname (Tailscale MagicDNS off, on-prem split-horizon DNS).
    # SOCTALK_L1_HOST_ALIASES is a comma-list of ``ip=hostname`` pairs — the
    # install-shared operator hint. Multiple hostnames for the same IP go
    # in as ``ip=host1,host2``. Rendered into pod.spec.hostAliases on the
    # adapter + runs-worker via the chart's ``soctalkSystem.hostAliases``.
    raw = os.getenv("SOCTALK_L1_HOST_ALIASES", "").strip()
    if raw:
        entries: list[dict[str, Any]] = []
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            ip, hosts = pair.split("=", 1)
            names = [h.strip() for h in hosts.split(",") if h.strip()]
            if ip.strip() and names:
                entries.append({"ip": ip.strip(), "hostnames": names})
        if entries:
            system_block["hostAliases"] = entries
    # TLS verification for the tenant adapter's connection back to L1. When L1
    # serves a self-signed cert (launchpad demo / pending launchpad-owned certs)
    # the operator sets SOCTALK_L1_VERIFY_SSL=false so the adapter can heartbeat
    # + forward alerts without cert validation. Rendered into the tenant chart's
    # soctalkSystem.verifySsl (-> the adapter's SOCTALK_API_VERIFY_SSL env).
    # Only an explicit false/0 disables it; absence leaves the chart default on.
    verify_raw = os.getenv("SOCTALK_L1_VERIFY_SSL", "").strip().lower()
    if verify_raw in {"false", "0"}:
        system_block["verifySsl"] = False
    elif verify_raw in {"true", "1"}:
        system_block["verifySsl"] = True
    values["soctalkSystem"] = system_block
    # The runs-worker pod loads its bearer JWT from
    # ``runsWorker.tokenSecretRef`` (name=runs-worker-token by default).
    # In the same-cluster controller path this Secret is created by
    # ``_write_worker_token``; in the cross-cluster agent path the
    # tenant chart must materialize it from values. Mint it here so
    # the chart's 25-secrets.yaml emits the Secret alongside the
    # adapter token.
    from soctalk.core.tenancy.auth import mint_worker_token
    values.setdefault("runsWorker", {})["token"] = mint_worker_token(tenant.id)
    # LLM API key plaintext (the chart creates the Secret named by
    # ``llm.apiKeyRef.name``). Precedence:
    #   1. IntegrationConfig.llm_api_key_plain — per-tenant Postgres
    #      column, set by PATCH /api/mssp/tenants/{id}/llm. This is
    #      the intended production path.
    #   2. SOCTALK_DEFAULT_LLM_API_KEY env — install-wide dev fallback.
    #      Keeps existing tests + local runs working when no tenant-
    #      specific key is set.
    #   3. empty string — chart guards on apiKey truthiness and simply
    #      doesn't create the Secret when unset, so the legacy
    #      pre-provisioned-Secret path still works.
    # LLM API key plaintext. Precedence:
    #   1. IntegrationConfig.llm_api_key_plain — per-tenant Postgres column.
    #   2. SOCTALK_DEFAULT_LLM_API_KEY env — install-wide dev fallback.
    #   3. The soctalk-system install Secret named by
    #      SOCTALK_INSTALL_LLM_SECRET_NAME (whose key is provider-derived:
    #      anthropic-api-key / openai-api-key). Read at spec-build time so
    #      install.sh / chart operators don't have to also export the env.
    #   4. empty string — chart skips Secret creation; legacy
    #      pre-provisioned-Secret path still works.
    llm_key = (
        integration.llm_api_key_plain
        or os.getenv("SOCTALK_DEFAULT_LLM_API_KEY", "").strip()
        or await _read_install_llm_key(integration.llm_provider)
        or ""
    )
    if not integration.llm_api_key_plain and llm_key:
        import structlog
        structlog.get_logger().info(
            "install_spec_llm_key_from_install_fallback",
            tenant_id=str(tenant.id),
            reason="per_tenant_key_unset",
        )
    values["llm"]["apiKey"] = llm_key

    # Wazuh + linux-ep subchart values. The cross-cluster agent runs a single
    # ``install_helm_release`` per tenant; for the in-cluster SOC bundle to
    # come up with adapter+runs-worker+wazuh+linux-ep in one go, the parent
    # soctalk-tenant chart pulls them as subcharts (deps gated on
    # components.<name>.enabled). We layer per-tenant credentials + service
    # wiring here so the chart's subchart values aren't left at chart-default
    # placeholders.
    # Per-tenant namespace + release name. The legacy controller derives
    # both per tenant; keep the same shape for cross-tooling consistency.
    namespace = f"tenant-{tenant.slug}"
    release_name = f"tenant-{tenant.slug}"

    wazuh_block: dict[str, Any] | None = None
    if values["components"]["wazuh"]["enabled"]:
        from soctalk.core.provisioning.secrets_gen import generate_bootstrap_secrets
        # Idempotency: mint fresh every spec build. A rebuild on :retry will
        # produce new passwords; the wazuh chart's templates write the creds
        # Secret from values at install time, so the live deployment picks
        # them up on the next reconcile.
        bootstrap = generate_bootstrap_secrets()
        wazuh_block = render_wazuh_values(
            tenant=tenant,
            profile=tenant.profile or "poc",
            admin_password=bootstrap.wazuh_admin_pw,
            authd_password=bootstrap.wazuh_authd_secret,
        )
        values["wazuh"] = wazuh_block
        # render_tenant_values assumes wazuh is a SEPARATE helm release
        # named ``wazuh-<slug>`` and points the adapter at
        # ``wazuh-<slug>-wazuh-*`` resources. In the L2 cross-cluster path
        # wazuh runs as a SUBCHART of the tenant release, so its resources
        # are prefixed with the PARENT release name (``tenant-<slug>-wazuh-*``).
        # Rewrite the adapter's wiring here — the single-cluster
        # ``render_tenant_values`` output is only correct for the sync path
        # that installs wazuh as its own release.
        values["adapter"]["wazuhIndexer"]["url"] = (
            f"https://{release_name}-wazuh-indexer:9200"
        )
        values["adapter"]["wazuhIndexer"]["credsSecret"] = (
            f"{release_name}-wazuh-creds"
        )
        # Same rewrite for the runs-worker's Wazuh MCP enrichment wiring
        # (issue #109): render_tenant_values points it at the standalone
        # ``wazuh-<slug>-wazuh-*`` release, but on the L2 subchart path those
        # resources are ``<release>-wazuh-*``. Without this the worker binds
        # against non-existent Services/Secrets and enrichment silently no-ops.
        rw_wazuh = values.get("runsWorker", {}).get("wazuh")
        if isinstance(rw_wazuh, dict) and rw_wazuh.get("enabled"):
            rw_wazuh["apiUrl"] = f"https://{release_name}-wazuh-manager:55000"
            rw_wazuh["indexerHost"] = f"{release_name}-wazuh-indexer"
            rw_wazuh["credsSecret"] = f"{release_name}-wazuh-creds"

    if values["components"]["linuxep"]["enabled"]:
        from soctalk.core.provisioning.render import render_linux_ep_values
        # When wazuh is a subchart of this release, its Service is
        # ``<release>-wazuh-manager`` per the chart's standard naming.
        # authd password lives in the wazuh chart's generated creds Secret
        # under the ``wazuh_authd_secret`` key.
        values["linuxep"] = render_linux_ep_values(
            tenant=tenant,
            wazuh_manager_host=f"{release_name}-wazuh-manager",
            authd_secret_name=f"{release_name}-wazuh-creds",
            # The wazuh chart's ``<release>-wazuh-creds`` Secret stores the
            # authd password under key ``AUTHD_PASS`` (uppercased, matching
            # the wazuh chart's Secret template). Not ``wazuh_authd_secret``
            # — that's the KEY in the parent-owned ``tenant-bootstrap`` Secret
            # the single-cluster controller writes but the wazuh chart
            # doesn't reference.
            authd_secret_key="AUTHD_PASS",
        )

    return {
        "chart_ref": installation.desired_chart_ref,
        "chart_version": installation.desired_chart_version,
        "release_name": release_name,
        "namespace": namespace,
        "create_namespace": True,
        "values": values,
    }


async def _read_install_llm_key(provider: str | None) -> str:
    """Read the install-shared LLM API key from the soctalk-system Secret.

    The Secret name comes from ``SOCTALK_INSTALL_LLM_SECRET_NAME`` (the chart
    sets it; install.sh's ``create_llm_secret`` populates both
    ``anthropic-api-key`` and ``openai-api-key`` keys). The key picked here
    is provider-derived:

      - ``anthropic``  → ``anthropic-api-key``
      - anything else  → ``openai-api-key``

    Falls back to either-or-empty silently — any failure returns "" so the
    spec builder lets the chart's ``if .Values.llm.apiKey`` guard skip
    Secret creation. Best-effort only; per-tenant or env override are still
    the primary paths.
    """
    try:
        secret_name = os.getenv("SOCTALK_INSTALL_LLM_SECRET_NAME", "").strip()
        secret_ns = os.getenv("SOCTALK_SYSTEM_NAMESPACE", "soctalk-system").strip()
        if not secret_name:
            return ""
        key_name = (
            "anthropic-api-key" if (provider or "").lower() == "anthropic"
            else "openai-api-key"
        )
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            try:
                await k8s_config.load_kube_config()
            except Exception:
                return ""
        v1 = k8s_client.CoreV1Api()
        try:
            sec = await v1.read_namespaced_secret(secret_name, secret_ns)
        finally:
            await v1.api_client.close()
        import base64
        data = sec.data or {}
        raw = (
            data.get(key_name)
            or data.get("anthropic-api-key")
            or data.get("openai-api-key")
            or ""
        )
        return base64.b64decode(raw).decode() if raw else ""
    except Exception:
        return ""


async def _enqueue_agent_job(
    db,
    *,
    installation_id,
    kind: str,
    idempotency_key: str,
    spec: dict[str, Any],
) -> None:
    """Insert an AgentJob row if one with the same idempotency key isn't
    already present for this installation. Commits so the job survives a
    subsequent per-request rollback in the same worker cycle.
    """
    existing = (
        await db.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        AgentJob(
            installation_id=installation_id,
            kind=kind,
            idempotency_key=idempotency_key,
            spec=spec,
            status="pending",
        )
    )
    await db.commit()


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    plaintext = _extract_bearer(authorization)
    db = request.state.db

    bootstrap = await _consume_bootstrap(db, plaintext)
    installation = (
        await db.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == bootstrap.installation_id
            )
        )
    ).scalar_one()

    # First-contact state update.
    installation.cluster_label = body.cluster_label
    installation.agent_version = body.agent_version
    installation.agent_last_seen = _now()

    just_connected = False
    if installation.state == "pending":
        db.add(
            TenantInstallationEvent(
                installation_id=installation.id,
                event_type="agent_registered",
                from_state="pending",
                to_state="agent_connected",
                actor_id=f"agent:{body.cluster_label}",
                details={
                    "agent_version": body.agent_version,
                    "kubernetes_version": body.kubernetes_version,
                    "node_count": body.node_count,
                },
            )
        )
        installation.state = "agent_connected"
        installation.state_changed_at = _now()
        just_connected = True

    # Rotate runtime tokens: revoke prior active, mint fresh.
    await db.execute(
        update(TenantInstallationRuntimeToken)
        .where(
            TenantInstallationRuntimeToken.installation_id == installation.id
        )
        .where(TenantInstallationRuntimeToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    runtime_plain = mint_token()
    db.add(
        TenantInstallationRuntimeToken(
            installation_id=installation.id,
            token_hash=hash_token(runtime_plain),
        )
    )
    await db.commit()

    # First-time connect: enqueue the preflight job. Second and later
    # registrations (token rotations) skip — the queue already has whatever
    # was pending. Keyed by installation-id so a re-register doesn't
    # double-enqueue.
    if just_connected:
        await _enqueue_agent_job(
            db,
            installation_id=installation.id,
            kind="preflight",
            idempotency_key=f"preflight:{installation.id}",
            spec={
                "required_apis": [
                    "apps/v1",
                    "networking.k8s.io/v1",
                ],
                "min_kube_version": "1.28",
            },
        )

    return RegisterResponse(
        installation_id=str(installation.id),
        runtime_token=runtime_plain,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        claim_max_wait_seconds=CLAIM_MAX_WAIT_SECONDS,
    )


async def _reclaim_stale_for_installation(db, installation_id) -> int:
    """Reset ``in_flight`` jobs older than the stale threshold back to
    ``pending`` so a subsequent claim can pick them up.

    Returns the number of rows reclaimed. Emits a lifecycle event per
    reclaim so the timeline records the recovery (otherwise a crashed
    agent's job looks identical to a successful one in audit).
    """
    from datetime import timedelta

    cutoff = _now() - timedelta(seconds=STALE_CLAIM_THRESHOLD_SECONDS)
    stale_rows = (
        await db.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.status == "in_flight")
            .where(AgentJob.claimed_at < cutoff)
        )
    ).scalars().all()
    if not stale_rows:
        return 0
    for job in stale_rows:
        job.status = "pending"
        job.claimed_at = None
        db.add(
            TenantInstallationEvent(
                installation_id=installation_id,
                event_type="agent_job_reclaimed",
                from_state=None,
                to_state=None,
                actor_id="controller",
                details={
                    "job_id": str(job.id),
                    "kind": job.kind,
                    "reason": "stale_claim_timeout",
                },
            )
        )
    await db.commit()
    return len(stale_rows)


@router.post("/jobs:claim")
async def claim_job(
    request: Request,
    wait: int = 30,
    authorization: str | None = Header(default=None),
):
    plaintext = _extract_bearer(authorization)
    db = request.state.db
    token_row = await _resolve_runtime(db, plaintext)
    wait = min(max(wait, 0), CLAIM_MAX_WAIT_SECONDS)

    # Before attempting to pick up work, reclaim any rows that are
    # in_flight-but-stale for this installation. Covers the common case
    # where the agent crashed mid-job and is now re-polling fresh.
    await _reclaim_stale_for_installation(db, token_row.installation_id)

    deadline = asyncio.get_event_loop().time() + wait
    while True:
        job = (
            await db.execute(
                select(AgentJob)
                .where(AgentJob.installation_id == token_row.installation_id)
                .where(AgentJob.status == "pending")
                .order_by(AgentJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is not None:
            job.status = "in_flight"
            job.claimed_at = _now()
            await db.commit()
            return ClaimResponse(
                job_id=str(job.id),
                kind=job.kind,
                idempotency_key=job.idempotency_key,
                spec=job.spec,
            )
        # Release the row-lock transaction before sleeping, otherwise
        # other workers (future) would block on nothing.
        await db.commit()

        if asyncio.get_event_loop().time() >= deadline:
            from fastapi.responses import Response
            return Response(status_code=204)
        await asyncio.sleep(CLAIM_POLL_INTERVAL_SECONDS)


@router.post("/jobs/{job_id}/events")
async def post_event(
    job_id: UUID,
    body: EventBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    plaintext = _extract_bearer(authorization)
    db = request.state.db
    token_row = await _resolve_runtime(db, plaintext)

    job = (
        await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    ).scalar_one_or_none()
    if job is None or job.installation_id != token_row.installation_id:
        raise HTTPException(404, "job not found")

    # Idempotent write: UNIQUE(job_id, seq) at the DB level.
    existing = (
        await db.execute(
            select(AgentJobEvent)
            .where(AgentJobEvent.job_id == job_id)
            .where(AgentJobEvent.seq == body.seq)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.event_type != body.event_type:
            raise HTTPException(
                409,
                detail={
                    "error_code": "EVENT_SEQ_CONFLICT",
                    "message": "seq already recorded with different event_type",
                },
            )
        return {"ok": True, "duplicate": True}

    db.add(
        AgentJobEvent(
            job_id=job_id,
            seq=body.seq,
            event_type=body.event_type,
            timestamp=body.timestamp,
            step=body.step,
            detail=body.detail,
        )
    )
    # Mirror into installation event log for the single-query UI timeline.
    db.add(
        TenantInstallationEvent(
            installation_id=token_row.installation_id,
            event_type=body.event_type,
            from_state=None,
            to_state=None,
            actor_id=f"agent:{token_row.installation_id}",
            details={"step": body.step, "seq": body.seq, **body.detail},
        )
    )
    await db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: UUID,
    body: CompleteBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    if body.outcome not in ("success", "failed"):
        raise HTTPException(400, "outcome must be 'success' or 'failed'")

    plaintext = _extract_bearer(authorization)
    db = request.state.db
    token_row = await _resolve_runtime(db, plaintext)

    job = (
        await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    ).scalar_one_or_none()
    if job is None or job.installation_id != token_row.installation_id:
        raise HTTPException(404, "job not found")

    # Terminal writes are idempotent; duplicate returns the recorded terminal.
    if job.status in ("succeeded", "failed"):
        return {
            "ok": True,
            "duplicate": True,
            "recorded": {
                "outcome": job.outcome,
                "error_code": job.error_code,
                "summary": job.summary,
            },
        }

    job.status = "succeeded" if body.outcome == "success" else "failed"
    job.outcome = body.outcome
    job.error_code = body.error_code
    job.summary = body.summary
    job.detail = body.detail
    job.completed_at = _now()

    db.add(
        TenantInstallationEvent(
            installation_id=token_row.installation_id,
            event_type=(
                "job_succeeded" if body.outcome == "success" else "job_failed"
            ),
            from_state=None,
            to_state=None,
            actor_id=f"agent:{token_row.installation_id}",
            details={
                "job_id": str(job_id),
                "kind": job.kind,
                "error_code": body.error_code,
                "summary": body.summary,
            },
        )
    )

    # Inline controller: advance the Installation state machine based on
    # the job kind and outcome. For MVP we keep this inline; a dedicated
    # worker can claim the role once there are multiple drive paths to
    # coordinate (upgrades, decommissions, profile conversions).
    installation = (
        await db.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == token_row.installation_id
            )
        )
    ).scalar_one()
    prior_state = installation.state

    if body.outcome == "failed":
        installation.state = "degraded"
        installation.state_changed_at = _now()
        db.add(
            TenantInstallationEvent(
                installation_id=installation.id,
                event_type="provisioning_failed",
                from_state=prior_state,
                to_state="degraded",
                actor_id="controller",
                details={"kind": job.kind, "error_code": body.error_code},
            )
        )
        await _project_installation_state_to_tenant(
            db,
            installation,
            target_state="degraded",
            event_type="provisioning_failed_l2",
            details={
                "installation_id": str(installation.id),
                "kind": job.kind,
                "error_code": body.error_code,
            },
        )
        await db.commit()
        return {"ok": True}

    # Success path — advance based on job kind.
    if job.kind == "preflight" and installation.state == "agent_connected":
        # Build the next-phase spec FIRST. If it raises (RLS, missing
        # IntegrationConfig, render error), the job.status update + state
        # transition staged above roll back together — no wedge where the
        # installation is stuck in 'provisioning' without an enqueued job.
        spec = await _build_install_helm_release_spec(db, installation)

        installation.state = "provisioning"
        installation.state_changed_at = _now()
        db.add(
            TenantInstallationEvent(
                installation_id=installation.id,
                event_type="provisioning_started",
                from_state="agent_connected",
                to_state="provisioning",
                actor_id="controller",
                details={"after": "preflight"},
            )
        )

        await _enqueue_agent_job(
            db,
            installation_id=installation.id,
            kind="install_helm_release",
            idempotency_key=(
                f"install:{installation.id}:"
                f"{installation.desired_chart_ref}:"
                f"{installation.desired_chart_version}"
            ),
            spec=spec,
        )
        # _enqueue_agent_job commits, which also flushes job.status,
        # installation.state, and the lifecycle event — all atomic.
        return {"ok": True}

    # install_helm_release and upgrade_helm_release both terminate the
    # Helm apply step — the only difference is the starting state. Both
    # transition into a readiness wait keyed by the same post-apply
    # idempotency, so enqueue wait_for_ready once either one succeeds.
    apply_kinds = ("install_helm_release", "upgrade_helm_release")
    apply_states = {"install_helm_release": "provisioning",
                    "upgrade_helm_release": "upgrading"}
    if job.kind in apply_kinds and installation.state == apply_states[job.kind]:
        # Build spec first — same atomic-rollback invariant as the preflight
        # branch above.
        wait_spec = await _build_wait_for_ready_spec(db, installation)

        db.add(
            TenantInstallationEvent(
                installation_id=installation.id,
                event_type="helm_apply_succeeded",
                from_state=None,
                to_state=None,
                actor_id="controller",
                details={
                    "kind": job.kind,
                    "chart_version": installation.desired_chart_version,
                },
            )
        )

        await _enqueue_agent_job(
            db,
            installation_id=installation.id,
            kind="wait_for_ready",
            # Idempotency key differentiates upgrade vs install so both
            # paths can enqueue their own readiness job without collision.
            idempotency_key=(
                f"wait:{job.kind}:{installation.id}:"
                f"{installation.desired_chart_version}"
            ),
            spec=wait_spec,
        )
        return {"ok": True}

    # wait_for_ready success: advance back to active regardless of
    # whether we arrived from first-install or upgrade.
    if job.kind == "wait_for_ready" and installation.state in {
        "provisioning", "upgrading",
    }:
        prior_state = installation.state
        installation.state = "active"
        installation.state_changed_at = _now()
        installation.reported_chart_version = installation.desired_chart_version
        installation.desired_action = "none"
        db.add(
            TenantInstallationEvent(
                installation_id=installation.id,
                event_type=(
                    "provisioning_succeeded"
                    if prior_state == "provisioning"
                    else "upgrade_succeeded"
                ),
                from_state=prior_state,
                to_state="active",
                actor_id="controller",
                details={
                    "chart_version": installation.desired_chart_version,
                    "probes": body.detail.get("probes") if body.detail else None,
                },
            )
        )
        # Project L2 install success onto tenants.state so the MSSP UI
        # health widget, SOC gating, and retry buttons agree with the
        # actual tenant stack. Without this the tenant stays 'degraded'
        # forever if the sync helm path failed earlier — even though the
        # L2 agent has stood the stack up successfully.
        await _project_installation_state_to_tenant(
            db,
            installation,
            target_state="active",
            event_type=(
                "provisioning_succeeded_l2"
                if prior_state == "provisioning"
                else "upgrade_succeeded_l2"
            ),
            details={"installation_id": str(installation.id)},
        )

    await db.commit()
    return {"ok": True}


async def _project_installation_state_to_tenant(
    db,
    installation: TenantInstallation,
    *,
    target_state: str,
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Mirror an L2 installation state transition onto ``tenants.state``.

    The L2 agent's install state is the authoritative signal for whether
    the tenant's SOC stack is actually up. ``tenants.state`` is what the
    MSSP UI and SOC gating consume. Without this projection, a tenant
    whose sync helm-apply step failed but whose L2 agent later succeeded
    stays 'degraded' forever — false-alarming the operator and blocking
    :retry paths.

    Terminal / operator-set states (suspended, decommissioning, archived,
    purged) are never overridden — the L2 install must not clobber a
    deliberate operator decision.
    """
    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.models import Tenant as _Tenant
    from soctalk.core.tenancy.models import TenantLifecycleEvent

    async with tenant_context(db, installation.tenant_id):
        tenant = (
            await db.execute(
                select(_Tenant).where(_Tenant.id == installation.tenant_id)
            )
        ).scalar_one_or_none()
        if tenant is None:
            return
        if tenant.state in {
            "suspended", "decommissioning", "archived", "purged",
        }:
            return
        if tenant.state == target_state:
            return
        prior = tenant.state
        tenant.state = target_state
        # Tenant.state_changed_at is stored as a NAIVE datetime (see the
        # model's ``default_factory=datetime.utcnow``); ``_now()`` returns
        # a tz-aware UTC value which asyncpg rejects with a naive/aware
        # mismatch. Match the column's naive convention.
        tenant.state_changed_at = datetime.utcnow()
        db.add(
            TenantLifecycleEvent(
                tenant_id=tenant.id,
                event_type=event_type,
                from_state=prior,
                to_state=target_state,
                actor_id="controller:l2",
                details=details or {},
            )
        )


async def _build_wait_for_ready_spec(
    db, installation: TenantInstallation,
) -> dict[str, Any]:
    """Build the readiness-probe spec for the post-install wait job.

    Contract alignment notes (don't regress these — the probes are
    generated from the L1 side but must match service endpoints the
    soctalk-tenant chart actually creates):

    - Wazuh: service name is ``{{ include "wazuh.fullname" . }}-manager``
      (charts/wazuh/templates/manager.yaml), which resolves to
      ``<release>-wazuh-manager``. Manager API is HTTPS on 55000 with a
      self-signed cert (the wazuh chart generates it inline); hence
      ``verify_tls: false``. Matches the legacy controller URL in
      provisioning/controller.py:725.
    - TheHive / Cortex: subchart deps are currently commented out in
      soctalk-tenant's Chart.yaml. Until those are wired we can't know
      the real service name, so we omit probes rather than ship a URL
      that never resolves and trips the whole wait job.
    - The release name passed to Helm is ``tenant-<slug>`` (see
      _build_install_helm_release_spec); anchor probe URLs to that.

    The agent treats any HTTP response as "live" — including 401/403 —
    because MVP endpoints don't expose an unauthenticated healthz;
    connection refused or NXDOMAIN keeps polling.
    """
    from soctalk.core.tenancy.models import IntegrationConfig
    from soctalk.core.tenancy.models import Tenant as _Tenant

    # RLS: see _build_install_helm_release_spec — same wrap required.
    async with tenant_context(db, installation.tenant_id):
        tenant = (
            await db.execute(
                select(_Tenant).where(_Tenant.id == installation.tenant_id)
            )
        ).scalar_one()
        integration = (
            await db.execute(
                select(IntegrationConfig).where(
                    IntegrationConfig.tenant_id == tenant.id
                )
            )
        ).scalar_one()

    ns = f"tenant-{tenant.slug}"
    release_name = f"tenant-{tenant.slug}"
    probes: list[dict[str, Any]] = []
    # Harness-only knob: the real soctalk-tenant chart's Wazuh manager
    # listens HTTPS on 55000 with a self-signed cert. The e2e-tenant-stub
    # chart used by the L1→L2 k3d harness listens plain HTTP to keep
    # the test pod free of a TLS bootstrap. Defaults to https in all
    # real deploys; tests set SOCTALK_TENANT_PROBE_SCHEME=http.
    probe_scheme = os.getenv("SOCTALK_TENANT_PROBE_SCHEME", "https").lower()
    if probe_scheme not in {"http", "https"}:
        probe_scheme = "https"
    if integration.wazuh_enabled:
        probes.append({
            "name": "wazuh-manager",
            "url": (
                f"{probe_scheme}://{release_name}-wazuh-manager."
                f"{ns}.svc.cluster.local:55000/"
            ),
            "component": "wazuh",
            # Self-signed cert from the wazuh chart's inline TLS bootstrap.
            # Irrelevant for the http scheme (stub mode) — kept for the
            # https production path.
            "verify_tls": False,
        })
    # TheHive/Cortex probes intentionally omitted — their subchart deps
    # aren't wired in soctalk-tenant's Chart.yaml yet. Re-add with the
    # correct service names when dependencies are declared.
    return {
        "timeout_seconds": 600,
        "poll_interval_seconds": 10,
        "probes": probes,
    }


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    plaintext = _extract_bearer(authorization)
    db = request.state.db
    token_row = await _resolve_runtime(db, plaintext)

    db.add(
        TenantInstallationHeartbeat(
            installation_id=token_row.installation_id,
            timestamp=body.timestamp,
            agent_version=body.agent_version,
            reported_chart_version=body.reported_chart_version,
            reported_state=body.reported_state,
        )
    )

    installation = (
        await db.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == token_row.installation_id
            )
        )
    ).scalar_one()
    installation.agent_last_seen = _now()
    if body.agent_version is not None:
        installation.agent_version = body.agent_version
    if body.reported_chart_version is not None:
        installation.reported_chart_version = body.reported_chart_version
    if body.reported_state is not None:
        installation.reported_state = body.reported_state

    await db.commit()
    return {"ok": True}
