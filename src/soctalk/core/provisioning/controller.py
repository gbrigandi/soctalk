"""Tenant controller: orchestrates the full provisioning lifecycle.

``docs/multi-tenant/two-chart-contract.md`` §6.

Operations exposed:

- :meth:`TenantController.provision`: drive a new tenant to ``active``.
  Structured as a sequence of idempotent, resumable steps — a crashed
  worker simply re-enters ``provision()`` and each step short-circuits
  when its postcondition already holds.
- :meth:`TenantController.reconcile`: re-render + helm-upgrade an
  ``active`` tenant's release without any lifecycle transition, so
  chart-affecting config edits (e.g. LLM provider/base_url/model)
  actually propagate to the running release.
- :meth:`TenantController.suspend`: scale data plane to zero.
- :meth:`TenantController.resume`: scale back up.
- :meth:`TenantController.decommission`: teardown.
- :meth:`TenantController.sync_state`: reconcile DB desired state vs K8s.

Each state transition appends a :class:`TenantLifecycleEvent` row, and
every step emits its own lifecycle event so the wizard landing can render
progress without inventing a second timeline.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import select

from soctalk.core.provisioning.helm import (
    HelmError,
    helm_install_tenant,
    helm_install_wazuh,
    helm_uninstall,
)
from soctalk.core.provisioning.k8s import K8sClient, new_k8s_client
from soctalk.core.provisioning.render import (
    Profile,
    primary_llm_engine_for_render,
    render_tenant_values,
    render_wazuh_values,
)
from soctalk.core.provisioning.secrets_gen import (
    TenantBootstrapSecrets,
    bootstrap_as_k8s_secret_data,
    generate_bootstrap_secrets,
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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into a copy of ``base``.

    Dict values merge key-by-key; any non-dict value in the overlay
    overwrites the base value wholesale (including lists).
    """
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _llm_secret_data(api_key: str, llm_tiers: dict | None) -> dict[str, str]:
    """Build the tenant-llm-key Secret data map (issue #12).

    ``api_key`` (primary) plus ``<tier>_api_key`` for each per-tier backend that
    carries its OWN credential — the chart's 35-runs-worker mounts
    SOCTALK_<TIER>_API_KEY from these. Same-provider tiers reuse ``api_key`` and
    contribute no own key. Note: ``put_secret`` patches/merges, so a removed tier
    leaves a now-unused stale key until the deferred rotation follow-up adds
    full-replace (harmless — the worker env no longer references it).
    """
    data = {"api_key": api_key}
    for tier, block in (llm_tiers or {}).items():
        tier_key = (block or {}).get("api_key_plain")
        if tier_key:
            data[f"{tier}_api_key"] = tier_key
    return data


class ProvisionError(RuntimeError):
    """Raised when a provisioning step fails irrecoverably.

    Carries ``step`` so the worker can tag the failure in lifecycle events
    and the wizard can show which named step tripped.
    """

    def __init__(self, message: str, *, step: str | None = None) -> None:
        super().__init__(message)
        self.step = step


class TenantLifecycleError(RuntimeError):
    """Raised when a state transition is invalid."""


# ---------------------------------------------------------------------------
def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp stored in ``tenant.runtime``; None on garbage.

    Tolerates a trailing ``Z`` and strips tz to a naive UTC datetime so it
    compares cleanly against ``datetime.utcnow()`` (the convention used
    throughout the controller's transition timestamps)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=None)


# State transition table: valid next states from each state.
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    TenantState.PENDING.value: frozenset({TenantState.PROVISIONING.value}),
    TenantState.PROVISIONING.value: frozenset(
        {TenantState.ACTIVE.value, TenantState.DEGRADED.value}
    ),
    TenantState.ACTIVE.value: frozenset(
        {TenantState.SUSPENDED.value, TenantState.DEGRADED.value,
         TenantState.DECOMMISSIONING.value}
    ),
    TenantState.DEGRADED.value: frozenset(
        {TenantState.PROVISIONING.value, TenantState.ACTIVE.value,
         TenantState.DECOMMISSIONING.value}
    ),
    TenantState.SUSPENDED.value: frozenset(
        {TenantState.ACTIVE.value, TenantState.DECOMMISSIONING.value}
    ),
    TenantState.DECOMMISSIONING.value: frozenset({TenantState.ARCHIVED.value}),
    TenantState.ARCHIVED.value: frozenset({TenantState.PURGED.value}),
    TenantState.PURGED.value: frozenset(),
}


@dataclass
class ControllerSettings:
    """Configuration for the controller: normally set from env at startup.

    Release-name-aware fields let the controller locate chart-produced
    Services that are prefixed with ``{{ .Release.Name }}``.
    """

    soctalk_system_namespace: str = "soctalk-system"
    soctalk_system_release_name: str = "soctalk-system"
    api_service_name: str = "soctalk-system-api"
    tenant_chart_ref: str = "oci://ghcr.io/soctalk/charts/soctalk-tenant"
    tenant_chart_version: str = "0.1.0"
    wazuh_chart_path: str = "charts/wazuh"
    default_agent_dns_suffix: str = "soc.mssp.local"
    default_cert_issuer: str = "letsencrypt-prod"
    wait_timeout: str = "15m"
    # Profile-specific storage class overrides (controller → wazuh chart).
    # ``persistent`` needs a real provisioner; ``poc`` uses whatever the
    # chart default gives it (usually ``local-path`` on k3s/k3d).
    # Operator-settable through the soctalk-system chart
    # (``tenantProvisioning.persistentStorageClass`` → the env var read in
    # ``from_env``): ``standard`` is the GKE / minikube name, K3s ships
    # ``local-path``. Empty is normalized back to ``standard`` rather than
    # meaning "use the cluster default" -- the wazuh PVC templates render
    # ``storageClassName`` unconditionally, and an empty value there tells
    # Kubernetes to DISABLE dynamic provisioning.
    persistent_storage_class: str = "standard"
    # Pod-readiness wait settings.
    readiness_poll_interval_seconds: float = 3.0
    readiness_timeout_seconds: float = 600.0
    # Optional values overlays applied last, after profile defaults and
    # render_tenant_values. Deep-merged at every nesting level. Intended
    # for: (a) the live k3d e2e test, which swaps the adapter image for
    # a reachable stub; (b) ops overrides that shouldn't live in the
    # per-tenant DB row. Leave empty in production.
    tenant_values_overlay: dict | None = None
    wazuh_values_overlay: dict | None = None
    # Default value for the tenant chart's ``networkPolicies.enabled``.
    # Defaults to True (safe). Set to False on clusters whose CNI's NP
    # implementation breaks SocTalk traffic (e.g. kube-router on the
    # 192.168.1.28 lab — NPs that look correct on paper still trigger
    # connection-refused between soctalk-system and tenant-* pods).
    # Env: ``SOCTALK_TENANT_NETWORK_POLICIES_ENABLED=0|false``.
    tenant_network_policies_enabled: bool = True
    # Grace before ``sync_state`` treats a continuously-missing namespace as a
    # real disposal and ARCHIVES the tenant (irreversible). The first sustained
    # absence only degrades; archival waits this long, so a transient 404 or an
    # in-flight re-provision recovers first (issue #104). Env:
    # ``SOCTALK_NAMESPACE_MISSING_GRACE_SECONDS``.
    namespace_missing_grace_seconds: float = 900.0

    @classmethod
    def from_env(cls) -> ControllerSettings:
        ns = os.getenv("SOCTALK_SYSTEM_NS", "soctalk-system")
        release = os.getenv("SOCTALK_SYSTEM_RELEASE_NAME", "soctalk-system")
        return cls(
            soctalk_system_namespace=ns,
            soctalk_system_release_name=release,
            api_service_name=os.getenv(
                "SOCTALK_API_SERVICE_NAME", f"{release}-api"
            ),
            tenant_chart_ref=os.getenv(
                "SOCTALK_TENANT_CHART_REF",
                "oci://ghcr.io/soctalk/charts/soctalk-tenant",
            ),
            tenant_chart_version=os.getenv(
                "SOCTALK_TENANT_CHART_VERSION", "0.1.0"
            ),
            wazuh_chart_path=os.getenv(
                "SOCTALK_WAZUH_CHART_PATH", "charts/wazuh"
            ),
            default_agent_dns_suffix=os.getenv(
                "SOCTALK_AGENT_DNS_SUFFIX", "soc.mssp.local"
            ),
            default_cert_issuer=os.getenv(
                "SOCTALK_CERT_ISSUER", "letsencrypt-prod"
            ),
            wait_timeout=os.getenv("SOCTALK_HELM_TIMEOUT", "15m"),
            persistent_storage_class=(
                os.getenv(
                    "SOCTALK_PERSISTENT_STORAGE_CLASS", "standard"
                ).strip()
                or "standard"
            ),
            tenant_network_policies_enabled=(
                os.getenv("SOCTALK_TENANT_NETWORK_POLICIES_ENABLED", "1")
                not in {"0", "false", "False", "no", "NO"}
            ),
            namespace_missing_grace_seconds=float(
                os.getenv("SOCTALK_NAMESPACE_MISSING_GRACE_SECONDS", "900")
            ),
        )


# ---------------------------------------------------------------------------
# Per-call context shared across the step functions.
# ---------------------------------------------------------------------------


@dataclass
class _StepContext:
    tenant: Tenant
    organization: Organization
    integration: IntegrationConfig
    branding: BrandingConfig
    namespace: str
    release_tenant: str
    release_wazuh: str
    profile: Profile
    actor_id: str | None
    llm_secret_name: str
    # Per-step mutable bag — e.g. minted Wazuh creds shared between
    # mint_secrets and helm_apply_wazuh.
    bag: dict = field(default_factory=dict)


# A provisioning step: takes the shared context, mutates K8s/DB, returns None.
# The step list is assembled per-profile in :meth:`TenantController.provision`.
_StepFn = Callable[["_StepContext"], Awaitable[None]]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class TenantController:
    """Drives tenant lifecycle operations.

    Takes a DB session supplied by the caller. The worker hands in an
    MSSP-role session; request-driven calls hand in an app-role session
    already inside a ``tenant_context`` for the target tenant.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        k8s: K8sClient | None = None,
        settings: ControllerSettings | None = None,
    ) -> None:
        self.session = session
        self.k8s = k8s or new_k8s_client()
        self.settings = settings or ControllerSettings.from_env()

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def provision(self, tenant_id: UUID, *, actor_id: str | None = None) -> Tenant:
        """Drive a tenant toward ``active``, resumable at step boundaries.

        Callable from three states:

        - ``pending`` — fresh tenant; transitions to ``provisioning``.
        - ``provisioning`` — worker resume after crash; no state change.
        - ``degraded`` — explicit retry; back to ``provisioning``.

        Every step is idempotent (checks its own postcondition first),
        so the whole method is safe to re-enter.
        """
        tenant = await self._load_tenant(tenant_id)

        if tenant.state == TenantState.ACTIVE.value:
            # Nothing to do; treat as success.
            return tenant

        if tenant.state == TenantState.PENDING.value:
            await self._transition(
                tenant,
                TenantState.PROVISIONING.value,
                actor_id=actor_id,
                event_type="provisioning_started",
                details={"profile": tenant.profile},
            )
        elif tenant.state == TenantState.DEGRADED.value:
            await self._transition(
                tenant,
                TenantState.PROVISIONING.value,
                actor_id=actor_id,
                event_type="retry_requested",
                details={"profile": tenant.profile},
            )
        elif tenant.state != TenantState.PROVISIONING.value:
            raise TenantLifecycleError(
                f"tenant {tenant.id}: cannot provision from state={tenant.state}"
            )

        # Load context
        org = await self._load_organization(tenant.organization_id)
        integration = await self._load_integration(tenant.id)
        branding = await self._load_branding(tenant.id)

        profile: Profile = tenant.profile if tenant.profile in ("poc", "persistent", "provided") else "poc"
        ctx = _StepContext(
            tenant=tenant,
            organization=org,
            integration=integration,
            branding=branding,
            namespace=f"tenant-{tenant.slug}",
            release_tenant=f"tenant-{tenant.slug}",
            release_wazuh=f"wazuh-{tenant.slug}",
            profile=profile,
            actor_id=actor_id,
            llm_secret_name=f"tenant-{tenant.id}-llm",
        )

        # Agent-managed skip guard: when :issue-agent has already minted a
        # TenantInstallation row, the L2 cloud-agent is going to install
        # the tenant chart on its own cluster. Running the sync helm-apply
        # locally here would race (or fail with "namespace not found" on
        # single-cluster MSSPs that aren't hosting the tenant) and leave
        # tenants.state 'degraded' even after the L2 install succeeds.
        # Emit a marker event and short-circuit — the L2 completion hook
        # in agents/api.py will project the L2 install state onto
        # tenants.state when the agent reports success.
        from soctalk.core.agents.models import TenantInstallation
        installation_row = (
            await self.session.execute(
                select(TenantInstallation)
                .where(TenantInstallation.tenant_id == tenant.id)
                .where(TenantInstallation.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if installation_row is not None:
            db_event = TenantLifecycleEvent(
                tenant_id=tenant.id,
                event_type="agent_managed_provisioning_skipped",
                from_state=tenant.state,
                to_state=tenant.state,
                actor_id=f"user:{actor_id}",
                details={"installation_id": str(installation_row.id)},
            )
            self.session.add(db_event)
            await self.session.commit()
            return

        # Profile-aware step list. A 'provided' tenant brings its own external
        # Wazuh, so SocTalk:
        #   - writes the external-SIEM creds Secret (extra step, between
        #     apply_secrets and helm_apply_tenant),
        #   - never installs the wazuh-<slug> release (a single
        #     'wazuh_skipped_provided' marker stands in for it),
        #   - does NOT clobber integration.wazuh_url with an in-cluster URL.
        steps: list[tuple[str, _StepFn]] = [
            ("preflight", self._step_preflight),
            ("mint_secrets", self._step_mint_secrets),
            ("ensure_namespace", self._step_ensure_namespace),
            ("apply_secrets", self._step_apply_secrets),
        ]
        if ctx.profile == "provided":
            steps.append(
                ("write_external_siem_secret", self._step_write_external_siem_secret)
            )
        steps.append(("helm_apply_tenant", self._step_helm_apply_tenant))
        if ctx.profile == "provided":
            steps.append(("wazuh_skipped", self._step_wazuh_skipped_provided))
        else:
            steps.append(("helm_apply_wazuh", self._step_helm_apply_wazuh))
        steps.append(("wait_workloads", self._step_wait_workloads))
        if ctx.profile != "provided":
            steps.append(
                ("write_integration_config", self._step_write_integration_config)
            )
        steps.append(("finalize_active", self._step_finalize_active))

        for name, step in steps:
            try:
                await step(ctx)
            except ProvisionError as e:
                e.step = e.step or name
                await self._transition(
                    tenant,
                    TenantState.DEGRADED.value,
                    actor_id=actor_id,
                    event_type="provisioning_failed",
                    details={"step": e.step, "error": str(e)},
                )
                await self.session.commit()
                raise
            except Exception as e:  # noqa: BLE001
                # Unexpected failures are still terminal for this attempt.
                await self._transition(
                    tenant,
                    TenantState.DEGRADED.value,
                    actor_id=actor_id,
                    event_type="provisioning_failed",
                    details={"step": name, "error": str(e)},
                )
                await self.session.commit()
                raise ProvisionError(str(e), step=name) from e

        await self.session.commit()
        return tenant

    async def reconcile(self, tenant_id: UUID, *, actor_id: str | None = None) -> Tenant:
        """Re-render + helm-upgrade an ``active`` tenant's release in place.

        Why this exists: chart-affecting LLM edits (provider/base_url/model)
        flow through ``render_tenant_values`` into ``values.llm.*`` and the
        networkPolicies LLM-host FQDN egress allow-list. ``provision()``
        early-returns on ``active`` and the active→provisioning transition
        is illegal, so without this operation a PATCHed config never reached
        the running release (stale env schema + stale egress allow-list).

        Runs ONLY the value-affecting subset of the provision step list —
        each step is idempotent by design, so this is a strict reuse, not a
        re-implementation:

        - ``write_external_siem_secret`` (profile='provided' only): rewrite
          ``Secret/tenant-external-siem-creds`` from the current
          IntegrationConfig row,
        - ``helm_apply_tenant``: re-render + ``helm upgrade`` of release
          ``tenant-<slug>``,
        - ``wait_workloads``: poll the namespace back to Ready.

        No lifecycle transition on success — the tenant stays ``active`` and
        the run is bracketed by ``reconcile_started`` / ``reconcile_succeeded``
        events. On failure the tenant transitions active → degraded (legal
        per the transition table) with a ``reconcile_failed`` event naming
        the step + error. Never touches the ``wazuh-<slug>`` release: SIEM
        topology changes are a provision/decommission concern.

        Raises :class:`TenantLifecycleError` for non-active tenants —
        callers route every other state to :meth:`provision`.
        """
        tenant = await self._load_tenant(tenant_id)

        if tenant.state != TenantState.ACTIVE.value:
            raise TenantLifecycleError(
                f"tenant {tenant.id}: cannot reconcile from state={tenant.state}; "
                "reconcile applies only to 'active' tenants "
                "(use provision() for the rest)"
            )

        org = await self._load_organization(tenant.organization_id)
        integration = await self._load_integration(tenant.id)
        branding = await self._load_branding(tenant.id)

        profile: Profile = (
            tenant.profile
            if tenant.profile in ("poc", "persistent", "provided")
            else "poc"
        )
        ctx = _StepContext(
            tenant=tenant,
            organization=org,
            integration=integration,
            branding=branding,
            namespace=f"tenant-{tenant.slug}",
            release_tenant=f"tenant-{tenant.slug}",
            release_wazuh=f"wazuh-{tenant.slug}",
            profile=profile,
            actor_id=actor_id,
            llm_secret_name=f"tenant-{tenant.id}-llm",
        )

        await self._emit_event(
            ctx, "reconcile_started", details={"profile": profile}
        )

        steps: list[tuple[str, _StepFn]] = []
        if ctx.profile == "provided":
            steps.append(
                ("write_external_siem_secret", self._step_write_external_siem_secret)
            )
        # Re-sync the LLM key Secret before the chart re-renders (issue #12): a
        # PATCH that adds/changes a per-tier backend must write the tier's
        # ``<tier>_api_key`` into tenant-llm-key, else the re-rendered worker
        # references a Secret key that doesn't exist yet and CrashLoops.
        # Guarded on llm_tiers so single-provider reconciles don't gain a new
        # hard LLM-key dependency (_copy_llm_key raises when no key resolves) —
        # their key is already in place and unchanged by a non-LLM reconcile.
        if ctx.integration.llm_tiers:
            steps.append(("sync_llm_key", self._copy_llm_key_to_tenant_ns))
        steps.append(("helm_apply_tenant", self._step_helm_apply_tenant))
        steps.append(("wait_workloads", self._step_wait_workloads))

        for name, step in steps:
            try:
                await step(ctx)
            except ProvisionError as e:
                e.step = e.step or name
                await self._transition(
                    tenant,
                    TenantState.DEGRADED.value,
                    actor_id=actor_id,
                    event_type="reconcile_failed",
                    details={"step": e.step, "error": str(e)},
                )
                await self.session.commit()
                raise
            except Exception as e:  # noqa: BLE001
                await self._transition(
                    tenant,
                    TenantState.DEGRADED.value,
                    actor_id=actor_id,
                    event_type="reconcile_failed",
                    details={"step": name, "error": str(e)},
                )
                await self.session.commit()
                raise ProvisionError(str(e), step=name) from e

        await self._emit_event(
            ctx,
            "reconcile_succeeded",
            details={"release": ctx.release_tenant, "profile": profile},
        )
        await self.session.commit()
        return tenant

    async def suspend(self, tenant_id: UUID, *, actor_id: str | None = None) -> Tenant:
        tenant = await self._load_tenant(tenant_id)
        self._assert_transition(tenant, TenantState.SUSPENDED.value)
        await self._transition(
            tenant,
            TenantState.SUSPENDED.value,
            actor_id=actor_id,
            event_type="suspended",
        )
        await self.session.commit()
        return tenant

    async def resume(self, tenant_id: UUID, *, actor_id: str | None = None) -> Tenant:
        tenant = await self._load_tenant(tenant_id)
        self._assert_transition(tenant, TenantState.ACTIVE.value)
        await self._transition(
            tenant,
            TenantState.ACTIVE.value,
            actor_id=actor_id,
            event_type="resumed",
        )
        await self.session.commit()
        return tenant

    async def decommission(
        self,
        tenant_id: UUID,
        *,
        actor_id: str | None = None,
        force: bool = False,
    ) -> Tenant:
        """Tear down a tenant's data plane and archive the record."""
        tenant = await self._load_tenant(tenant_id)

        # Decommission can run from any non-terminal state; just make the
        # initial transition explicit.
        if tenant.state not in (
            TenantState.DECOMMISSIONING.value,
            TenantState.ARCHIVED.value,
            TenantState.PURGED.value,
        ):
            await self._transition(
                tenant,
                TenantState.DECOMMISSIONING.value,
                actor_id=actor_id,
                event_type="decommission_started",
            )

        ns = f"tenant-{tenant.slug}"
        release_tenant = f"tenant-{tenant.slug}"
        release_wazuh = f"wazuh-{tenant.slug}"

        # Order: wazuh first (so agents stop talking), then soctalk-tenant,
        # then namespace (PVCs cascade).
        for release in (release_wazuh, release_tenant):
            try:
                await helm_uninstall(release, ns)
                self.session.add(
                    TenantLifecycleEvent(
                        tenant_id=tenant.id,
                        event_type="helm_uninstalled",
                        actor_id=actor_id,
                        details={"release": release},
                    )
                )
            except HelmError as e:
                if not force:
                    raise ProvisionError(
                        f"helm uninstall {release} failed: {e}",
                        step="helm_uninstall",
                    ) from e
                logger.warning(
                    "helm_uninstall_failed_continuing",
                    tenant=str(tenant.id),
                    release=release,
                    error=str(e),
                )

        await self.k8s.delete_namespace(ns)

        tenant.deleted_at = datetime.utcnow()
        await self._transition(
            tenant,
            TenantState.ARCHIVED.value,
            actor_id=actor_id,
            event_type="archived",
        )
        await self.session.commit()
        return tenant

    async def sync_state(
        self, tenant_id: UUID, *, actor_id: str | None = None
    ) -> Tenant:
        """Reconcile SocTalk DB state against actual Kubernetes workloads.

        Drift repair for a tenant that should have a running data plane —
        ``active``/``degraded``, plus a ``decommissioning`` finalizer. Never
        touches ``suspended`` (operator-owned) or transitional/terminal states.

        Safety rails, because ``archived`` is irreversible (issue #104):

        1. **Defer to the job queue.** A tenant with a pending/in-flight
           provisioning job is owned by that job — skip it entirely, so a
           re-provision or decommission in flight is never raced.
        2. **Probe errors never mutate.** Only a *definitive* namespace-404
           (not a 403/5xx/connection blip) counts as "gone".
        3. **Two-phase, grace-gated archive.** The FIRST sustained absence
           only DEGRADES (reversible) and stamps ``namespace_missing_since``.
           Archival waits until the namespace has been continuously absent for
           ``namespace_missing_grace_seconds``, so a transient 404 or an
           in-flight re-provision recovers without an irreversible archive.
        4. **Locked writes.** Every mutation re-reads the row ``FOR UPDATE``
           and re-verifies (state + no active job) before committing, so a
           concurrent suspend/decommission on another replica is never
           clobbered by this drift-time write.

        A committed ``decommissioning`` whose namespace is already gone is
        finalized straight to ``archived`` (the operator already asked for
        teardown — no grace needed).
        """
        tenant = await self._load_tenant(tenant_id)
        if tenant.state not in (
            TenantState.ACTIVE.value,
            TenantState.DEGRADED.value,
            TenantState.DECOMMISSIONING.value,
        ):
            return tenant
        # A queued/in-flight lifecycle job owns this tenant; defer to it.
        if await self._has_active_provisioning_job(tenant.id):
            return tenant
        ns = f"tenant-{tenant.slug}"

        # --- Probe actual cluster state (slow, unlocked) ---
        try:
            present = await self.k8s.namespace_exists(ns)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "sync_state_probe_failed",
                tenant=str(tenant.id), step="namespace", error=str(e),
            )
            return tenant

        if not present:
            return await self._reconcile_missing_namespace(
                tenant_id, ns, actor_id=actor_id
            )

        try:
            pods = await self.k8s.read_pods(ns)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "sync_state_probe_failed",
                tenant=str(tenant.id), step="pods", error=str(e),
            )
            return tenant
        # Namespace is back / present: an empty namespace counts as "no
        # workloads" (not ready); a Pending pod with no container statuses is
        # NOT ready (``all([])`` would wrongly be True).
        all_ready = bool(pods) and all(p["ready"] for p in pods)
        return await self._reconcile_present_namespace(
            tenant_id, all_ready, actor_id=actor_id
        )

    async def _has_active_provisioning_job(self, tenant_id: UUID) -> bool:
        from soctalk.core.tenancy.models import ProvisioningJob

        row = (
            await self.session.execute(
                select(ProvisioningJob.id)
                .where(ProvisioningJob.tenant_id == tenant_id)
                .where(ProvisioningJob.status.in_(("pending", "in_flight")))
                .limit(1)
            )
        ).first()
        return row is not None

    async def _reconcile_missing_namespace(
        self, tenant_id: UUID, ns: str, *, actor_id: str | None
    ) -> Tenant:
        """Namespace is definitively gone. Degrade-then-archive under grace."""
        tenant = await self._load_tenant(tenant_id, for_update=True)
        # Re-verify under the lock: state may have moved, or a job may have
        # been enqueued, between the probe and this write.
        if tenant.state not in (
            TenantState.ACTIVE.value,
            TenantState.DEGRADED.value,
            TenantState.DECOMMISSIONING.value,
        ) or await self._has_active_provisioning_job(tenant.id):
            await self.session.rollback()
            return await self._load_tenant(tenant_id)

        now = datetime.utcnow()

        # A decommission that already committed but left the namespace behind:
        # finalize immediately — teardown was the intent.
        if tenant.state == TenantState.DECOMMISSIONING.value:
            return await self._finalize_archive(
                tenant, actor_id=actor_id, ns=ns,
                reason="decommission_namespace_gone",
            )

        missing_since = _parse_iso(
            (tenant.runtime or {}).get("namespace_missing_since")
        )
        grace = self.settings.namespace_missing_grace_seconds
        if missing_since is not None and (
            (now - missing_since).total_seconds() >= grace
        ):
            # Sustained absence past the grace window → real disposal.
            return await self._finalize_archive(
                tenant, actor_id=actor_id, ns=ns,
                reason="workloads_missing_drift",
            )

        # First observation (or still within grace): DEGRADE (reversible) and
        # stamp the marker. If already degraded, just refresh the marker.
        if missing_since is None:
            tenant.runtime = {
                **(tenant.runtime or {}),
                "namespace_missing_since": now.isoformat(),
            }
            logger.warning(
                "sync_state_namespace_missing",
                tenant=str(tenant.id), namespace=ns, from_state=tenant.state,
            )
            if tenant.state == TenantState.ACTIVE.value:
                await self._transition(
                    tenant, TenantState.DEGRADED.value, actor_id=actor_id,
                    event_type="degraded_namespace_missing",
                    details={"namespace": ns},
                )
        await self.session.commit()
        return tenant

    async def _reconcile_present_namespace(
        self, tenant_id: UUID, all_ready: bool, *, actor_id: str | None
    ) -> Tenant:
        tenant = await self._load_tenant(tenant_id, for_update=True)
        if tenant.state not in (
            TenantState.ACTIVE.value, TenantState.DEGRADED.value
        ) or await self._has_active_provisioning_job(tenant.id):
            await self.session.rollback()
            return await self._load_tenant(tenant_id)

        # The namespace is back — a prior missing-marker is stale.
        marker_cleared = False
        if (tenant.runtime or {}).get("namespace_missing_since") is not None:
            rt = {**(tenant.runtime or {})}
            rt.pop("namespace_missing_since", None)
            tenant.runtime = rt
            marker_cleared = True

        if tenant.state == TenantState.ACTIVE.value and not all_ready:
            if (tenant.runtime or {}).get("last_heartbeat") is None:
                await self._transition(
                    tenant, TenantState.DEGRADED.value, actor_id=actor_id,
                    event_type="degraded_pods_not_ready",
                )
        elif tenant.state == TenantState.DEGRADED.value and all_ready:
            await self._transition(
                tenant, TenantState.ACTIVE.value, actor_id=actor_id,
                event_type="recovered", details={"reason": "pods_ready"},
            )
        elif not marker_cleared:
            await self.session.rollback()
            return await self._load_tenant(tenant_id)
        await self.session.commit()
        return tenant

    async def _finalize_archive(
        self, tenant: Tenant, *, actor_id: str | None, ns: str, reason: str
    ) -> Tenant:
        """Drive a tenant with a vanished data plane to the terminal
        ``archived`` state atomically (single commit). Caller holds the row
        lock and has re-verified state + no active job."""
        logger.warning(
            "sync_state_archiving_missing_workloads",
            tenant=str(tenant.id), namespace=ns, from_state=tenant.state,
            reason=reason,
        )
        if tenant.state != TenantState.DECOMMISSIONING.value:
            await self._transition(
                tenant, TenantState.DECOMMISSIONING.value, actor_id=actor_id,
                event_type="decommission_detected",
                details={"reason": reason, "namespace": ns},
            )
        if tenant.deleted_at is None:
            tenant.deleted_at = datetime.utcnow()
        await self._transition(
            tenant, TenantState.ARCHIVED.value, actor_id=actor_id,
            event_type="archived",
            details={"reason": reason, "namespace": ns},
        )
        await self.session.commit()
        return tenant

    # ------------------------------------------------------------------
    # Provisioning steps
    # ------------------------------------------------------------------

    async def _storage_class_hint(self) -> str:
        """Best-effort ``; available: ...`` suffix for the preflight error.

        Never raises. It only decorates a message that is already an
        error, so an RBAC gap -- or a client that doesn't implement the
        call -- must not mask the real StorageClass problem. Naming what
        the cluster actually has turns a dead-end error into a one-line
        fix (``tenantProvisioning.persistentStorageClass``).
        """
        try:
            names = await self.k8s.list_storage_classes()
        except Exception:  # noqa: BLE001
            return ""
        if not names:
            return "; cluster has no storage classes at all"
        return f"; available: {', '.join(sorted(names))}"

    async def _step_preflight(self, ctx: _StepContext) -> None:
        """Fail fast on cluster prerequisites before any tenant mutation."""
        # Kube API reachable (read a benign object the controller SA owns).
        try:
            await self.k8s.check_reachable()
        except Exception as e:  # noqa: BLE001
            raise ProvisionError(
                f"kubernetes API unreachable: {e}", step="preflight"
            ) from e

        # Helm binary.
        try:
            from soctalk.core.provisioning.helm import helm_version

            await helm_version()
        except Exception as e:  # noqa: BLE001
            raise ProvisionError(
                f"helm CLI not available on controller image: {e}",
                step="preflight",
            ) from e

        # StorageClass only matters for persistent profile.
        if ctx.profile == "persistent":
            sc = self.settings.persistent_storage_class
            try:
                ok = await self.k8s.storage_class_exists(sc)
            except Exception as e:  # noqa: BLE001
                raise ProvisionError(
                    f"could not enumerate storage classes: {e}",
                    step="preflight",
                ) from e
            if not ok:
                hint = await self._storage_class_hint()
                raise ProvisionError(
                    f"storage class '{sc}' not present on cluster "
                    f"(required by persistent profile){hint}",
                    step="preflight",
                )

        await self._emit_event(
            ctx, "preflight_ok", details={"profile": ctx.profile}
        )

    async def _step_mint_secrets(self, ctx: _StepContext) -> None:
        """Mint the per-tenant ``TenantSecret`` reference rows once per tenant.

        Postcondition (idempotency marker): a ``TenantSecret`` row exists for
        the tenant whose ``purpose`` is the profile-appropriate Wazuh marker —
        ``bootstrap`` for in-cluster profiles (poc/persistent) and
        ``external-siem-creds`` for ``provided`` (which never mints a bootstrap
        Secret because there is no in-namespace Wazuh). If already present we
        skip re-minting the DB rows, but for poc/persistent we still READ BACK
        the raw material from ``Secret/tenant-bootstrap`` (regenerating if it
        vanished) — the wazuh chart renders its creds Secret FROM helm values,
        so every later helm apply needs the true material in the bag.
        """
        # 'provided' tenants never mint a bootstrap Secret (no in-namespace
        # Wazuh), so idempotency keys off the external-siem-creds row instead.
        marker_purpose = (
            "external-siem-creds" if ctx.profile == "provided" else "bootstrap"
        )
        marker_row = (
            await self.session.execute(
                select(TenantSecret).where(
                    TenantSecret.tenant_id == ctx.tenant.id,
                    TenantSecret.purpose == marker_purpose,
                )
            )
        ).scalar_one_or_none()

        if marker_row is not None:
            # Already-minted case. The wazuh chart renders its credentials
            # Secret (``<release_wazuh>-wazuh-creds``) FROM helm values — it
            # is NOT pre-written by this controller — so every install or
            # upgrade of the wazuh release must carry the TRUE material in
            # values. Read it back from ``Secret/tenant-bootstrap`` (written
            # by a prior run's apply_secrets) so later steps see the SAME
            # credentials as the first run. 'provided' tenants have no
            # in-namespace Wazuh and therefore no bootstrap material.
            if ctx.profile == "provided":
                ctx.bag["bootstrap"] = None
            else:
                ctx.bag["bootstrap"] = await self._read_back_bootstrap(ctx)
            return

        # Common reference rows minted for every profile.
        mint_targets = [
            TenantSecret(
                tenant_id=ctx.tenant.id,
                purpose="llm",
                k8s_namespace=self.settings.soctalk_system_namespace,
                k8s_secret_name=ctx.llm_secret_name,
                k8s_secret_key="api_key",
                version_label="v1",
            ),
            TenantSecret(
                tenant_id=ctx.tenant.id,
                purpose="adapter-token",
                k8s_namespace=ctx.namespace,
                k8s_secret_name="adapter-token",
                k8s_secret_key="token",
                version_label="v1",
            ),
            TenantSecret(
                tenant_id=ctx.tenant.id,
                purpose="runs-worker-token",
                k8s_namespace=ctx.namespace,
                k8s_secret_name="runs-worker-token",
                k8s_secret_key="token",
                version_label="v1",
            ),
        ]

        if ctx.profile == "provided":
            # No in-namespace Wazuh ⇒ no bootstrap creds to generate. The only
            # Wazuh-related Secret SocTalk owns is the external-SIEM creds
            # Secret (written by ``_step_write_external_siem_secret``); track
            # it so ``tenant_secrets`` reflects every namespace Secret we own
            # and the chat resolver can locate the Wazuh API creds.
            ctx.bag["bootstrap"] = None
            mint_targets.append(
                TenantSecret(
                    tenant_id=ctx.tenant.id,
                    purpose="external-siem-creds",
                    k8s_namespace=ctx.namespace,
                    k8s_secret_name="tenant-external-siem-creds",
                    k8s_secret_key="multi",
                    version_label="v1",
                )
            )
        else:
            bootstrap = generate_bootstrap_secrets()
            ctx.bag["bootstrap"] = bootstrap
            mint_targets.extend([
                TenantSecret(
                    tenant_id=ctx.tenant.id,
                    purpose="bootstrap",
                    k8s_namespace=ctx.namespace,
                    k8s_secret_name="tenant-bootstrap",
                    k8s_secret_key="multi",
                    version_label="v1",
                ),
                # Pointer used by the chat agent's per-tenant Wazuh
                # resolver (soctalk.chat.wazuh_primitives). The wazuh
                # chart renders the credentials Secret as
                # ``<release_wazuh>-wazuh-creds`` (release name =
                # ``wazuh-<slug>``). The resolver reads four keys from
                # this Secret: WAZUH_API_USERNAME, WAZUH_API_PASSWORD,
                # INDEXER_USERNAME, INDEXER_PASSWORD. ``k8s_secret_key``
                # is informational here — the resolver reads all keys.
                TenantSecret(
                    tenant_id=ctx.tenant.id,
                    purpose="wazuh-api",
                    k8s_namespace=ctx.namespace,
                    k8s_secret_name=f"{ctx.release_wazuh}-wazuh-creds",
                    k8s_secret_key="multi",
                    version_label="v1",
                ),
            ])

        self.session.add_all(mint_targets)
        await self.session.flush()
        await self._emit_event(ctx, "secrets_minted")

    async def _read_back_bootstrap(self, ctx: _StepContext) -> TenantBootstrapSecrets:
        """Reconstruct the bootstrap material on the already-minted path.

        Reads ``Secret/tenant-bootstrap`` from the tenant namespace and
        rebuilds :class:`TenantBootstrapSecrets` from the 7 keys written by
        :func:`bootstrap_as_k8s_secret_data`, so a retried provision hands
        helm the SAME credentials the first run minted.

        If the Secret is missing (404) or incomplete, fresh material is
        regenerated instead — the later ``apply_secrets`` step rewrites the
        Secret from the bag, so namespace and helm values converge on the
        regenerated credentials. Either way a retry can NEVER proceed with
        placeholder credentials.
        """
        from kubernetes.client.exceptions import ApiException

        data: dict[str, Any] | None = None
        try:
            sec = await self.k8s.get_secret(ctx.namespace, "tenant-bootstrap")
            data = sec["data"]
        except ApiException as e:
            if e.status != 404:
                raise ProvisionError(
                    f"could not read back Secret/tenant-bootstrap from "
                    f"{ctx.namespace}: {e}",
                    step="mint_secrets",
                ) from e

        if data is not None:
            try:
                return TenantBootstrapSecrets(
                    wazuh_admin_pw=data["wazuh_admin_pw"],
                    wazuh_authd_secret=data["wazuh_authd_secret"],
                    thehive_admin_pw=data["thehive_admin_pw"],
                    thehive_api_token=data["thehive_api_token"],
                    cortex_admin_pw=data["cortex_admin_pw"],
                    cortex_api_key=data["cortex_api_key"],
                    cassandra_pw=data["cassandra_pw"],
                )
            except KeyError as e:
                logger.warning(
                    "tenant_bootstrap_secret_incomplete",
                    tenant=str(ctx.tenant.id),
                    namespace=ctx.namespace,
                    missing_key=str(e),
                )

        # Missing or incomplete: mint fresh material; apply_secrets will
        # rewrite Secret/tenant-bootstrap from the bag on this same run.
        logger.warning(
            "tenant_bootstrap_secret_regenerated",
            tenant=str(ctx.tenant.id),
            namespace=ctx.namespace,
        )
        return generate_bootstrap_secrets()

    async def _step_ensure_namespace(self, ctx: _StepContext) -> None:
        await self.k8s.ensure_namespace(
            ctx.namespace,
            labels={
                "tenant": "true",
                "managed-by": "soctalk",
                "soctalk.io/mssp-id": str(ctx.organization.mssp_id),
                "soctalk.io/install-id": str(ctx.organization.install_id),
                "soctalk.io/tenant-id": str(ctx.tenant.id),
                "soctalk.io/profile": ctx.profile,
                "kubernetes.io/metadata.name": ctx.namespace,
            },
        )
        await self._emit_event(ctx, "namespace_ready")

    async def _step_apply_secrets(self, ctx: _StepContext) -> None:
        """Write K8s Secrets into the tenant + system namespaces.

        Idempotent via ``put_secret`` (create-or-patch).
        ``ctx.bag['bootstrap']`` is None only for the 'provided' profile
        (no in-namespace Wazuh, nothing to write). For poc/persistent the
        bag always carries real material — freshly minted, read back from
        ``Secret/tenant-bootstrap`` on a retry, or regenerated if that
        Secret vanished — and re-writing identical data is a harmless
        no-op patch.
        """
        # LLM placeholder in system namespace.
        await self.k8s.put_secret(
            self.settings.soctalk_system_namespace,
            ctx.llm_secret_name,
            data={"api_key": ""},
            labels={
                "soctalk.io/tenant-id": str(ctx.tenant.id),
                "soctalk.io/secret-purpose": "llm",
                "managed-by": "soctalk",
            },
        )

        bootstrap = ctx.bag.get("bootstrap")
        if bootstrap is not None:
            await self.k8s.put_secret(
                ctx.namespace,
                "tenant-bootstrap",
                data=bootstrap_as_k8s_secret_data(bootstrap),
                labels={"soctalk.io/secret-purpose": "bootstrap"},
            )

        await self._write_adapter_token(ctx.namespace, ctx.tenant.id)
        await self._write_worker_token(ctx.namespace, ctx.tenant.id)
        await self._copy_llm_key_to_tenant_ns(ctx)
        await self._mint_tenant_admin_user(ctx)
        await self._emit_event(ctx, "secrets_applied")

    async def _step_write_external_siem_secret(self, ctx: _StepContext) -> None:
        """Write the ``tenant-external-siem-creds`` Secret ('provided' only).

        Carries BOTH credential pairs in ONE Secret, keyed with the same
        UPPERCASE names the in-cluster ``<release>-wazuh-creds`` Secret uses so
        the adapter and the chat resolver stay profile-agnostic:

          - ``INDEXER_USERNAME`` / ``INDEXER_PASSWORD``  → adapter alert ingest
          - ``WAZUH_API_USERNAME`` / ``WAZUH_API_PASSWORD`` → L1 chat resolver
          - ``WAZUH_API_TOKEN`` (optional)               → pre-minted API token

        Idempotent: ``put_secret`` is create-or-patch, so re-entry never errors
        on AlreadyExists. Runs AFTER ``_step_apply_secrets`` and BEFORE
        ``_step_helm_apply_tenant`` (the adapter pod the tenant chart starts
        mounts this Secret by reference).
        """
        ic = ctx.integration
        # Defensive: the onboard endpoint already blocks 'provided' onboarding
        # without these. Both HTTP-Basic pairs (indexer + API) are required.
        required = (
            ic.wazuh_indexer_url,
            ic.wazuh_indexer_username,
            ic.wazuh_indexer_password_plain,
            ic.wazuh_api_url,
            ic.wazuh_username,
            ic.wazuh_password_plain,
        )
        if not all(required):
            raise ProvisionError(
                "provided profile missing external SIEM credentials "
                "(indexer/api url + username + password all required)",
                step="write_external_siem_secret",
            )

        data = {
            "INDEXER_USERNAME": ic.wazuh_indexer_username,
            "INDEXER_PASSWORD": ic.wazuh_indexer_password_plain,
            "WAZUH_API_USERNAME": ic.wazuh_username,
            "WAZUH_API_PASSWORD": ic.wazuh_password_plain,
        }
        # Optional pre-minted API token: only ship the key when it's set so a
        # NULL column doesn't materialize an empty WAZUH_API_TOKEN env var.
        if ic.wazuh_api_token_plain:
            data["WAZUH_API_TOKEN"] = ic.wazuh_api_token_plain

        await self.k8s.put_secret(
            ctx.namespace,
            "tenant-external-siem-creds",
            data=data,
            labels={
                "soctalk.io/tenant-id": str(ctx.tenant.id),
                "soctalk.io/secret-purpose": "external-siem-creds",
                "managed-by": "soctalk",
            },
        )
        await self._emit_event(ctx, "external_siem_secret_applied")

    async def _step_wazuh_skipped_provided(self, ctx: _StepContext) -> None:
        """Timeline marker for 'provided' tenants where ``helm_apply_wazuh``
        would have run. The tenant brings its own external Wazuh, so the
        ``wazuh-<slug>`` release is never installed; emit a single
        ``wazuh_skipped_provided`` lifecycle event so the wizard still shows a
        step between ``helm_apply_tenant`` and ``wait_workloads``.
        """
        await self._emit_event(ctx, "wazuh_skipped_provided")

    async def _step_helm_apply_tenant(self, ctx: _StepContext) -> None:
        api_host = (
            f"{self.settings.api_service_name}."
            f"{self.settings.soctalk_system_namespace}.svc.cluster.local"
        )
        try:
            primary_llm_engine_for_render(
                ctx.integration.llm_provider,
                ctx.integration.llm_engine,
                ctx.integration.llm_base_url,
            )
        except ValueError as exc:
            raise ProvisionError(str(exc), step="helm_apply_tenant") from exc
        # Materialize DB-authored ACTIVE triage policies into the tenant chart values (#44).
        # Fail-closed: an invalid/oversized active row raises here → the reconcile step
        # fails → the job surfaces the failure (never a silent "active but not governing").
        from soctalk.triage_policy.authoring import render_active_authored_values

        authored_pb = await render_active_authored_values(
            self.session, tenant_id=ctx.tenant.id
        )
        values = render_tenant_values(
            tenant=ctx.tenant,
            integration=ctx.integration,
            branding=ctx.branding,
            authored_triage_policies=authored_pb,
            mssp_id=str(ctx.organization.mssp_id),
            install_id=str(ctx.organization.install_id),
            llm_secret_name=ctx.llm_secret_name,
            api_service_host=api_host,
            agent_hostname=f"{ctx.tenant.slug}.{self.settings.default_agent_dns_suffix}",
            cert_issuer=self.settings.default_cert_issuer,
            profile=ctx.profile,
            network_policies_enabled=self.settings.tenant_network_policies_enabled,
            # The controller path NEVER ships the plaintext key through
            # chart values: ``_copy_llm_key_to_tenant_ns`` (apply_secrets,
            # earlier in this same step list) already wrote
            # ``Secret/tenant-llm-key`` directly. Letting the chart render
            # the same Secret name fails install/upgrade with helm's
            # "invalid ownership metadata" — the controller-written Secret
            # carries no meta.helm.sh/release-* annotations and helm
            # refuses to adopt it. ``values.llm.apiKey`` stays for the
            # cross-cluster L2 install-spec (agents/api.py), where no
            # controller pre-writes Secrets on the remote cluster.
            include_llm_api_key=False,
            # Disable the tenant chart's bundled Wazuh subchart: this L1 path
            # installs Wazuh as a separate ``wazuh-<slug>`` release below
            # (``_step_helm_apply_wazuh``). Leaving the subchart on would stand
            # up a second, orphaned Wazuh stack (``tenant-<slug>-wazuh-*``) that
            # nothing points at — double the SIEM footprint. The bundled path
            # is the cross-cluster L2 install-spec only.
            bundled_siem=False,
        )
        if self.settings.tenant_values_overlay:
            values = _deep_merge(values, self.settings.tenant_values_overlay)
        try:
            # wait=False: the unified ``_step_wait_workloads`` polls readiness
            # for both releases' pods at the end. Letting helm block here
            # would double the wait budget and make per-release failure
            # diagnostics worse.
            await helm_install_tenant(
                release_name=ctx.release_tenant,
                namespace=ctx.namespace,
                chart_ref=self.settings.tenant_chart_ref,
                chart_version=self.settings.tenant_chart_version,
                values=values,
                wait=False,
                timeout=self.settings.wait_timeout,
            )
        except HelmError as e:
            raise ProvisionError(str(e), step="helm_apply_tenant") from e
        await self._emit_event(
            ctx, "helm_applied",
            details={"release": ctx.release_tenant, "chart": "soctalk-tenant"},
        )

    async def _step_helm_apply_wazuh(self, ctx: _StepContext) -> None:
        """Deploy the per-tenant Wazuh release.

        Layered values: base ``values.yaml`` + profile-specific file +
        per-tenant overrides (minted creds, tenant-id).
        """
        # Contract: the wazuh chart renders its credentials Secret
        # (``<release_wazuh>-wazuh-creds``) FROM these values, so they must
        # ALWAYS carry the true minted/read-back material. ``mint_secrets``
        # guarantees the bag holds it for poc/persistent (fresh mint,
        # read-back from Secret/tenant-bootstrap, or regeneration); shipping
        # any placeholder would become the live admin/authd password and
        # crash-loop the manager (Wazuh API error 5007).
        bootstrap = ctx.bag.get("bootstrap")
        if bootstrap is None:
            raise ProvisionError(
                "no bootstrap credentials available for wazuh helm values "
                "(mint_secrets must mint or read them back before "
                "helm_apply_wazuh)",
                step="helm_apply_wazuh",
            )
        admin_pw = bootstrap.wazuh_admin_pw
        authd_pw = bootstrap.wazuh_authd_secret

        storage_override = (
            self.settings.persistent_storage_class
            if ctx.profile == "persistent"
            else None
        )
        values = render_wazuh_values(
            tenant=ctx.tenant,
            profile=ctx.profile,
            admin_password=admin_pw,
            authd_password=authd_pw,
            storage_class_override=storage_override,
        )
        if self.settings.wazuh_values_overlay:
            values = _deep_merge(values, self.settings.wazuh_values_overlay)

        try:
            await helm_install_wazuh(
                release_name=ctx.release_wazuh,
                namespace=ctx.namespace,
                chart_path=self.settings.wazuh_chart_path,
                profile=ctx.profile,
                per_tenant_values=values,
                wait=False,
                timeout=self.settings.wait_timeout,
            )
        except HelmError as e:
            raise ProvisionError(str(e), step="helm_apply_wazuh") from e

        await self._emit_event(
            ctx, "helm_applied",
            details={"release": ctx.release_wazuh, "chart": "wazuh",
                     "profile": ctx.profile},
        )

    async def _step_wait_workloads(self, ctx: _StepContext) -> None:
        """Poll pods in the tenant namespace until all Ready, or time out.

        This is the step that turns "helm applied" into "data plane
        actually healthy." Heartbeat from the adapter is a separate
        milestone (``adapter_heartbeat_received``), not a gate.
        """
        import asyncio

        deadline = (
            asyncio.get_event_loop().time()
            + self.settings.readiness_timeout_seconds
        )
        while True:
            pods = await self.k8s.read_pods(ctx.namespace)
            if pods and all(p["ready"] for p in pods):
                await self._emit_event(
                    ctx, "workloads_ready",
                    details={"pod_count": len(pods)},
                )
                return
            if asyncio.get_event_loop().time() >= deadline:
                not_ready = [p["name"] for p in pods if not p["ready"]]
                raise ProvisionError(
                    f"workloads not ready after "
                    f"{self.settings.readiness_timeout_seconds:.0f}s; "
                    f"pending: {not_ready}",
                    step="wait_workloads",
                )
            await asyncio.sleep(self.settings.readiness_poll_interval_seconds)

    async def _step_write_integration_config(self, ctx: _StepContext) -> None:
        """Write the in-cluster Wazuh Manager + Indexer URLs onto integration_configs.

        Manager API (:55000) is what SocTalk calls; agent ingress is a
        separate concept handled by the tenant chart's ``agentIngress``.
        Indexer (:9200) is queried directly by the chat agent's Wazuh
        primitives for alert/vulnerability searches — it's a separate
        Service from the Manager (``<release>-wazuh-indexer`` vs
        ``<release>-wazuh-manager``). Service-name conventions match
        the chart's ``{{ include "wazuh.fullname" . }}``.

        No-op for the ``provided`` profile: that tenant's
        ``integration.wazuh_url`` / ``wazuh_indexer_url`` point at the
        customer's own external Wazuh, and overwriting them with in-cluster
        Service DNS on every reconcile would break the adapter and the chat
        resolver on the next pod restart. (Defensive: ``provided`` already
        omits this step from the provision step list.)
        """
        if ctx.profile == "provided":
            return

        target_url = (
            f"https://{ctx.release_wazuh}-wazuh-manager."
            f"{ctx.namespace}.svc.cluster.local:55000"
        )
        target_indexer = (
            f"https://{ctx.release_wazuh}-wazuh-indexer."
            f"{ctx.namespace}.svc.cluster.local:9200"
        )
        changed: dict[str, Any] = {}
        if ctx.integration.wazuh_url != target_url:
            ctx.integration.wazuh_url = target_url
            changed["wazuh_url"] = target_url
        if ctx.integration.wazuh_indexer_url != target_indexer:
            ctx.integration.wazuh_indexer_url = target_indexer
            changed["wazuh_indexer_url"] = target_indexer
        # The chart-installed Wazuh stack always uses self-signed certs
        # for its Manager + Indexer endpoints (charts/wazuh emits a
        # cluster-internal CA at install time). The DB column defaults
        # to ``true`` for safety, but for chart-deployed tenants the
        # truthful value is ``false`` — otherwise the chat agent's
        # ``_resolve_wazuh_for`` resolver builds an httpx client that
        # fails TLS verify on every call. Operators who terminate
        # Wazuh behind a public-CA-signed reverse proxy can flip it
        # back to ``true`` via the tenant settings API.
        if ctx.integration.wazuh_verify_ssl is not False:
            ctx.integration.wazuh_verify_ssl = False
            changed["wazuh_verify_ssl"] = False
        if changed:
            await self.session.flush()
            await self._emit_event(
                ctx, "integration_config_written", details=changed,
            )

    async def _step_finalize_active(self, ctx: _StepContext) -> None:
        if ctx.tenant.state == TenantState.ACTIVE.value:
            return
        await self._transition(
            ctx.tenant,
            TenantState.ACTIVE.value,
            actor_id=ctx.actor_id,
            event_type="active",
            details={
                "release_tenant": ctx.release_tenant,
                "release_wazuh": ctx.release_wazuh,
                "profile": ctx.profile,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _load_tenant(
        self, tenant_id: UUID, *, for_update: bool = False
    ) -> Tenant:
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        tenant = result.scalar_one_or_none()
        if tenant is None:
            raise ProvisionError(f"tenant {tenant_id} not found")
        return tenant

    async def _load_organization(self, org_id: UUID) -> Organization:
        result = await self.session.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise ProvisionError(f"organization {org_id} not found")
        return org

    async def _load_integration(self, tenant_id: UUID) -> IntegrationConfig:
        result = await self.session.execute(
            select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = IntegrationConfig(tenant_id=tenant_id)
            self.session.add(cfg)
            await self.session.flush()
        return cfg

    async def _load_branding(self, tenant_id: UUID) -> BrandingConfig:
        result = await self.session.execute(
            select(BrandingConfig).where(BrandingConfig.tenant_id == tenant_id)
        )
        b = result.scalar_one_or_none()
        if b is None:
            b = BrandingConfig(tenant_id=tenant_id)
            self.session.add(b)
            await self.session.flush()
        return b

    def _assert_transition(self, tenant: Tenant, new_state: str) -> None:
        allowed = VALID_TRANSITIONS.get(tenant.state, frozenset())
        if new_state not in allowed:
            raise TenantLifecycleError(
                f"tenant {tenant.id}: cannot transition {tenant.state} -> {new_state}; "
                f"allowed: {sorted(allowed)}"
            )

    async def _transition(
        self,
        tenant: Tenant,
        to_state: str,
        *,
        event_type: str,
        actor_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        from_state = tenant.state
        tenant.state = to_state
        tenant.state_changed_at = datetime.utcnow()
        self.session.add(
            TenantLifecycleEvent(
                tenant_id=tenant.id,
                event_type=event_type,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                details=details or {},
            )
        )
        await self.session.flush()
        logger.info(
            "tenant_transition",
            tenant=str(tenant.id),
            from_state=from_state,
            to_state=to_state,
            event_kind=event_type,
        )

    async def _emit_event(
        self,
        ctx: _StepContext,
        event_type: str,
        *,
        details: dict | None = None,
    ) -> None:
        self.session.add(
            TenantLifecycleEvent(
                tenant_id=ctx.tenant.id,
                event_type=event_type,
                from_state=ctx.tenant.state,
                to_state=ctx.tenant.state,
                actor_id=ctx.actor_id,
                details=details or {},
            )
        )
        await self.session.flush()

    async def _write_adapter_token(self, tenant_ns: str, tenant_id: UUID) -> None:
        """Write a tenant-bound adapter token into the tenant namespace."""
        from soctalk.core.tenancy.auth import mint_adapter_token

        await self.k8s.put_secret(
            tenant_ns,
            "adapter-token",
            data={"token": mint_adapter_token(tenant_id)},
            labels={"soctalk.io/secret-purpose": "adapter-token", "managed-by": "soctalk"},
        )

    async def _copy_llm_key_to_tenant_ns(self, ctx: _StepContext) -> None:
        """Mirror the install's shared LLM API key into the tenant ns.

        Background: ``secretKeyRef`` on a container env var only
        resolves Secrets in the same namespace as the Pod. The
        ``llm.apiKeyRef`` block render.py emits used to point at
        ``soctalk-system/<tenant-id>-llm`` — that Secret exists but is
        unreachable from the L2 runs-worker / adapter pods. Copy the
        bytes into the tenant namespace under the canonical name so
        the chart's ``llm.apiKeyRef.name`` resolves locally.

        For deployments where each tenant has its own LLM provider key,
        replace this with a tenant-config-driven write — e.g., read
        from ``integration_configs.llm_api_key`` instead of the install
        shared key. V1 default is shared.

        Key resolution precedence:

        1. Per-tenant ``IntegrationConfig.llm_api_key_plain`` (set via
           ``PATCH /api/mssp/tenants/{id}/llm``) — used verbatim, the
           install-wide Secret is NOT read.
        2. The install-wide LLM Secret
           (``SOCTALK_INSTALL_LLM_SECRET_NAME``, default
           ``soctalk-system-llm-api-key``; keys ``openai-api-key`` /
           ``anthropic-api-key``).

        When neither source yields a non-empty key this raises
        ``ProvisionError(step='apply_secrets')`` and emits a
        ``llm_key_missing`` lifecycle event, rather than silently skipping
        the Secret and stranding the runs-worker in
        CreateContainerConfigError. Because this step precedes
        ``helm_apply_tenant``, the runs-worker Deployment is never created
        without the Secret it mounts.
        """
        # Per-tenant override wins: when an MSSP set
        # ``IntegrationConfig.llm_api_key_plain`` (via
        # PATCH /api/mssp/tenants/{id}/llm), the runs-worker should
        # use that tenant's key, not the install-wide shared one.
        per_tenant = getattr(ctx.integration, "llm_api_key_plain", None)
        if per_tenant:
            api_key = per_tenant
            logger.info(
                "llm_key_from_per_tenant_config",
                tenant=str(ctx.tenant.id),
            )
        else:
            src_ns = self.settings.soctalk_system_namespace
            src_name = os.getenv(
                "SOCTALK_INSTALL_LLM_SECRET_NAME",
                "soctalk-system-llm-api-key",
            )
            # Chart's 60-secrets.yaml writes the install's shared LLM key
            # under either ``anthropic-api-key`` or ``openai-api-key``
            # depending on ``llm.provider``. Try both — the explicit env
            # override still wins so operators can pin a specific key.
            #
            # Order the fallback by the TENANT's configured provider.
            # install.sh's ``create_llm_secret`` populates BOTH sub-keys with
            # the SAME value, so a fixed openai-first order picks
            # ``openai-api-key`` for an anthropic tenant, then the block below
            # flips it to ``openai-compatible`` and the runs-worker mounts the
            # (Anthropic) key as ``OPENAI_API_KEY`` — every call then hits
            # OpenAI's API with an ``sk-ant-`` key and 401s. Preferring the key
            # that matches ``ctx.integration.llm_provider`` keeps a correctly
            # onboarded anthropic tenant on anthropic; the reconcile below only
            # fires when the install Secret genuinely lacks the tenant's key.
            explicit_key = os.getenv("SOCTALK_INSTALL_LLM_SECRET_KEY")
            if explicit_key:
                candidate_keys = [explicit_key]
            elif ctx.integration.llm_provider == "anthropic":
                candidate_keys = ["anthropic-api-key", "openai-api-key"]
            else:
                candidate_keys = ["openai-api-key", "anthropic-api-key"]

            api_key = None
            chosen_key_name = None
            try:
                src = await self.k8s.get_secret(src_ns, src_name)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "llm_key_source_unreadable",
                    ns=src_ns,
                    name=src_name,
                    err=str(e),
                )
            else:
                data = src.get("data") or {}
                chosen_key_name = next(
                    (k for k in candidate_keys if data.get(k)), None
                )
                api_key = data.get(chosen_key_name) if chosen_key_name else None

            if not api_key:
                # Fail fast instead of silently skipping. Leaving
                # Secret/tenant-llm-key uncreated stranded the L2 runs-worker
                # pod in CreateContainerConfigError
                # (``secret "tenant-llm-key" not found``). This runs inside
                # ``apply_secrets`` — BEFORE ``helm_apply_tenant`` — so raising
                # here guarantees the runs-worker Deployment is never created
                # without the Secret it mounts. Emit a typed lifecycle event
                # first so the wizard/timeline names the exact cause, then
                # raise so provision() drives the tenant to ``degraded``.
                logger.warning(
                    "llm_key_source_empty",
                    ns=src_ns,
                    name=src_name,
                    tried=candidate_keys,
                )
                await self._emit_event(
                    ctx,
                    "llm_key_missing",
                    details={
                        "install_secret_namespace": src_ns,
                        "install_secret_name": src_name,
                        "tried_keys": candidate_keys,
                    },
                )
                raise ProvisionError(
                    "no LLM API key available for tenant "
                    f"'{ctx.tenant.slug}': cannot write Secret/tenant-llm-key. "
                    "IntegrationConfig.llm_api_key_plain is unset and the "
                    f"install-wide LLM Secret '{src_ns}/{src_name}' has no "
                    f"non-empty key among {candidate_keys}. Supply an "
                    "install-wide key (llm.apiKey) or PATCH "
                    "/api/mssp/tenants/{id}/llm before retrying.",
                    step="apply_secrets",
                )
            # Mirror the provider hint that matches which install key
            # we actually picked. Without this, an Anthropic-only
            # install Secret would still ship its key into a tenant
            # whose ``llm.provider`` defaults to openai-compatible,
            # and the runs-worker would mount the Anthropic key as
            # ``OPENAI_API_KEY`` and call OpenAI's API.
            #
            # The model has to flip alongside: a tenant onboarded with
            # ``llm.model=gpt-4o`` and then auto-switched to anthropic
            # would render ``SOCTALK_FAST_MODEL=gpt-4o`` on the
            # runs-worker, which the Anthropic SDK rejects on every
            # call. The shared ``reconcile_provider_model`` helper
            # (also used by the onboard API) only overwrites the model
            # when the existing one clearly belongs to the *other*
            # provider — preserves operator-set custom models that
            # already match.
            # The per-tenant fast/reasoning overrides must flip alongside
            # llm_model: an anthropic auto-switch that left
            # ``llm_fast_model=gpt-4o-mini`` in place would still render
            # ``SOCTALK_FAST_MODEL=gpt-4o-mini`` on the runs-worker (the
            # renderer prefers the override). NULL overrides stay NULL —
            # the flip never materializes a concrete model into an unset
            # override; render-time fallback to llm_model keeps working.
            from soctalk.core.llm_provider import reconcile_provider_model

            def _flip_models(provider: str) -> None:
                ctx.integration.llm_model = reconcile_provider_model(
                    provider, ctx.integration.llm_model
                )
                if ctx.integration.llm_fast_model:
                    ctx.integration.llm_fast_model = reconcile_provider_model(
                        provider, ctx.integration.llm_fast_model
                    )
                if ctx.integration.llm_reasoning_model:
                    ctx.integration.llm_reasoning_model = (
                        reconcile_provider_model(
                            provider, ctx.integration.llm_reasoning_model
                        )
                    )

            if chosen_key_name == "anthropic-api-key":
                if ctx.integration.llm_provider != "anthropic":
                    ctx.integration.llm_provider = "anthropic"
                    _flip_models("anthropic")
                    await self.session.flush()
            elif chosen_key_name == "openai-api-key":
                if ctx.integration.llm_provider not in ("openai", "openai-compatible"):
                    # Tenant chart's values.schema.json only accepts
                    # ``openai-compatible`` or ``anthropic`` for
                    # ``llm.provider`` (the runs-worker template maps
                    # ``openai-compatible`` → ``openai`` env-side).
                    # Storing the bare ``openai`` here would fail
                    # Helm validation on the next retry and leave
                    # the tenant degraded.
                    ctx.integration.llm_provider = "openai-compatible"
                    _flip_models("openai-compatible")
                    await self.session.flush()
        await self.k8s.put_secret(
            ctx.namespace,
            "tenant-llm-key",
            data=_llm_secret_data(api_key, getattr(ctx.integration, "llm_tiers", None)),
            labels={
                "soctalk.io/tenant-id": str(ctx.tenant.id),
                "soctalk.io/secret-purpose": "llm",
                "managed-by": "soctalk",
            },
        )

    async def _mint_tenant_admin_user(self, ctx: _StepContext) -> None:
        """Provision a default tenant_admin login.

        Idempotent: skips if a user with this email already exists.
        Writes the temporary password into ``tenant-bootstrap-admin``
        Secret in the tenant ns so an operator can hand it off; the
        user is required to change it on first login
        (``must_change=true``).

        Email derivation: ``admin@<slug>.local`` unless
        ``integration.contact_email`` is set on the tenant — that
        overrides so tenants land with the email they expect.
        """
        import secrets as _secrets

        from sqlalchemy import select as _select

        from soctalk.core.auth.models import PasswordCredential
        from soctalk.core.auth.passwords import hash_password
        from soctalk.core.tenancy.models import User, UserType

        # Contact email lives on ``Tenant.config`` (set by the create
        # API at /api/mssp/tenants:142), not on IntegrationConfig.
        config = ctx.tenant.config or {}
        contact = config.get("contact_email") if isinstance(config, dict) else None
        email = (contact or f"admin@{ctx.tenant.slug}.local").lower()

        existing = (
            await self.session.execute(
                _select(User).where(User.email == email)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.tenant_id != ctx.tenant.id:
            # The email belongs to a user pinned to a *different* tenant
            # (operators reuse the same contact_email across tenants).
            # Don't rotate that user's password into this tenant's
            # bootstrap Secret — fall back to a tenant-unique synthetic
            # email so each tenant gets its own admin.
            logger.warning(
                "tenant_admin_email_collision_use_synthetic",
                email=email,
                colliding_tenant=str(existing.tenant_id),
                this_tenant=str(ctx.tenant.id),
            )
            email = f"admin@{ctx.tenant.slug}.local".lower()
            existing = (
                await self.session.execute(
                    _select(User).where(User.email == email)
                )
            ).scalar_one_or_none()
            # The synthetic address itself may already exist on a
            # different tenant (very unlikely — slug is unique — but
            # handle it). Last resort: append the tenant id so the
            # final address is guaranteed unique.
            if existing is not None and existing.tenant_id != ctx.tenant.id:
                logger.warning(
                    "tenant_admin_synthetic_email_collision_use_id",
                    email=email,
                    colliding_tenant=str(existing.tenant_id),
                    this_tenant=str(ctx.tenant.id),
                )
                email = f"admin+{ctx.tenant.id}@{ctx.tenant.slug}.local".lower()
                existing = (
                    await self.session.execute(
                        _select(User).where(User.email == email)
                    )
                ).scalar_one_or_none()
        if existing is not None:
            # A previous run got past user creation but may have failed
            # before writing ``tenant-bootstrap-admin``. If the Secret
            # is missing, rotate the password and re-write the Secret
            # so operators can still recover the temp credential —
            # without rotation the old hash would be unrecoverable.
            from kubernetes.client.exceptions import ApiException as _K8sApiException

            try:
                await self.k8s.get_secret(ctx.namespace, "tenant-bootstrap-admin")
                logger.info(
                    "tenant_admin_user_exists",
                    email=email,
                    tenant=str(ctx.tenant.id),
                )
                return
            except _K8sApiException as e:
                if getattr(e, "status", None) != 404:
                    # RBAC denial / API server hiccup — don't rotate the
                    # password on a transient read failure. Surface it
                    # so the caller retries cleanly.
                    raise
                logger.info(
                    "tenant_admin_secret_missing_rotate",
                    email=email,
                    tenant=str(ctx.tenant.id),
                )
            cred = (
                await self.session.execute(
                    _select(PasswordCredential).where(
                        PasswordCredential.user_id == existing.id
                    )
                )
            ).scalar_one_or_none()
            # Only rotate when the bootstrap credential is still in its
            # initial ``must_change=True`` state (the operator hasn't
            # been picked up yet, OR the admin hasn't completed first
            # login). If ``must_change=false`` the admin already
            # changed the password — overwriting it would lock them
            # out and reintroduce a plaintext temp Secret.
            if cred is not None and not cred.must_change:
                logger.info(
                    "tenant_admin_secret_missing_no_rotate",
                    email=email,
                    tenant=str(ctx.tenant.id),
                    reason="must_change_already_cleared",
                )
                return
            new_password = _secrets.token_urlsafe(18)
            new_hash = hash_password(new_password)
            if cred is None:
                self.session.add(
                    PasswordCredential(
                        user_id=existing.id,
                        password_hash=new_hash,
                        must_change=True,
                    )
                )
            else:
                cred.password_hash = new_hash
                cred.must_change = True
                cred.consecutive_failures = 0
                cred.locked_until = None
            await self.session.flush()
            await self.k8s.put_secret(
                ctx.namespace,
                "tenant-bootstrap-admin",
                data={"email": email, "password": new_password},
                labels={
                    "soctalk.io/secret-purpose": "tenant-bootstrap-admin",
                    "managed-by": "soctalk",
                },
            )
            return

        temp_password = _secrets.token_urlsafe(18)
        # users.display_name is Text but the API caps tenant
        # display_name at 255 chars; appending " Admin" can push the
        # generated user display past that. Bound it conservatively
        # so column-length surprises don't degrade an otherwise valid
        # tenant onboard.
        DISPLAY_LIMIT = 255
        suffix = " Admin"
        base_name = ctx.tenant.display_name or ctx.tenant.slug
        if len(base_name) + len(suffix) > DISPLAY_LIMIT:
            base_name = base_name[: DISPLAY_LIMIT - len(suffix)]
        display_name = f"{base_name}{suffix}"

        user = User(
            email=email,
            display_name=display_name,
            user_type=UserType.TENANT.value,
            role="tenant_admin",
            tenant_id=ctx.tenant.id,
            active=True,
        )
        self.session.add(user)
        await self.session.flush()
        self.session.add(
            PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(temp_password),
                must_change=True,
            )
        )
        await self.session.flush()

        # Operator hand-off: temp password lives in the tenant ns so
        # the customer's IT can pick it up out-of-band.
        await self.k8s.put_secret(
            ctx.namespace,
            "tenant-bootstrap-admin",
            data={
                "email": email,
                "password": temp_password,
            },
            labels={
                "soctalk.io/tenant-id": str(ctx.tenant.id),
                "soctalk.io/secret-purpose": "tenant-bootstrap-admin",
                "managed-by": "soctalk",
            },
        )
        logger.info(
            "tenant_admin_user_minted",
            email=email,
            tenant=str(ctx.tenant.id),
        )

    async def _write_worker_token(self, tenant_ns: str, tenant_id: UUID) -> None:
        """Write a tenant-bound runs-worker token into the tenant namespace."""
        from soctalk.core.tenancy.auth import mint_worker_token

        await self.k8s.put_secret(
            tenant_ns,
            "runs-worker-token",
            data={"token": mint_worker_token(tenant_id)},
            labels={
                "soctalk.io/secret-purpose": "runs-worker-token",
                "managed-by": "soctalk",
            },
        )


__all__ = [
    "ControllerSettings",
    "ProvisionError",
    "TenantController",
    "TenantLifecycleError",
    "VALID_TRANSITIONS",
]
