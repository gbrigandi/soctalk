"""Controller step idempotency + crash-resume, fakes only.

Fakes K8sClient and monkey-patches the two ``helm_*`` functions so the
controller runs end-to-end against an in-memory topology. Exercises:

- happy-path: pending → active, all lifecycle events emitted
- crash-resume: kill mid-run, re-enter, steps short-circuit
- failure propagates with the step name attached
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from soctalk.core.provisioning import controller as controller_mod
from soctalk.core.provisioning.controller import (
    ControllerSettings,
    ProvisionError,
    TenantController,
    TenantLifecycleError,
)
from soctalk.core.tenancy.models import (
    BrandingConfig,
    IntegrationConfig,
    Organization,
    Tenant,
    TenantLifecycleEvent,
    TenantSecret,
    TenantState,
)

SKIP_INTEGRATION = os.getenv("SKIP_INTEGRATION", "0") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        SKIP_INTEGRATION,
        reason="SKIP_INTEGRATION set; controller tests need Postgres",
    ),
]


# ---------------------------------------------------------------------------
# Fake k8s + helm
# ---------------------------------------------------------------------------


class FakeK8s:
    """Records all side-effecting calls; treats everything as idempotent."""

    def __init__(self) -> None:
        self.namespaces: set[str] = set()
        self.secrets: dict[tuple[str, str], dict[str, str]] = {}
        self.pods_ready = True  # flip to False to simulate not-ready workloads
        self.pods_empty = False  # flip to True to simulate an empty namespace
        self.namespace_probe_error = False  # flip to simulate a transient blip
        self.calls: list[str] = []

    async def check_reachable(self) -> None:
        self.calls.append("check_reachable")

    async def storage_class_exists(self, name: str) -> bool:
        return True

    async def ensure_namespace(self, name, labels) -> None:
        self.calls.append(f"ensure_namespace:{name}")
        self.namespaces.add(name)

    async def delete_namespace(self, name) -> None:
        self.namespaces.discard(name)

    async def put_secret(self, namespace, name, data, *, labels=None) -> None:
        self.calls.append(f"put_secret:{namespace}/{name}")
        self.secrets[(namespace, name)] = data

    async def get_secret(self, namespace, name):
        """Mirror the real client: raise 404 ApiException if missing,
        otherwise return ``{"name", "namespace", "data"}``. The
        controller's ``_mint_tenant_admin_user`` retry path
        distinguishes 404 (rotate password) from other errors (fail).
        """
        self.calls.append(f"get_secret:{namespace}/{name}")
        if (namespace, name) not in self.secrets:
            from kubernetes.client.exceptions import ApiException
            raise ApiException(status=404, reason="Not Found")
        return {
            "name": name,
            "namespace": namespace,
            "data": self.secrets[(namespace, name)],
        }

    async def read_pods(self, namespace):
        # Two pods; readiness flipped wholesale by tests.
        if self.pods_empty:
            return []
        return [
            {"name": "wazuh-manager-0", "phase": "Running", "ready": self.pods_ready},
            {"name": "wazuh-indexer-0", "phase": "Running", "ready": self.pods_ready},
        ]

    async def namespace_exists(self, name) -> bool:
        self.calls.append(f"namespace_exists:{name}")
        if self.namespace_probe_error:
            raise RuntimeError("kube API blip")
        return name in self.namespaces


async def _fake_helm_install_tenant(*_, **__):
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True})()


async def _fake_helm_install_wazuh(*_, **__):
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True})()


async def _fake_helm_version():
    return type("R", (), {"returncode": 0, "stdout": "v3.14.0", "stderr": "", "ok": True})()


@pytest.fixture
def patched_helm(monkeypatch):
    monkeypatch.setattr(controller_mod, "helm_install_tenant", _fake_helm_install_tenant)
    # helm_install_wazuh is imported into controller_mod at module load.
    monkeypatch.setattr(controller_mod, "helm_install_wazuh", _fake_helm_install_wazuh)
    # helm_version is imported lazily inside _step_preflight.
    from soctalk.core.provisioning import helm as helm_mod
    monkeypatch.setattr(helm_mod, "helm_version", _fake_helm_version)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


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
    """Admin session — used only for TRUNCATE at fixture start."""
    engine = create_async_engine(_admin_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # mssp role (BYPASSRLS) — the controller runs under this in production
    # worker, and tests need it to seed across tenants.
    engine = create_async_engine(_mssp_url(), echo=False, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_tenant(
    session: AsyncSession, admin_session: AsyncSession
) -> Tenant:
    """Fresh org + tenant + branding + integration, clean namespace collision."""
    from sqlalchemy import text

    # Admin does the TRUNCATE (mssp role doesn't have that grant).
    await admin_session.execute(
        text(
            "TRUNCATE tenant_lifecycle_events, integration_configs, "
            "branding_configs, tenant_secrets, provisioning_jobs, "
            "tenants, organizations CASCADE"
        )
    )
    await admin_session.commit()

    org = Organization(
        mssp_id=uuid4(), mssp_name="Test MSSP", slug="test-mssp",
        install_id=uuid4(), install_label="test",
    )
    session.add(org)
    await session.flush()

    tenant = Tenant(
        slug=f"tenant-{uuid4().hex[:8]}",
        display_name="Test Tenant",
        state=TenantState.PENDING.value,
        profile="poc",
        organization_id=org.id,
    )
    session.add(tenant)
    await session.flush()

    session.add_all([
        # A per-tenant LLM key so the happy-path/resume tests provision past
        # ``apply_secrets`` — ``_copy_llm_key_to_tenant_ns`` now fails fast
        # when no key (per-tenant or install-wide) is resolvable. The
        # LLM-guard tests override this value per scenario via ``_set_llm_key``.
        IntegrationConfig(tenant_id=tenant.id, llm_api_key_plain="sk-seeded-tenant-llm-key"),
        BrandingConfig(tenant_id=tenant.id, app_name="Test"),
    ])
    await session.commit()
    return tenant


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_provision_happy_path_emits_each_step(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm
):
    fake_k8s = FakeK8s()
    controller = TenantController(
        session, k8s=fake_k8s,
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )

    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    evs = (
        await session.execute(
            select(TenantLifecycleEvent)
            .where(TenantLifecycleEvent.tenant_id == seeded_tenant.id)
            .order_by(TenantLifecycleEvent.timestamp)
        )
    ).scalars().all()
    kinds = [e.event_type for e in evs]

    # Canonical order, plus final "active" transition.
    for must_have in (
        "provisioning_started",
        "preflight_ok",
        "secrets_minted",
        "namespace_ready",
        "secrets_applied",
        "helm_applied",  # appears twice (tenant + wazuh); at least one
        "workloads_ready",
        "integration_config_written",
        "active",
    ):
        assert must_have in kinds, f"missing lifecycle event: {must_have}"

    # Integration config got the Wazuh URL written.
    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    assert integ.wazuh_url is not None and integ.wazuh_url.endswith(":55000")


async def test_provision_rejects_stored_anthropic_served_engine(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm
):
    cfg = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_provider = "anthropic"
    cfg.llm_base_url = "https://api.anthropic.com"
    cfg.llm_model = "claude-sonnet-4-6"
    cfg.llm_engine = "sglang"
    await session.commit()

    controller = TenantController(
        session,
        k8s=FakeK8s(),
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )

    with pytest.raises(ProvisionError) as exc_info:
        await controller.provision(seeded_tenant.id, actor_id="test")

    assert exc_info.value.step == "helm_apply_tenant"
    assert "not valid with provider 'anthropic'" in str(exc_info.value)
    await session.refresh(seeded_tenant)
    assert seeded_tenant.state == TenantState.DEGRADED.value


async def test_provision_rejects_stored_served_engine_with_sentinel_base_url(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm
):
    cfg = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    cfg.llm_provider = "openai-compatible"
    cfg.llm_base_url = "https://api.openai.com/v1"
    cfg.llm_engine = "sglang"
    await session.commit()

    controller = TenantController(
        session,
        k8s=FakeK8s(),
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )

    with pytest.raises(ProvisionError) as exc_info:
        await controller.provision(seeded_tenant.id, actor_id="test")

    assert exc_info.value.step == "helm_apply_tenant"
    assert "requires a custom llm_base_url" in str(exc_info.value)
    await session.refresh(seeded_tenant)
    assert seeded_tenant.state == TenantState.DEGRADED.value


async def test_provision_resume_after_crash_is_idempotent(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm
):
    """Simulate crash right after helm_apply_tenant; re-enter; must succeed."""

    fake_k8s = FakeK8s()
    controller = TenantController(
        session, k8s=fake_k8s,
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )

    # Poison helm_install_wazuh on the first pass to force a failure mid-run.
    from soctalk.core.provisioning import controller as ctrl
    original = ctrl.helm_install_wazuh
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ctrl.HelmError("simulated crash")
        return await _fake_helm_install_wazuh(*a, **kw)

    ctrl.helm_install_wazuh = flaky
    try:
        with pytest.raises(ProvisionError):
            await controller.provision(seeded_tenant.id, actor_id="test")

        # Tenant should be degraded, failure recorded.
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == seeded_tenant.id))
        ).scalar_one()
        assert tenant.state == TenantState.DEGRADED.value

        # Resume: provision() again; retry_requested + remaining steps should fire.
        result = await controller.provision(seeded_tenant.id, actor_id="test")
        assert result.state == TenantState.ACTIVE.value
    finally:
        ctrl.helm_install_wazuh = original


async def test_preflight_failure_doesnt_mutate_tenant_beyond_degraded(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    fake_k8s = FakeK8s()

    async def fake_unreachable():
        raise RuntimeError("kube API timeout")

    monkeypatch.setattr(fake_k8s, "check_reachable", fake_unreachable)
    controller = TenantController(
        session, k8s=fake_k8s,
        settings=ControllerSettings(wazuh_chart_path="charts/wazuh"),
    )

    with pytest.raises(ProvisionError) as exc_info:
        await controller.provision(seeded_tenant.id, actor_id="test")
    assert exc_info.value.step == "preflight"

    # Nothing in k8s was touched beyond the failed probe.
    assert "ensure_namespace" not in " ".join(fake_k8s.calls)

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == seeded_tenant.id))
    ).scalar_one()
    assert tenant.state == TenantState.DEGRADED.value


# ---------------------------------------------------------------------------
# 'provided' profile: external Wazuh, no in-namespace SIEM
# ---------------------------------------------------------------------------


# External SIEM endpoints + creds the customer brings for a 'provided' tenant.
_PROVIDED_INDEXER_URL = "https://indexer.acme.example:9200"
_PROVIDED_API_URL = "https://wazuh.acme.example:55000"
_PROVIDED_INDEXER_USER = "idx-user"
_PROVIDED_INDEXER_PASS = "idx-pass"
_PROVIDED_API_USER = "api-user"
_PROVIDED_API_PASS = "api-pass"


@pytest_asyncio.fixture
async def provided_tenant(
    session: AsyncSession, admin_session: AsyncSession
) -> Tenant:
    """A pending 'provided'-profile tenant whose IntegrationConfig already
    carries BOTH the external indexer creds and the Wazuh API creds (the
    onboard endpoint guarantees these before profile='provided' is allowed).
    ``wazuh_api_token_plain`` is intentionally left NULL so the test can pin
    the "WAZUH_API_TOKEN omitted when absent" branch.
    """
    from sqlalchemy import text

    await admin_session.execute(
        text(
            "TRUNCATE tenant_lifecycle_events, integration_configs, "
            "branding_configs, tenant_secrets, provisioning_jobs, "
            "tenants, organizations CASCADE"
        )
    )
    await admin_session.commit()

    org = Organization(
        mssp_id=uuid4(), mssp_name="Test MSSP", slug="test-mssp",
        install_id=uuid4(), install_label="test",
    )
    session.add(org)
    await session.flush()

    tenant = Tenant(
        slug=f"tenant-{uuid4().hex[:8]}",
        display_name="Provided Tenant",
        state=TenantState.PENDING.value,
        profile="provided",
        organization_id=org.id,
    )
    session.add(tenant)
    await session.flush()

    session.add_all([
        IntegrationConfig(
            tenant_id=tenant.id,
            wazuh_enabled=True,
            # Per-tenant LLM key: the LLM-key guard fails fast without one,
            # and the 'provided' profile still runs apply_secrets/_copy_llm_key.
            llm_api_key_plain="sk-provided-tenant-llm-key",
            # Customer-provided external endpoints (must survive provisioning).
            wazuh_url=_PROVIDED_API_URL,
            wazuh_api_url=_PROVIDED_API_URL,
            wazuh_indexer_url=_PROVIDED_INDEXER_URL,
            # Wazuh API (manager, :55000) HTTP-Basic creds.
            wazuh_username=_PROVIDED_API_USER,
            wazuh_password_plain=_PROVIDED_API_PASS,
            # Indexer (:9200) HTTP-Basic creds.
            wazuh_indexer_username=_PROVIDED_INDEXER_USER,
            wazuh_indexer_password_plain=_PROVIDED_INDEXER_PASS,
            # No pre-minted API token: WAZUH_API_TOKEN must be omitted.
            wazuh_api_token_plain=None,
        ),
        BrandingConfig(tenant_id=tenant.id, app_name="Provided"),
    ])
    await session.commit()
    return tenant


async def test_provision_provided_profile(
    session: AsyncSession, provided_tenant: Tenant, monkeypatch
):
    """Drive a 'provided' tenant pending -> active and assert the controller:

    - installs the soctalk-tenant chart exactly once,
    - never installs the wazuh-<slug> release,
    - writes Secret/tenant-external-siem-creds with the four credential keys
      (WAZUH_API_TOKEN omitted because wazuh_api_token_plain is NULL),
    - runs that secret step AFTER apply_secrets and BEFORE helm_apply_tenant,
    - emits exactly one 'wazuh_skipped_provided' lifecycle event,
    - leaves integration.wazuh_url / wazuh_indexer_url untouched,
    - mints an 'external-siem-creds' reference row but NO 'bootstrap' row.
    """
    fake_k8s = FakeK8s()

    # Recording helm shims. Both also push a marker into fake_k8s.calls so we
    # get one ordered timeline across secret writes + helm installs.
    tenant_calls: list[dict] = []
    wazuh_calls: list[dict] = []

    def _ok():
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True})()

    async def rec_install_tenant(*_a, **kw):
        fake_k8s.calls.append("helm_install_tenant")
        tenant_calls.append(kw)
        return _ok()

    async def rec_install_wazuh(*_a, **kw):
        fake_k8s.calls.append("helm_install_wazuh")
        wazuh_calls.append(kw)
        return _ok()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_install_tenant)
    monkeypatch.setattr(controller_mod, "helm_install_wazuh", rec_install_wazuh)
    from soctalk.core.provisioning import helm as helm_mod
    monkeypatch.setattr(helm_mod, "helm_version", _fake_helm_version)

    controller = TenantController(
        session, k8s=fake_k8s,
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )

    result = await controller.provision(provided_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # --- helm: exactly one soctalk-tenant install, zero wazuh installs ------
    assert len(tenant_calls) == 1, f"expected one tenant install, got {tenant_calls}"
    assert wazuh_calls == [], f"wazuh release must not be installed, got {wazuh_calls}"

    ns = f"tenant-{provided_tenant.slug}"

    # --- the external-SIEM Secret carries exactly the four basic-auth keys --
    assert (ns, "tenant-external-siem-creds") in fake_k8s.secrets
    secret = fake_k8s.secrets[(ns, "tenant-external-siem-creds")]
    assert set(secret.keys()) == {
        "INDEXER_USERNAME",
        "INDEXER_PASSWORD",
        "WAZUH_API_USERNAME",
        "WAZUH_API_PASSWORD",
    }
    assert secret["INDEXER_USERNAME"] == _PROVIDED_INDEXER_USER
    assert secret["INDEXER_PASSWORD"] == _PROVIDED_INDEXER_PASS
    assert secret["WAZUH_API_USERNAME"] == _PROVIDED_API_USER
    assert secret["WAZUH_API_PASSWORD"] == _PROVIDED_API_PASS
    # No pre-minted API token on the integration row -> key omitted.
    assert "WAZUH_API_TOKEN" not in secret

    # --- step ordering: apply_secrets < write_external_siem_secret < helm ---
    calls = fake_k8s.calls
    i_adapter = calls.index(f"put_secret:{ns}/adapter-token")          # apply_secrets
    i_ext = calls.index(f"put_secret:{ns}/tenant-external-siem-creds")  # new step
    i_helm = calls.index("helm_install_tenant")                        # helm_apply_tenant
    assert i_adapter < i_ext < i_helm

    # --- lifecycle events: wazuh skipped once; secret-applied marker present-
    evs = (
        await session.execute(
            select(TenantLifecycleEvent)
            .where(TenantLifecycleEvent.tenant_id == provided_tenant.id)
            .order_by(TenantLifecycleEvent.timestamp)
        )
    ).scalars().all()
    kinds = [e.event_type for e in evs]
    assert kinds.count("wazuh_skipped_provided") == 1
    assert "external_siem_secret_applied" in kinds

    # --- integration endpoints unchanged (no in-cluster clobber) ------------
    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == provided_tenant.id
            )
        )
    ).scalar_one()
    assert integ.wazuh_url == _PROVIDED_API_URL
    assert integ.wazuh_indexer_url == _PROVIDED_INDEXER_URL

    # --- mint rows: external-siem-creds present, bootstrap absent -----------
    secret_rows = (
        await session.execute(
            select(TenantSecret).where(
                TenantSecret.tenant_id == provided_tenant.id
            )
        )
    ).scalars().all()
    purposes = {r.purpose for r in secret_rows}
    assert "external-siem-creds" in purposes
    assert "bootstrap" not in purposes
    assert {"llm", "adapter-token", "runs-worker-token"} <= purposes


# ---------------------------------------------------------------------------
# LLM key guard: explicit precedence + fail-fast (tenant.provisioning.llm-key-guard)
# ---------------------------------------------------------------------------
#
# ``_copy_llm_key_to_tenant_ns`` resolves the tenant's LLM API key with a fixed
# precedence — per-tenant ``IntegrationConfig.llm_api_key_plain`` first, then
# the install-wide Secret (``SOCTALK_INSTALL_LLM_SECRET_NAME``, default
# ``soctalk-system-llm-api-key``, keys ``openai-api-key`` / ``anthropic-api-key``).
# When neither yields a non-empty key the step must FAIL FAST with a typed
# ``ProvisionError(step='apply_secrets')`` and a ``llm_key_missing`` lifecycle
# event — instead of silently skipping the Secret and stranding the L2
# runs-worker in CreateContainerConfigError (`secret "tenant-llm-key" not found`).

# Default install-wide LLM Secret coordinates: namespace matches
# ``ControllerSettings.soctalk_system_namespace`` and name matches the
# ``SOCTALK_INSTALL_LLM_SECRET_NAME`` default the controller reads.
_INSTALL_LLM_NS = "soctalk-system"
_INSTALL_LLM_SECRET = "soctalk-system-llm-api-key"


async def _set_llm_key(
    session: AsyncSession, tenant_id, value: str | None
) -> None:
    """Set (or clear) the per-tenant LLM key on the tenant's IntegrationConfig.

    Lets each LLM-guard test pin the exact precedence input it exercises,
    independent of whatever default the shared ``seeded_tenant`` fixture uses.
    """
    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    integ.llm_api_key_plain = value
    await session.commit()


def _llm_guard_controller(session: AsyncSession, fake_k8s: FakeK8s) -> TenantController:
    return TenantController(
        session,
        k8s=fake_k8s,
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )


async def test_llm_key_per_tenant_precedence(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Per-tenant key wins: Secret/tenant-llm-key is written from
    ``llm_api_key_plain`` and the install-wide Secret is never read."""
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    await _set_llm_key(session, seeded_tenant.id, "sk-per-tenant-PRECEDENCE")

    fake_k8s = FakeK8s()
    # Even with an install-wide Secret present, precedence must skip reading it.
    fake_k8s.secrets[(_INSTALL_LLM_NS, _INSTALL_LLM_SECRET)] = {
        "openai-api-key": "sk-install-MUST-NOT-BE-USED",
    }

    controller = _llm_guard_controller(session, fake_k8s)
    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    ns = f"tenant-{seeded_tenant.slug}"
    assert (ns, "tenant-llm-key") in fake_k8s.secrets
    assert fake_k8s.secrets[(ns, "tenant-llm-key")] == {
        "api_key": "sk-per-tenant-PRECEDENCE"
    }
    # The install-wide Secret was NOT read (per-tenant precedence short-circuit).
    assert not any(
        _INSTALL_LLM_SECRET in c for c in fake_k8s.calls
    ), f"install-wide LLM Secret must not be read: {fake_k8s.calls}"


async def test_llm_key_missing_fails_fast(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Neither source yields a key ⇒ fail fast in apply_secrets.

    Asserts: ProvisionError(step='apply_secrets') naming the LLM key, a
    'llm_key_missing' lifecycle event, the tenant ends degraded, no
    tenant-llm-key Secret is put, and helm_apply_tenant never runs (so no
    runs-worker Deployment is created without its Secret).
    """
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    await _set_llm_key(session, seeded_tenant.id, None)

    fake_k8s = FakeK8s()  # no install-wide LLM Secret present
    controller = _llm_guard_controller(session, fake_k8s)

    with pytest.raises(ProvisionError) as exc_info:
        await controller.provision(seeded_tenant.id, actor_id="test")
    assert exc_info.value.step == "apply_secrets"
    assert "llm" in str(exc_info.value).lower()

    # Tenant ends degraded.
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == seeded_tenant.id))
    ).scalar_one()
    assert tenant.state == TenantState.DEGRADED.value

    # Lifecycle event emitted; helm_apply_tenant never ran.
    evs = (
        await session.execute(
            select(TenantLifecycleEvent)
            .where(TenantLifecycleEvent.tenant_id == seeded_tenant.id)
            .order_by(TenantLifecycleEvent.timestamp)
        )
    ).scalars().all()
    kinds = [e.event_type for e in evs]
    assert "llm_key_missing" in kinds
    assert "helm_applied" not in kinds, (
        "runs-worker Deployment must not be created without its LLM Secret"
    )

    # No tenant-llm-key Secret was put.
    ns = f"tenant-{seeded_tenant.slug}"
    assert (ns, "tenant-llm-key") not in fake_k8s.secrets


async def test_llm_key_install_wide_unchanged(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Regression guard: with only the install-wide Secret present, the
    tenant-llm-key Secret is still written from it (poc/persistent path)."""
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    await _set_llm_key(session, seeded_tenant.id, None)

    fake_k8s = FakeK8s()
    fake_k8s.secrets[(_INSTALL_LLM_NS, _INSTALL_LLM_SECRET)] = {
        "openai-api-key": "sk-install-SHARED-KEY",
    }
    controller = _llm_guard_controller(session, fake_k8s)

    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    ns = f"tenant-{seeded_tenant.slug}"
    assert (ns, "tenant-llm-key") in fake_k8s.secrets
    assert fake_k8s.secrets[(ns, "tenant-llm-key")] == {
        "api_key": "sk-install-SHARED-KEY"
    }
    # The install-wide Secret WAS read this time.
    assert any(_INSTALL_LLM_SECRET in c for c in fake_k8s.calls)


async def test_llm_anthropic_autoflip_reconciles_model_overrides(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """tenant.llm.models.render, criterion 3+5: provisioning via the
    install-shared ``anthropic-api-key`` auto-flips the provider AND
    reconciles the clearly-OpenAI ``llm_fast_model`` / ``llm_reasoning_model``
    overrides to the anthropic default alongside ``llm_model`` — the rendered
    runsWorker values must never carry an OpenAI model after the flip.
    """
    from soctalk.core.llm_provider import ANTHROPIC_DEFAULT_MODEL

    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    # No per-tenant key ⇒ install-wide Secret path; clearly-OpenAI overrides.
    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    integ.llm_api_key_plain = None
    integ.llm_provider = "openai-compatible"
    integ.llm_model = "gpt-4o"
    integ.llm_fast_model = "gpt-4o-mini"
    integ.llm_reasoning_model = "o3"
    await session.commit()

    fake_k8s = FakeK8s()
    # Anthropic-only install Secret ⇒ chosen_key_name == "anthropic-api-key".
    fake_k8s.secrets[(_INSTALL_LLM_NS, _INSTALL_LLM_SECRET)] = {
        "anthropic-api-key": "sk-ant-install-SHARED-KEY",
    }

    helm_values: list[dict] = []

    async def rec_install_tenant(*_, values=None, **__):
        helm_values.append(values or {})
        return type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True}
        )()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_install_tenant)

    controller = _llm_guard_controller(session, fake_k8s)
    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # DB row: provider flipped, ALL three model columns reconciled.
    await session.refresh(integ)
    assert integ.llm_provider == "anthropic"
    assert integ.llm_model == ANTHROPIC_DEFAULT_MODEL
    assert integ.llm_fast_model == ANTHROPIC_DEFAULT_MODEL
    assert integ.llm_reasoning_model == ANTHROPIC_DEFAULT_MODEL

    # Rendered values: the runs-worker never sees an OpenAI model.
    assert helm_values, "helm_install_tenant was not invoked"
    rw = helm_values[0]["runsWorker"]
    assert rw["fastModel"] == ANTHROPIC_DEFAULT_MODEL, (
        "anthropic auto-switch must never leave SOCTALK_FAST_MODEL=gpt-4o-mini"
    )
    assert rw["reasoningModel"] == ANTHROPIC_DEFAULT_MODEL


async def test_llm_anthropic_autoflip_leaves_null_overrides_null(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """tenant.llm.models.render, criterion 3+5 (NULL branch): the provider
    auto-flip never materializes a concrete model into an unset override —
    NULL ``llm_fast_model`` / ``llm_reasoning_model`` stay NULL and the
    renderer's fallback to ``llm_model`` keeps working.
    """
    from soctalk.core.llm_provider import ANTHROPIC_DEFAULT_MODEL

    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    integ.llm_api_key_plain = None
    integ.llm_provider = "openai-compatible"
    integ.llm_model = "gpt-4o"
    integ.llm_fast_model = None
    integ.llm_reasoning_model = None
    await session.commit()

    fake_k8s = FakeK8s()
    fake_k8s.secrets[(_INSTALL_LLM_NS, _INSTALL_LLM_SECRET)] = {
        "anthropic-api-key": "sk-ant-install-SHARED-KEY",
    }

    helm_values: list[dict] = []

    async def rec_install_tenant(*_, values=None, **__):
        helm_values.append(values or {})
        return type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True}
        )()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_install_tenant)

    controller = _llm_guard_controller(session, fake_k8s)
    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    await session.refresh(integ)
    assert integ.llm_provider == "anthropic"
    assert integ.llm_model == ANTHROPIC_DEFAULT_MODEL
    # NULL overrides stay NULL — never materialized by the flip.
    assert integ.llm_fast_model is None
    assert integ.llm_reasoning_model is None

    # Render-time fallback: both runsWorker models come from llm_model.
    assert helm_values, "helm_install_tenant was not invoked"
    rw = helm_values[0]["runsWorker"]
    assert rw["fastModel"] == ANTHROPIC_DEFAULT_MODEL
    assert rw["reasoningModel"] == ANTHROPIC_DEFAULT_MODEL


async def test_llm_anthropic_tenant_prefers_anthropic_key_when_both_present(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Regression: an anthropic tenant on an install whose shared Secret holds
    BOTH ``openai-api-key`` and ``anthropic-api-key`` (install.sh writes both
    with the same value) must resolve the ANTHROPIC key and stay anthropic.

    The old fixed openai-first fallback picked ``openai-api-key``, flipped the
    tenant to ``openai-compatible``, and mounted the Anthropic key as
    ``OPENAI_API_KEY`` — every runs-worker call then 401'd against OpenAI. The
    candidate order now follows the tenant's configured provider so no flip
    happens and the rendered primary stays anthropic.
    """
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    integ.llm_api_key_plain = None
    integ.llm_provider = "anthropic"
    integ.llm_model = "claude-sonnet-4-6"
    integ.llm_base_url = "https://api.anthropic.com"
    await session.commit()

    fake_k8s = FakeK8s()
    # BOTH sub-keys present, same value — the standard install.sh layout.
    fake_k8s.secrets[(_INSTALL_LLM_NS, _INSTALL_LLM_SECRET)] = {
        "openai-api-key": "sk-ant-install-SHARED-KEY",
        "anthropic-api-key": "sk-ant-install-SHARED-KEY",
    }

    helm_values: list[dict] = []

    async def rec_install_tenant(*_, values=None, **__):
        helm_values.append(values or {})
        return type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True}
        )()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_install_tenant)

    controller = _llm_guard_controller(session, fake_k8s)
    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # No flip: the primary stays anthropic/claude in the DB and the render.
    await session.refresh(integ)
    assert integ.llm_provider == "anthropic"
    assert integ.llm_model == "claude-sonnet-4-6"
    assert helm_values, "helm_install_tenant was not invoked"
    assert helm_values[0]["llm"]["provider"] == "anthropic"
    assert helm_values[0]["llm"]["model"] == "claude-sonnet-4-6"


async def test_llm_key_provided_onboard_drives_tenant_llm_secret(
    session: AsyncSession, admin_session: AsyncSession, patched_helm, monkeypatch
):
    """tenant.llm.onboard-key, criterion 6: a tenant onboarded with
    profile='provided' + llm_api_key provisions via the existing
    precedence-1 branch in ``_copy_llm_key_to_tenant_ns`` —
    Secret/tenant-llm-key in tenant-<slug> contains the ONBOARD key and the
    install-wide Secret ``soctalk-system-llm-api-key`` is NEVER read, even
    though it exists and holds a different key.
    """
    from sqlalchemy import text

    from soctalk.core.api.tenants import (
        ExternalSiemOnboard,
        TenantOnboard,
        onboard_tenant,
    )

    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    await admin_session.execute(
        text(
            "TRUNCATE tenant_lifecycle_events, integration_configs, "
            "branding_configs, tenant_secrets, provisioning_jobs, "
            "tenants, organizations CASCADE"
        )
    )
    await admin_session.commit()

    org = Organization(
        mssp_id=uuid4(), mssp_name="Onboard MSSP", slug="onboard-mssp",
        install_id=uuid4(), install_label="test",
    )
    session.add(org)
    await session.commit()

    class FakeRequest:
        class State:
            user_identity = {"user_id": "test-user"}
            db = session
        state = State()

    onboard_key = "sk-onboard-PROVIDED-" + uuid4().hex
    payload = TenantOnboard(
        slug=f"ob{uuid4().hex[:8]}",
        display_name="Provided Onboard → Provision",
        profile="provided",
        llm_api_key=onboard_key,
        external_siem=ExternalSiemOnboard(
            indexer_url="https://indexer.acme.example:9200",
            indexer_username="idx-user",
            indexer_password="idx-pass",
            api_url="https://wazuh.acme.example:55000",
            api_username="api-user",
            api_password="api-pass",
        ),
    )
    result = await onboard_tenant(payload, FakeRequest())

    fake_k8s = FakeK8s()
    # Install-wide Secret present with a DIFFERENT key — precedence 1 must
    # short-circuit so it is never even read.
    fake_k8s.secrets[("soctalk-system", "soctalk-system-llm-api-key")] = {
        "openai-api-key": "sk-install-MUST-NOT-BE-USED",
    }

    # Record the values handed to helm: the controller path must NOT pass
    # the plaintext key through values.llm.apiKey — the chart would render
    # Secret/tenant-llm-key on top of the controller-written one and helm
    # install fails with "invalid ownership metadata" (live-cluster
    # regression, tenant a3c).
    helm_values: list[dict] = []

    async def rec_install_tenant(*_, values=None, **__):
        helm_values.append(values or {})
        return type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True}
        )()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_install_tenant)

    controller = TenantController(
        session, k8s=fake_k8s,
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            readiness_poll_interval_seconds=0.01,
            readiness_timeout_seconds=5.0,
        ),
    )
    provisioned = await controller.provision(result.id, actor_id="test")
    assert provisioned.state == TenantState.ACTIVE.value

    ns = f"tenant-{payload.slug}"
    assert (ns, "tenant-llm-key") in fake_k8s.secrets
    assert fake_k8s.secrets[(ns, "tenant-llm-key")] == {"api_key": onboard_key}
    # The install-wide Secret was never read.
    assert not any(
        "soctalk-system-llm-api-key" in c for c in fake_k8s.calls
    ), f"install-wide LLM Secret must not be read: {fake_k8s.calls}"
    # Single Secret owner: the chart must not also render tenant-llm-key,
    # so the plaintext key never travels through chart values on this path.
    assert helm_values, "helm_install_tenant was not invoked"
    assert helm_values[0]["llm"]["apiKey"] == "", (
        "controller path must suppress values.llm.apiKey "
        "(chart-rendered Secret would conflict with the controller-written one)"
    )
    assert onboard_key not in str(helm_values), (
        "plaintext LLM key leaked into helm values on the controller path"
    )


# ---------------------------------------------------------------------------
# reconcile: re-render + helm upgrade for ACTIVE tenants (tenant.llm.reconcile-active)
# ---------------------------------------------------------------------------
#
# ``TenantController.reconcile`` re-runs the value-affecting idempotent steps
# (render_tenant_values + helm upgrade of tenant-<slug>, plus the
# external-SIEM Secret rewrite for 'provided', plus wait_workloads) WITHOUT
# any lifecycle transition. Success leaves the tenant 'active'; failure
# degrades it with a 'reconcile_failed' event naming the step + error.


def _quick_settings() -> ControllerSettings:
    return ControllerSettings(
        wazuh_chart_path="charts/wazuh",
        readiness_poll_interval_seconds=0.01,
        readiness_timeout_seconds=5.0,
    )


async def _events_for(session: AsyncSession, tenant_id) -> list[TenantLifecycleEvent]:
    return (
        await session.execute(
            select(TenantLifecycleEvent)
            .where(TenantLifecycleEvent.tenant_id == tenant_id)
            .order_by(TenantLifecycleEvent.timestamp)
        )
    ).scalars().all()


async def test_reconcile_active_rerenders_values_and_helm_upgrades(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Happy path: an ACTIVE tenant's base_url change propagates into the
    values handed to helm during reconcile — new host present in BOTH
    values.llm.baseUrl and networkPolicies.allowedLlmHosts — the tenant
    stays 'active', and the only reconcile-emitted events are step markers
    (from_state == to_state) plus reconcile_started / reconcile_succeeded.
    """
    fake_k8s = FakeK8s()
    controller = TenantController(session, k8s=fake_k8s, settings=_quick_settings())

    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # Chart-affecting LLM edit: new base_url host.
    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == seeded_tenant.id
            )
        )
    ).scalar_one()
    integ.llm_base_url = "https://llm.newhost.example/v1"
    await session.commit()

    # Recording helm shim for the reconcile pass.
    upgrade_calls: list[dict] = []

    async def rec_upgrade(*_a, **kw):
        upgrade_calls.append(kw)
        return type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True}
        )()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_upgrade)

    n_events_before = len(await _events_for(session, seeded_tenant.id))

    result = await controller.reconcile(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # helm upgrade of release tenant-<slug> with re-rendered values.
    assert len(upgrade_calls) == 1
    call = upgrade_calls[0]
    assert call["release_name"] == f"tenant-{seeded_tenant.slug}"
    assert call["namespace"] == f"tenant-{seeded_tenant.slug}"
    values = call["values"]
    assert values["llm"]["baseUrl"] == "https://llm.newhost.example/v1"
    assert "llm.newhost.example" in values["networkPolicies"]["allowedLlmHosts"]

    # Lifecycle events: reconcile_started + reconcile_succeeded, and NO
    # state-change events (every reconcile event keeps from_state == to_state).
    evs = (await _events_for(session, seeded_tenant.id))[n_events_before:]
    kinds = [e.event_type for e in evs]
    assert kinds[0] == "reconcile_started"
    assert kinds[-1] == "reconcile_succeeded"
    for e in evs:
        assert e.from_state == e.to_state == TenantState.ACTIVE.value, (
            f"reconcile must not transition state: {e.event_type} "
            f"{e.from_state} -> {e.to_state}"
        )


async def test_reconcile_failure_degrades_with_reconcile_failed_event(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Failure path: helm upgrade blows up → tenant transitions
    active → degraded with event_type='reconcile_failed' carrying the
    failing step and error message in details.
    """
    fake_k8s = FakeK8s()
    controller = TenantController(session, k8s=fake_k8s, settings=_quick_settings())
    await controller.provision(seeded_tenant.id, actor_id="test")

    async def boom(*_a, **_kw):
        raise controller_mod.HelmError("simulated upgrade failure")

    monkeypatch.setattr(controller_mod, "helm_install_tenant", boom)

    with pytest.raises(ProvisionError) as exc_info:
        await controller.reconcile(seeded_tenant.id, actor_id="test")
    assert exc_info.value.step == "helm_apply_tenant"

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == seeded_tenant.id))
    ).scalar_one()
    assert tenant.state == TenantState.DEGRADED.value

    evs = await _events_for(session, seeded_tenant.id)
    failed = [e for e in evs if e.event_type == "reconcile_failed"]
    assert len(failed) == 1
    assert failed[0].from_state == TenantState.ACTIVE.value
    assert failed[0].to_state == TenantState.DEGRADED.value
    assert failed[0].details["step"] == "helm_apply_tenant"
    assert "simulated upgrade failure" in failed[0].details["error"]


async def test_reconcile_non_active_raises_lifecycle_error(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm
):
    """reconcile() only applies to 'active' tenants; callers route every
    other state to provision(). The seeded tenant is still 'pending'.
    """
    fake_k8s = FakeK8s()
    controller = TenantController(session, k8s=fake_k8s, settings=_quick_settings())

    with pytest.raises(TenantLifecycleError):
        await controller.reconcile(seeded_tenant.id, actor_id="test")

    # No reconcile events, no state mutation.
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == seeded_tenant.id))
    ).scalar_one()
    assert tenant.state == TenantState.PENDING.value
    kinds = [e.event_type for e in await _events_for(session, seeded_tenant.id)]
    assert "reconcile_started" not in kinds


async def test_reconcile_provided_rewrites_siem_secret_never_touches_wazuh(
    session: AsyncSession, provided_tenant: Tenant, monkeypatch
):
    """'provided' reconcile rewrites Secret/tenant-external-siem-creds from
    the current IntegrationConfig row and never installs/upgrades a
    wazuh-<slug> release.
    """
    fake_k8s = FakeK8s()

    tenant_calls: list[dict] = []
    wazuh_calls: list[dict] = []

    def _ok():
        return type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "ok": True}
        )()

    async def rec_install_tenant(*_a, **kw):
        tenant_calls.append(kw)
        return _ok()

    async def rec_install_wazuh(*_a, **kw):
        wazuh_calls.append(kw)
        return _ok()

    monkeypatch.setattr(controller_mod, "helm_install_tenant", rec_install_tenant)
    monkeypatch.setattr(controller_mod, "helm_install_wazuh", rec_install_wazuh)
    from soctalk.core.provisioning import helm as helm_mod
    monkeypatch.setattr(helm_mod, "helm_version", _fake_helm_version)

    controller = TenantController(session, k8s=fake_k8s, settings=_quick_settings())
    result = await controller.provision(provided_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value
    assert len(tenant_calls) == 1
    assert wazuh_calls == []

    # Rotate the external indexer password, then reconcile.
    integ = (
        await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == provided_tenant.id
            )
        )
    ).scalar_one()
    integ.wazuh_indexer_password_plain = "rotated-idx-pass"
    await session.commit()

    result = await controller.reconcile(provided_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # Secret rewritten with the rotated credential.
    ns = f"tenant-{provided_tenant.slug}"
    secret = fake_k8s.secrets[(ns, "tenant-external-siem-creds")]
    assert secret["INDEXER_PASSWORD"] == "rotated-idx-pass"

    # Exactly one more tenant-chart upgrade; STILL zero wazuh installs.
    assert len(tenant_calls) == 2
    assert wazuh_calls == [], "reconcile must never touch a wazuh-<slug> release"


# ---------------------------------------------------------------------------
# Bootstrap read-back on retry (tenant.provisioning.bootstrap-readback)
# ---------------------------------------------------------------------------
#
# Root-caused live on k3d: attempts 1..N failed at apply_secrets (LLM-key
# guard) AFTER ``_step_mint_secrets`` wrote Secret/tenant-bootstrap but BEFORE
# the wazuh release was ever installed. The retry's already-minted branch used
# to park ``bag['bootstrap'] = None`` and ``_step_helm_apply_wazuh`` then did
# the FIRST helm install with the literal placeholder 'rotated-prior-run' as
# admin/authd password — Wazuh rejects it (Error 5007) and the manager
# crash-loops forever. The chart renders ``<release>-wazuh-creds`` FROM helm
# values, so the values must always carry the true material.


async def _fail_then_capture_wazuh_values(
    session: AsyncSession,
    seeded_tenant: Tenant,
    monkeypatch,
) -> tuple[FakeK8s, TenantController, dict[str, str], dict]:
    """Shared first phase for the read-back tests.

    Runs provision once with no resolvable LLM key so the run fails at
    ``apply_secrets`` — strictly after mint_secrets wrote
    ``Secret/tenant-bootstrap`` and strictly before any wazuh helm install.
    Returns the fake k8s, the controller, a snapshot of the
    originally-minted bootstrap Secret data, and a mutable dict that the
    patched ``helm_install_wazuh`` fills with the values of every install.
    """
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_NAME", raising=False)
    monkeypatch.delenv("SOCTALK_INSTALL_LLM_SECRET_KEY", raising=False)

    await _set_llm_key(session, seeded_tenant.id, None)

    fake_k8s = FakeK8s()
    controller = _llm_guard_controller(session, fake_k8s)

    captured: dict = {"wazuh_values": []}

    async def rec_install_wazuh(*_a, per_tenant_values=None, **_kw):
        captured["wazuh_values"].append(per_tenant_values)
        return await _fake_helm_install_wazuh()

    monkeypatch.setattr(controller_mod, "helm_install_wazuh", rec_install_wazuh)

    # Attempt 1: mint_secrets mints + apply_secrets writes the bootstrap
    # Secret, then the LLM-key guard fails the run before any helm install.
    with pytest.raises(ProvisionError) as exc_info:
        await controller.provision(seeded_tenant.id, actor_id="test")
    assert exc_info.value.step == "apply_secrets"
    assert captured["wazuh_values"] == [], "wazuh must not be installed yet"

    ns = f"tenant-{seeded_tenant.slug}"
    minted = dict(fake_k8s.secrets[(ns, "tenant-bootstrap")])
    # The mint really happened: all 7 keys present, with real material.
    assert minted["wazuh_admin_pw"]
    assert minted["wazuh_authd_secret"]
    return fake_k8s, controller, minted, captured


async def test_retry_uses_originally_minted_wazuh_creds_not_placeholder(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Retry after a post-mint / pre-helm failure must hand helm the SAME
    wazuh credentials the first run minted — never 'rotated-prior-run'."""
    import json

    fake_k8s, controller, minted, captured = (
        await _fail_then_capture_wazuh_values(session, seeded_tenant, monkeypatch)
    )

    # Operator provides the LLM key; retry must now go all the way.
    await _set_llm_key(session, seeded_tenant.id, "sk-now-present")
    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    assert len(captured["wazuh_values"]) == 1
    values = captured["wazuh_values"][0]
    creds = values["credentials"]
    assert creds["apiPassword"] == minted["wazuh_admin_pw"], (
        "wazuh helm values must carry the ORIGINALLY-minted admin password "
        "(read back from Secret/tenant-bootstrap), not a placeholder"
    )
    assert creds["authdPassword"] == minted["wazuh_authd_secret"]
    assert "rotated-prior-run" not in json.dumps(values), (
        f"placeholder credential leaked into wazuh helm values: {values}"
    )

    # The bootstrap Secret still holds the same (re-written, idempotent) data.
    ns = f"tenant-{seeded_tenant.slug}"
    assert fake_k8s.secrets[(ns, "tenant-bootstrap")] == minted


async def test_retry_with_bootstrap_secret_deleted_regenerates_creds(
    session: AsyncSession, seeded_tenant: Tenant, patched_helm, monkeypatch
):
    """Already-minted path with Secret/tenant-bootstrap gone: fresh
    credentials are regenerated, written back, and used in the wazuh helm
    values — a retry can never proceed with placeholder credentials."""
    import json

    fake_k8s, controller, minted, captured = (
        await _fail_then_capture_wazuh_values(session, seeded_tenant, monkeypatch)
    )

    # Simulate the Secret vanishing between attempts (manual cleanup, etc.).
    ns = f"tenant-{seeded_tenant.slug}"
    del fake_k8s.secrets[(ns, "tenant-bootstrap")]

    await _set_llm_key(session, seeded_tenant.id, "sk-now-present")
    result = await controller.provision(seeded_tenant.id, actor_id="test")
    assert result.state == TenantState.ACTIVE.value

    # Fresh material was regenerated AND written back to the namespace.
    assert (ns, "tenant-bootstrap") in fake_k8s.secrets
    rewritten = fake_k8s.secrets[(ns, "tenant-bootstrap")]
    assert rewritten["wazuh_admin_pw"]
    assert rewritten["wazuh_admin_pw"] != minted["wazuh_admin_pw"]

    # And the SAME fresh material went into the wazuh helm values.
    assert len(captured["wazuh_values"]) == 1
    values = captured["wazuh_values"][0]
    creds = values["credentials"]
    assert creds["apiPassword"] == rewritten["wazuh_admin_pw"]
    assert creds["authdPassword"] == rewritten["wazuh_authd_secret"]
    assert "rotated-prior-run" not in json.dumps(values)


# ---------------------------------------------------------------------------
# sync_state: drift reconciliation (issue #104)
# ---------------------------------------------------------------------------


async def _controller_for_sync(session, fake_k8s):
    return TenantController(
        session, k8s=fake_k8s,
        settings=ControllerSettings(
            wazuh_chart_path="charts/wazuh",
            tenant_chart_ref="charts/soctalk-tenant",
        ),
    )


async def _set_state(session, tenant, state, *, runtime=None):
    tenant.state = state
    if runtime is not None:
        tenant.runtime = runtime
    session.add(tenant)
    await session.commit()


async def _events(session, tenant_id):
    rows = (await session.execute(
        select(TenantLifecycleEvent).where(
            TenantLifecycleEvent.tenant_id == tenant_id
        )
    )).scalars().all()
    return [e.event_type for e in rows]


async def test_sync_state_missing_namespace_first_only_degrades(
    session: AsyncSession, seeded_tenant: Tenant
):
    # First observation of a missing namespace must NOT archive (irreversible);
    # it degrades and stamps a grace marker so a transient 404 / in-flight
    # re-provision can recover.
    await _set_state(session, seeded_tenant, TenantState.ACTIVE.value)
    fake_k8s = FakeK8s()  # namespaces empty -> tenant-<slug> is absent
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.DEGRADED.value
    assert result.deleted_at is None
    assert (result.runtime or {}).get("namespace_missing_since") is not None
    evs = await _events(session, seeded_tenant.id)
    assert "degraded_namespace_missing" in evs
    assert "archived" not in evs


async def test_sync_state_archives_after_grace_window(
    session: AsyncSession, seeded_tenant: Tenant
):
    # Namespace still absent past the grace window -> irreversible archive.
    from datetime import datetime, timedelta
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    await _set_state(
        session, seeded_tenant, TenantState.DEGRADED.value,
        runtime={"namespace_missing_since": past},
    )
    fake_k8s = FakeK8s()  # namespace absent
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.ARCHIVED.value
    assert result.deleted_at is not None
    evs = await _events(session, seeded_tenant.id)
    assert "decommission_detected" in evs
    assert "archived" in evs


async def test_sync_state_within_grace_stays_degraded(
    session: AsyncSession, seeded_tenant: Tenant
):
    from datetime import datetime
    recent = datetime.utcnow().isoformat()
    await _set_state(
        session, seeded_tenant, TenantState.DEGRADED.value,
        runtime={"namespace_missing_since": recent},
    )
    fake_k8s = FakeK8s()
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.DEGRADED.value
    assert result.deleted_at is None


async def test_sync_state_clears_marker_when_namespace_returns(
    session: AsyncSession, seeded_tenant: Tenant
):
    from datetime import datetime
    await _set_state(
        session, seeded_tenant, TenantState.DEGRADED.value,
        runtime={"namespace_missing_since": datetime.utcnow().isoformat()},
    )
    fake_k8s = FakeK8s()
    fake_k8s.namespaces.add(f"tenant-{seeded_tenant.slug}")
    fake_k8s.pods_ready = True
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    # Recovered to active AND the stale marker was cleared.
    assert result.state == TenantState.ACTIVE.value
    assert (result.runtime or {}).get("namespace_missing_since") is None


async def test_sync_state_skips_tenant_with_active_job(
    session: AsyncSession, seeded_tenant: Tenant
):
    # A queued provision/decommission owns the tenant; drift must defer to it
    # and never archive a tenant that is mid-(re)provision.
    from soctalk.core.tenancy.models import ProvisioningJob
    await _set_state(session, seeded_tenant, TenantState.ACTIVE.value)
    session.add(ProvisioningJob(
        tenant_id=seeded_tenant.id, kind="tenant.provision", status="pending",
    ))
    await session.commit()
    fake_k8s = FakeK8s()  # namespace absent
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.ACTIVE.value
    assert await _events(session, seeded_tenant.id) == []


async def test_sync_state_finalizes_decommissioning_with_missing_namespace(
    session: AsyncSession, seeded_tenant: Tenant
):
    # A committed decommission whose namespace is already gone (no in-flight
    # job) is finalized straight to archived — no grace, teardown was intended.
    await _set_state(session, seeded_tenant, TenantState.DECOMMISSIONING.value)
    fake_k8s = FakeK8s()  # namespace absent
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.ARCHIVED.value
    assert result.deleted_at is not None
    assert "archived" in await _events(session, seeded_tenant.id)


async def test_sync_state_recovers_degraded_when_pods_ready(
    session: AsyncSession, seeded_tenant: Tenant
):
    await _set_state(session, seeded_tenant, TenantState.DEGRADED.value)
    fake_k8s = FakeK8s()
    fake_k8s.namespaces.add(f"tenant-{seeded_tenant.slug}")
    fake_k8s.pods_ready = True
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.ACTIVE.value
    assert "recovered" in await _events(session, seeded_tenant.id)


async def test_sync_state_degrades_active_when_pods_not_ready(
    session: AsyncSession, seeded_tenant: Tenant
):
    await _set_state(
        session, seeded_tenant, TenantState.ACTIVE.value, runtime={}
    )
    fake_k8s = FakeK8s()
    fake_k8s.namespaces.add(f"tenant-{seeded_tenant.slug}")
    fake_k8s.pods_ready = False
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.DEGRADED.value
    assert "degraded_pods_not_ready" in await _events(session, seeded_tenant.id)


async def test_sync_state_noop_when_active_and_ready(
    session: AsyncSession, seeded_tenant: Tenant
):
    await _set_state(session, seeded_tenant, TenantState.ACTIVE.value)
    fake_k8s = FakeK8s()
    fake_k8s.namespaces.add(f"tenant-{seeded_tenant.slug}")
    fake_k8s.pods_ready = True
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.ACTIVE.value
    # No lifecycle transition event was written for a healthy tenant.
    assert await _events(session, seeded_tenant.id) == []


async def test_sync_state_probe_error_never_archives(
    session: AsyncSession, seeded_tenant: Tenant
):
    # A transient kube API error must NOT be mistaken for a deleted tenant.
    await _set_state(session, seeded_tenant, TenantState.ACTIVE.value)
    fake_k8s = FakeK8s()
    fake_k8s.namespace_probe_error = True
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.ACTIVE.value
    assert await _events(session, seeded_tenant.id) == []


async def test_sync_state_ignores_transitional_states(
    session: AsyncSession, seeded_tenant: Tenant
):
    # A provisioning tenant has no namespace yet; sync_state must leave it be.
    await _set_state(session, seeded_tenant, TenantState.PROVISIONING.value)
    fake_k8s = FakeK8s()  # namespace absent
    controller = await _controller_for_sync(session, fake_k8s)

    result = await controller.sync_state(seeded_tenant.id)

    assert result.state == TenantState.PROVISIONING.value
    assert await _events(session, seeded_tenant.id) == []
