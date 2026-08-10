"""Assertions that the legacy POST /api/mssp/tenants no longer
provisions inline, and that ``:decommission`` is async.

Both contracts are the review-closing invariants: a caller that hits
either endpoint must not trigger helm from inside the request handler.
The worker owns every data-plane mutation now.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from soctalk.core.tenancy.models import (
    IntegrationConfig,
    Organization,
    ProvisioningJob,
    Tenant,
    TenantLifecycleEvent,
    TenantState,
)

SKIP_INTEGRATION = os.getenv("SKIP_INTEGRATION", "0") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        SKIP_INTEGRATION,
        reason="SKIP_INTEGRATION set; tenants-API async tests need Postgres",
    ),
]


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
async def mssp_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_mssp_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_org(
    mssp_session: AsyncSession, admin_session: AsyncSession
) -> Organization:
    await admin_session.execute(
        text(
            "TRUNCATE tenant_lifecycle_events, integration_configs, "
            "branding_configs, tenant_secrets, provisioning_jobs, "
            "tenants, organizations CASCADE"
        )
    )
    await admin_session.commit()
    org = Organization(
        mssp_id=uuid4(), mssp_name="Legacy-API Test", slug="legacy-api-test",
        install_id=uuid4(), install_label="test",
    )
    mssp_session.add(org)
    await mssp_session.commit()
    return org


# ---------------------------------------------------------------------------
# Call the handler function directly so we don't need the full FastAPI
# app stack (auth middleware, DB middleware, etc.). The guarantees we
# want to prove are about what the handler writes to the DB and what it
# does NOT call — not HTTP plumbing.
# ---------------------------------------------------------------------------


async def test_legacy_create_is_identity_only_no_provisioning(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """Review gap #4: POST /api/mssp/tenants must not provision inline."""

    from soctalk.core.api.tenants import TenantCreate, create_tenant
    from soctalk.core.provisioning import controller as ctrl_mod

    # Poison the controller so any inline provisioning attempt is obvious.
    sentinel = {"called": False}

    async def forbidden(*_a, **_kw):
        sentinel["called"] = True
        raise AssertionError(
            "create_tenant must not call TenantController.provision"
        )

    monkeypatch.setattr(ctrl_mod.TenantController, "provision", forbidden)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantCreate(
        slug=f"id{uuid4().hex[:8]}",
        display_name="Identity Only",
    )

    result = await create_tenant(payload, FakeRequest())
    assert sentinel["called"] is False
    assert result.state == TenantState.PENDING.value
    assert result.profile in (None, "legacy", "poc")

    # No provisioning job should have been enqueued on this path.
    jobs = (
        await mssp_session.execute(
            select(ProvisioningJob).where(ProvisioningJob.tenant_id == result.id)
        )
    ).scalars().all()
    assert jobs == []


async def test_decommission_enqueues_job_and_returns_immediately(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """Review gap #4: :decommission must not call helm from the handler."""

    from soctalk.core.api.tenants import decommission_tenant
    from soctalk.core.provisioning import controller as ctrl_mod

    sentinel = {"called": False}

    async def forbidden(*_a, **_kw):
        sentinel["called"] = True
        raise AssertionError(
            "decommission_tenant must not call TenantController.decommission"
        )

    monkeypatch.setattr(ctrl_mod.TenantController, "decommission", forbidden)

    tenant = Tenant(
        slug=f"dc{uuid4().hex[:8]}",
        display_name="Decomm target",
        state=TenantState.ACTIVE.value,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.commit()
    await mssp_session.refresh(tenant)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await decommission_tenant(tenant.id, FakeRequest(), force=False)
    assert sentinel["called"] is False
    assert result.state == TenantState.DECOMMISSIONING.value

    jobs = (
        await mssp_session.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.tenant_id == tenant.id)
            .where(ProvisioningJob.kind == "tenant.decommission")
        )
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == "pending"


async def test_decommission_is_idempotent_under_double_call(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """Two :decommission calls on the same tenant enqueue at most one
    active job (partial unique index would otherwise throw)."""

    from soctalk.core.api.tenants import decommission_tenant
    from soctalk.core.provisioning import controller as ctrl_mod

    async def noop(*_a, **_kw):  # let the call through, but do nothing
        raise AssertionError("unreachable")

    monkeypatch.setattr(ctrl_mod.TenantController, "decommission", noop)

    tenant = Tenant(
        slug=f"dd{uuid4().hex[:8]}",
        display_name="Double Decomm",
        state=TenantState.ACTIVE.value,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.commit()
    await mssp_session.refresh(tenant)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    await decommission_tenant(tenant.id, FakeRequest(), force=False)
    await decommission_tenant(tenant.id, FakeRequest(), force=False)

    jobs = (
        await mssp_session.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.tenant_id == tenant.id)
            .where(ProvisioningJob.kind == "tenant.decommission")
        )
    ).scalars().all()
    assert len(jobs) == 1


async def test_provided_profile_persists_external_wazuh_fields(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """The 'provided' deployment profile lets a tenant point at an
    external Wazuh deployment. The five new IntegrationConfig.wazuh_*
    credential / endpoint columns must round-trip via Postgres so the
    renderer (next feature) can read them back when building the
    adapter values.
    """
    tenant = Tenant(
        slug=f"pv{uuid4().hex[:8]}",
        display_name="Provided-SIEM Tenant",
        state=TenantState.PENDING.value,
        profile="provided",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.flush()

    integration = IntegrationConfig(
        tenant_id=tenant.id,
        wazuh_enabled=True,
        wazuh_username="soctalk-adapter",
        wazuh_password_plain="s3cret-pw-" + uuid4().hex,
        wazuh_api_token_plain="tok-" + uuid4().hex,
        wazuh_indexer_url="https://indexer.example.com:9200",
        wazuh_api_url="https://wazuh.example.com:55000",
        wazuh_indexer_username="indexer-ro",
        wazuh_indexer_password_plain="idx-pw-" + uuid4().hex,
    )
    mssp_session.add(integration)
    await mssp_session.commit()

    # Drop ORM cache so we hit the DB on the next read.
    mssp_session.expunge_all()

    read_back = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()

    assert read_back.wazuh_username == integration.wazuh_username
    assert read_back.wazuh_password_plain == integration.wazuh_password_plain
    assert read_back.wazuh_api_token_plain == integration.wazuh_api_token_plain
    assert read_back.wazuh_indexer_url == integration.wazuh_indexer_url
    assert read_back.wazuh_api_url == integration.wazuh_api_url
    assert read_back.wazuh_indexer_username == integration.wazuh_indexer_username
    assert (
        read_back.wazuh_indexer_password_plain
        == integration.wazuh_indexer_password_plain
    )

    # Tenant.profile also round-trips with the new enum value.
    tenant_read = (
        await mssp_session.execute(select(Tenant).where(Tenant.id == tenant.id))
    ).scalar_one()
    assert tenant_read.profile == "provided"


async def test_onboard_provided_persists_external_wazuh_fields(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """The onboarding wizard payload carries external Wazuh connection
    material for the 'provided' profile inside a nested ``external_siem``
    block. ``onboard_tenant`` must write the *full* credential set onto the
    tenant's IntegrationConfig (passwords/token → *_plain columns) so the
    adapter and chat resolver can later reach the tenant-supplied SIEM.

    Exercises the complete payload → column mapping, including api_token and
    verify_ssl.
    """
    from soctalk.core.api.tenants import (
        ExternalSiemOnboard,
        TenantOnboard,
        onboard_tenant,
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    pw = "byo-pw-" + uuid4().hex
    tok = "byo-tok-" + uuid4().hex
    ipw = "byo-ipw-" + uuid4().hex
    payload = TenantOnboard(
        slug=f"pv{uuid4().hex[:8]}",
        display_name="BYO Wazuh Tenant",
        profile="provided",
        llm_api_key="sk-byo-" + uuid4().hex,
        external_siem=ExternalSiemOnboard(
            indexer_url="https://indexer.example.com:9200",
            indexer_username="indexer-ro",
            indexer_password=ipw,
            api_url="https://wazuh.example.com:55000",
            api_username="soctalk-adapter",
            api_password=pw,
            api_token=tok,
            verify_ssl=False,
        ),
    )

    result = await onboard_tenant(payload, FakeRequest())
    assert result.profile == "provided"

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    # api side
    assert integration.wazuh_api_url == "https://wazuh.example.com:55000"
    assert integration.wazuh_username == "soctalk-adapter"
    assert integration.wazuh_password_plain == pw
    assert integration.wazuh_api_token_plain == tok
    # indexer side
    assert integration.wazuh_indexer_url == "https://indexer.example.com:9200"
    assert integration.wazuh_indexer_username == "indexer-ro"
    assert integration.wazuh_indexer_password_plain == ipw
    # flags
    assert integration.wazuh_verify_ssl is False


async def test_onboard_non_provided_leaves_external_wazuh_null(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """For poc/persistent the wizard must ignore any ``external_siem`` block —
    those columns stay NULL (and verify_ssl keeps its default) so the
    controller fills in-cluster URLs. Guards against a regression where a
    smuggled external_siem leaks onto a non-provided tenant.
    """
    from soctalk.core.api.tenants import (
        ExternalSiemOnboard,
        TenantOnboard,
        onboard_tenant,
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    # Even if a client smuggles a full external_siem on a poc onboard, drop it.
    payload = TenantOnboard(
        slug=f"pc{uuid4().hex[:8]}",
        display_name="PoC Tenant",
        profile="poc",
        external_siem=ExternalSiemOnboard(
            indexer_url="https://should-be-ignored:9200",
            indexer_username="ignored",
            indexer_password="ignored",
            api_url="https://should-be-ignored:55000",
            api_username="ignored",
            api_password="ignored",
            api_token="ignored",
            verify_ssl=False,
        ),
    )

    result = await onboard_tenant(payload, FakeRequest())
    assert result.profile == "poc"

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.wazuh_api_url is None
    assert integration.wazuh_username is None
    assert integration.wazuh_password_plain is None
    assert integration.wazuh_api_token_plain is None
    assert integration.wazuh_indexer_url is None
    assert integration.wazuh_indexer_username is None
    assert integration.wazuh_indexer_password_plain is None
    # verify_ssl untouched → DB default True (not the smuggled False).
    assert integration.wazuh_verify_ssl is True


def _onboard_route_status() -> int:
    """Declared HTTP status for POST /api/mssp/tenants/onboard (202 Accepted)."""
    from soctalk.core.api.tenants import router

    for route in router.routes:
        if getattr(route, "path", "").endswith("/onboard"):
            return route.status_code  # type: ignore[attr-defined]
    raise AssertionError("onboard route not registered")


class _FakeK8s:
    """Records the external-SIEM dual-write side effects.

    Mirrors the slice of :class:`K8sClient` the PATCH endpoint touches:
    a create-or-patch Secret write and a Deployment template-annotation
    patch (the rolling-restart mechanism). Everything is treated as
    idempotent + always-succeeds so the tests assert on what was called.
    """

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], dict[str, str]] = {}
        self.deployment_patches: list[tuple[str, str, dict]] = []

    async def put_secret(self, namespace, name, data, *, labels=None) -> None:
        self.secrets[(namespace, name)] = dict(data)

    async def patch_deployment(self, namespace, name, patch) -> None:
        self.deployment_patches.append((namespace, name, patch))


async def test_patch_external_siem(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """PATCH /api/mssp/tenants/{id}/external-siem updates BOTH credential
    pairs, rewrites the ``tenant-external-siem-creds`` Secret, rolls the
    ``soctalk-adapter`` Deployment (annotation changes), and returns a
    masked view where passwords + token are NEVER echoed.
    """
    import soctalk.core.api.tenants as tenants_mod
    from soctalk.core.api.tenants import (
        ExternalSiemPatch,
        update_tenant_external_siem,
    )

    fake = _FakeK8s()
    monkeypatch.setattr(tenants_mod, "new_k8s_client", lambda: fake)

    slug = f"es{uuid4().hex[:8]}"
    tenant = Tenant(
        slug=slug,
        display_name="External SIEM Patch",
        state=TenantState.ACTIVE.value,
        profile="provided",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.flush()
    mssp_session.add(
        IntegrationConfig(
            tenant_id=tenant.id,
            wazuh_indexer_url="https://old-indexer:9200",
            wazuh_indexer_username="old-idx",
            wazuh_indexer_password_plain="old-idx-pw",
            wazuh_api_url="https://old-wazuh:55000",
            wazuh_username="old-api",
            wazuh_password_plain="old-api-pw",
            wazuh_verify_ssl=True,
        )
    )
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    new_idx_pw = "new-idx-pw-" + uuid4().hex
    new_api_pw = "new-api-pw-" + uuid4().hex
    new_token = "new-token-" + uuid4().hex
    result = await update_tenant_external_siem(
        tenant.id,
        ExternalSiemPatch(
            indexer_username="new-idx",
            indexer_password=new_idx_pw,
            api_username="new-api",
            api_password=new_api_pw,
            api_token=new_token,
            verify_ssl=False,  # False is NOT None → must be written
        ),
        FakeRequest(),
    )

    # --- masked response: has_* flags set; plaintext NEVER echoed ---
    assert result.has_indexer_password is True
    assert result.has_api_password is True
    assert result.has_api_token is True
    assert result.indexer_username == "new-idx"
    assert result.api_username == "new-api"
    assert result.verify_ssl is False
    serialized = str(result.model_dump())
    assert new_idx_pw not in serialized
    assert new_api_pw not in serialized
    assert new_token not in serialized

    # --- BOTH consumers of the Secret rolled (rolling restart): the adapter
    # (alert ingest) and the runs-worker (Wazuh MCP enrichment, issue #109),
    # whose secretKeyRef env would otherwise keep stale credentials. ---
    ns = f"tenant-{slug}"
    for deploy in ("soctalk-adapter", "soctalk-runs-worker"):
        patches = [
            p
            for (pns, name, p) in fake.deployment_patches
            if pns == ns and name == deploy
        ]
        assert patches, f"expected a {deploy} deployment patch"
        annotations = patches[-1]["spec"]["template"]["metadata"][
            "annotations"
        ]
        assert "soctalk.io/restartedAt" in annotations
        assert annotations["soctalk.io/restartedAt"].isdigit()

    # --- Secret rewritten with BOTH pairs + token, UPPERCASE keys ---
    secret = fake.secrets[(ns, "tenant-external-siem-creds")]
    assert secret["INDEXER_USERNAME"] == "new-idx"
    assert secret["INDEXER_PASSWORD"] == new_idx_pw
    assert secret["WAZUH_API_USERNAME"] == "new-api"
    assert secret["WAZUH_API_PASSWORD"] == new_api_pw
    assert secret["WAZUH_API_TOKEN"] == new_token

    # --- Postgres columns updated; untouched URLs preserved ---
    mssp_session.expunge_all()
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    assert cfg.wazuh_indexer_password_plain == new_idx_pw
    assert cfg.wazuh_password_plain == new_api_pw
    assert cfg.wazuh_api_token_plain == new_token
    assert cfg.wazuh_verify_ssl is False
    assert cfg.wazuh_indexer_url == "https://old-indexer:9200"
    assert cfg.wazuh_api_url == "https://old-wazuh:55000"


async def test_patch_external_siem_works_for_poc_profile(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """The endpoint is profile-agnostic: a ``poc`` tenant can be repointed
    at an external SIEM after the fact. The columns + the Secret update even
    though the tenant is NOT on the ``provided`` profile, and there is no
    profile gate that 4xxs the request.
    """
    import soctalk.core.api.tenants as tenants_mod
    from soctalk.core.api.tenants import (
        ExternalSiemPatch,
        update_tenant_external_siem,
    )

    fake = _FakeK8s()
    monkeypatch.setattr(tenants_mod, "new_k8s_client", lambda: fake)

    slug = f"pc{uuid4().hex[:8]}"
    tenant = Tenant(
        slug=slug,
        display_name="PoC repoint",
        state=TenantState.ACTIVE.value,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.flush()
    mssp_session.add(
        IntegrationConfig(
            tenant_id=tenant.id,
            wazuh_url="https://wazuh-poc.in-cluster",
        )
    )
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    idx_pw = "poc-idx-pw-" + uuid4().hex
    api_pw = "poc-api-pw-" + uuid4().hex
    result = await update_tenant_external_siem(
        tenant.id,
        ExternalSiemPatch(
            indexer_url="https://ext-indexer:9200",
            indexer_username="ext-idx",
            indexer_password=idx_pw,
            api_url="https://ext-wazuh:55000",
            api_username="ext-api",
            api_password=api_pw,
            # api_token intentionally omitted → key omitted from the Secret
        ),
        FakeRequest(),
    )

    assert result.has_indexer_password is True
    assert result.has_api_password is True
    assert result.has_api_token is False

    # Profile was never gated and is unchanged.
    tenant_row = (
        await mssp_session.execute(select(Tenant).where(Tenant.id == tenant.id))
    ).scalar_one()
    assert tenant_row.profile == "poc"

    # Columns updated despite the non-provided profile.
    mssp_session.expunge_all()
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    assert cfg.wazuh_indexer_url == "https://ext-indexer:9200"
    assert cfg.wazuh_indexer_username == "ext-idx"
    assert cfg.wazuh_indexer_password_plain == idx_pw
    assert cfg.wazuh_api_url == "https://ext-wazuh:55000"
    assert cfg.wazuh_username == "ext-api"
    assert cfg.wazuh_password_plain == api_pw

    # Secret written to the tenant namespace; token key omitted when unset.
    secret = fake.secrets[(f"tenant-{slug}", "tenant-external-siem-creds")]
    assert secret["INDEXER_USERNAME"] == "ext-idx"
    assert secret["INDEXER_PASSWORD"] == idx_pw
    assert secret["WAZUH_API_USERNAME"] == "ext-api"
    assert secret["WAZUH_API_PASSWORD"] == api_pw
    assert "WAZUH_API_TOKEN" not in secret


async def test_onboard_provided_profile_happy_path(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """profile='provided' + a complete external_siem block → 202 Accepted, a
    Tenant row is created, and BOTH the Wazuh API
    (api_username→wazuh_username) and the Indexer
    (indexer_username→wazuh_indexer_username) credentials land on the
    IntegrationConfig.

    api_token is omitted to prove its absence never blocks onboarding.
    """
    from soctalk.core.api.tenants import (
        ExternalSiemOnboard,
        TenantOnboard,
        onboard_tenant,
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"hp{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Provided Happy Path",
        profile="provided",
        llm_api_key="sk-happy-" + uuid4().hex,
        external_siem=ExternalSiemOnboard(
            indexer_url="https://indexer.example.com:9200",
            indexer_username="indexer-ro",
            indexer_password="idx-pw-" + uuid4().hex,
            api_url="https://wazuh.example.com:55000",
            api_username="soctalk-adapter",
            api_password="api-pw-" + uuid4().hex,
            # api_token intentionally omitted (defaults to None) — must not 422.
        ),
    )

    # Success contract for the onboard wizard is HTTP 202 Accepted.
    assert _onboard_route_status() == 202

    result = await onboard_tenant(payload, FakeRequest())
    assert result.profile == "provided"

    # A Tenant row exists for the slug.
    tenant_row = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one()
    assert str(tenant_row.id) == str(result.id)

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_row.id
            )
        )
    ).scalar_one()
    # Both credential families are populated.
    assert integration.wazuh_username == "soctalk-adapter"
    assert integration.wazuh_indexer_username == "indexer-ro"
    # api_token absent → stored NULL, and onboarding still succeeded.
    assert integration.wazuh_api_token_plain is None


async def test_onboard_provided_profile_missing_creds(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """profile='provided' with external_siem=None → HTTP 422 with field-level
    errors and NO Tenant row created.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"mc{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Missing Creds",
        profile="provided",
        external_siem=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422

    # No Tenant row was created for the rejected onboard.
    rows = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalars().all()
    assert rows == []


async def test_onboard_provided_profile_partial_creds(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """profile='provided' with an external_siem block whose api_password is
    empty → HTTP 422 and NO Tenant row created. A partially-filled block is
    just as invalid as a missing one.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import (
        ExternalSiemOnboard,
        TenantOnboard,
        onboard_tenant,
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"pp{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Partial Creds",
        profile="provided",
        external_siem=ExternalSiemOnboard(
            indexer_url="https://indexer.example.com:9200",
            indexer_username="indexer-ro",
            indexer_password="idx-pw",
            api_url="https://wazuh.example.com:55000",
            api_username="soctalk-adapter",
            api_password="",  # empty → must reject
            api_token=None,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422

    rows = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# Adapter-status proxy: GET /api/mssp/tenants/{id}/adapter-status
# ---------------------------------------------------------------------------
#
# The detail-page External-SIEM panel polls live adapter ingest status. The
# browser CANNOT reach the per-tenant adapter Service (cluster-internal DNS,
# no ingress), so the control plane SERVER-SIDE proxies to the adapter's
# /health/ready and relays the JSON. A wedged/absent adapter must degrade
# softly — HTTP 200 with ``{"reachable": false, "error": "<msg>"}`` — so the
# poller renders a degraded badge instead of surfacing an API error.


class _FakeAdapterResponse:
    """Minimal stand-in for the ``httpx.Response`` the adapter returns."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        # The tests only exercise the 200 success path + a connection error
        # raised from .get(); a non-2xx here would simply be caught by the
        # handler's blanket except and folded into the soft-fail body.
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAdapterHttpClient:
    """Stand-in for ``httpx.AsyncClient`` used by the adapter-status proxy.

    Records the URL fetched and either returns a canned response or raises a
    connection error to simulate an unreachable adapter — so the test never
    opens a real socket to the (non-existent) per-tenant adapter Service.
    """

    last_url: str | None = None

    def __init__(
        self, *, payload: dict | None = None, exc: Exception | None = None
    ) -> None:
        self._payload = payload
        self._exc = exc

    async def __aenter__(self) -> "_FakeAdapterHttpClient":
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def get(self, url: str) -> _FakeAdapterResponse:
        type(self).last_url = url
        if self._exc is not None:
            raise self._exc
        assert self._payload is not None
        return _FakeAdapterResponse(self._payload)


async def test_adapter_status_proxies_adapter_health_ready(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """GET /api/mssp/tenants/{id}/adapter-status server-side proxies to the
    tenant adapter's ``/health/ready`` and relays the JSON verbatim.

    The outbound httpx call is mocked — no real adapter is contacted. The
    proxied URL must be the in-cluster Service address
    ``http://soctalk-adapter.tenant-<slug>.svc.cluster.local:8080/health/ready``
    (NOT a browser-reachable URL), proving this is a server-side proxy rather
    than browser CORS.
    """
    import soctalk.core.api.tenants as tenants_mod
    from soctalk.core.api.tenants import get_tenant_adapter_status

    slug = f"as{uuid4().hex[:8]}"
    tenant = Tenant(
        slug=slug,
        display_name="Adapter Status",
        state=TenantState.ACTIVE.value,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.commit()
    await mssp_session.refresh(tenant)

    payload = {
        "ok": True,
        "alerts_forwarded": 7,
        "last_alert_ts": "2026-06-09T00:00:00+00:00",
        "last_ingest_error": None,
    }
    captured: dict[str, _FakeAdapterHttpClient] = {}

    def _factory() -> _FakeAdapterHttpClient:
        client = _FakeAdapterHttpClient(payload=payload)
        captured["client"] = client
        return client

    monkeypatch.setattr(tenants_mod, "_new_adapter_http_client", _factory)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await get_tenant_adapter_status(tenant.id, FakeRequest())

    # Adapter JSON is proxied through unchanged.
    assert result["alerts_forwarded"] == 7
    assert result["last_alert_ts"] == "2026-06-09T00:00:00+00:00"
    assert result["last_ingest_error"] is None

    # Server-side proxy → in-cluster Service DNS, NOT a browser CORS request.
    assert (
        captured["client"].last_url
        == f"http://soctalk-adapter.tenant-{slug}"
        ".svc.cluster.local:8080/health/ready"
    )


async def test_adapter_status_soft_fails_on_unreachable_adapter(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """When the adapter is unreachable (DNS / connection error), the proxy must
    NOT raise / 5xx. It returns HTTP 200 with
    ``{"reachable": false, "error": "<msg>"}`` so the detail-page poller renders
    a degraded state instead of an API error.
    """
    import httpx

    import soctalk.core.api.tenants as tenants_mod
    from soctalk.core.api.tenants import get_tenant_adapter_status

    slug = f"dn{uuid4().hex[:8]}"
    tenant = Tenant(
        slug=slug,
        display_name="Adapter Down",
        state=TenantState.ACTIVE.value,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.commit()
    await mssp_session.refresh(tenant)

    boom = httpx.ConnectError("Name or service not known")
    monkeypatch.setattr(
        tenants_mod,
        "_new_adapter_http_client",
        lambda: _FakeAdapterHttpClient(exc=boom),
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    # Must NOT raise — the soft-fail body is returned as the default HTTP 200.
    result = await get_tenant_adapter_status(tenant.id, FakeRequest())
    assert result["reachable"] is False
    assert "Name or service not known" in result["error"]


async def test_adapter_status_unknown_tenant_404(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """An unknown tenant id is a real 404 — distinct from the soft-fail body
    used for an unreachable (but existing) tenant's adapter.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import get_tenant_adapter_status

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    with pytest.raises(HTTPException) as exc_info:
        await get_tenant_adapter_status(uuid4(), FakeRequest())
    assert exc_info.value.status_code == 404


def test_adapter_status_route_declares_no_error_status():
    """The adapter-status route declares no explicit (error) status_code so the
    soft-fail body is served as the default HTTP 200, not a 4xx/5xx.
    """
    from soctalk.core.api.tenants import router

    for route in router.routes:
        if getattr(route, "path", "").endswith("/adapter-status"):
            assert route.status_code in (None, 200)
            return
    raise AssertionError("adapter-status route not registered")


# ---------------------------------------------------------------------------
# Per-tenant LLM credentials on onboard (tenant.llm.onboard-key)
# ---------------------------------------------------------------------------
#
# TenantOnboard carries llm_api_key + llm_provider. REQUIRED for
# profile='provided' (field-level 422 BEFORE any DB write → zero Tenant
# rows); optional for poc/persistent (install-shared fallback unchanged).
# Non-null values persist onto IntegrationConfig (llm_api_key_plain /
# llm_provider) in the SAME transaction as the Tenant row. The raw key is
# never echoed in any response body or error detail.


def _complete_external_siem():
    from soctalk.core.api.tenants import ExternalSiemOnboard

    return ExternalSiemOnboard(
        indexer_url="https://indexer.example.com:9200",
        indexer_username="indexer-ro",
        indexer_password="idx-pw-" + uuid4().hex,
        api_url="https://wazuh.example.com:55000",
        api_username="soctalk-adapter",
        api_password="api-pw-" + uuid4().hex,
    )


async def test_onboard_provided_without_llm_key_422_no_tenant_row(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """profile='provided' with complete external_siem but NO llm_api_key →
    HTTP 422 with a field-level error at ['body', 'llm_api_key'], raised
    BEFORE any DB write so zero Tenant rows exist for the slug.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"nk{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="No LLM Key",
        profile="provided",
        external_siem=_complete_external_siem(),
        # llm_api_key intentionally omitted.
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422
    # Field-level error shape: loc points at body.llm_api_key.
    locs = [tuple(e["loc"]) for e in exc_info.value.detail]
    assert ("body", "llm_api_key") in locs

    # Zero Tenant rows created for the rejected onboard.
    rows = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalars().all()
    assert rows == []


async def test_onboard_provided_blank_llm_key_422(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A whitespace-only llm_api_key is just as missing as an absent one —
    422 with the field-level error, no Tenant row, and the (blank) key value
    never reflected into the error detail.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"bk{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Blank LLM Key",
        profile="provided",
        llm_api_key="   ",
        external_siem=_complete_external_siem(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422
    locs = [tuple(e["loc"]) for e in exc_info.value.detail]
    assert ("body", "llm_api_key") in locs

    rows = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalars().all()
    assert rows == []


async def test_onboard_provided_with_llm_key_persists_never_echoes(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """provided onboard with a key → 202 declared on the route, the key
    persists onto IntegrationConfig.llm_api_key_plain in the same transaction
    as the Tenant row, and the raw key is absent from the response body.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    key = "sk-proj-onboard-" + uuid4().hex
    slug = f"lk{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="LLM Key Onboard",
        profile="provided",
        llm_api_key=key,
        external_siem=_complete_external_siem(),
    )

    assert _onboard_route_status() == 202
    result = await onboard_tenant(payload, FakeRequest())

    # The key is NEVER echoed in the response body.
    assert key not in str(result.model_dump())

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_api_key_plain == key


async def test_onboard_llm_provider_normalized_openai(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """llm_provider='openai' is canonicalized to 'openai-compatible' on the
    persisted IntegrationConfig row (same normalizer as LlmConfigUpdate) so
    the next helm render passes the tenant chart's values.schema.json.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"no{uuid4().hex[:8]}",
        display_name="Normalized Provider",
        profile="provided",
        llm_api_key="sk-openai-" + uuid4().hex,
        llm_provider="openai",
        external_siem=_complete_external_siem(),
    )

    result = await onboard_tenant(payload, FakeRequest())
    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_provider == "openai-compatible"


async def test_onboard_sk_ant_key_infers_anthropic_and_flips_model(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """An sk-ant- key with no explicit provider → llm_provider='anthropic'
    is inferred, and the clearly-mismatched default model (gpt-4o) is
    flipped to the Anthropic default so the runs-worker never renders an
    OpenAI model name against the Anthropic SDK.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"an{uuid4().hex[:8]}",
        display_name="Anthropic Inference",
        profile="provided",
        llm_api_key="sk-ant-" + uuid4().hex,
        # llm_provider intentionally omitted → inferred from the key prefix.
        external_siem=_complete_external_siem(),
    )

    result = await onboard_tenant(payload, FakeRequest())
    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_provider == "anthropic"
    # Default model flipped off gpt-4o (Anthropic SDK would reject it).
    assert integration.llm_model != "gpt-4o"
    assert integration.llm_model.startswith("claude")


async def test_onboard_non_sk_ant_key_keeps_openai_compatible_default(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A non-sk-ant key with no explicit provider keeps the
    openai-compatible default and the default model stays gpt-4o.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"oc{uuid4().hex[:8]}",
        display_name="OpenAI-compatible Inference",
        profile="provided",
        llm_api_key="sk-proj-" + uuid4().hex,
        external_siem=_complete_external_siem(),
    )

    result = await onboard_tenant(payload, FakeRequest())
    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_provider == "openai-compatible"
    assert integration.llm_model == "gpt-4o"


async def test_onboard_folded_defaults_reject_anthropic_served_engine(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """The request body can omit provider/engine, so validate the pair after
    install defaults are folded in. Anthropic + sglang must fail before a Tenant
    row is written.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    monkeypatch.setenv("SOCTALK_LLM_PROVIDER_DEFAULT", "anthropic")
    monkeypatch.setenv("SOCTALK_LLM_ENGINE_DEFAULT", "sglang")
    slug = f"ed{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Contradictory Defaults",
        profile="poc",
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422
    assert "not valid with provider 'anthropic'" in str(exc_info.value.detail)

    rows = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalars().all()
    assert rows == []


async def test_onboard_rejects_served_engine_with_default_base_url(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A served primary engine needs a real endpoint at onboard too; otherwise
    the stored row would render SOCTALK_LLM_ENGINE but no OPENAI_BASE_URL.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"sb{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Served Without Endpoint",
        profile="poc",
        llm_engine="sglang",
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422
    assert "requires a custom llm_base_url" in str(exc_info.value.detail)

    rows = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalars().all()
    assert rows == []


async def test_onboard_poc_without_llm_key_still_succeeds(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """For poc the LLM key stays optional: onboarding without one succeeds
    (202 route contract) and llm_api_key_plain stays NULL so the controller's
    install-shared fallback path is unchanged.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"po{uuid4().hex[:8]}",
        display_name="PoC No Key",
        profile="poc",
        # llm_api_key intentionally omitted — must NOT 422.
    )

    assert _onboard_route_status() == 202
    result = await onboard_tenant(payload, FakeRequest())
    assert result.profile == "poc"

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_api_key_plain is None
    # Column default preserved when no provider was supplied or inferred.
    assert integration.llm_provider == "openai-compatible"


async def test_onboard_poc_with_llm_key_persists(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Optional-for-poc still means *persisted* when supplied: a poc onboard
    carrying a key lands it on llm_api_key_plain in the same transaction.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    key = "sk-poc-" + uuid4().hex
    payload = TenantOnboard(
        slug=f"pk{uuid4().hex[:8]}",
        display_name="PoC With Key",
        profile="poc",
        llm_api_key=key,
    )

    result = await onboard_tenant(payload, FakeRequest())
    assert key not in str(result.model_dump())

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_api_key_plain == key


async def test_onboard_422_detail_never_contains_llm_key(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A provided onboard that fails validation on the external-SIEM block
    while carrying an llm_api_key must not reflect the key into the 422
    error detail.
    """
    from fastapi import HTTPException

    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    key = "sk-secret-" + uuid4().hex
    payload = TenantOnboard(
        slug=f"se{uuid4().hex[:8]}",
        display_name="Secret Never Echoed",
        profile="provided",
        llm_api_key=key,
        external_siem=None,  # forces the 422
    )

    with pytest.raises(HTTPException) as exc_info:
        await onboard_tenant(payload, FakeRequest())
    assert exc_info.value.status_code == 422
    assert key not in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# PATCH /api/mssp/tenants/{id}/llm — job-kind routing (tenant.llm.reconcile-active)
# ---------------------------------------------------------------------------
#
# Chart-affecting LLM edits (provider/base_url/model) must actually
# propagate. For an ACTIVE tenant the handler enqueues 'tenant.reconcile'
# (provision() early-returns on active and active→provisioning is illegal);
# for any other state it keeps enqueuing 'tenant.provision'. A key-only
# PATCH never enqueues anything.


async def _seed_llm_tenant(
    mssp_session: AsyncSession,
    seeded_org: Organization,
    *,
    state: str,
    llm_base_url: str = "https://api.openai.com/v1",
) -> Tenant:
    tenant = Tenant(
        slug=f"lm{uuid4().hex[:8]}",
        display_name="LLM Patch Tenant",
        state=state,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.flush()
    mssp_session.add(
        IntegrationConfig(
            tenant_id=tenant.id,
            llm_base_url=llm_base_url,
            # Cost accounting is ON in CI (#141 phase 3), and these tenants
            # point at gateway endpoints whose (provider_kind, model) pairs are
            # deliberately absent from the catalog - a gateway's price for a
            # model is not the vendor's price for it. A tenant price override is
            # exactly what a real operator sets in that situation, so the
            # fixture sets one instead of the suite switching the feature off.
            llm_model_prices={
                m: {"input": 1.0, "output": 2.0}
                for m in (
                    "gpt-4o", "gpt-4o-mini", "gpt-4", "o3",
                    "claude-3-5-haiku-latest", "claude-3-5-haiku",
                )
            },
        )
    )
    await mssp_session.commit()
    await mssp_session.refresh(tenant)
    return tenant


async def _llm_jobs_by_kind(
    mssp_session: AsyncSession, tenant_id
) -> dict[str, int]:
    jobs = (
        await mssp_session.execute(
            select(ProvisioningJob).where(ProvisioningJob.tenant_id == tenant_id)
        )
    ).scalars().all()
    out: dict[str, int] = {}
    for j in jobs:
        out[j.kind] = out.get(j.kind, 0) + 1
    return out


async def test_patch_llm_base_url_active_tenant_enqueues_reconcile(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """base_url change on an ACTIVE tenant → exactly one tenant.reconcile
    job, zero tenant.provision jobs.
    """
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(base_url="https://llm.newhost.example/v1"),
        FakeRequest(),
    )

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.reconcile") == 1
    assert "tenant.provision" not in kinds

    # Idempotent under double-PATCH: the pre-check sees the pending
    # reconcile job and does not enqueue a second one.
    await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(base_url="https://llm.other.example/v1"),
        FakeRequest(),
    )
    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.reconcile") == 1


async def test_patch_llm_base_url_degraded_tenant_enqueues_provision(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """The same PATCH on a DEGRADED tenant keeps the existing behavior:
    enqueue tenant.provision (provision() handles degraded → provisioning).
    """
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.DEGRADED.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(base_url="https://llm.newhost.example/v1"),
        FakeRequest(),
    )

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.provision") == 1
    assert "tenant.reconcile" not in kinds


async def test_patch_llm_key_only_enqueues_no_job(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """A key-only PATCH is propagated by the Secret rewrite + rolling
    restart — no chart re-render needed, so neither job kind is enqueued.
    """
    import soctalk.core.api.llm_config as llm_mod
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    async def _noop_write(*_a, **_kw):
        return None

    monkeypatch.setattr(llm_mod, "_write_api_key", _noop_write)

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(api_key="sk-rotated-" + uuid4().hex),
        FakeRequest(),
    )

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds == {}, f"key-only PATCH must not enqueue jobs, got {kinds}"


async def test_patch_llm_unknown_engine_rejected_at_payload_boundary():
    """Unknown primary engines must 422 instead of being stored."""
    from pydantic import ValidationError

    from soctalk.core.api.llm_config import LlmConfigUpdate

    with pytest.raises(ValidationError):
        LlmConfigUpdate(engine="banana")


async def test_patch_llm_engine_only_rejects_suppressed_base_url(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Round-16 trigger: engine-only PATCH cannot pair sglang with the hosted
    OpenAI sentinel, because the chart suppresses OPENAI_BASE_URL for it.
    """
    from fastapi import HTTPException

    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    tenant_id = tenant.id

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    with pytest.raises(HTTPException) as exc_info:
        await update_tenant_llm(
            tenant.id,
            LlmConfigUpdate(engine="sglang"),
            FakeRequest(),
        )
    assert exc_info.value.status_code == 422
    assert "requires a custom llm_base_url" in str(exc_info.value.detail)

    await mssp_session.rollback()
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    assert cfg.llm_base_url == "https://api.openai.com/v1"
    assert cfg.llm_engine is None
    assert await _llm_jobs_by_kind(mssp_session, tenant_id) == {}


async def test_patch_llm_engine_only_persists_with_custom_base_url(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Changing only llm_engine is chart-affecting: it must enqueue reconcile.

    Sending '' clears the stored engine so an operator can move from a
    self-hosted endpoint back to a gateway without stale pricing classification.
    """
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session,
        seeded_org,
        state=TenantState.ACTIVE.value,
        llm_base_url="http://sglang.internal:8000/v1",
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(engine="sglang"),
        FakeRequest(),
    )
    assert result.engine == "sglang"
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    assert cfg.llm_engine == "sglang"
    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.reconcile") == 1
    assert "tenant.provision" not in kinds

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(engine=""),
        FakeRequest(),
    )
    assert result.engine is None
    await mssp_session.refresh(cfg)
    assert cfg.llm_engine is None


async def test_patch_llm_base_url_to_sentinel_rejected_when_engine_set(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A base-url-only PATCH is also validated after merge: an existing served
    engine cannot be moved back to the suppressed hosted sentinel.
    """
    from fastapi import HTTPException

    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session,
        seeded_org,
        state=TenantState.ACTIVE.value,
        llm_base_url="http://sglang.internal:8000/v1",
    )
    tenant_id = tenant.id
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    cfg.llm_engine = "sglang"
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    with pytest.raises(HTTPException) as exc_info:
        await update_tenant_llm(
            tenant.id,
            LlmConfigUpdate(base_url="https://api.openai.com/v1"),
            FakeRequest(),
        )
    assert exc_info.value.status_code == 422
    assert "requires a custom llm_base_url" in str(exc_info.value.detail)

    await mssp_session.rollback()
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    assert cfg.llm_base_url == "http://sglang.internal:8000/v1"
    assert cfg.llm_engine == "sglang"
    assert await _llm_jobs_by_kind(mssp_session, tenant_id) == {}


async def test_patch_llm_anthropic_served_engine_rejected(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Provider and engine can arrive in one PATCH; validate the result."""
    from fastapi import HTTPException

    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    with pytest.raises(HTTPException) as exc_info:
        await update_tenant_llm(
            tenant.id,
            LlmConfigUpdate(provider="anthropic", engine="sglang"),
            FakeRequest(),
        )
    assert exc_info.value.status_code == 422
    assert "not valid with provider 'anthropic'" in str(exc_info.value.detail)


async def test_patch_llm_tier_served_engine_hosted_openai_base_url_rejected(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A tier PATCH must not persist a served engine on hosted OpenAI.

    That persisted JSONB would render SOCTALK_FAST_BASE_URL +
    SOCTALK_FAST_ENGINE and make the tenant worker fail load_config().
    """
    from fastapi import HTTPException

    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    tenant_id = tenant.id

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    with pytest.raises(HTTPException) as exc_info:
        await update_tenant_llm(
            tenant.id,
            LlmConfigUpdate(
                tiers={
                    "fast": {
                        "provider": "openai-compatible",
                        "base_url": "https://api.openai.com/v1",
                        "model": "qwen3-32b",
                        "engine": "sglang",
                    }
                }
            ),
            FakeRequest(),
        )
    assert exc_info.value.status_code == 422
    assert "requires a custom base_url" in str(exc_info.value.detail)

    await mssp_session.rollback()
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    assert cfg.llm_tiers is None
    assert await _llm_jobs_by_kind(mssp_session, tenant_id) == {}


# ---------------------------------------------------------------------------
# Onboard-time per-tenant model overrides (tenant.llm.models.onboard-api)
# ---------------------------------------------------------------------------
#
# TenantOnboard optionally carries llm_fast_model / llm_reasoning_model.
# Non-null values persist onto IntegrationConfig in the SAME transaction as
# the Tenant row; blank/whitespace values normalize to None so the columns
# stay NULL and render falls back to llm_model. Provider reconciliation
# (explicit or sk-ant- key-inferred) applies to the overrides exactly as it
# does to llm_model — only-flip-when-clearly-mismatched.


async def test_onboard_persists_model_overrides_verbatim(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Onboard with both overrides set → 202 route contract and both columns
    persisted verbatim onto IntegrationConfig alongside llm_model.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"mo{uuid4().hex[:8]}",
        display_name="Model Overrides",
        profile="poc",
        llm_fast_model="gpt-4o-mini",
        llm_reasoning_model="o3",
    )

    assert _onboard_route_status() == 202
    result = await onboard_tenant(payload, FakeRequest())

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_fast_model == "gpt-4o-mini"
    assert integration.llm_reasoning_model == "o3"
    # llm_model itself is untouched by the overrides.
    assert integration.llm_model == "gpt-4o"


async def test_onboard_omitted_model_overrides_stay_null(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Onboard without the overrides → both columns stay NULL (never
    defaulted to a concrete model) so render falls back to llm_model.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"mn{uuid4().hex[:8]}",
        display_name="No Model Overrides",
        profile="poc",
        # llm_fast_model / llm_reasoning_model intentionally omitted.
    )

    result = await onboard_tenant(payload, FakeRequest())

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_fast_model is None
    assert integration.llm_reasoning_model is None


async def test_onboard_blank_model_overrides_normalized_to_null(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Blank / whitespace-only overrides normalize to None so the columns
    stay NULL — render's ``or llm_model`` fallback then applies uniformly.
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"mb{uuid4().hex[:8]}",
        display_name="Blank Model Overrides",
        profile="poc",
        llm_fast_model="",
        llm_reasoning_model="   ",
    )
    # Normalized at the schema boundary, not just at persist time.
    assert payload.llm_fast_model is None
    assert payload.llm_reasoning_model is None

    result = await onboard_tenant(payload, FakeRequest())

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_fast_model is None
    assert integration.llm_reasoning_model is None


async def test_onboard_sk_ant_key_reconciles_model_overrides(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """An sk-ant- key infers anthropic; a clearly-OpenAI llm_fast_model is
    flipped to the Anthropic default while an already-matching 'claude-*'
    reasoning override is preserved (only-flip-when-clearly-mismatched, same
    rule as llm_model).
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant
    from soctalk.core.llm_provider import ANTHROPIC_DEFAULT_MODEL

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    payload = TenantOnboard(
        slug=f"ma{uuid4().hex[:8]}",
        display_name="Anthropic Override Reconcile",
        profile="provided",
        llm_api_key="sk-ant-" + uuid4().hex,
        # llm_provider intentionally omitted → inferred from the key prefix.
        llm_fast_model="gpt-4o-mini",
        llm_reasoning_model="claude-3-5-haiku-latest",
        external_siem=_complete_external_siem(),
    )

    result = await onboard_tenant(payload, FakeRequest())

    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    assert integration.llm_provider == "anthropic"
    # Clearly-OpenAI override flipped to the Anthropic default.
    assert integration.llm_fast_model == ANTHROPIC_DEFAULT_MODEL
    # Already-matching override preserved verbatim.
    assert integration.llm_reasoning_model == "claude-3-5-haiku-latest"


async def test_onboard_model_overrides_render_into_runs_worker(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """End-to-end: a tenant onboarded with overrides renders
    ``runsWorker.fastModel`` / ``reasoningModel`` from the persisted
    overrides via the render seam (tenant.llm.models.render).
    """
    from soctalk.core.api.tenants import TenantOnboard, onboard_tenant
    from soctalk.core.provisioning.render import render_tenant_values
    from soctalk.core.tenancy.models import BrandingConfig

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    slug = f"mr{uuid4().hex[:8]}"
    payload = TenantOnboard(
        slug=slug,
        display_name="Render Override Tenant",
        profile="poc",
        llm_fast_model="gpt-4o-mini",
        llm_reasoning_model="o3",
    )

    result = await onboard_tenant(payload, FakeRequest())

    tenant_row = (
        await mssp_session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one()
    integration = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == result.id
            )
        )
    ).scalar_one()
    branding = (
        await mssp_session.execute(
            select(BrandingConfig).where(
                BrandingConfig.tenant_id == result.id
            )
        )
    ).scalar_one()

    values = render_tenant_values(
        tenant=tenant_row,
        integration=integration,
        branding=branding,
        mssp_id=str(seeded_org.mssp_id),
        install_id=str(seeded_org.install_id),
        llm_secret_name=f"tenant-{slug}-llm",
        profile="poc",
    )
    assert values["runsWorker"]["fastModel"] == "gpt-4o-mini"
    assert values["runsWorker"]["reasoningModel"] == "o3"
    # llm.model fallback untouched.
    assert values["llm"]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# GET/PATCH /api/mssp/tenants/{id}/llm — model overrides (tenant.llm.models.patch-api)
# ---------------------------------------------------------------------------
#
# LlmConfigRead exposes fast_model / reasoning_model (null = falls back to
# llm_model); LlmConfigUpdate accepts them with tri-state semantics:
# omitted/None = leave unchanged, ''/whitespace = clear to NULL, anything
# else = set verbatim. Changes count as chart-affecting (tenant.reconcile
# for active, tenant.provision otherwise); no-op PATCHes enqueue nothing.


async def test_get_llm_returns_null_model_overrides_for_default_tenant(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """GET on a tenant without overrides reports both fields as null —
    meaning 'falls back to llm_model'."""
    from soctalk.core.api.llm_config import get_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await get_tenant_llm(tenant.id, FakeRequest())
    assert result.fast_model is None
    assert result.reasoning_model is None


async def test_patch_llm_fast_model_persists_returns_and_enqueues_reconcile(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """PATCH fast_model='gpt-4o-mini' on an ACTIVE tenant persists the
    column, echoes it in the response, and enqueues tenant.reconcile."""
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(fast_model="gpt-4o-mini"),
        FakeRequest(),
    )
    assert result.fast_model == "gpt-4o-mini"
    assert result.reasoning_model is None

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    assert cfg.llm_fast_model == "gpt-4o-mini"
    assert cfg.llm_reasoning_model is None

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.reconcile") == 1
    assert "tenant.provision" not in kinds


async def test_patch_llm_global_sampling_persists_and_enqueues_reconcile(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """PATCH temperature/max_tokens persists the columns, echoes them, and
    enqueues a reconcile (chart-affecting — flows into SOCTALK_LLM_* env)."""
    from soctalk.core.api.llm_config import (
        LlmConfigUpdate,
        get_tenant_llm,
        update_tenant_llm,
    )

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    # Defaults surface on GET.
    before = await get_tenant_llm(tenant.id, FakeRequest())
    assert before.temperature == 0.0
    assert before.max_tokens == 4096

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(temperature=0.7, max_tokens=512),
        FakeRequest(),
    )
    assert result.temperature == 0.7
    assert result.max_tokens == 512

    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    assert cfg.llm_temperature == 0.7
    assert cfg.llm_max_tokens == 512

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.reconcile") == 1


async def test_patch_llm_sampling_out_of_range_rejected():
    """Bounds are enforced on the payload model (temperature 0–2, max_tokens
    1–8192) so an out-of-range value never reaches Helm validation. Booleans are
    rejected too — Pydantic would otherwise coerce True→1.0 (Codex #4)."""
    import pytest as _pytest
    from pydantic import ValidationError

    from soctalk.core.api.llm_config import LlmConfigUpdate

    with _pytest.raises(ValidationError):
        LlmConfigUpdate(temperature=2.5)
    with _pytest.raises(ValidationError):
        LlmConfigUpdate(temperature=-0.1)
    with _pytest.raises(ValidationError):
        LlmConfigUpdate(max_tokens=0)
    with _pytest.raises(ValidationError):
        LlmConfigUpdate(max_tokens=8193)  # above the router ceiling
    with _pytest.raises(ValidationError):
        LlmConfigUpdate(temperature=True)  # bool must not coerce to 1.0
    with _pytest.raises(ValidationError):
        LlmConfigUpdate(max_tokens=True)
    # Both per-run budget caps are gone from this surface entirely (#103, #128).
    # They rendered to worker env, so a change needed a rollout, and the value
    # never reached the run row. Unknown fields are ignored by the model rather
    # than raising, so assert they are not part of it at all.
    assert "dollar_budget_per_run" not in LlmConfigUpdate.model_fields
    assert "token_budget_per_run" not in LlmConfigUpdate.model_fields


async def test_patch_llm_reasoning_model_degraded_tenant_enqueues_provision(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """reasoning_model change on a DEGRADED tenant keeps the provision
    kind — same state-aware routing as provider/base_url/model edits."""
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.DEGRADED.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(reasoning_model="o3"),
        FakeRequest(),
    )
    assert result.reasoning_model == "o3"

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.provision") == 1
    assert "tenant.reconcile" not in kinds


async def test_patch_llm_empty_string_clears_override_to_null(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """PATCH reasoning_model='' on a tenant with a stored override clears
    the column to NULL (revert to llm_model fallback) and enqueues a job
    — the cleared override changes the rendered chart values."""
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_reasoning_model = "o3"
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(reasoning_model=""),
        FakeRequest(),
    )
    assert result.reasoning_model is None

    await mssp_session.refresh(cfg)
    assert cfg.llm_reasoning_model is None

    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds.get("tenant.reconcile") == 1


async def test_patch_llm_whitespace_string_also_clears_override(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Whitespace-only override strings behave exactly like '' — CLEAR."""
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_fast_model = "gpt-4o-mini"
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(fast_model="   "),
        FakeRequest(),
    )
    assert result.fast_model is None

    await mssp_session.refresh(cfg)
    assert cfg.llm_fast_model is None


async def test_patch_llm_no_model_fields_is_noop_and_enqueues_nothing(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """A PATCH carrying no model-override fields leaves both columns
    untouched and enqueues no provisioning job. Same for a PATCH whose
    override equals the stored value (no actual change)."""
    from soctalk.core.api.llm_config import LlmConfigUpdate, update_tenant_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_fast_model = "gpt-4o-mini"
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    # Empty payload → nothing changes, nothing enqueued.
    await update_tenant_llm(tenant.id, LlmConfigUpdate(), FakeRequest())
    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds == {}, f"empty PATCH must not enqueue jobs, got {kinds}"

    # Override equal to the stored value → still a no-op.
    await update_tenant_llm(
        tenant.id,
        LlmConfigUpdate(fast_model="gpt-4o-mini"),
        FakeRequest(),
    )
    kinds = await _llm_jobs_by_kind(mssp_session, tenant.id)
    assert kinds == {}, f"same-value PATCH must not enqueue jobs, got {kinds}"

    await mssp_session.refresh(cfg)
    assert cfg.llm_fast_model == "gpt-4o-mini"
    assert cfg.llm_reasoning_model is None


async def test_tenant_get_llm_carries_model_override_fields(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """The tenant-side GET /api/tenant/llm read model carries the new
    fields — overrides are visible (read-only) to the tenant admin."""
    from soctalk.core.api.llm_config import tenant_get_llm

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_fast_model = "gpt-4o-mini"
    await mssp_session.commit()

    class FakeRequest:
        class State:
            user_identity = {
                "user_id": str(uuid4()),
                "email": "admin@tenant.example",
                "user_type": "tenant",
                "role": "tenant_admin",
                "tenant_id": str(tenant.id),
            }
            db = mssp_session
        state = State()

    result = await tenant_get_llm(FakeRequest())
    assert result.fast_model == "gpt-4o-mini"
    assert result.reasoning_model is None


async def test_tenant_byok_put_and_clear_return_valid_config_body(
    mssp_session: AsyncSession, seeded_org: Organization, monkeypatch
):
    """Tenant BYOK set/clear must return 200 with a complete LlmConfigRead body."""
    import httpx
    from fastapi import FastAPI

    import soctalk.core.api.llm_config as llm_mod
    from soctalk.core.api.llm_config import tenant_router

    async def _noop_write(*_a, **_kw):
        return None

    async def _install_key():
        return "sk-install-shared"

    monkeypatch.setattr(llm_mod, "_write_api_key", _noop_write)
    monkeypatch.setattr(llm_mod, "_install_shared_llm_key", _install_key)

    tenant = await _seed_llm_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )
    cfg = (
        await mssp_session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_temperature = 0.7
    cfg.llm_max_tokens = 512
    await mssp_session.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _attach_request_state(request, call_next):
        request.state.db = mssp_session
        request.state.user_identity = {
            "user_id": str(uuid4()),
            "email": "admin@tenant.example",
            "user_type": "tenant",
            "role": "tenant_admin",
            "tenant_id": str(tenant.id),
            "current_tenant": None,
        }
        return await call_next(request)

    app.include_router(tenant_router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        put_resp = await client.put(
            "/api/tenant/llm/api-key",
            json={"api_key": "sk-tenant-byok-123456"},
        )
        assert put_resp.status_code == 200, put_resp.text
        put_body = put_resp.json()
        assert put_body["has_api_key"] is True
        assert put_body["api_key_preview"].startswith("sk-")
        assert put_body["api_key_preview"].endswith("3456")
        assert put_body["temperature"] == 0.7
        assert put_body["max_tokens"] == 512

        delete_resp = await client.delete("/api/tenant/llm/api-key")
        assert delete_resp.status_code == 200, delete_resp.text
        delete_body = delete_resp.json()
        assert delete_body["has_api_key"] is False
        assert delete_body["api_key_preview"] == ""
        assert delete_body["temperature"] == 0.7
        assert delete_body["max_tokens"] == 512

    await mssp_session.refresh(cfg)
    assert cfg.llm_api_key_plain is None


# ---------------------------------------------------------------------------
# :retry on a degraded tenant (tenant.provisioning.retry-active-job)
# ---------------------------------------------------------------------------
#
# The worker flips the tenant to 'degraded' on the FIRST ProvisionError but
# the job only reaches status='failed' after max_attempts. During the whole
# backoff window the tenant.provision job is still ACTIVE (pending with a
# future next_attempt_at, or in_flight). retry_provisioning must pre-check
# for that active job instead of blind-inserting a duplicate row that the
# partial unique index uq_provisioning_jobs_active rejects (500).


async def _seed_retry_tenant(
    mssp_session: AsyncSession,
    seeded_org: Organization,
    state: str = TenantState.DEGRADED.value,
) -> Tenant:
    tenant = Tenant(
        slug=f"rt{uuid4().hex[:8]}",
        display_name="Retry Target",
        state=state,
        profile="poc",
        organization_id=seeded_org.id,
    )
    mssp_session.add(tenant)
    await mssp_session.commit()
    await mssp_session.refresh(tenant)
    return tenant


async def _provision_jobs(
    mssp_session: AsyncSession, tenant_id
) -> list[ProvisioningJob]:
    return list(
        (
            await mssp_session.execute(
                select(ProvisioningJob)
                .where(ProvisioningJob.tenant_id == tenant_id)
                .where(ProvisioningJob.kind == "tenant.provision")
            )
        ).scalars().all()
    )


async def _retry_events(
    mssp_session: AsyncSession, tenant_id
) -> list[TenantLifecycleEvent]:
    return list(
        (
            await mssp_session.execute(
                select(TenantLifecycleEvent)
                .where(TenantLifecycleEvent.tenant_id == tenant_id)
                .where(TenantLifecycleEvent.event_type == "retry_requested")
            )
        ).scalars().all()
    )


async def test_retry_pending_job_short_circuits_backoff(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Degraded tenant + still-ACTIVE pending job (mid-backoff): :retry must
    NOT insert a duplicate row (the live 500). It resets next_attempt_at to
    now so the worker picks it up immediately, and returns the same job."""
    from soctalk.core.api.tenants import retry_provisioning

    tenant = await _seed_retry_tenant(mssp_session, seeded_org)
    job = ProvisioningJob(
        tenant_id=tenant.id,
        kind="tenant.provision",
        status="pending",
        attempts=2,
        last_error="ProvisionError: helm timed out",
        next_attempt_at=datetime.utcnow() + timedelta(minutes=30),
    )
    mssp_session.add(job)
    await mssp_session.commit()
    await mssp_session.refresh(job)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await retry_provisioning(tenant.id, FakeRequest())

    # Same job returned, no new row inserted.
    assert result.id == job.id
    assert result.status == "pending"
    jobs = await _provision_jobs(mssp_session, tenant.id)
    assert len(jobs) == 1

    # Backoff short-circuited: retry-now means next_attempt_at <= now.
    # (Read-back is tz-aware UTC; strip tzinfo to compare, same convention
    # as test_provisioning_worker.py.)
    await mssp_session.refresh(job)
    assert job.next_attempt_at.replace(tzinfo=None) <= datetime.utcnow()

    events = await _retry_events(mssp_session, tenant.id)
    assert len(events) == 1
    assert events[0].details == {"job_action": "backoff_short_circuited"}


async def test_retry_in_flight_job_returns_untouched(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Degraded tenant + in_flight job: it is executing right now — :retry
    returns it untouched (claim fields preserved), inserts nothing."""
    from soctalk.core.api.tenants import retry_provisioning

    tenant = await _seed_retry_tenant(mssp_session, seeded_org)
    claimed_at = datetime.utcnow() - timedelta(seconds=5)
    job = ProvisioningJob(
        tenant_id=tenant.id,
        kind="tenant.provision",
        status="in_flight",
        attempts=3,
        claimed_at=claimed_at,
        claimed_by="worker-1",
        next_attempt_at=datetime.utcnow() - timedelta(minutes=1),
    )
    mssp_session.add(job)
    await mssp_session.commit()
    await mssp_session.refresh(job)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await retry_provisioning(tenant.id, FakeRequest())

    assert result.id == job.id
    assert result.status == "in_flight"
    jobs = await _provision_jobs(mssp_session, tenant.id)
    assert len(jobs) == 1

    # Claim fields and attempts untouched — the run in progress owns them.
    await mssp_session.refresh(job)
    assert job.claimed_at.replace(tzinfo=None) == claimed_at
    assert job.claimed_by == "worker-1"
    assert job.attempts == 3

    events = await _retry_events(mssp_session, tenant.id)
    assert len(events) == 1
    assert events[0].details == {"job_action": "already_in_flight"}


async def test_retry_failed_job_is_reopened(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Existing behavior preserved: a failed job is reopened in place —
    status→pending, attempts=0, last_error cleared, claim released."""
    from soctalk.core.api.tenants import retry_provisioning

    tenant = await _seed_retry_tenant(mssp_session, seeded_org)
    job = ProvisioningJob(
        tenant_id=tenant.id,
        kind="tenant.provision",
        status="failed",
        attempts=5,
        last_error="ProvisionError: out of retries",
        claimed_at=datetime.utcnow() - timedelta(hours=1),
        claimed_by="worker-1",
    )
    mssp_session.add(job)
    await mssp_session.commit()
    await mssp_session.refresh(job)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await retry_provisioning(tenant.id, FakeRequest())

    assert result.id == job.id
    assert result.status == "pending"
    assert result.attempts == 0
    assert result.last_error is None
    jobs = await _provision_jobs(mssp_session, tenant.id)
    assert len(jobs) == 1

    await mssp_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_error is None
    assert job.claimed_at is None
    assert job.claimed_by is None
    assert job.next_attempt_at.replace(tzinfo=None) <= datetime.utcnow()

    events = await _retry_events(mssp_session, tenant.id)
    assert len(events) == 1
    assert events[0].details == {"job_action": "reopened_failed"}


async def test_retry_without_job_inserts_fresh(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """Degraded tenant with no tenant.provision job at all: :retry inserts a
    fresh pending row (existing behavior)."""
    from soctalk.core.api.tenants import retry_provisioning

    tenant = await _seed_retry_tenant(mssp_session, seeded_org)

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    result = await retry_provisioning(tenant.id, FakeRequest())

    assert result.status == "pending"
    assert result.kind == "tenant.provision"
    jobs = await _provision_jobs(mssp_session, tenant.id)
    assert len(jobs) == 1
    assert jobs[0].id == result.id

    events = await _retry_events(mssp_session, tenant.id)
    assert len(events) == 1
    assert events[0].details == {"job_action": "enqueued_new"}


async def test_retry_non_degraded_tenant_409(
    mssp_session: AsyncSession, seeded_org: Organization
):
    """The state gate is unchanged: :retry on a non-degraded tenant is 409
    and writes nothing."""
    from fastapi import HTTPException

    from soctalk.core.api.tenants import retry_provisioning

    tenant = await _seed_retry_tenant(
        mssp_session, seeded_org, state=TenantState.ACTIVE.value
    )

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = mssp_session
        state = State()

    with pytest.raises(HTTPException) as exc_info:
        await retry_provisioning(tenant.id, FakeRequest())
    assert exc_info.value.status_code == 409

    assert await _provision_jobs(mssp_session, tenant.id) == []
    assert await _retry_events(mssp_session, tenant.id) == []
