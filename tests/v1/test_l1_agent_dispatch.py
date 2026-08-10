"""Tests for the L1→L2 agent dispatch surface.

Covers all five wire endpoints (register / jobs:claim / events /
jobs/complete / heartbeat), the two MSSP-facing actions on
``/api/mssp/tenants`` (:issue-agent, :retry-install), and the inline
state machine that drives first-install (preflight → install_helm_release
→ wait_for_ready → active) and upgrade (issue-agent on active → upgrading
→ upgrade_helm_release → wait_for_ready → active).

These are direct handler tests — no HTTP stack, no auth middleware. The
handlers take a ``request`` whose only used fields are ``state.db`` and
``state.user_identity``, so a lightweight fake is enough.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from soctalk.core.agents.api import (
    CompleteBody,
    EventBody,
    HeartbeatBody,
    RegisterBody,
    _build_install_helm_release_spec,
    _reclaim_stale_for_installation,
    claim_job,
    complete_job,
    heartbeat,
    post_event,
    register,
)
from soctalk.core.agents.models import (
    AgentJob,
    AgentJobEvent,
    TenantInstallation,
    TenantInstallationBootstrapToken,
    TenantInstallationEvent,
    TenantInstallationHeartbeat,
    TenantInstallationRuntimeToken,
)
from soctalk.core.agents.tokens import verify_token
from soctalk.core.api.tenants import issue_agent, retry_install
from soctalk.core.tenancy.models import (
    BrandingConfig,
    IntegrationConfig,
    Organization,
    Tenant,
    TenantState,
)


SKIP_INTEGRATION = os.getenv("SKIP_INTEGRATION", "0") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        SKIP_INTEGRATION,
        reason="SKIP_INTEGRATION set; L1 dispatch tests need Postgres",
    ),
]


def _admin_url() -> str:
    return os.getenv(
        "DATABASE_URL_ADMIN",
        "postgresql+asyncpg://soctalk_admin:soctalk_admin@localhost:5432/soctalk",
    )


def _mssp_url() -> str:
    return os.getenv(
        "DATABASE_URL_MSSP",
        "postgresql+asyncpg://soctalk_mssp:soctalk_mssp@localhost:5432/soctalk",
    )


@pytest_asyncio.fixture
async def admin_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_admin_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def mssp_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_mssp_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_tenant(
    admin_session: AsyncSession, mssp_session: AsyncSession
) -> Tenant:
    """Clean slate + a tenant with the integration/branding rows the
    install-spec builder requires."""
    await admin_session.execute(
        text(
            "TRUNCATE agent_job_events, agent_jobs, "
            "tenant_installation_heartbeats, tenant_installation_events, "
            "tenant_installation_runtime_tokens, "
            "tenant_installation_bootstrap_tokens, tenant_installations, "
            "tenant_lifecycle_events, integration_configs, branding_configs, "
            "tenant_secrets, provisioning_jobs, tenants, organizations CASCADE"
        )
    )
    await admin_session.commit()

    org = Organization(
        mssp_id=uuid4(), mssp_name="L1 Dispatch Test", slug="l1-dispatch-test",
        install_id=uuid4(), install_label="test",
    )
    mssp_session.add(org)
    await mssp_session.commit()
    await mssp_session.refresh(org)

    tenant = Tenant(
        slug=f"t{uuid4().hex[:8]}",
        display_name="Dispatch Test Tenant",
        state=TenantState.PENDING.value,
        profile="poc",
        organization_id=org.id,
        config={},
    )
    mssp_session.add(tenant)
    await mssp_session.flush()
    mssp_session.add_all([
        IntegrationConfig(
            tenant_id=tenant.id,
            llm_base_url="https://api.example.com/v1",
            llm_model="gpt-4",
            llm_provider="openai-compatible",
            # Cost accounting is ON in CI (#141 phase 3). This tenant points at
            # a gateway, whose (provider_kind, model) pair is deliberately not
            # in the catalog — a gateway's price for a model is not the
            # vendor's price for it — so the price gate refuses the PATCH
            # without an override. Setting one is what a real operator does.
            llm_model_prices={
                m: {"input": 1.0, "output": 2.0}
                for m in ("gpt-4", "gpt-4o", "gpt-4o-mini", "o3")
            },
            wazuh_url=None,  # controller writes after install
            wazuh_enabled=True,
            thehive_enabled=False,
            cortex_enabled=False,
        ),
        BrandingConfig(
            tenant_id=tenant.id,
            app_name="Dispatch Test",
        ),
    ])
    await mssp_session.commit()
    await mssp_session.refresh(tenant)
    return tenant


class FakeRequest:
    """Minimal stand-in for FastAPI Request — handlers only use
    ``state.db`` and, for the MSSP action, ``state.user_identity``.
    """

    def __init__(self, db):
        class State:
            pass
        self.state = State()
        self.state.db = db
        self.state.user_identity = {"user_id": "test-user"}


# ---------------------------------------------------------------------------
# :issue-agent creates Installation + bootstrap; re-issue refreshes chart.
# ---------------------------------------------------------------------------


async def test_issue_agent_creates_installation_and_bootstrap(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    resp = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    assert resp.tenant_id == str(seeded_tenant.id)
    assert resp.bootstrap_token and len(resp.bootstrap_token) > 40

    # Response must expose the AGENT chart (what the tenant admin
    # installs), NOT the tenant SOC-stack chart (what the agent
    # dispatches once registered).
    assert "soctalk-cloud-agent" in resp.agent_chart_ref
    assert resp.agent_chart_version
    assert resp.control_plane_url.startswith(("http://", "https://"))
    # Copy-pasteable install hint includes the sensitive bits the
    # tenant admin needs.
    assert resp.helm_install_hint.startswith("helm install ")
    assert resp.bootstrap_token in resp.helm_install_hint
    assert resp.control_plane_url in resp.helm_install_hint

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    assert installation.state == "pending"
    # Tenant SOC-stack chart is NOT in the response — it lives only
    # on the installation row for operator observability.
    assert "soctalk-tenant" in installation.desired_chart_ref
    assert installation.desired_chart_version

    # Bootstrap row is stored hashed, not in plaintext.
    bootstrap_rows = (
        await mssp_session.execute(
            select(TenantInstallationBootstrapToken)
            .where(
                TenantInstallationBootstrapToken.installation_id
                == installation.id
            )
        )
    ).scalars().all()
    assert len(bootstrap_rows) == 1
    assert bootstrap_rows[0].token_hash != resp.bootstrap_token
    assert verify_token(bootstrap_rows[0].token_hash, resp.bootstrap_token)


async def test_issue_agent_reissue_refreshes_desired_chart_and_revokes_prior(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Re-calling :issue-agent after the env-driven chart version has
    moved must update the Installation's desired_* fields AND revoke any
    un-consumed prior bootstrap — otherwise the response hint drifts
    from what the install job eventually uses.
    """
    # First call at v0.1.0
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    first = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    # Second call at v0.2.0 — simulates an operator re-issuing after a
    # chart bump.
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.2.0")
    second = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    assert second.bootstrap_token != first.bootstrap_token

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    # Tenant SOC-stack chart — moved on the installation row.
    assert installation.desired_chart_version == "0.2.0"
    assert installation.desired_action == "upgrade"

    # Prior bootstrap is revoked; new one isn't.
    rows = (
        await mssp_session.execute(
            select(TenantInstallationBootstrapToken)
            .where(
                TenantInstallationBootstrapToken.installation_id
                == installation.id
            )
        )
    ).scalars().all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.revoked_at is not None) == 1
    assert sum(1 for r in rows if r.revoked_at is None) == 1


# ---------------------------------------------------------------------------
# /register: bootstrap → runtime token, transitions to agent_connected,
# enqueues preflight.
# ---------------------------------------------------------------------------


async def test_register_consumes_bootstrap_and_enqueues_preflight(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    issued = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    resp = await register(
        RegisterBody(
            cluster_label="t-test-k3d",
            agent_version="0.1.0",
            kubernetes_version="v1.29.0",
            node_count=1,
        ),
        FakeRequest(mssp_session),
        authorization=f"Bearer {issued.bootstrap_token}",
    )

    assert resp.installation_id == issued.installation_id
    assert resp.runtime_token and len(resp.runtime_token) > 40

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == resp.installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "agent_connected"
    assert installation.agent_version == "0.1.0"

    # Bootstrap must be burned.
    bootstrap = (
        await mssp_session.execute(
            select(TenantInstallationBootstrapToken)
            .where(
                TenantInstallationBootstrapToken.installation_id
                == installation.id
            )
        )
    ).scalar_one()
    assert bootstrap.consumed_at is not None

    # Preflight enqueued.
    jobs = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation.id)
        )
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].kind == "preflight"


# ---------------------------------------------------------------------------
# Pipeline: preflight ok → install spec correctness → wait_for_ready →
# active.
# ---------------------------------------------------------------------------


async def _complete(mssp_session, job_id, outcome="success", **extra):
    return await complete_job(
        job_id,
        CompleteBody(outcome=outcome, **extra),
        FakeRequest(mssp_session),
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )


# Tests need to ferry the runtime token between calls; we mint+stash.
_RUNTIME_CACHE: dict[int, str] = {}


def _current_runtime_plaintext(session: AsyncSession) -> str:
    return _RUNTIME_CACHE[id(session)]


async def _drive_through_register(mssp_session, seeded_tenant) -> tuple:
    """Helper that issues + registers + caches the runtime token, then
    returns (installation_id, first_job). Keeps the pipeline tests small.
    """
    issued = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))
    reg = await register(
        RegisterBody(cluster_label="t-k3d", agent_version="0.1.0"),
        FakeRequest(mssp_session),
        authorization=f"Bearer {issued.bootstrap_token}",
    )
    _RUNTIME_CACHE[id(mssp_session)] = reg.runtime_token
    preflight = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == reg.installation_id)
        )
    ).scalar_one()
    return reg.installation_id, preflight


async def test_preflight_success_enqueues_install_with_correct_spec(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )

    # Agent claims — this advances preflight to in_flight.
    claim = await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    assert getattr(claim, "kind", None) == "preflight"

    # Completing advances the installation to "provisioning" and
    # enqueues install_helm_release with the correct spec shape.
    await _complete(mssp_session, preflight.id, outcome="success")

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "provisioning"

    install_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "install_helm_release")
        )
    ).scalar_one()
    spec = install_job.spec

    # Contract assertions the earlier review flagged:
    assert spec["create_namespace"] is True
    assert spec["namespace"] == f"tenant-{seeded_tenant.slug}"
    assert spec["release_name"] == f"tenant-{seeded_tenant.slug}"

    values = spec["values"]
    # render_tenant_values contract — no stray keys, slug/msspId/installId
    # present.
    assert set(values["tenant"]) >= {"id", "slug", "msspId", "installId"}
    assert values["tenant"]["slug"] == seeded_tenant.slug
    assert "cluster_label" not in values["tenant"]

    # Cross-cluster + secret-feeds that B3/B5 added.
    assert "soctalkSystem" in values
    assert values["soctalkSystem"]["url"].startswith(("http://", "https://"))
    assert values["soctalkSystem"]["adapterToken"]
    assert "apiKey" in values["llm"]

    # L2 subchart path (issue #109): when wazuh is bundled into the tenant
    # release its resources are ``<release>-wazuh-*``, so BOTH the adapter and
    # the runs-worker MCP wiring must be rewritten off the standalone
    # ``wazuh-<slug>-*`` naming render_tenant_values emits. A missed rewrite
    # leaves the worker binding against non-existent Services/Secrets.
    # The seeded tenant is poc with wazuh_enabled, so the bundle IS on and the
    # worker MCP IS wired: assert that (fail-closed) rather than skipping, so a
    # future fixture change can't silently make this vacuous.
    rel = f"tenant-{seeded_tenant.slug}"
    assert values["components"]["wazuh"]["enabled"] is True
    assert values["adapter"]["wazuhIndexer"]["url"] == (
        f"https://{rel}-wazuh-indexer:9200"
    )
    assert values["adapter"]["wazuhIndexer"]["credsSecret"] == f"{rel}-wazuh-creds"
    rw = values["runsWorker"]["wazuh"]
    assert rw["enabled"] is True
    assert rw["indexerHost"] == f"{rel}-wazuh-indexer"
    assert rw["apiUrl"] == f"https://{rel}-wazuh-manager:55000"
    assert rw["credsSecret"] == f"{rel}-wazuh-creds"


async def test_l2_install_spec_rejects_stored_anthropic_served_engine(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    from fastapi import HTTPException

    installation_id, _preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_provider = "anthropic"
    cfg.llm_base_url = "https://api.anthropic.com"
    cfg.llm_model = "claude-sonnet-4-6"
    cfg.llm_engine = "sglang"
    await mssp_session.commit()
    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(TenantInstallation.id == installation_id)
        )
    ).scalar_one()

    with pytest.raises(HTTPException) as exc_info:
        await _build_install_helm_release_spec(mssp_session, installation)
    assert exc_info.value.status_code == 422
    assert "not valid with provider 'anthropic'" in str(exc_info.value.detail)


async def test_l2_install_spec_rejects_stored_served_engine_with_sentinel_base_url(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    from fastapi import HTTPException

    installation_id, _preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_provider = "openai-compatible"
    cfg.llm_base_url = "https://api.openai.com/v1"
    cfg.llm_engine = "sglang"
    await mssp_session.commit()
    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(TenantInstallation.id == installation_id)
        )
    ).scalar_one()

    with pytest.raises(HTTPException) as exc_info:
        await _build_install_helm_release_spec(mssp_session, installation)
    assert exc_info.value.status_code == 422
    assert "requires a custom llm_base_url" in str(exc_info.value.detail)


async def test_l2_install_spec_rejects_stored_tier_served_engine_with_hosted_base_url(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    from fastapi import HTTPException

    installation_id, _preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_tiers = {
        "fast": {
            "provider": "openai-compatible",
            "base_url": "https://api.openai.com/v1",
            "model": "qwen3-32b",
            "engine": "sglang",
        }
    }
    await mssp_session.commit()
    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(TenantInstallation.id == installation_id)
        )
    ).scalar_one()

    with pytest.raises(HTTPException) as exc_info:
        await _build_install_helm_release_spec(mssp_session, installation)
    assert exc_info.value.status_code == 422
    assert "requires a custom base_url" in str(exc_info.value.detail)


async def test_install_success_enqueues_wait_for_ready_with_correct_probe(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    """wait_for_ready probe for Wazuh must match the chart contract:
    release-prefixed service name, HTTPS on 55000, TLS skip for
    self-signed chart cert.
    """
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(mssp_session, preflight.id, outcome="success")

    install_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "install_helm_release")
        )
    ).scalar_one()
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(mssp_session, install_job.id, outcome="success")

    wait_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "wait_for_ready")
        )
    ).scalar_one()
    probes = wait_job.spec["probes"]
    release = f"tenant-{seeded_tenant.slug}"
    wazuh = next((p for p in probes if p["component"] == "wazuh"), None)
    assert wazuh is not None
    # Release-prefixed service name matches the chart's wazuh.fullname.
    assert wazuh["url"].startswith(
        f"https://{release}-wazuh-manager.tenant-{seeded_tenant.slug}"
    )
    # Port 55000 over HTTPS (self-signed chart cert → skip verify).
    assert ":55000/" in wazuh["url"]
    assert wazuh["verify_tls"] is False


async def test_wait_for_ready_success_transitions_to_active(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )

    async def _drain_one(expected_kind: str):
        await claim_job(
            FakeRequest(mssp_session), wait=0,
            authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
        )
        # Find the in_flight job of the expected kind for this install.
        row = (
            await mssp_session.execute(
                select(AgentJob)
                .where(AgentJob.installation_id == installation_id)
                .where(AgentJob.kind == expected_kind)
                .where(AgentJob.status == "in_flight")
            )
        ).scalar_one()
        await _complete(mssp_session, row.id, outcome="success")

    await _drain_one("preflight")
    await _drain_one("install_helm_release")
    await _drain_one("wait_for_ready")

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "active"
    assert installation.reported_chart_version == installation.desired_chart_version
    assert installation.desired_action == "none"


# ---------------------------------------------------------------------------
# Stale-claim reclaim + :retry-install
# ---------------------------------------------------------------------------


async def test_stale_in_flight_jobs_are_reclaimed_on_next_claim(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    # Simulate agent crashed after claim: set the preflight job to
    # in_flight with claimed_at well past the stale threshold.
    preflight.status = "in_flight"
    preflight.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await mssp_session.commit()

    reclaimed = await _reclaim_stale_for_installation(
        mssp_session, installation_id
    )
    assert reclaimed == 1

    await mssp_session.refresh(preflight)
    assert preflight.status == "pending"
    assert preflight.claimed_at is None

    # An audit event should have been recorded for the reclaim.
    events = (
        await mssp_session.execute(
            select(TenantInstallationEvent)
            .where(
                TenantInstallationEvent.installation_id == installation_id
            )
            .where(
                TenantInstallationEvent.event_type == "agent_job_reclaimed"
            )
        )
    ).scalars().all()
    assert len(events) == 1


async def test_retry_install_resets_failed_and_flips_degraded(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    # Wedge the installation: fail the preflight job + drop state to
    # degraded to mimic the full bad-run shape.
    preflight.status = "failed"
    preflight.outcome = "failed"
    preflight.error_code = "TEST"
    preflight.summary = "test-induced"
    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    installation.state = "degraded"
    await mssp_session.commit()

    resp = await retry_install(seeded_tenant.id, FakeRequest(mssp_session))
    assert resp.reset_to_pending == 1
    assert resp.state == "provisioning"

    await mssp_session.refresh(preflight)
    assert preflight.status == "pending"
    assert preflight.outcome is None
    assert preflight.error_code is None


# ---------------------------------------------------------------------------
# /jobs/{id}/events: happy path mirrors to installation events; replay
# returns duplicate=True; different event_type at same seq returns 409.
# ---------------------------------------------------------------------------


async def test_post_event_mirrors_to_installation_events(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )

    await post_event(
        preflight.id,
        EventBody(
            seq=1,
            event_type="step_started",
            timestamp=datetime.now(timezone.utc),
            step="kube_reachable",
            detail={"node_count": 3},
        ),
        FakeRequest(mssp_session),
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )

    # One agent_job_events row for the wire event.
    wire = (
        await mssp_session.execute(
            select(AgentJobEvent).where(AgentJobEvent.job_id == preflight.id)
        )
    ).scalars().all()
    assert len(wire) == 1
    assert wire[0].seq == 1
    assert wire[0].event_type == "step_started"

    # Mirrored into installation events so the UI timeline can render
    # agent progress alongside lifecycle transitions.
    mirrored = (
        await mssp_session.execute(
            select(TenantInstallationEvent)
            .where(TenantInstallationEvent.installation_id == installation_id)
            .where(TenantInstallationEvent.event_type == "step_started")
        )
    ).scalars().all()
    assert len(mirrored) == 1
    assert mirrored[0].details["step"] == "kube_reachable"
    assert mirrored[0].details["node_count"] == 3


async def test_post_event_replay_same_seq_same_type_is_idempotent(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )

    ev = EventBody(
        seq=1,
        event_type="step_started",
        timestamp=datetime.now(timezone.utc),
        step="kube_reachable",
        detail={},
    )
    tok = f"Bearer {_current_runtime_plaintext(mssp_session)}"

    first = await post_event(
        preflight.id, ev, FakeRequest(mssp_session), authorization=tok
    )
    second = await post_event(
        preflight.id, ev, FakeRequest(mssp_session), authorization=tok
    )
    assert first == {"ok": True}
    assert second == {"ok": True, "duplicate": True}

    # Still exactly one row at the wire-event level.
    rows = (
        await mssp_session.execute(
            select(AgentJobEvent).where(AgentJobEvent.job_id == preflight.id)
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_post_event_different_type_same_seq_returns_409(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    from fastapi import HTTPException

    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )

    tok = f"Bearer {_current_runtime_plaintext(mssp_session)}"
    await post_event(
        preflight.id,
        EventBody(
            seq=1, event_type="step_started",
            timestamp=datetime.now(timezone.utc),
        ),
        FakeRequest(mssp_session), authorization=tok,
    )

    with pytest.raises(HTTPException) as exc:
        await post_event(
            preflight.id,
            EventBody(
                seq=1, event_type="step_failed",
                timestamp=datetime.now(timezone.utc),
            ),
            FakeRequest(mssp_session), authorization=tok,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail.get("error_code") == "EVENT_SEQ_CONFLICT"


# ---------------------------------------------------------------------------
# /heartbeat: persists InstallationHeartbeat row + refreshes installation
# columns (agent_last_seen, agent_version, reported_chart_version, state).
# ---------------------------------------------------------------------------


async def test_heartbeat_persists_row_and_refreshes_installation(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    installation_id, _ = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    before = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert before.reported_chart_version is None
    initial_last_seen = before.agent_last_seen

    await heartbeat(
        HeartbeatBody(
            timestamp=datetime.now(timezone.utc),
            agent_version="0.1.1",
            reported_chart_version="0.1.0",
            reported_state="ok",
        ),
        FakeRequest(mssp_session),
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )

    # Heartbeat row persisted.
    hb_rows = (
        await mssp_session.execute(
            select(TenantInstallationHeartbeat).where(
                TenantInstallationHeartbeat.installation_id == installation_id
            )
        )
    ).scalars().all()
    assert len(hb_rows) == 1
    assert hb_rows[0].agent_version == "0.1.1"
    assert hb_rows[0].reported_state == "ok"

    # Installation columns refreshed.
    after = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert after.agent_version == "0.1.1"
    assert after.reported_chart_version == "0.1.0"
    assert after.reported_state == "ok"
    # agent_last_seen must have advanced (register set an initial value).
    assert after.agent_last_seen is not None
    if initial_last_seen is not None:
        assert after.agent_last_seen >= initial_last_seen


# ---------------------------------------------------------------------------
# Upgrade path: :issue-agent on an already-active install with a moved
# desired chart enqueues upgrade_helm_release and flips state to upgrading;
# its success (+ wait_for_ready success) transitions back to active.
# ---------------------------------------------------------------------------


async def _drive_to_active(mssp_session, seeded_tenant) -> str:
    """Helper: walk a fresh tenant through first-install all the way to
    state=active. Returns installation_id.
    """
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )

    async def _drain(expected_kind: str):
        await claim_job(
            FakeRequest(mssp_session), wait=0,
            authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
        )
        row = (
            await mssp_session.execute(
                select(AgentJob)
                .where(AgentJob.installation_id == installation_id)
                .where(AgentJob.kind == expected_kind)
                .where(AgentJob.status == "in_flight")
            )
        ).scalar_one()
        await _complete(mssp_session, row.id, outcome="success")

    await _drain("preflight")
    await _drain("install_helm_release")
    await _drain("wait_for_ready")
    return installation_id


async def test_issue_agent_on_active_install_dispatches_upgrade(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """The bug closed in L1-D1: an already-active install whose desired
    chart version moved must have upgrade_helm_release actually enqueued.
    """
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    installation_id = await _drive_to_active(mssp_session, seeded_tenant)

    # Fresh issue-agent with a moved chart version.
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.2.0")
    await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "upgrading"
    assert installation.desired_chart_version == "0.2.0"

    upgrade_jobs = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "upgrade_helm_release")
        )
    ).scalars().all()
    assert len(upgrade_jobs) == 1
    assert upgrade_jobs[0].status == "pending"
    assert "0.2.0" in upgrade_jobs[0].idempotency_key


async def test_issue_agent_rejects_chart_bump_while_in_flight(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """E1: mid-flight chart bump must 409.

    Without the gate, updating installation.desired_chart_version
    while provisioning/upgrading desyncs — the in-flight agent job
    holds the old spec, but wait_for_ready success copies the current
    desired version into reported_chart_version, settling the install
    under the wrong version.
    """
    from fastapi import HTTPException

    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    # Drive to provisioning (preflight succeeded, install_helm_release
    # pending/in-flight).
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(mssp_session, preflight.id, outcome="success")

    installation_before = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation_before.state == "provisioning"

    # Bump chart + re-issue mid-flight.
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.2.0")
    with pytest.raises(HTTPException) as exc:
        await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))
    assert exc.value.status_code == 409
    assert exc.value.detail["error_code"] == "AGENT_JOB_IN_FLIGHT"
    assert exc.value.detail["state"] == "provisioning"

    # Critical: installation.desired_chart_version must NOT have moved.
    await mssp_session.rollback()
    installation_after = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation_after.desired_chart_version == "0.1.0"
    assert installation_after.state == "provisioning"


async def test_issue_agent_allows_chart_bump_in_pending(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """E1 complement: pending is safe to update (spec not yet built)."""
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    first = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.2.0")
    second = await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))
    assert second.bootstrap_token  # re-issue succeeded, new token minted

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    assert installation.state == "pending"
    # Tenant SOC-stack chart moved even though the response surfaces
    # only agent coordinates — desired_chart_version is authoritative
    # when the install spec is eventually built.
    assert installation.desired_chart_version == "0.2.0"


async def test_retry_install_recovers_failed_upgrade_to_upgrading(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """E2: a failed upgrade retried via :retry-install must resume
    under state=upgrading, not provisioning, so the completer's
    kind→state table accepts the retried upgrade_helm_release success.
    """
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    installation_id = await _drive_to_active(mssp_session, seeded_tenant)

    # Bump chart → dispatches upgrade_helm_release, state=upgrading.
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.2.0")
    await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))
    upgrade_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "upgrade_helm_release")
        )
    ).scalar_one()

    # Fail the upgrade → state=degraded.
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(
        mssp_session, upgrade_job.id,
        outcome="failed", error_code="HELM_APPLY_FAILED", summary="sim",
    )

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "degraded"

    # Retry should unwedge to upgrading, not provisioning.
    resp = await retry_install(seeded_tenant.id, FakeRequest(mssp_session))
    assert resp.state == "upgrading"

    # Re-claim + re-complete the retried upgrade → wait_for_ready
    # should enqueue and the state machine should progress.
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await mssp_session.refresh(upgrade_job)
    assert upgrade_job.status == "in_flight"
    await _complete(mssp_session, upgrade_job.id, outcome="success")

    wait_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "wait_for_ready")
            .where(AgentJob.status == "pending")
        )
    ).scalar_one()
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(mssp_session, wait_job.id, outcome="success")

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "active"
    assert installation.reported_chart_version == "0.2.0"


# ---------------------------------------------------------------------------
# Per-tenant LLM key: precedence (Postgres > env), fallback behaviour,
# dual-write through the MSSP PATCH endpoint.
# ---------------------------------------------------------------------------


async def _get_install_spec(mssp_session, seeded_tenant):
    """Helper: drive through preflight so the install_helm_release job
    materializes with the spec builder's latest values. Returns the
    spec dict.
    """
    installation_id, preflight = await _drive_through_register(
        mssp_session, seeded_tenant
    )
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(mssp_session, preflight.id, outcome="success")
    install_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "install_helm_release")
        )
    ).scalar_one()
    return install_job.spec


async def test_install_spec_prefers_per_tenant_llm_key_over_env(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Per-tenant column wins over the install-wide env fallback."""
    from soctalk.core.tenancy.models import IntegrationConfig

    monkeypatch.setenv("SOCTALK_DEFAULT_LLM_API_KEY", "env-wide-key")

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_api_key_plain = "per-tenant-key"
    await mssp_session.commit()

    spec = await _get_install_spec(mssp_session, seeded_tenant)
    assert spec["values"]["llm"]["apiKey"] == "per-tenant-key"


async def test_install_spec_falls_back_to_env_when_tenant_key_unset(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """No per-tenant key set → env fallback is used."""
    monkeypatch.setenv("SOCTALK_DEFAULT_LLM_API_KEY", "env-wide-key")
    spec = await _get_install_spec(mssp_session, seeded_tenant)
    assert spec["values"]["llm"]["apiKey"] == "env-wide-key"


async def test_install_spec_empty_when_neither_key_set(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Both unset → empty string. Chart guards on truthy and skips
    the Secret, preserving the legacy pre-provisioned-Secret path.
    """
    monkeypatch.delenv("SOCTALK_DEFAULT_LLM_API_KEY", raising=False)
    spec = await _get_install_spec(mssp_session, seeded_tenant)
    assert spec["values"]["llm"]["apiKey"] == ""


async def test_llm_probe_returns_false_when_kube_unreachable(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Finding 1: pure cross-cluster operator has no kubeconfig. The
    probe must treat that as "absent", not raise — otherwise GET +
    PATCH 500 before the first per-tenant key write.
    """
    import soctalk.core.api.llm_config as llm_mod

    def _boom():
        raise RuntimeError("no kubeconfig (pure cross-cluster mode)")

    monkeypatch.setattr(llm_mod, "new_k8s_client", _boom)

    # Probe alone must not raise.
    present = await llm_mod._probe_api_key_present(seeded_tenant.id)
    assert present is False


async def test_get_llm_returns_has_api_key_false_when_kube_down_and_column_unset(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """The read path must not 500 when neither source exists."""
    from soctalk.core.api.llm_config import get_tenant_llm
    import soctalk.core.api.llm_config as llm_mod

    def _boom():
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(llm_mod, "new_k8s_client", _boom)

    resp = await get_tenant_llm(seeded_tenant.id, FakeRequest(mssp_session))
    assert resp.has_api_key is False


async def test_get_llm_has_api_key_false_after_clear_even_if_k8s_secret_lingers(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """K1: Postgres is authoritative. If the best-effort K8s Secret
    delete is skipped or fails, a later GET must NOT resurrect
    has_api_key=true from the lingering Secret.

    Simulates the pessimistic case by monkeypatching the probe to
    always return True — if the read path consulted it as a fallback
    (the prior OR-semantics), the assertion would fail.
    """
    from soctalk.core.api.llm_config import (
        clear_tenant_llm_api_key, get_tenant_llm,
    )
    from soctalk.core.tenancy.models import IntegrationConfig
    import soctalk.core.api.llm_config as llm_mod

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_api_key_plain = "will-be-cleared"
    await mssp_session.commit()

    # K8s delete is a no-op (simulates the "skipped" path).
    async def _noop_delete(*_a, **_kw):
        return

    async def _probe_says_present(*_a, **_kw):
        return True  # Simulates lingering Secret.

    monkeypatch.setattr(llm_mod, "_delete_api_key_secret", _noop_delete)
    monkeypatch.setattr(llm_mod, "_probe_api_key_present", _probe_says_present)

    await clear_tenant_llm_api_key(seeded_tenant.id, FakeRequest(mssp_session))

    resp = await get_tenant_llm(seeded_tenant.id, FakeRequest(mssp_session))
    assert resp.has_api_key is False, (
        "Postgres-authoritative contract: a lingering Secret must not "
        "resurrect has_api_key after the column is cleared"
    )


async def test_get_llm_returns_has_api_key_true_from_postgres_when_kube_down(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Postgres is authoritative: with a per-tenant key set, has_api_key
    is true even when the K8s probe is unreachable.
    """
    from soctalk.core.api.llm_config import get_tenant_llm
    from soctalk.core.tenancy.models import IntegrationConfig
    import soctalk.core.api.llm_config as llm_mod

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_api_key_plain = "cross-cluster-key"
    await mssp_session.commit()

    def _boom():
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(llm_mod, "new_k8s_client", _boom)

    resp = await get_tenant_llm(seeded_tenant.id, FakeRequest(mssp_session))
    assert resp.has_api_key is True


async def test_delete_llm_api_key_clears_postgres_column(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """I2: explicit clear path for the per-tenant key."""
    from soctalk.core.api.llm_config import clear_tenant_llm_api_key
    from soctalk.core.tenancy.models import IntegrationConfig
    import soctalk.core.api.llm_config as llm_mod

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_api_key_plain = "to-be-cleared"
    await mssp_session.commit()

    # Stub the K8s delete path — not running against a real cluster.
    async def _noop(*_a, **_kw):
        return

    monkeypatch.setattr(llm_mod, "_delete_api_key_secret", _noop)

    await clear_tenant_llm_api_key(seeded_tenant.id, FakeRequest(mssp_session))

    await mssp_session.refresh(cfg)
    assert cfg.llm_api_key_plain is None


async def test_delete_llm_api_key_is_idempotent(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Clearing an already-null key is a no-op, not an error."""
    from soctalk.core.api.llm_config import clear_tenant_llm_api_key
    from soctalk.core.tenancy.models import IntegrationConfig
    import soctalk.core.api.llm_config as llm_mod

    async def _noop(*_a, **_kw):
        return

    monkeypatch.setattr(llm_mod, "_delete_api_key_secret", _noop)

    # Column starts as None; call clear twice.
    await clear_tenant_llm_api_key(seeded_tenant.id, FakeRequest(mssp_session))
    await clear_tenant_llm_api_key(seeded_tenant.id, FakeRequest(mssp_session))

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    assert cfg.llm_api_key_plain is None


async def test_patch_commits_db_before_k8s_write(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """J1: DB commit must precede the K8s side effect.

    Without this order, a late middleware commit failure after the
    handler returns leaves the K8s Secret written while the Postgres
    column rolls back — violates "Postgres-is-authoritative".

    Tested by recording the ordered call sequence of session.commit
    and _write_api_key, then asserting commit fires first.
    """
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm
    import soctalk.core.api.llm_config as llm_mod

    events: list[str] = []
    orig_commit = mssp_session.commit

    async def tracked_commit(*a, **kw):
        events.append("commit")
        await orig_commit(*a, **kw)

    async def tracked_k8s_write(*_a, **_kw):
        events.append("k8s_write")

    mssp_session.commit = tracked_commit
    monkeypatch.setattr(llm_mod, "_write_api_key", tracked_k8s_write)

    try:
        await update_tenant_llm(
            seeded_tenant.id,
            LlmConfigUpdate(api_key="ordering-test"),
            FakeRequest(mssp_session),
        )
    finally:
        mssp_session.commit = orig_commit

    # The sequence must include commit BEFORE k8s_write.
    commit_idx = events.index("commit")
    k8s_idx = events.index("k8s_write")
    assert commit_idx < k8s_idx, (
        f"expected commit before k8s_write, got {events}"
    )


async def test_delete_commits_db_before_k8s_delete(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """Same ordering guarantee for the clear path."""
    from soctalk.core.api.llm_config import clear_tenant_llm_api_key
    from soctalk.core.tenancy.models import IntegrationConfig
    import soctalk.core.api.llm_config as llm_mod

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_api_key_plain = "to-be-cleared"
    await mssp_session.commit()

    events: list[str] = []
    orig_commit = mssp_session.commit

    async def tracked_commit(*a, **kw):
        events.append("commit")
        await orig_commit(*a, **kw)

    async def tracked_k8s_delete(*_a, **_kw):
        events.append("k8s_delete")

    mssp_session.commit = tracked_commit
    monkeypatch.setattr(
        llm_mod, "_delete_api_key_secret", tracked_k8s_delete
    )

    try:
        await clear_tenant_llm_api_key(
            seeded_tenant.id, FakeRequest(mssp_session),
        )
    finally:
        mssp_session.commit = orig_commit

    commit_idx = events.index("commit")
    delete_idx = events.index("k8s_delete")
    assert commit_idx < delete_idx, (
        f"expected commit before k8s_delete, got {events}"
    )


async def test_delete_api_key_secret_graceful_skip_when_kube_unreachable(
    monkeypatch,
):
    """The K8s delete helper must not raise when the cluster can't be
    reached — same contract as the probe. Covers the
    new_k8s_client() failure branch.
    """
    from soctalk.core.api.llm_config import _delete_api_key_secret
    import soctalk.core.api.llm_config as llm_mod
    from uuid import uuid4

    def _boom():
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(llm_mod, "new_k8s_client", _boom)

    # No exception should escape.
    await _delete_api_key_secret(uuid4())


async def test_delete_api_key_secret_graceful_skip_on_non_404_api_error(
    monkeypatch,
):
    """Non-404 ApiException from K8s (RBAC denied, conflict, etc.)
    must not raise — delete is best-effort by contract.
    """
    from kubernetes.client.exceptions import ApiException
    from soctalk.core.api.llm_config import _delete_api_key_secret
    import soctalk.core.api.llm_config as llm_mod
    from uuid import uuid4

    class _FakeCore:
        async def delete_namespaced_secret(self, *_a, **_kw):
            raise ApiException(status=403, reason="Forbidden")

    class _FakeClient:
        def __init__(self):
            self._core = _FakeCore()

        async def _run(self, fn, *args, **kwargs):
            return await fn(*args, **kwargs)

    monkeypatch.setattr(llm_mod, "new_k8s_client", _FakeClient)

    # No exception should escape.
    await _delete_api_key_secret(uuid4())


async def test_patch_llm_writes_postgres_column(
    mssp_session: AsyncSession, seeded_tenant: Tenant,
):
    """MSSP-facing PATCH /api/mssp/tenants/{id}/llm persists the key
    in Postgres so the spec builder can reach it, even when the K8s
    Secret write path isn't reachable (pure cross-cluster deploy).
    """
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm
    from soctalk.core.tenancy.models import IntegrationConfig

    # Stub the K8s writer so the test doesn't require a reachable
    # API server; handler wraps it in try/except so the Postgres
    # write proceeds on failure.
    import soctalk.core.api.llm_config as llm_mod

    async def _noop_k8s_write(*_a, **_kw):
        raise RuntimeError("no k8s in unit-test mode")

    original = llm_mod._write_api_key
    llm_mod._write_api_key = _noop_k8s_write
    try:
        await update_tenant_llm(
            seeded_tenant.id,
            LlmConfigUpdate(api_key="patch-written-key"),
            FakeRequest(mssp_session),
        )
    finally:
        llm_mod._write_api_key = original

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    assert cfg.llm_api_key_plain == "patch-written-key"


async def test_upgrade_roundtrip_returns_to_active(
    mssp_session: AsyncSession, seeded_tenant: Tenant, monkeypatch,
):
    """upgrade_helm_release success → wait_for_ready → active (with the
    upgrade-specific event_type so audit can distinguish first-install
    from upgrade).
    """
    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    installation_id = await _drive_to_active(mssp_session, seeded_tenant)

    monkeypatch.setenv("SOCTALK_TENANT_CHART_VERSION", "0.2.0")
    await issue_agent(seeded_tenant.id, FakeRequest(mssp_session))

    # Claim + complete upgrade_helm_release.
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    upgrade_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "upgrade_helm_release")
            .where(AgentJob.status == "in_flight")
        )
    ).scalar_one()
    await _complete(mssp_session, upgrade_job.id, outcome="success")

    # Post-apply wait_for_ready (new idempotency key so this one
    # coexists alongside the first-install wait_for_ready row in the
    # same installation).
    wait_job = (
        await mssp_session.execute(
            select(AgentJob)
            .where(AgentJob.installation_id == installation_id)
            .where(AgentJob.kind == "wait_for_ready")
            .where(AgentJob.status == "pending")
        )
    ).scalar_one()
    await claim_job(
        FakeRequest(mssp_session), wait=0,
        authorization=f"Bearer {_current_runtime_plaintext(mssp_session)}",
    )
    await _complete(mssp_session, wait_job.id, outcome="success")

    installation = (
        await mssp_session.execute(
            select(TenantInstallation).where(
                TenantInstallation.id == installation_id
            )
        )
    ).scalar_one()
    assert installation.state == "active"
    assert installation.reported_chart_version == "0.2.0"
    assert installation.desired_action == "none"

    # Audit distinguishes upgrade success from first-install success.
    upgrade_succ = (
        await mssp_session.execute(
            select(TenantInstallationEvent)
            .where(TenantInstallationEvent.installation_id == installation_id)
            .where(TenantInstallationEvent.event_type == "upgrade_succeeded")
        )
    ).scalars().all()
    assert len(upgrade_succ) == 1
