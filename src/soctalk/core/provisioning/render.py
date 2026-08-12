"""Render a Tenant row into ``soctalk-tenant`` and ``wazuh`` chart values.

Pure functions: given a Tenant + associated config + deployment profile,
produce dicts matching the respective chart's values schema. Output is
written to a temp file and passed to ``helm install -f`` by the caller.

Profiles (see ``docs/multi-tenant/wazuh-profiles.md``):

- ``poc`` — ephemeral / cheapest viable. Single-node, node-local storage,
  no ingress, tight resource quotas. Intended for demo tenants.
- ``persistent`` — single-node but durable. PVC-backed indexer and
  manager. No HA (deferred).
- ``legacy`` — tenants provisioned before the profile concept existed.
  The controller refuses to re-render for ``legacy`` tenants on the MVP
  path: their topology is whatever was applied at install time, and we
  do not assume.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from soctalk.core.llm_provider import (
    effective_llm_engine,
    hosted_provider_kind_for_base_url,
)
from soctalk.core.tenancy.models import (
    SERVED_ENGINES,
    BrandingConfig,
    IntegrationConfig,
    Tenant,
    check_primary_llm_engine_config,
    normalize_llm_engine,
    validate_llm_tiers,
)

Profile = Literal["poc", "persistent", "provided", "legacy"]

_ENGINE_NATIVE_DECODING_MODES = frozenset({"guided_json", "guided_grammar"})


def _profile_tenant_overrides(profile: Profile) -> dict[str, Any]:
    """Layer profile-specific defaults over the shared base values.

    Only the fields that actually differ between profiles. Values merged
    shallowly by key at the top level, so section dicts overwrite —
    re-specify the full section when overriding.
    """

    if profile == "poc":
        return {
            "resourceQuota": {
                "enabled": True,
                # Sized for adapter + wazuh-manager + wazuh-indexer +
                # wazuh-dashboard + linux-ep simulator at PoC limits, with
                # headroom for one restart and the indexer's init
                # containers. Empirical live-run: wazuh-indexer alone burns
                # 1500m/2560Mi limits, dashboard + adapter + runs-worker
                # together tack on ~2200m more — the previous cpu limit
                # of 4 was hit before wazuh-manager could schedule.
                "requests": {"cpu": "3", "memory": "6Gi"},
                "limits": {"cpu": "8", "memory": "12Gi"},
                "persistentVolumeClaims": "5",
                "pods": "20",
            },
            "limitRange": {
                "enabled": True,
                "defaults": {"memory": "512Mi", "cpu": "250m"},
                "defaultRequests": {"memory": "128Mi", "cpu": "50m"},
                # Wazuh indexer's per-container limits are 1500m cpu /
                # 2560Mi memory at PoC settings — the previous cap of 1cpu
                # / 2Gi rejected the indexer pod with "maximum X per
                # container is Y, but limit is Z". Room for one restart
                # spike on top of that.
                "max": {"memory": "3Gi", "cpu": "2"},
            },
        }

    if profile == "persistent":
        return {
            "resourceQuota": {
                "enabled": True,
                "requests": {"cpu": "2", "memory": "6Gi"},
                "limits": {"cpu": "5", "memory": "12Gi"},
                "persistentVolumeClaims": "6",
                "pods": "30",
            },
        }

    if profile == "provided":
        return {
            "resourceQuota": {
                "enabled": True,
                # No in-namespace Wazuh/TheHive/Cortex — only the adapter and
                # runs-worker run here, so the quota is a fraction of poc's.
                # Two PVCs cover the adapter's checkpoint volume plus one
                # spare; ten pods cover the two Deployments with restart and
                # rollout headroom.
                "requests": {"cpu": "1", "memory": "2Gi"},
                "limits": {"cpu": "2", "memory": "4Gi"},
                "persistentVolumeClaims": "2",
                "pods": "10",
            },
        }

    # legacy: no overrides. Caller should not normally re-render legacy
    # tenants; if they do, hand back the base values unchanged.
    return {}


def _default_tenant_image_tag() -> str:
    """Image tag for tenant workloads when the chart did not supply one.

    This used to default to ``latest``. A tenant provisioned that way runs a
    moving tag: whichever build ``latest`` happened to point at, changing under
    the operator without the release changing — the failure mode the pinning
    rule exists to prevent, and the reason the code below has to force
    ``pullPolicy: Always`` to compensate.

    The chart normally sets ``SOCTALK_TENANT_*_IMAGE_TAG`` from
    ``tenantProvisioning.*ImageTag`` (default: the chart's own version), but it
    only emits the env when the value is truthy, so an operator who clears it
    lands here. Fall back to the version of the running control plane instead:
    it *is* the release, so tenants pin to the same build as the API that
    provisioned them, and unlike a literal it cannot go stale on the next cut.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("soctalk")
    except PackageNotFoundError as exc:  # pragma: no cover - broken install
        raise RuntimeError(
            "cannot determine the SocTalk version to pin tenant images to, and "
            "refusing to fall back to a moving 'latest' tag; set "
            "tenantProvisioning.adapterImageTag / runsWorkerImageTag / "
            "linuxEpImageTag explicitly"
        ) from exc


def _external_siem_hosts(integration: IntegrationConfig) -> list[str]:
    """Deduped hostnames the adapter must reach for an external Wazuh.

    Parses the host portion of the externally-provided indexer (:9200) and
    Wazuh API (:55000) URLs. Order-preserving + deduped so a tenant whose
    indexer and API share a hostname yields a single egress entry. Empty/None
    URLs are skipped. Feeds ``networkPolicies.externalSiemHosts`` (the Cilium
    FQDN egress allow-list) for the ``provided`` profile.
    """
    from urllib.parse import urlparse

    hosts: list[str] = []
    for url in (integration.wazuh_indexer_url, integration.wazuh_api_url):
        if not url:
            continue
        host = urlparse(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _external_siem_endpoints(integration: IntegrationConfig) -> list[dict[str, object]]:
    """``{host, port}`` pairs the adapter must reach for an external Wazuh.

    Same sources as :func:`_external_siem_hosts`, but keeps the **port** as
    well. The standard ``NetworkPolicy`` (the only one that applies on a
    non-Cilium cluster) opens exactly these ports: hardcoding 9200/55000 there
    silently drops a BYO Wazuh published behind a NodePort, load balancer, or
    reverse proxy on any other port, while the tenant still reports healthy.
    Mirrors the LLM egress port derivation above, which exists for the same
    reason (self-hosted endpoints on non-standard ports).

    Falls back to the scheme default when the URL omits an explicit port, so
    the common ``https://wazuh.example.com`` case keeps working. Deduped on
    ``(host, port)`` so a shared host+port yields one rule.
    """
    from urllib.parse import urlparse

    endpoints: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for url, default_port in (
        (integration.wazuh_indexer_url, 9200),
        (integration.wazuh_api_url, 55000),
    ):
        if not url:
            continue
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            continue
        try:
            port = parsed.port or default_port
        except ValueError as exc:
            # Default only when the port is OMITTED. An explicitly supplied but
            # unparseable port is operator error: silently substituting 9200 or
            # 55000 would render an egress rule for a port the adapter never
            # dials, reproducing the very "connects to nothing, looks healthy"
            # failure this function exists to prevent.
            raise ValueError(
                f"external SIEM URL {url!r} has an invalid port: {exc}"
            ) from exc
        key = (host, int(port))
        if key in seen:
            continue
        seen.add(key)
        endpoints.append({"host": host, "port": int(port)})
    return endpoints


# Filenames must satisfy the chart schema's propertyNames pattern (and the k8s
# ConfigMap key ceiling) or the whole helm install would fail on one odd file.
_TRIAGE_POLICY_FILENAME_RE = re.compile(r"[A-Za-z0-9._-]{1,247}\.ya?ml")

# Total payload budget across all files for one tenant — a ConfigMap tops out
# around 1MiB including metadata, so the sum must stay well under it.
_TRIAGE_POLICIES_TOTAL_BUDGET = 800 * 1024


def primary_llm_engine_for_render(
    provider: str | None, engine: str | None, base_url: str | None
) -> str | None:
    """Engine value to render for the primary LLM block.

    Stale rows can carry a served-engine claim together with a hosted billing
    authority because older API boundaries allowed that pair. Pricing already
    lets the hosted authority win; render does the same by omitting the inert
    engine so reconciliation can proceed and the worker boots with the backend
    it actually calls. New writes still use ``check_primary_llm_engine_config``
    and remain rejected at the API boundary.
    """
    normalized = normalize_llm_engine(engine)
    effective = effective_llm_engine(provider, normalized, base_url)
    if normalized in SERVED_ENGINES and effective is None and (provider or "").strip().lower() in {
        "openai",
        "openai-compatible",
    }:
        hosted_kind = hosted_provider_kind_for_base_url(base_url)
        if hosted_kind is not None:
            import structlog

            structlog.get_logger(__name__).warning(
                "tenant_llm_engine_ignored_for_hosted_authority",
                provider=provider,
                engine=normalized,
                hosted_provider_kind=hosted_kind,
            )
            return None

    check_primary_llm_engine_config(provider, effective, base_url)
    return effective


def render_triage_policy_values(tenant_slug: str, tenant_id: str = "") -> dict[str, str]:
    """Triage-policy files to inject into a tenant's ``runsWorker.triagePolicies`` (#44).

    Source: ``SOCTALK_TENANT_PLAYBOOKS_DIR`` on the controller/API process — an
    operator-mounted directory of declarative triage policy YAMLs (install-level
    config, e.g. a ConfigMap on the api pod). Each file is read ONCE and that
    exact text is both validated (the worker registry's own fail-closed parser:
    schema, conditions, priority floor, no deterministic dispositions) and
    shipped — no second read, no validate/ship drift. Skipped with an error log,
    never failing the render: invalid content, filenames the chart schema would
    reject, files past the per-tenant total budget (a ConfigMap tops out ~1MiB).
    A concrete ``tenant:`` may name the slug or the UUID; both are matched here
    and re-checked by the worker at load. Returns {} when the env is unset.
    """
    import structlog

    from soctalk.triage_policy.registry import parse_triage_policy_text

    log = structlog.get_logger()
    directory = os.getenv("SOCTALK_TENANT_TRIAGE_POLICIES_DIR") or os.getenv(
        "SOCTALK_TENANT_PLAYBOOKS_DIR", ""
    )
    if not directory:
        return {}
    from pathlib import Path

    root = Path(directory)
    if not root.is_dir():
        log.warning("tenant_triage_policies_dir_missing", dir=directory)
        return {}
    identifiers = {v for v in (tenant_slug, tenant_id) if v}
    out: dict[str, str] = {}
    total = 0
    for path in sorted(root.glob("*.y*ml")):
        if not _TRIAGE_POLICY_FILENAME_RE.fullmatch(path.name):
            log.error(
                "tenant_triage_policy_rejected", file=str(path),
                error="filename not accepted by the chart schema",
            )
            continue
        try:
            text = path.read_text()
            pb = parse_triage_policy_text(text)
        except Exception as exc:  # noqa: BLE001 — invalid files never reach the chart
            log.error(
                "tenant_triage_policy_rejected", file=str(path), error=str(exc)[:300]
            )
            continue
        if pb.tenant != "*" and pb.tenant not in identifiers:
            continue
        size = len(text.encode())
        if total + size > _TRIAGE_POLICIES_TOTAL_BUDGET:
            log.error(
                "tenant_triage_policy_rejected", file=str(path),
                error=(
                    "per-tenant triage policy budget "
                    f"({_TRIAGE_POLICIES_TOTAL_BUDGET}B) exceeded"
                ),
            )
            continue
        total += size
        out[path.name] = text
    return out


def _canonical_llm_provider(provider: str) -> str:
    """Map the install-side enum to the runtime provider the worker knows.

    ``openai-compatible`` (self-hosted vLLM/SGLang/gateway) speaks the OpenAI
    protocol, so the worker reads ``OPENAI_API_KEY`` / ``SOCTALK_<TIER>_PROVIDER=
    openai``. Same mapping the single-block env uses in 35-runs-worker.yaml.
    """
    return "openai" if provider == "openai-compatible" else provider


def _render_llm_tiers(
    integration: IntegrationConfig, *, include_llm_api_key: bool, primary_port: int
) -> tuple[dict[str, Any], dict[str, str], list[int]]:
    """Render per-tier LLM backends (issue #12) into chart values.

    Returns ``(tiers, tier_keys, extra_ports)``:
      * ``tiers``       — ``{tier: {provider, baseUrl, model, engine?}}`` for the
        worker env (provider canonicalized). Empty when the tenant is
        single-provider (``llm_tiers`` NULL) — the caller then adds nothing, so
        the rendered values are byte-identical to today.
      * ``tier_keys``   — ``{tier: plaintext}`` for tiers carrying their OWN
        credential; materialized as extra data keys on ``tenant-llm-key``.
        Plaintext only on the L2 chart-owned path (``include_llm_api_key``);
        "" on the L1 controller path (the controller mirrors the real key).
      * ``extra_ports`` — distinct LLM egress ports beyond ``primary_port``,
        added to the worker NetworkPolicy (port-union, additive).
    """
    raw = integration.llm_tiers
    if not raw:
        return {}, {}, []
    renderable_raw: dict[str, Any] = {}
    for tier_name, block in raw.items():
        block = dict(block or {})
        normalized = normalize_llm_engine(block.get("engine"))
        effective = effective_llm_engine(
            block.get("provider"), normalized, block.get("base_url")
        )
        if normalized in SERVED_ENGINES and effective is None:
            hosted_kind = hosted_provider_kind_for_base_url(block.get("base_url"))
            if hosted_kind is not None:
                import structlog

                structlog.get_logger(__name__).warning(
                    "tenant_llm_tier_engine_ignored_for_hosted_authority",
                    tier=tier_name,
                    provider=block.get("provider"),
                    engine=normalized,
                    hosted_provider_kind=hosted_kind,
                )
                block.pop("engine", None)
                if block.get("decoding_mode") in _ENGINE_NATIVE_DECODING_MODES:
                    block.pop("decoding_mode", None)
        elif effective is None:
            block.pop("engine", None)
        else:
            block["engine"] = effective
        renderable_raw[tier_name] = block
    raw = validate_llm_tiers(renderable_raw) or {}
    from urllib.parse import urlparse

    tiers: dict[str, Any] = {}
    tier_keys: dict[str, str] = {}
    ports: set[int] = set()
    for tier_name, block in raw.items():
        entry: dict[str, Any] = {
            "provider": _canonical_llm_provider(block["provider"]),
            "baseUrl": block["base_url"],
            "model": block["model"],
        }
        if block.get("engine"):
            entry["engine"] = block["engine"]
        if block.get("decoding_mode"):
            entry["decodingMode"] = block["decoding_mode"]
        # Per-tier sampling override (omitted → tier uses its caller default).
        # ``temperature`` may be 0.0, so test for presence, not truthiness.
        if block.get("temperature") is not None:
            entry["temperature"] = block["temperature"]
        if block.get("max_tokens") is not None:
            entry["maxTokens"] = block["max_tokens"]
        tiers[tier_name] = entry

        u = urlparse(block["base_url"])
        ports.add(u.port or (80 if u.scheme == "http" else 443))

        if block.get("api_key_plain"):
            tier_keys[tier_name] = block["api_key_plain"] if include_llm_api_key else ""

    extra_ports = sorted(ports - {primary_port})
    return tiers, tier_keys, extra_ports


def render_tenant_values(
    tenant: Tenant,
    integration: IntegrationConfig,
    branding: BrandingConfig,
    *,
    mssp_id: str,
    install_id: str,
    llm_secret_name: str,
    adapter_token_secret: str = "adapter-token",
    api_service_host: str = "soctalk-system-api.soctalk-system.svc.cluster.local",
    api_service_port: int = 8000,
    allowed_llm_hosts: list[str] | None = None,
    agent_hostname: str | None = None,
    cert_issuer: str | None = None,
    profile: Profile = "poc",
    network_policies_enabled: bool = True,
    include_llm_api_key: bool = True,
    bundled_siem: bool = True,
    authored_triage_policies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Produce a values dict for the tenant chart.

    Args:
        tenant: the ``Tenant`` DB row.
        integration: per-tenant integration config (LLM, Wazuh/TheHive/Cortex URLs).
        branding: per-tenant branding config.
        mssp_id: install's MSSP UUID (from ``Organization``).
        install_id: install's UUID.
        llm_secret_name: name of the K8s Secret in ``soctalk-system`` holding
            this tenant's LLM API key.
        allowed_llm_hosts: FQDNs permitted in Cilium egress policy.
            Defaults to the host portion of ``integration.llm_base_url``.
        agent_hostname: public hostname used by Wazuh agents (see wazuh-ingress).
        cert_issuer: cert-manager ClusterIssuer for per-tenant TLS.
        include_llm_api_key: whether to pass the plaintext LLM key through
            ``values.llm.apiKey`` so the chart's 25-secrets.yaml materializes
            ``tenant-llm-key``. MUST be False on the L1 controller path —
            there the controller already writes ``Secret/tenant-llm-key``
            directly (``_copy_llm_key_to_tenant_ns``, no Helm ownership
            metadata), and letting the chart render the same Secret name
            makes ``helm install`` fail with "invalid ownership metadata".
            True (default) is for the cross-cluster L2 install-spec path,
            where no controller pre-writes Secrets on the remote cluster and
            the chart template is the ONLY way the Secret materializes.

    Returns:
        A dict matching ``soctalk-tenant/values.schema.json``.
    """
    from urllib.parse import urlparse

    _llm_url = urlparse(integration.llm_base_url)
    if allowed_llm_hosts is None:
        host = _llm_url.hostname
        allowed_llm_hosts = [host] if host else []

    # Egress port for the LLM endpoint. The runs-worker NetworkPolicy opens
    # exactly this TCP port for outbound LLM calls. Default by scheme when
    # the URL omits an explicit port (443 for https, 80 for http) — but a
    # self-hosted OpenAI-compatible endpoint (Ollama :11434, vLLM :8000,
    # LiteLLM, …) on a non-standard port would otherwise be blocked.
    llm_egress_port = _llm_url.port or (
        80 if _llm_url.scheme == "http" else 443
    )

    # Per-tier LLM backends (issue #12) — empty for single-provider tenants,
    # so nothing below is added and the values are byte-identical to today.
    llm_tier_values, llm_tier_keys, extra_llm_ports = _render_llm_tiers(
        integration, include_llm_api_key=include_llm_api_key, primary_port=llm_egress_port
    )
    llm_engine = primary_llm_engine_for_render(
        integration.llm_provider, integration.llm_engine, integration.llm_base_url
    )

    # 'provided' = tenant brings their OWN externally-deployed Wazuh stack.
    # SocTalk deploys only the adapter + runs-worker here and points the
    # adapter at the external indexer using credentials the controller stores
    # in the ``tenant-external-siem-creds`` Secret (created by a later feature,
    # tenant.profile.provided.controller — referenced by name only here).
    is_provided = profile == "provided"

    if is_provided:
        indexer_url = integration.wazuh_indexer_url
        indexer_creds_secret = "tenant-external-siem-creds"
        # The tenant's own Wazuh manager REST API — the worker's MCP
        # enrichment target (issue #109).
        wazuh_api_url = integration.wazuh_api_url
    else:
        # In-cluster Wazuh provisioned by the wazuh subchart: derive the
        # indexer Service DNS + the chart-minted ``*-wazuh-creds`` Secret.
        indexer_url = f"https://wazuh-{tenant.slug}-wazuh-indexer:9200"
        indexer_creds_secret = f"wazuh-{tenant.slug}-wazuh-creds"
        wazuh_api_url = f"https://wazuh-{tenant.slug}-wazuh-manager:55000"

    # Worker Wazuh MCP wiring (issue #109): the runs-worker's enrichment used
    # to bind ZERO Wazuh tools on every profile — the chart emitted no WAZUH_*
    # env at all, so a provided tenant (whose external Wazuh holds the richest
    # history) triaged on the alert payload alone. Both creds Secrets share the
    # same four key names, so one block serves in-cluster and external alike.
    from urllib.parse import urlparse as _urlparse

    _idx = _urlparse(indexer_url) if indexer_url else None
    runs_worker_wazuh = {
        "enabled": bool(
            integration.wazuh_enabled and wazuh_api_url and indexer_url
        ),
        "apiUrl": wazuh_api_url or "",
        "indexerHost": (_idx.hostname if _idx else "") or "",
        "indexerPort": (_idx.port if _idx and _idx.port else 9200),
        "credsSecret": indexer_creds_secret,
        # Same TLS rule as the adapter: in-cluster wazuh always presents the
        # chart's self-signed cert, so only 'provided' honours the DB row.
        "verifySsl": bool(integration.wazuh_verify_ssl) if is_provided else False,
    }

    # For 'provided', the Cilium FQDN egress allow-list must include every
    # external SIEM host the adapter talks to (indexer :9200 + API :55000).
    # Empty for in-cluster profiles (poc/persistent) — egress stays in-ns.
    external_siem_hosts = _external_siem_hosts(integration) if is_provided else []
    external_siem_endpoints = (
        _external_siem_endpoints(integration) if is_provided else []
    )

    values: dict[str, Any] = {
        "tenant": {
            "id": str(tenant.id),
            "slug": tenant.slug,
            "msspId": mssp_id,
            "installId": install_id,
            "displayName": tenant.display_name,
        },
        "branding": {
            "appName": branding.app_name,
            "logoUrl": branding.logo_url or "",
            "primaryColor": branding.primary_color or "#1a73e8",
            "secondaryColor": branding.secondary_color or "#fbbc04",
            "favicon": branding.favicon_url or "",
        },
        "llm": {
            "provider": integration.llm_provider,
            "baseUrl": integration.llm_base_url,
            "model": integration.llm_model,
            **({"engine": llm_engine} if llm_engine else {}),
            # Tenant-global default sampling (issue #4/#12 follow-up). Rendered
            # to SOCTALK_LLM_TEMPERATURE / SOCTALK_LLM_MAX_TOKENS worker env,
            # which config.load_config() already reads into LLMConfig.temperature
            # / .max_tokens — consumed by the router/supervisor tier. Always
            # emitted (columns carry defaults) so the env reflects the tenant
            # value rather than falling back to the process default.
            "temperature": integration.llm_temperature,
            "maxTokens": integration.llm_max_tokens,
            # Cross-cluster (L2) install-spec path only: pass the
            # plaintext through so the chart's 25-secrets.yaml
            # materializes ``tenant-llm-key`` on install — there is no
            # controller on the remote cluster to pre-write the Secret.
            # On the L1 controller path ``include_llm_api_key=False``
            # forces this to "" so the chart's
            # ``{{- if .Values.llm.apiKey }}`` guard skips the Secret
            # template: the controller already wrote
            # ``Secret/tenant-llm-key`` in apply_secrets (per-tenant key
            # or install-shared fallback), and a chart-rendered Secret
            # with the same name fails helm install with "invalid
            # ownership metadata" (the pre-existing Secret has no
            # meta.helm.sh/release-* annotations).
            "apiKey": (
                (integration.llm_api_key_plain or "")
                if include_llm_api_key
                else ""
            ),
            "apiKeyRef": {
                # Tenant-local Secret. The controller mirrors the
                # install's shared LLM key into the tenant ns under
                # ``tenant-llm-key`` at provisioning so secretKeyRef
                # resolves same-namespace from the worker + adapter
                # pods. ``namespace`` is informational only — k8s
                # secretKeyRef ignores cross-namespace.
                "namespace": "",
                "name": "tenant-llm-key",
                "key": "api_key",
            },
        },
        "components": {
            # 'provided' tenants run no in-namespace SOC stack — the adapter
            # talks to the tenant's external Wazuh. Force these OFF regardless
            # of the integration flags so a stale ``wazuh_enabled=true`` row
            # can't accidentally re-deploy the in-cluster bundle.
            #
            # ``bundled_siem`` gates the tenant chart's own Wazuh subchart. It
            # must be False on the L1 in-cluster controller path: there the
            # controller installs Wazuh as a SEPARATE ``wazuh-<slug>`` release
            # (``_step_helm_apply_wazuh``) that the adapter/runs-worker/linux-ep
            # already target (``wazuh-<slug>-wazuh-*``). Leaving the subchart on
            # too deploys a SECOND, orphaned Wazuh stack in the same namespace
            # (``tenant-<slug>-wazuh-*``) — double the manager/indexer/dashboard
            # footprint, which on a right-sized poc node exhausts RAM/quota and
            # can block the tenant from ever reaching ``active``. True (default)
            # is the cross-cluster L2 install-spec path, where the cloud-agent
            # runs a single helm release and the bundled subchart IS the SIEM.
            "wazuh": {
                "enabled": (
                    False
                    if is_provided
                    else bool(integration.wazuh_enabled and bundled_siem)
                )
            },
            # linux-ep simulator subchart — only the 'poc' profile installs it.
            # ``persistent`` runs real customer endpoints, so a simulator would
            # contaminate the alert pipeline. ``provided`` has no in-cluster
            # SOC at all.
            "linuxep": {"enabled": profile == "poc"},
            "thehive": {
                "enabled": False if is_provided else integration.thehive_enabled
            },
            "cortex": {
                "enabled": False if is_provided else integration.cortex_enabled
            },
            "misp": {"enabled": False},  # V1: MISP deferred regardless of config
        },
        "networkPolicies": {
            "enabled": network_policies_enabled,
            "allowedLlmHosts": allowed_llm_hosts,
            # TCP port the runs-worker egress NetworkPolicy opens for LLM
            # calls. Derived from the LLM base URL so self-hosted endpoints
            # on non-standard ports (Ollama :11434, vLLM :8000, …) aren't
            # blocked. Defaults to 443 for https / 80 for http.
            "llmEgressPort": llm_egress_port,
            # Cilium FQDN egress allow-list for the external SIEM. Empty list
            # for in-cluster profiles; populated for 'provided'.
            "externalSiemHosts": external_siem_hosts,
            "externalSiemEndpoints": external_siem_endpoints,
        },
        "resourceQuota": {
            "enabled": True,
            "requests": {"cpu": "3", "memory": "8Gi"},
            "limits": {"cpu": "7", "memory": "16Gi"},
            "persistentVolumeClaims": "10",
            "pods": "50",
        },
        "limitRange": {
            "enabled": True,
            "defaults": {"memory": "2Gi", "cpu": "500m"},
            "defaultRequests": {"memory": "256Mi", "cpu": "100m"},
            "max": {"memory": "6Gi", "cpu": "2"},
        },
        "agentIngress": {
            "hostname": agent_hostname or f"{tenant.slug}.soc.mssp.local",
            "tls": {
                "issuerRef": cert_issuer or "letsencrypt-prod",
                "secretName": "wazuh-tls",
            },
        },
        "adapter": {
            "image": {
                "repository": os.getenv(
                    "SOCTALK_TENANT_ADAPTER_IMAGE_REPO",
                    "ghcr.io/soctalk/soctalk-adapter",
                ),
                "tag": (_adapter_tag := os.getenv("SOCTALK_TENANT_ADAPTER_IMAGE_TAG", _default_tenant_image_tag())),
                # A moving `latest` tag MUST pull Always, or the node caches the
                # first image it ever pulled and the tenant silently runs weeks-
                # old code (IfNotPresent never re-pulls an unchanged tag) — the
                # reason the demo runs-worker/adapter ran stale triage code. A
                # pinned tag (SHA/version) is immutable, so IfNotPresent is fine.
                "pullPolicy": "Always" if _adapter_tag == "latest" else "IfNotPresent",
            },
            "resources": {
                "requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"cpu": "200m", "memory": "256Mi"},
            },
            "api": {
                "serviceHost": api_service_host,
                "servicePort": api_service_port,
            },
            "tokenSecretRef": {
                "name": adapter_token_secret,
                "key": "token",
            },
            "wazuhIndexer": {
                # In-cluster indexer Service + chart Secret for poc/persistent;
                # external indexer URL + ``tenant-external-siem-creds`` for
                # 'provided'. The credential KEY names are identical either way
                # (the external Secret mirrors the in-cluster 4-key layout).
                "url": indexer_url,
                "credsSecret": indexer_creds_secret,
                "usernameKey": "INDEXER_USERNAME",
                "passwordKey": "INDEXER_PASSWORD",
                # Rendered for ALL profiles so the adapter honours the tenant's
                # TLS-verification preference whether the indexer is in-cluster
                # (self-signed chart cert) or external. In-cluster wazuh
                # (poc / persistent) ALWAYS uses the wazuh chart's inline
                # self-signed cert, so verify_ssl=true crashes the adapter
                # with ``CERTIFICATE_VERIFY_FAILED``; force false regardless
                # of the DB row for these profiles. Only 'provided' honours
                # the operator's setting since they own the external cert.
                "verifySsl": (
                    integration.wazuh_verify_ssl if is_provided else False
                ),
                "minSeverity": int(
                    os.getenv("SOCTALK_ADAPTER_MIN_SEVERITY", "10")
                ),
            },
        },
        "runsWorker": {
            "enabled": True,
            "replicas": 1,
            "image": {
                "repository": os.getenv(
                    "SOCTALK_TENANT_RUNS_WORKER_IMAGE_REPO",
                    "ghcr.io/soctalk/soctalk-orchestrator",
                ),
                "tag": (_worker_tag := os.getenv(
                    "SOCTALK_TENANT_RUNS_WORKER_IMAGE_TAG", _default_tenant_image_tag()
                )),
                # Always-pull a moving `latest` (see adapter above) — otherwise
                # the runs-worker runs stale triage code cached on the node.
                "pullPolicy": "Always" if _worker_tag == "latest" else "IfNotPresent",
            },
            "resources": {
                # Lab tenants share a 4-CPU ResourceQuota with the
                # wazuh stack + adapter (~3 CPU together). Original
                # 1-CPU limit was unschedulable in fresh tenants;
                # 500m fits with headroom for one restart.
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
            "tokenSecretRef": {
                "name": "runs-worker-token",
                "key": "token",
            },
            # Wazuh MCP enrichment target (issue #109) — computed above,
            # profile-aware (in-cluster service DNS vs external SIEM URLs).
            "wazuh": runs_worker_wazuh,
            # Per-tenant model overrides (integration_configs.llm_fast_model /
            # llm_reasoning_model). NULL *or* empty string falls back to
            # llm_model — a cleared override may be stored either way — so
            # every pre-override tenant row renders exactly as before.
            "fastModel": integration.llm_fast_model or integration.llm_model,
            "reasoningModel": (
                integration.llm_reasoning_model or integration.llm_model
            ),
            # Declarative triage policies (#44): validated install-level files scoped
            # to this tenant; {} when SOCTALK_TENANT_PLAYBOOKS_DIR is unset.
            "triagePolicies": render_triage_policy_values(tenant.slug, str(tenant.id)),
        },
        "namespaceLabels": {
            "tenant": "true",
            "managed-by": "soctalk",
        },
    }

    # Per-tier LLM backends (issue #12): inject ONLY when configured so a
    # single-provider tenant renders exactly as before. The worker template
    # emits SOCTALK_<TIER>_* env from ``llm.tiers``; 25-secrets materializes the
    # per-tier keys into ``tenant-llm-key``; the worker NetworkPolicy opens the
    # extra ports.
    if llm_tier_values:
        values["llm"]["tiers"] = llm_tier_values
        if llm_tier_keys:
            values["llm"]["tierKeys"] = llm_tier_keys
        if extra_llm_ports:
            values["networkPolicies"]["extraLlmEgressPorts"] = extra_llm_ports

    # Rollout hash for the runs-worker (issue #12 Codex review). LLM key material
    # (primary + per-tier own keys) lands in ``tenant-llm-key`` as env-from-secret
    # — which does NOT hot-reload in a running pod. A key-only rotation produces
    # byte-identical chart values (the plaintext never rides values on the L1
    # path), so ``helm upgrade`` would be a no-op and the worker would keep the
    # stale key until an unrelated restart. Emit a checksum of the true key
    # material (independent of ``include_llm_api_key``) as a pod-template
    # annotation input so any rotation changes the Deployment spec and forces a
    # rollout. Salted with the tenant id — a bare sha256 is not reversible for a
    # high-entropy key, and the salt removes any cross-tenant equality signal.
    import hashlib

    material = "\x00".join(
        [
            str(integration.tenant_id),
            integration.llm_api_key_plain or "",
            *(
                f"{t}={(b or {}).get('api_key_plain') or ''}"
                for t, b in sorted((integration.llm_tiers or {}).items())
            ),
        ]
    )
    values["llm"]["secretChecksum"] = hashlib.sha256(material.encode()).hexdigest()

    # Per-tenant case-run budget caps are NOT rendered to worker env any more.
    # Both dimensions are DB-resolved at run creation (#103 tokens, #128
    # dollars) and stamped on the run row, so a change takes effect on the next
    # run with no worker rollout, and the row is what every reader sees. The
    # install-global SOCTALK_CASE_RUN_*_BUDGET envs remain only as the default
    # for work that has no run row at all (evals, bench, chat).

    # The linux-ep subchart is enabled on the 'poc' profile
    # (components.linuxep.enabled above), but its statefulset template HARD-FAILS
    # helm install unless ``linuxep.wazuh.managerHost`` + ``credsSecret`` are set
    # ("wazuh.managerHost is required") — which tips a poc tenant to 'degraded'
    # at the helm_apply_tenant step. Wire the passthrough values here using the
    # same ``wazuh-<slug>`` release convention the in-cluster indexer URL uses
    # (``render_linux_ep_values`` was written for exactly this but never called).
    if profile == "poc":
        values["linuxep"] = render_linux_ep_values(
            tenant,
            wazuh_manager_host=f"wazuh-{tenant.slug}-wazuh-manager",
            authd_secret_name=f"wazuh-{tenant.slug}-wazuh-creds",
            # The wazuh chart stores the authd password under key AUTHD_PASS
            # (charts/wazuh secrets.yaml); render_linux_ep_values defaults to
            # 'wazuh_authd_secret', a key that doesn't exist in that Secret — the
            # linuxep pod's secretKeyRef then can't resolve, the pod never starts,
            # and wait_workloads times out on tenant-<slug>-linuxep-0.
            authd_secret_key="AUTHD_PASS",
        )

    if is_provided:
        # No Wazuh agents enroll against this namespace — the tenant's own
        # Wazuh fronts its agents — so there is no agent ingress to publish.
        values.pop("agentIngress", None)
        # Emit the CiliumNetworkPolicy that permits adapter egress to the
        # external SIEM FQDNs listed in networkPolicies.externalSiemHosts.
        values["networkPolicies"]["fqdnEgress"] = {"enabled": True}

    # Layer profile overrides on top. Shallow-merge at the top level.
    # (No top-level "profile" key: the chart's values.schema.json rejects
    # unknown root fields, and the profile is already visible via
    # namespaceLabels.soctalk.io/profile emitted during ensure_namespace.)
    for k, v in _profile_tenant_overrides(profile).items():
        values[k] = v

    # Merge DB-authored ACTIVE triage policies (#44) alongside the operator file triage policies,
    # into the same runsWorker.triagePolicies ConfigMap. Fail closed on a filename collision
    # so an authored triage policy can never silently overwrite an operator file (or vice
    # versa) — the reconcile fails and the UI shows activation failed.
    if authored_triage_policies:
        file_pb = values["runsWorker"].setdefault("triagePolicies", {})
        for filename, text in authored_triage_policies.items():
            if filename in file_pb:
                raise ValueError(
                    f"authored triage policy '{filename}' collides with a file triage policy"
                )
            file_pb[filename] = text
        # Enforce the per-tenant budget across BOTH file and authored triage policies — the
        # file-only check in render_triage_policy_values can't see the authored additions.
        total = sum(len(t.encode()) for t in file_pb.values())
        if total > _TRIAGE_POLICIES_TOTAL_BUDGET:
            raise ValueError(
                f"combined triage policy payload ({total}B) exceeds the per-tenant budget "
                f"({_TRIAGE_POLICIES_TOTAL_BUDGET}B)"
            )
    return values


def render_wazuh_values(
    tenant: Tenant,
    *,
    profile: Profile,
    admin_password: str,
    authd_password: str,
    storage_class_override: str | None = None,
) -> dict[str, Any]:
    """Produce per-tenant values for ``charts/wazuh``.

    The base chart has ``values.yaml`` (defaults) plus ``values.poc.yaml``
    and ``values.persistent.yaml`` on disk. Helm layers those first (via
    ``-f``), and this dict is the **last** layer — per-tenant overrides
    on top: minted credentials, tenant-scoped cluster name, optional
    storage-class pin from controller settings (for ``persistent``).
    """

    values: dict[str, Any] = {
        "credentials": {
            "apiUsername": "wazuh-wui",
            "apiPassword": admin_password,
            "indexerUsername": "admin",
            # The demo-security init in the upstream indexer image still
            # expects admin:admin; until we override internal_users.yml
            # at install time, filebeat on the manager will 401 if this
            # changes. See k3d setup notes.
            "indexerPassword": "admin",
            "authdPassword": authd_password,
        },
        "tenant": {
            "id": str(tenant.id),
            "slug": tenant.slug,
            "profile": profile,
        },
    }

    base_domain = os.getenv("SOCTALK_TENANT_BASE_DOMAIN", "").strip()
    if base_domain:
        values["dashboard"] = {
            "ingress": {
                "enabled": True,
                "className": os.getenv("SOCTALK_INGRESS_CLASS_NAME", "nginx"),
                "hostname": f"wazuh-{tenant.slug}.{base_domain}",
            },
        }

    if storage_class_override:
        values["storage"] = {"storageClass": storage_class_override}

    return values


def render_linux_ep_values(
    tenant: Tenant,
    *,
    wazuh_manager_host: str,
    authd_secret_name: str,
    authd_secret_key: str = "wazuh_authd_secret",
    replicas: int = 1,
) -> dict[str, Any]:
    """Produce per-tenant values for the ``linux-ep`` subchart.

    Used by ``_build_install_helm_release_spec`` (L2 cross-cluster) to feed
    the bundled linux-ep simulator with the wazuh manager service it should
    register against and the authd password it should enrol with. When linux-
    ep is installed as a subchart of soctalk-tenant, the wazuh subchart's
    Service resolves to ``<release>-wazuh-manager`` in the same namespace —
    same DNS the in-namespace adapter uses for its API calls.

    Field names mirror ``charts/linux-ep/values.yaml`` exactly (the chart's
    ``.Values.wazuh.credsSecret.{name,authdPasswordKey}``); the two `fail`
    guards at the top of ``templates/statefulset.yaml`` require both to be
    present.
    """
    return {
        "replicas": replicas,
        "image": {
            "repository": os.getenv(
                "SOCTALK_TENANT_LINUX_EP_IMAGE_REPO",
                "ghcr.io/soctalk/soctalk-linux-ep",
            ),
            "tag": (_linuxep_tag := os.getenv("SOCTALK_TENANT_LINUX_EP_IMAGE_TAG", _default_tenant_image_tag())),
            # Moving `latest` must pull Always (same reason as the adapter /
            # runs-worker above); a pinned release tag is immutable so
            # IfNotPresent is fine. Without an override the tenant would fall
            # to the linux-ep subchart default, which on a release cut points
            # at soctalk-linux-ep:<version> — unpublished until cut-k8s-release
            # runs, so demo (which tracks `latest`) must pin this to latest.
            "pullPolicy": "Always" if _linuxep_tag == "latest" else "IfNotPresent",
        },
        # Auto-run the attack simulator. linux-ep only installs on the 'poc'
        # profile (its endpoints ARE simulators), so a pilot demos live Wazuh
        # detections immediately — no manual /opt/scripts/run-attack.sh step.
        # Rate is governed by the chart's attackInterval / dailyAlertCap.
        "simulator": {"enabled": True},
        "wazuh": {
            "managerHost": wazuh_manager_host,
            "credsSecret": {
                "name": authd_secret_name,
                "authdPasswordKey": authd_secret_key,
            },
        },
        "tenant": {
            "slug": tenant.slug,
            "id": str(tenant.id),
        },
    }
