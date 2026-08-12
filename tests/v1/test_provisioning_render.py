"""Unit tests for profile-driven chart values rendering.

Pure functions only — no DB, no kube. The ``adapter-fqdn-egress`` chart
assertions shell out to ``helm template`` (the only place that touches a
binary); they self-skip where ``helm`` is not on PATH so the rest of the
suite stays a pure-python run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from soctalk.core.llm_provider import (
    OPENAI_SENTINEL_BASE_URL,
    has_usable_served_base_url,
)
from soctalk.core.provisioning.render import (
    _profile_tenant_overrides,
    render_tenant_values,
    render_wazuh_values,
)
from soctalk.core.tenancy.models import (
    BrandingConfig,
    IntegrationConfig,
    Tenant,
    TenantState,
)


def _make_tenant(profile: str = "poc") -> Tenant:
    return Tenant(
        id=uuid4(),
        slug="acme",
        display_name="Acme Corp",
        state=TenantState.PROVISIONING.value,
        profile=profile,
        organization_id=uuid4(),
    )


def _make_integration(tid) -> IntegrationConfig:
    return IntegrationConfig(
        tenant_id=tid,
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o",
    )


def _make_branding(tid) -> BrandingConfig:
    return BrandingConfig(
        tenant_id=tid,
        app_name="Acme SOC",
        primary_color="#112233",
    )


_HOSTED_VENDOR_SERVED_BASE_URL_REF = "#/$defs/hostedVendorServedBaseUrl"
_HOSTED_VENDOR_DOT = "[.\u3002\uff0e\uff61]"
_HOSTED_VENDOR_SERVED_BASE_URL_PATTERN = (
    r"^\s*(?:$|[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/?#\s]+@)?"
    r"(?:"
    r"(?:[A-Za-z0-9-]+"
    + _HOSTED_VENDOR_DOT
    + r")*"
    r"[aA][pP][iI]"
    + _HOSTED_VENDOR_DOT
    + r"(?:[oO][pP][eE][nN][aA][iI]|"
    r"[aA][nN][tT][hH][rR][oO][pP][iI][cC])"
    + _HOSTED_VENDOR_DOT
    + r"[cC][oO][mM]"
    r"|"
    r"(?:[A-Za-z0-9-]+"
    + _HOSTED_VENDOR_DOT
    + r")*"
    r"[oO][pP][eE][nN][rR][oO][uU][tT][eE][rR]"
    + _HOSTED_VENDOR_DOT
    + r"[aA][iI]"
    r")"
    r"(?:"
    + _HOSTED_VENDOR_DOT
    + r")?"
    r"(?::[0-9]+)?(?:[/\?#].*)?)\s*$"
)
_HOSTED_UNICODE_DOT_BASE_URLS = (
    "https://api\u3002openai\u3002com/v1",
    "https://api\uff0eopenai\uff0ecom/v1",
    "https://api\uff61openai\uff61com/v1",
    "https://api\u3002anthropic\u3002com",
    "https://api\uff0eanthropic\uff0ecom",
    "https://api\uff61anthropic\uff61com",
    "https://openrouter\u3002ai/api/v1",
    "https://openrouter\uff0eai/api/v1",
    "https://openrouter\uff61ai/api/v1",
)
_HOSTED_VENDOR_REJECT_BASE_URLS = (
    OPENAI_SENTINEL_BASE_URL,
    " https://api.openai.com/v1 ",
    "https://api.openai.com/v1/",
    "https://api.openai.com:443/v1",
    "https://api.openai.com./v1",
    "https://api.openai.com\u3002/v1",
    "https://api.openai.com\uff0e/v1",
    "https://api.openai.com\uff61/v1",
    "https://gateway.api.openai.com/v1",
    "https://gateway.api.anthropic.com",
    "https://openrouter.ai/api/v1",
    "https://gateway.openrouter.ai/api/v1",
    "https://OPENROUTER.AI/api/v1",
    "https://openrouter.ai./api/v1",
    "https://API.OPENAI.COM/v1",
    "https://API.OPENAI.COM/V1",
    " https://Api.OpenAI.Com:443/v1 ",
    "https://API.ANTHROPIC.COM",
    *_HOSTED_UNICODE_DOT_BASE_URLS,
)
_HOSTED_MIXED_CASE_BASE_URL = "https://API.OPENAI.COM/v1"
_HOSTED_SUBSTRING_GATEWAY_BASE_URL = "https://API.OPENAI.COM.gateway.example:8443/V1"


# ---------------------------------------------------------------------------
# render_tenant_values: profile layering
# ---------------------------------------------------------------------------


def test_poc_profile_emits_tight_resource_quota():
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    # Chart schema disallows unknown top-level fields, so no "profile"
    # key ends up in values; the overrides land in resourceQuota etc.
    assert "profile" not in v
    # 6Gi covers adapter + wazuh-{manager,indexer,dashboard} + linux-ep
    # at PoC limits with restart headroom; bumped from 4Gi when linux-ep
    # joined the poc bundle (attack simulator + Wazuh agent side-by-side).
    assert v["resourceQuota"]["requests"]["memory"] == "6Gi"
    assert v["resourceQuota"]["pods"] == "20"


def test_poc_profile_wires_linuxep_wazuh_manager():
    # The poc profile enables the linux-ep subchart, whose statefulset hard-fails
    # helm install unless wazuh.managerHost + credsSecret are set — the cause of
    # the demo 'degraded' provisioning failure. Assert the passthrough block is
    # emitted with the wazuh-<slug> release convention.
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t, integration=_make_integration(t.id), branding=_make_branding(t.id),
        mssp_id=str(uuid4()), install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm", profile="poc",
    )
    lep = v["linuxep"]
    assert lep["wazuh"]["managerHost"] == f"wazuh-{t.slug}-wazuh-manager"
    assert lep["wazuh"]["credsSecret"]["name"] == f"wazuh-{t.slug}-wazuh-creds"
    # Must match the key the wazuh chart's creds Secret actually uses (AUTHD_PASS),
    # not render_linux_ep_values' default — else the linuxep pod can't start.
    assert lep["wazuh"]["credsSecret"]["authdPasswordKey"] == "AUTHD_PASS"
    assert v["components"]["linuxep"]["enabled"] is True
    # Non-poc profiles don't enable linux-ep → no passthrough block.
    v2 = render_tenant_values(
        tenant=_make_tenant("persistent"), integration=_make_integration(t.id),
        branding=_make_branding(t.id), mssp_id=str(uuid4()), install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm", profile="persistent",
    )
    assert "linuxep" not in v2


def test_moving_latest_tag_pulls_always(monkeypatch):
    # A moving `latest` image tag MUST render pullPolicy=Always or the node caches
    # stale code (the demo runs-worker/adapter ran weeks-old triage code).
    # `latest` is no longer the DEFAULT (see the no-latest-by-default test below),
    # so an operator has to ask for it explicitly — but when they do, the
    # Always-pull guard still has to hold.
    monkeypatch.setenv("SOCTALK_TENANT_RUNS_WORKER_IMAGE_TAG", "latest")
    monkeypatch.setenv("SOCTALK_TENANT_ADAPTER_IMAGE_TAG", "latest")
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t, integration=_make_integration(t.id), branding=_make_branding(t.id),
        mssp_id=str(uuid4()), install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm", profile="poc",
    )
    assert v["runsWorker"]["image"]["tag"] == "latest"
    assert v["runsWorker"]["image"]["pullPolicy"] == "Always"
    assert v["adapter"]["image"]["pullPolicy"] == "Always"


def test_tenant_images_never_default_to_latest(monkeypatch):
    """With no env from the chart, tenants must pin to the release — not `latest`.

    The chart only emits SOCTALK_TENANT_*_IMAGE_TAG when the value is truthy, so
    an operator who clears tenantProvisioning.adapterImageTag lands on this
    default. It used to be `latest`, which silently gave every tenant a moving
    tag whose build changed under them.
    """
    from importlib.metadata import version

    for var in (
        "SOCTALK_TENANT_ADAPTER_IMAGE_TAG",
        "SOCTALK_TENANT_RUNS_WORKER_IMAGE_TAG",
        "SOCTALK_TENANT_LINUX_EP_IMAGE_TAG",
    ):
        monkeypatch.delenv(var, raising=False)
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t, integration=_make_integration(t.id), branding=_make_branding(t.id),
        mssp_id=str(uuid4()), install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm", profile="poc",
    )
    release = version("soctalk")
    # linux-ep is rendered from the same default and must not be forgotten.
    assert v["linuxep"]["image"]["tag"] == release
    assert v["linuxep"]["image"]["tag"] != "latest"
    for section in ("adapter", "runsWorker"):
        assert v[section]["image"]["tag"] == release, section
        assert v[section]["image"]["tag"] != "latest"
        # A pinned tag is immutable, so IfNotPresent is correct and cheaper.
        assert v[section]["image"]["pullPolicy"] == "IfNotPresent", section


def test_persistent_profile_emits_larger_quota():
    t = _make_tenant("persistent")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="persistent",
    )
    assert "profile" not in v
    assert v["resourceQuota"]["requests"]["memory"] == "6Gi"
    # Persistent leaves limitRange at the base (no override).


# ---------------------------------------------------------------------------
# render_tenant_values: 'provided' profile (tenant brings their own Wazuh)
# ---------------------------------------------------------------------------


def _tenant_values_schema() -> dict:
    """Load the soctalk-tenant chart values schema from the repo."""
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "charts"
        / "soctalk-tenant"
        / "values.schema.json"
    )
    return json.loads(schema_path.read_text())


def _system_values_schema() -> dict:
    """Load the soctalk-system chart values schema from the repo."""
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "charts"
        / "soctalk-system"
        / "values.schema.json"
    )
    return json.loads(schema_path.read_text())


def _system_default_values() -> dict:
    """Load the soctalk-system chart defaults as a complete schema instance."""
    yaml = pytest.importorskip("yaml")
    values_path = (
        Path(__file__).resolve().parents[2]
        / "charts"
        / "soctalk-system"
        / "values.yaml"
    )
    return yaml.safe_load(values_path.read_text())


def _count_schema_refs(schema: dict, ref: str) -> int:
    count = 0
    stack: list = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("$ref") == ref:
                count += 1
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return count


def _schema_accepts(instance: dict, schema: dict) -> bool:
    jsonschema = pytest.importorskip("jsonschema")
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError:
        return False
    return True


def _tenant_values_with_served_primary_base_url(base_url: str) -> dict:
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"]["baseUrl"] = base_url
    v["llm"]["engine"] = "sglang"
    return v


def _tenant_values_with_served_tier_base_url(base_url: str) -> dict:
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"]["tiers"] = {
        "fast": {
            "provider": "openai",
            "baseUrl": base_url,
            "model": "qwen3-32b",
            "engine": "sglang",
        }
    }
    return v


def _system_values_with_served_primary_base_url(base_url: str) -> dict:
    values = _system_default_values()
    values["defaults"]["llm"]["baseUrl"] = base_url
    values["defaults"]["llm"]["engine"] = "sglang"
    return values


def _system_values_with_served_fast_tier_base_url(base_url: str) -> dict:
    values = _system_default_values()
    values["defaults"]["llm"]["fastTier"] = {
        "provider": "openai-compatible",
        "baseUrl": base_url,
        "model": "qwen3-32b",
        "engine": "sglang",
    }
    return values


def test_chart_schemas_share_hosted_vendor_served_base_url_guard():
    system_schema = _system_values_schema()
    tenant_schema = _tenant_values_schema()

    assert _count_schema_refs(system_schema, _HOSTED_VENDOR_SERVED_BASE_URL_REF) == 2
    assert _count_schema_refs(tenant_schema, _HOSTED_VENDOR_SERVED_BASE_URL_REF) == 2
    assert (
        system_schema["$defs"]["hostedVendorServedBaseUrl"]
        == tenant_schema["$defs"]["hostedVendorServedBaseUrl"]
        == {"pattern": _HOSTED_VENDOR_SERVED_BASE_URL_PATTERN}
    )


@pytest.mark.parametrize(
    "base_url",
    (
        "https://API.OPENAI.COM/v1",
        "https://API.OPENAI.COM/V1",
        " https://Api.OpenAI.Com:443/v1 ",
        "https://api.openai.com./v1",
        "https://api.openai.com\u3002/v1",
        "https://gateway.api.openai.com/v1",
        "https://gateway.api.anthropic.com",
        "https://openrouter.ai/api/v1",
        "https://gateway.openrouter.ai/api/v1",
        "https://OPENROUTER.AI/api/v1",
        "https://openrouter.ai./api/v1",
        "https://API.ANTHROPIC.COM",
        _HOSTED_SUBSTRING_GATEWAY_BASE_URL,
        "https://evilapi.openai.com/v1",
        "https://notapi.anthropic.com",
        "https://evilopenrouter.ai/api/v1",
        "https://gateway.example/API.OPENAI.COM/v1",
        " http://SGLANG.INTERNAL:8000/V1 ",
        *_HOSTED_UNICODE_DOT_BASE_URLS,
        "https://api%2eopenai%2ecom/v1",
        "https://api%2eanthropic%2ecom",
    ),
)
def test_chart_schema_served_base_url_authority_variants_match_python_classifier(
    base_url: str,
):
    runtime_accepts = has_usable_served_base_url(base_url)
    system_schema = _system_values_schema()
    tenant_schema = _tenant_values_schema()

    assert (
        _schema_accepts(
            _system_values_with_served_primary_base_url(base_url),
            system_schema,
        )
        is runtime_accepts
    )
    assert (
        _schema_accepts(
            _system_values_with_served_fast_tier_base_url(base_url),
            system_schema,
        )
        is runtime_accepts
    )
    assert (
        _schema_accepts(
            _tenant_values_with_served_primary_base_url(base_url),
            tenant_schema,
        )
        is runtime_accepts
    )
    assert (
        _schema_accepts(
            _tenant_values_with_served_tier_base_url(base_url),
            tenant_schema,
        )
        is runtime_accepts
    )


def _assert_validates_against_tenant_schema(values: dict) -> None:
    """The rendered values must satisfy the tenant chart's JSON Schema."""
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=values, schema=_tenant_values_schema())


def test_render_tenant_values_emits_primary_llm_engine():
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.llm_base_url = "http://sglang.internal:8000/v1"
    integration.llm_engine = "sglang"

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )

    assert v["llm"]["engine"] == "sglang"
    _assert_validates_against_tenant_schema(v)


def test_render_tenant_values_rejects_bad_stored_primary_engine():
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.llm_provider = "anthropic"
    integration.llm_base_url = "https://api.anthropic.com"
    integration.llm_engine = "sglang"

    with pytest.raises(ValueError, match="not valid with provider 'anthropic'"):
        render_tenant_values(
            tenant=t,
            integration=integration,
            branding=_make_branding(t.id),
            mssp_id=str(uuid4()),
            install_id=str(uuid4()),
            llm_secret_name="tenant-x-llm",
            profile="poc",
        )


def test_render_tenant_values_ignores_stale_served_engine_on_hosted_openai():
    from soctalk.core.pricing.resolve import provider_kind_for

    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.llm_provider = "openai-compatible"
    integration.llm_base_url = "https://api.openai.com/v1"
    integration.llm_engine = "sglang"

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )

    assert "engine" not in v["llm"]
    assert (
        provider_kind_for(v["llm"]["provider"], v["llm"]["baseUrl"], v["llm"].get("engine"))
        == provider_kind_for(
            integration.llm_provider, integration.llm_base_url, integration.llm_engine
        )
        == "openai"
    )
    _assert_validates_against_tenant_schema(v)


def test_tenant_chart_schema_rejects_anthropic_primary_served_engine():
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"]["provider"] = "anthropic"
    v["llm"]["engine"] = "sglang"

    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=v, schema=_tenant_values_schema())


@pytest.mark.parametrize(
    "base_url",
    _HOSTED_VENDOR_REJECT_BASE_URLS,
)
def test_tenant_chart_schema_rejects_primary_served_engine_hosted_openai_base_url(
    base_url: str,
):
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"]["baseUrl"] = base_url
    v["llm"]["engine"] = "sglang"

    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=v, schema=_tenant_values_schema())


@pytest.mark.parametrize("tier", ("fast", "reasoning"))
@pytest.mark.parametrize(
    "base_url",
    _HOSTED_VENDOR_REJECT_BASE_URLS,
)
def test_tenant_chart_schema_rejects_tier_served_engine_hosted_openai_base_url(
    tier: str, base_url: str,
):
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"]["tiers"] = {
        tier: {
            "provider": "openai",
            "baseUrl": base_url,
            "model": "qwen3-32b",
            "engine": "sglang",
        }
    }

    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=v, schema=_tenant_values_schema())


def test_system_chart_schema_rejects_contradictory_llm_defaults():
    jsonschema = pytest.importorskip("jsonschema")
    for engine in ("openai_compatible", "vllm", "sglang"):
        values = _system_default_values()
        values["defaults"]["llm"].update(
            {
                "provider": "anthropic",
                "baseUrl": "https://api.anthropic.com",
                "model": "claude-sonnet-4-6",
                "engine": engine,
            }
        )

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=values, schema=_system_values_schema())


@pytest.mark.parametrize("engine", ("openai_compatible", "vllm", "sglang"))
@pytest.mark.parametrize(
    "base_url",
    (None, "", *_HOSTED_VENDOR_REJECT_BASE_URLS),
)
def test_system_chart_schema_rejects_served_engine_without_custom_base_url(
    engine: str, base_url: str | None
):
    jsonschema = pytest.importorskip("jsonschema")
    values = _system_default_values()
    values["defaults"]["llm"]["engine"] = engine
    if base_url is None:
        values["defaults"]["llm"].pop("baseUrl", None)
    else:
        values["defaults"]["llm"]["baseUrl"] = base_url

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=values, schema=_system_values_schema())


@pytest.mark.parametrize("engine", ("openai_compatible", "vllm", "sglang"))
@pytest.mark.parametrize(
    "base_url",
    (None, "", *_HOSTED_VENDOR_REJECT_BASE_URLS),
)
def test_system_chart_schema_rejects_default_fast_tier_served_engine_hosted_base_url(
    engine: str, base_url: str | None
):
    jsonschema = pytest.importorskip("jsonschema")
    values = _system_default_values()
    values["defaults"]["llm"]["fastTier"] = {
        "provider": "openai-compatible",
        "model": "qwen3-32b",
        "engine": engine,
    }
    if base_url is not None:
        values["defaults"]["llm"]["fastTier"]["baseUrl"] = base_url

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=values, schema=_system_values_schema())


@pytest.mark.parametrize(
    "label,llm_patch,drop_keys",
    [
        (
            "hosted default no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
            },
            ("engine",),
        ),
        (
            "hosted default mixed-case no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": _HOSTED_MIXED_CASE_BASE_URL,
                "model": "gpt-4o",
            },
            ("engine",),
        ),
        (
            "openrouter hosted no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "deepseek/deepseek-chat",
            },
            ("engine",),
        ),
        (
            "gateway base URL no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://llm-gateway.example/v1",
                "model": "gpt-4o",
            },
            ("engine",),
        ),
        (
            "gateway engine custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://llm-gateway.example/v1",
                "model": "qwen3-32b",
                "engine": "openai_compatible",
            },
            (),
        ),
        (
            "served engine hosted-substring gateway",
            {
                "provider": "openai-compatible",
                "baseUrl": _HOSTED_SUBSTRING_GATEWAY_BASE_URL,
                "model": "qwen3-32b",
                "engine": "sglang",
            },
            (),
        ),
        (
            "anthropic provider no base URL",
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            ("baseUrl", "engine"),
        ),
        (
            "frontier engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "frontier",
            },
            (),
        ),
        (
            "empty engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "",
            },
            (),
        ),
        (
            "served engine custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": "http://sglang.internal:8000/v1",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
            (),
        ),
        (
            "served engine whitespace custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": " http://sglang.internal:8000/v1 ",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
            (),
        ),
    ],
)
def test_system_chart_schema_accepts_legitimate_llm_default_shapes(
    label: str, llm_patch: dict, drop_keys: tuple[str, ...]
):
    jsonschema = pytest.importorskip("jsonschema")
    values = _system_default_values()
    values["defaults"]["llm"].update(llm_patch)
    for key in drop_keys:
        values["defaults"]["llm"].pop(key, None)

    try:
        jsonschema.validate(instance=values, schema=_system_values_schema())
    except jsonschema.ValidationError as exc:
        pytest.fail(f"{label} should validate: {exc.message}")


@pytest.mark.parametrize(
    "label,fast_tier",
    [
        (
            "hosted default no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
            },
        ),
        (
            "hosted default mixed-case no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": _HOSTED_MIXED_CASE_BASE_URL,
                "model": "gpt-4o",
            },
        ),
        (
            "openrouter hosted no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "deepseek/deepseek-chat",
            },
        ),
        (
            "gateway engine custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://llm-gateway.example/v1",
                "model": "qwen3-32b",
                "engine": "openai_compatible",
            },
        ),
        (
            "served engine hosted-substring gateway",
            {
                "provider": "openai-compatible",
                "baseUrl": _HOSTED_SUBSTRING_GATEWAY_BASE_URL,
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
        (
            "anthropic no engine",
            {
                "provider": "anthropic",
                "baseUrl": "https://api.anthropic.com",
                "model": "claude-sonnet-4-6",
            },
        ),
        (
            "frontier engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "frontier",
            },
        ),
        (
            "empty engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "",
            },
        ),
        (
            "served engine custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": "http://sglang.internal:8000/v1",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
        (
            "served engine whitespace custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": " http://sglang.internal:8000/v1 ",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
    ],
)
def test_system_chart_schema_accepts_legitimate_default_fast_tier_shapes(
    label: str, fast_tier: dict,
):
    jsonschema = pytest.importorskip("jsonschema")
    values = _system_default_values()
    values["defaults"]["llm"]["fastTier"] = fast_tier

    try:
        jsonschema.validate(instance=values, schema=_system_values_schema())
    except jsonschema.ValidationError as exc:
        pytest.fail(f"{label} should validate: {exc.message}")


@pytest.mark.parametrize(
    "label,llm_patch",
    [
        (
            "hosted default no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
            },
        ),
        (
            "hosted default mixed-case no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": _HOSTED_MIXED_CASE_BASE_URL,
                "model": "gpt-4o",
            },
        ),
        (
            "openrouter hosted no engine",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "deepseek/deepseek-chat",
            },
        ),
        (
            "gateway engine custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": "https://llm-gateway.example/v1",
                "model": "qwen3-32b",
                "engine": "openai_compatible",
            },
        ),
        (
            "served engine hosted-substring gateway",
            {
                "provider": "openai-compatible",
                "baseUrl": _HOSTED_SUBSTRING_GATEWAY_BASE_URL,
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
        (
            "anthropic no engine",
            {
                "provider": "anthropic",
                "baseUrl": "https://api.anthropic.com",
                "model": "claude-sonnet-4-6",
            },
        ),
        (
            "frontier engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "frontier",
            },
        ),
        (
            "empty engine",
            {
                "provider": "openai-compatible",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "",
            },
        ),
        (
            "served engine custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": "http://sglang.internal:8000/v1",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
        (
            "served engine whitespace custom base URL",
            {
                "provider": "openai-compatible",
                "baseUrl": " http://sglang.internal:8000/v1 ",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
    ],
)
def test_tenant_chart_schema_accepts_legitimate_primary_llm_shapes(
    label: str, llm_patch: dict,
):
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"].update(llm_patch)

    jsonschema = pytest.importorskip("jsonschema")
    try:
        jsonschema.validate(instance=v, schema=_tenant_values_schema())
    except jsonschema.ValidationError as exc:
        pytest.fail(f"{label} should validate: {exc.message}")


@pytest.mark.parametrize("tier", ("fast", "reasoning"))
@pytest.mark.parametrize(
    "label,tier_block",
    [
        (
            "hosted default no engine",
            {
                "provider": "openai",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
            },
        ),
        (
            "hosted default mixed-case no engine",
            {
                "provider": "openai",
                "baseUrl": _HOSTED_MIXED_CASE_BASE_URL,
                "model": "gpt-4o",
            },
        ),
        (
            "openrouter hosted no engine",
            {
                "provider": "openai",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "deepseek/deepseek-chat",
            },
        ),
        (
            "gateway engine custom base URL",
            {
                "provider": "openai",
                "baseUrl": "https://llm-gateway.example/v1",
                "model": "qwen3-32b",
                "engine": "openai_compatible",
            },
        ),
        (
            "served engine hosted-substring gateway",
            {
                "provider": "openai",
                "baseUrl": _HOSTED_SUBSTRING_GATEWAY_BASE_URL,
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
        (
            "anthropic no engine",
            {
                "provider": "anthropic",
                "baseUrl": "https://api.anthropic.com",
                "model": "claude-sonnet-4-6",
            },
        ),
        (
            "frontier engine",
            {
                "provider": "openai",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "frontier",
            },
        ),
        (
            "empty engine",
            {
                "provider": "openai",
                "baseUrl": OPENAI_SENTINEL_BASE_URL,
                "model": "gpt-4o",
                "engine": "",
            },
        ),
        (
            "served engine custom base URL",
            {
                "provider": "openai",
                "baseUrl": "http://sglang.internal:8000/v1",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
        (
            "served engine whitespace custom base URL",
            {
                "provider": "openai",
                "baseUrl": " http://sglang.internal:8000/v1 ",
                "model": "qwen3-32b",
                "engine": "sglang",
            },
        ),
    ],
)
def test_tenant_chart_schema_accepts_legitimate_tier_llm_shapes(
    tier: str, label: str, tier_block: dict,
):
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    v["llm"]["tiers"] = {tier: tier_block}

    jsonschema = pytest.importorskip("jsonschema")
    try:
        jsonschema.validate(instance=v, schema=_tenant_values_schema())
    except jsonschema.ValidationError as exc:
        pytest.fail(f"{tier} {label} should validate: {exc.message}")


def test_render_provided_profile():
    """The 'provided' profile: the tenant brings their OWN external Wazuh stack.

    SocTalk must NOT deploy in-cluster Wazuh/TheHive/Cortex; it points the
    per-tenant adapter at the EXTERNAL indexer using the controller-managed
    ``tenant-external-siem-creds`` Secret, drops the agent ingress, sizes the
    ResourceQuota for just the adapter + runs-worker, and emits the external
    SIEM egress allow-list (both indexer + API hosts) with FQDN egress on.

    Asserts every acceptance bullet of ``tenant.profile.provided.render``.
    """
    t = _make_tenant("provided")
    integration = _make_integration(t.id)
    # The integration row claims the in-cluster components are "enabled"; the
    # 'provided' profile must override them OFF regardless of these flags.
    integration.wazuh_enabled = True
    integration.thehive_enabled = True
    integration.cortex_enabled = True
    # External Wazuh — indexer + API on DISTINCT hosts; BOTH credential pairs.
    integration.wazuh_indexer_url = "https://indexer.siem.acme.example:9200"
    integration.wazuh_indexer_username = "ext-indexer"
    integration.wazuh_indexer_password_plain = "indexer-pw"
    integration.wazuh_url = "https://manager.siem.acme.example"
    integration.wazuh_api_url = "https://manager.siem.acme.example:55000"
    integration.wazuh_username = "ext-api"
    integration.wazuh_password_plain = "api-pw"
    # Set FALSE here (non-default) so the poc check below — which uses True —
    # proves verifySsl is wired to the integration row, not hardcoded.
    integration.wazuh_verify_ssl = False

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="provided",
    )

    # (1) in-cluster SOC stack disabled for 'provided'
    assert v["components"]["wazuh"]["enabled"] is False
    assert v["components"]["thehive"]["enabled"] is False
    assert v["components"]["cortex"]["enabled"] is False

    # (2) adapter points at the EXTERNAL indexer via the controller-managed Secret
    idx = v["adapter"]["wazuhIndexer"]
    assert idx["url"] == integration.wazuh_indexer_url
    assert idx["credsSecret"] == "tenant-external-siem-creds"
    assert idx["usernameKey"] == "INDEXER_USERNAME"
    assert idx["passwordKey"] == "INDEXER_PASSWORD"

    # (3) verifySsl mirrors the integration row (rendered for 'provided')
    assert idx["verifySsl"] == integration.wazuh_verify_ssl  # False here

    # (4) no agent ingress — the tenant's own Wazuh fronts its agents
    assert "agentIngress" not in v or v["agentIngress"].get("enabled") is False

    # (5) ResourceQuota sized for adapter + runs-worker only — assert both the
    #     rendered dict AND the override helper directly (acceptance names it).
    rq = v["resourceQuota"]
    assert rq["requests"] == {"cpu": "1", "memory": "2Gi"}
    assert rq["limits"] == {"cpu": "2", "memory": "4Gi"}
    assert rq["pods"] == "10"
    assert rq["persistentVolumeClaims"] == "2"
    override_rq = _profile_tenant_overrides("provided")["resourceQuota"]
    assert override_rq["requests"] == {"cpu": "1", "memory": "2Gi"}
    assert override_rq["limits"] == {"cpu": "2", "memory": "4Gi"}
    assert override_rq["pods"] == "10"
    assert override_rq["persistentVolumeClaims"] == "2"

    # (6) external SIEM egress allow-list: both hosts, deduped; FQDN egress on
    hosts = v["networkPolicies"]["externalSiemHosts"]
    assert "indexer.siem.acme.example" in hosts
    assert "manager.siem.acme.example" in hosts
    assert len(hosts) == len(set(hosts)) == 2  # deduped, no stray entries
    assert v["networkPolicies"]["fqdnEgress"]["enabled"] is True

    # (4, schema) the rendered 'provided' shape validates against the chart schema
    _assert_validates_against_tenant_schema(v)

    # (3 + 6, other profiles) verifySsl is FORCED false for poc regardless
    # of the integration row — poc ships in-cluster Wazuh with self-signed
    # certs, so the adapter can never verify them. externalSiemHosts stays
    # empty for non-provided profiles. poc still validates.
    poc_integration = _make_integration(t.id)
    poc_integration.wazuh_verify_ssl = True
    v_poc = render_tenant_values(
        tenant=_make_tenant("poc"),
        integration=poc_integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v_poc["adapter"]["wazuhIndexer"]["verifySsl"] is False
    assert v_poc["networkPolicies"]["externalSiemHosts"] == []
    _assert_validates_against_tenant_schema(v_poc)


# ---------------------------------------------------------------------------
# Chart render: adapter-fqdn-egress CiliumNetworkPolicy (helm template)
#
# These exercise the *chart* side of the FQDN-egress feature. The values from
# render_tenant_values are fed through ``helm template`` and the emitted
# CiliumNetworkPolicy is asserted on. ``helm`` also validates against
# values.schema.json while templating, so a schema violation surfaces as a
# non-zero exit here too. Skipped (not failed) where ``helm`` is absent.
# ---------------------------------------------------------------------------

_TENANT_CHART_DIR = Path(__file__).resolve().parents[2] / "charts" / "soctalk-tenant"


def _helm_template(values: dict, *, cilium: bool = True) -> list[dict]:
    """Render the soctalk-tenant chart with ``values`` → list of manifests.

    ``cilium=True`` advertises the CiliumNetworkPolicy API to helm (as a
    Cilium cluster would); ``cilium=False`` mimics a stock flannel k3s so the
    Capabilities guard (issue #107) suppresses the FQDN egress object.
    """
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm binary not on PATH")
    yaml = pytest.importorskip("yaml")

    cmd = [helm, "template", "t", str(_TENANT_CHART_DIR), "-f"]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(values, fh)
        values_path = fh.name
    cmd.append(values_path)
    if cilium:
        cmd += ["--api-versions", "cilium.io/v2/CiliumNetworkPolicy"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(values_path)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _fqdn_egress_match_names(manifests: list[dict]) -> list[str] | None:
    """toFQDNs matchName values from adapter-fqdn-egress, or None if absent."""
    for doc in manifests:
        if (
            doc.get("kind") == "CiliumNetworkPolicy"
            and doc.get("metadata", {}).get("name") == "adapter-fqdn-egress"
        ):
            names: list[str] = []
            for rule in doc.get("spec", {}).get("egress", []):
                for fqdn in rule.get("toFQDNs", []) or []:
                    if "matchName" in fqdn:
                        names.append(fqdn["matchName"])
            return names
    return None


def _provided_values_for_chart(
    *,
    indexer_url: str | None,
    api_url: str | None,
    soctalk_url: str,
) -> dict:
    """render_tenant_values for a 'provided' tenant, plus a soctalkSystem.url.

    render populates externalSiemHosts + fqdnEgress.enabled; soctalkSystem.url
    is injected here because the L1 :issue-agent path sets it, not the renderer.
    """
    t = _make_tenant("provided")
    integration = _make_integration(t.id)
    integration.wazuh_indexer_url = indexer_url
    integration.wazuh_api_url = api_url
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="provided",
    )
    v["soctalkSystem"] = {"url": soctalk_url, "adapterToken": ""}
    return v


def test_chart_fqdn_egress_includes_l1_and_external_siem_hosts():
    """Acceptance 1 + 6: the rendered adapter-fqdn-egress CiliumNetworkPolicy
    carries the L1 host (from soctalkSystem.url) AND every external SIEM host
    (from networkPolicies.externalSiemHosts) under toFQDNs."""
    values = _provided_values_for_chart(
        indexer_url="https://indexer.siem.acme.example:9200",
        api_url="https://manager.siem.acme.example:55000",
        soctalk_url="https://l1.mssp.example",
    )
    # Precondition: render produced the external host list the chart consumes.
    assert set(values["networkPolicies"]["externalSiemHosts"]) == {
        "indexer.siem.acme.example",
        "manager.siem.acme.example",
    }

    names = _fqdn_egress_match_names(_helm_template(values))
    assert names is not None, "adapter-fqdn-egress was not emitted"
    assert "l1.mssp.example" in names  # L1 host
    assert "indexer.siem.acme.example" in names  # external SIEM indexer
    assert "manager.siem.acme.example" in names  # external SIEM API


def test_chart_fqdn_egress_emitted_for_external_hosts_without_l1_url():
    """Acceptance 1 + 2 boundary: external SIEM hosts alone (soctalkSystem.url
    empty) still emit the policy — the skip only triggers when BOTH are empty."""
    values = _provided_values_for_chart(
        indexer_url="https://indexer.siem.acme.example:9200",
        api_url="https://manager.siem.acme.example:55000",
        soctalk_url="",
    )
    names = _fqdn_egress_match_names(_helm_template(values))
    assert names is not None, "adapter-fqdn-egress should emit for SIEM hosts"
    assert "indexer.siem.acme.example" in names
    assert "manager.siem.acme.example" in names
    assert "l1.mssp.example" not in names  # no L1 url ⇒ no L1 entry


def test_chart_fqdn_egress_skipped_when_no_hosts():
    """Acceptance 2: with externalSiemHosts empty AND soctalkSystem.url empty,
    the CiliumNetworkPolicy is not emitted (existing skip behavior preserved)
    even though the 'provided' profile forces fqdnEgress.enabled=true."""
    values = _provided_values_for_chart(
        indexer_url="https://indexer.siem.acme.example:9200",
        api_url="https://manager.siem.acme.example:55000",
        soctalk_url="",
    )
    # Force the exact skip precondition: no external hosts, no L1 url, while
    # leaving fqdnEgress enabled — proves the gate skips on host-emptiness,
    # not just on the toggle. (A real 'provided' tenant always has hosts, so
    # the adapter indexer URL is kept valid for the schema check.)
    values["networkPolicies"]["externalSiemHosts"] = []
    values["soctalkSystem"]["url"] = ""
    assert values["networkPolicies"]["fqdnEgress"]["enabled"] is True

    names = _fqdn_egress_match_names(_helm_template(values))
    assert names is None, "adapter-fqdn-egress must be skipped when no hosts"


def test_chart_renders_without_cilium_crds(  # issue #107
):
    """A stock ``soctalk install`` k3s runs flannel with NO Cilium CRDs; the
    provided profile force-enables fqdnEgress, and the whole release used to
    fail to build ("no matches for kind CiliumNetworkPolicy"). The
    Capabilities guard must suppress the object so the render SUCCEEDS, and
    IP-literal SIEM hosts must still get plain ipBlock egress rules on BOTH
    the adapter and the runs-worker (the worker dials the external SIEM for
    MCP enrichment, issue #109)."""
    values = _provided_values_for_chart(
        indexer_url="https://198.51.100.20:9200",
        api_url="https://198.51.100.20:55000",
        soctalk_url="",
    )
    manifests = _helm_template(values, cilium=False)  # would have raised before

    assert _fqdn_egress_match_names(manifests) is None, (
        "CiliumNetworkPolicy must not be emitted without the CRD"
    )

    def _ipblock_cidrs(policy_name: str) -> set[str]:
        for m in manifests:
            if (
                m.get("kind") == "NetworkPolicy"
                and m["metadata"]["name"] == policy_name
            ):
                return {
                    to["ipBlock"]["cidr"]
                    for rule in m["spec"].get("egress", [])
                    for to in rule.get("to", [])
                    if "ipBlock" in to
                }
        raise AssertionError(f"{policy_name} not found")

    assert "198.51.100.20/32" in _ipblock_cidrs("adapter-egress")
    assert "198.51.100.20/32" in _ipblock_cidrs("runs-worker-egress")


def test_chart_ipblock_skips_fqdn_hosts():  # issue #107
    """FQDN SIEM hosts are not expressible as ipBlocks; they must be left to
    the Cilium policy (or the operator) and never rendered as a bogus
    ``hostname/32`` CIDR."""
    values = _provided_values_for_chart(
        indexer_url="https://indexer.siem.acme.example:9200",
        api_url="https://manager.siem.acme.example:55000",
        soctalk_url="",
    )
    manifests = _helm_template(values, cilium=False)
    rendered = str(manifests)
    assert "indexer.siem.acme.example/32" not in rendered
    assert "manager.siem.acme.example/32" not in rendered


@pytest.mark.parametrize("profile", ["poc", "persistent", "provided"])
@pytest.mark.parametrize("verify", [True, False])
def test_verify_ssl_flows_from_integration_for_all_profiles(profile, verify):
    """``adapter.wazuhIndexer.verifySsl`` mirrors ``integration.wazuh_verify_ssl``
    only for the ``provided`` profile — the two in-cluster profiles (``poc``,
    ``persistent``) always emit ``verifySsl: false`` because the bundled Wazuh
    subchart ships a self-signed indexer cert with no operator-facing way to
    swap it. The adapter would fail every ingest if verify were True.
    """
    t = _make_tenant(profile)
    integration = _make_integration(t.id)
    integration.wazuh_verify_ssl = verify
    # 'provided' derives its external-SIEM shape from the indexer URL.
    integration.wazuh_indexer_url = "https://indexer.siem.example:9200"
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile=profile,
    )
    expected = verify if profile == "provided" else False
    assert v["adapter"]["wazuhIndexer"]["verifySsl"] is expected


def test_tenant_identity_always_rendered():
    """Regardless of profile, tenant / branding / llm blocks are filled."""
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id="11111111-1111-1111-1111-111111111111",
        install_id="22222222-2222-2222-2222-222222222222",
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v["tenant"]["slug"] == "acme"
    assert v["tenant"]["msspId"] == "11111111-1111-1111-1111-111111111111"
    assert v["branding"]["appName"] == "Acme SOC"
    assert v["branding"]["primaryColor"] == "#112233"
    # apiKeyRef points at the tenant-namespace Secret (always
    # ``tenant-llm-key``); ``llm_secret_name`` names the Secret in
    # ``soctalk-system`` that the controller mirrors from.
    assert v["llm"]["apiKeyRef"]["name"] == "tenant-llm-key"


def test_llm_api_key_propagated_to_chart_values():
    """When the integration row holds a plaintext key, the rendered
    values pass it through as ``llm.apiKey`` so the chart's secret
    template materializes ``tenant-llm-key`` with the actual key.

    Regression: previously the renderer dropped ``llm_api_key_plain``
    on the floor, the chart's ``{{- if .Values.llm.apiKey }}`` guard
    skipped the Secret, and the runs-worker mounted an empty
    ``secretKeyRef`` — triage would fail with "No LLM API key
    configured" on every alert until an admin PATCHed the LLM endpoint
    post-install.
    """
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.llm_api_key_plain = "sk-test-llm-key-deadbeef"
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v["llm"]["apiKey"] == "sk-test-llm-key-deadbeef"


def test_llm_api_key_empty_when_unset():
    """Empty plaintext renders as empty string, not absent. The chart
    treats empty + present as "operator pre-provisions the Secret",
    matching the legacy collapsed-tier contract."""
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),  # no llm_api_key_plain
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v["llm"]["apiKey"] == ""


def test_llm_api_key_suppressed_on_controller_path():
    """``include_llm_api_key=False`` (the L1 controller path) renders
    ``llm.apiKey`` as "" even when the integration row holds a key.

    Regression: the controller writes ``Secret/tenant-llm-key`` directly
    in apply_secrets (no Helm ownership metadata). When a per-tenant key
    was set at onboard, the renderer passed the plaintext through,
    the chart's ``{{- if .Values.llm.apiKey }}`` guard fired, and helm
    refused to install: 'Secret "tenant-llm-key" ... exists and cannot
    be imported into the current release: invalid ownership metadata'.
    The controller path must keep a single Secret owner (the controller);
    the plaintext-through-values path is reserved for the cross-cluster
    L2 install-spec where no controller pre-writes Secrets.
    """
    t = _make_tenant("provided")
    integration = _make_integration(t.id)
    integration.llm_api_key_plain = "sk-test-llm-key-deadbeef"
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="provided",
        include_llm_api_key=False,
    )
    assert v["llm"]["apiKey"] == ""
    # The mount reference is untouched — the runs-worker still reads the
    # controller-written Secret.
    assert v["llm"]["apiKeyRef"]["name"] == "tenant-llm-key"


@pytest.mark.parametrize("profile", ["poc", "persistent"])
def test_bundled_siem_disabled_on_controller_path(profile):
    """``bundled_siem=False`` (the L1 in-cluster controller path) renders
    ``components.wazuh.enabled`` False even when the integration row enables
    Wazuh, because the controller installs a SEPARATE ``wazuh-<slug>`` release.

    Regression: for poc/persistent the L1 controller both (a) rendered the
    tenant chart with the bundled Wazuh subchart ON and (b) ran
    ``_step_helm_apply_wazuh`` to install the standalone ``wazuh-<slug>``
    release. Result: two full Wazuh stacks in one namespace
    (``tenant-<slug>-wazuh-*`` orphaned + ``wazuh-<slug>-wazuh-*`` in use) —
    double the manager/indexer/dashboard footprint, enough to exhaust a
    right-sized poc node's RAM/quota and block provisioning. The adapter,
    runs-worker and linux-ep all target ``wazuh-<slug>-wazuh-*``, so the
    bundled copy is pure waste on this path.
    """
    t = _make_tenant(profile)
    integration = _make_integration(t.id)
    integration.wazuh_enabled = True
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile=profile,
        bundled_siem=False,
    )
    assert v["components"]["wazuh"]["enabled"] is False
    # The default (cross-cluster L2 single-release install-spec path) still
    # bundles the SIEM — that IS the tenant's Wazuh there.
    v_bundled = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile=profile,
    )
    assert v_bundled["components"]["wazuh"]["enabled"] is True


# ---------------------------------------------------------------------------
# runsWorker model overrides (tenant.llm.models.render)
# ---------------------------------------------------------------------------


def test_runs_worker_model_overrides_rendered_when_set():
    """Per-tenant ``llm_fast_model`` / ``llm_reasoning_model`` overrides flow
    into ``runsWorker.fastModel`` / ``runsWorker.reasoningModel`` — the chart
    maps those to SOCTALK_FAST_MODEL / SOCTALK_REASONING_MODEL on the
    runs-worker (35-runs-worker.yaml), so no chart edit is needed."""
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.llm_fast_model = "gpt-4o-mini"
    integration.llm_reasoning_model = "o3"
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v["runsWorker"]["fastModel"] == "gpt-4o-mini"
    assert v["runsWorker"]["reasoningModel"] == "o3"
    # llm.model itself is untouched by the overrides.
    assert v["llm"]["model"] == "gpt-4o"
    _assert_validates_against_tenant_schema(v)


def test_runs_worker_models_fall_back_to_llm_model_when_null():
    """NULL overrides preserve today's behavior: both runsWorker models
    render as ``integration.llm_model`` for every existing tenant row."""
    t = _make_tenant("poc")
    integration = _make_integration(t.id)  # llm_fast/reasoning_model both NULL
    assert integration.llm_fast_model is None
    assert integration.llm_reasoning_model is None
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v["runsWorker"]["fastModel"] == "gpt-4o"
    assert v["runsWorker"]["reasoningModel"] == "gpt-4o"


def test_runs_worker_models_treat_empty_string_as_unset():
    """A cleared override may be stored as '' instead of NULL; render time
    must treat both identically and fall back to llm_model."""
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.llm_fast_model = ""
    integration.llm_reasoning_model = ""
    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert v["runsWorker"]["fastModel"] == "gpt-4o"
    assert v["runsWorker"]["reasoningModel"] == "gpt-4o"


# ---------------------------------------------------------------------------
# render_wazuh_values: per-tenant layer
# ---------------------------------------------------------------------------


def test_wazuh_values_carry_minted_creds():
    t = _make_tenant("poc")
    v = render_wazuh_values(
        tenant=t,
        profile="poc",
        admin_password="a-random-admin-pw",
        authd_password="a-random-authd-pw",
    )
    # Api password is the minted one; indexer stays at demo `admin`
    # until internal_users.yml override lands (documented in render.py).
    assert v["credentials"]["apiPassword"] == "a-random-admin-pw"
    assert v["credentials"]["authdPassword"] == "a-random-authd-pw"
    assert v["credentials"]["indexerPassword"] == "admin"
    assert v["tenant"]["slug"] == "acme"
    assert v["tenant"]["profile"] == "poc"


def test_wazuh_values_storage_override_only_for_persistent():
    t = _make_tenant("persistent")
    v = render_wazuh_values(
        tenant=t,
        profile="persistent",
        admin_password="pw",
        authd_password="pw",
        storage_class_override="standard",
    )
    assert v["storage"]["storageClass"] == "standard"


def test_wazuh_values_no_storage_override_for_poc():
    """PoC profile relies on the chart's values.poc.yaml for storage."""
    t = _make_tenant("poc")
    v = render_wazuh_values(
        tenant=t,
        profile="poc",
        admin_password="pw",
        authd_password="pw",
        storage_class_override=None,
    )
    # Per-tenant layer should NOT push a storageClass; the profile values
    # file owns that default.
    assert "storage" not in v


# ---------------------------------------------------------------------------
# TriagePolicy provisioning (issue #44 level 2: chart + render wiring)
# ---------------------------------------------------------------------------

_VALID_TRIAGE_POLICY_YAML = """\
id: custom-ops-noise
version: 1
priority: 90
applies_to:
  rule_groups: [opsnoise]
guardrails:
  - when:
      "==": [{"var": "verdict"}, "close"]
    effect: override
    to: needs_more_info
    reason: second look on this class
"""


def _values_with_triage_policies() -> dict:
    t = _make_tenant()
    v = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
    )
    return v


def test_render_triage_policy_values_env_gated_and_validated(tmp_path, monkeypatch):
    from soctalk.core.provisioning.render import render_triage_policy_values

    # unset env -> {}
    monkeypatch.delenv("SOCTALK_TENANT_TRIAGE_POLICIES_DIR", raising=False)
    assert render_triage_policy_values("acme") == {}

    (tmp_path / "good.yaml").write_text(_VALID_TRIAGE_POLICY_YAML)
    (tmp_path / "bad.yaml").write_text("id: broken\nbogus_field: 1\n")
    (tmp_path / "foreign.yaml").write_text(
        "id: other-tenant-pb\ntenant: not-acme\n"
        "applies_to:\n  rule_groups: [x]\n"
    )
    monkeypatch.setenv("SOCTALK_TENANT_TRIAGE_POLICIES_DIR", str(tmp_path))
    out = render_triage_policy_values("acme")
    assert list(out) == ["good.yaml"], "invalid + foreign files must not ship"
    assert "custom-ops-noise" in out["good.yaml"]

    # and render_tenant_values threads it into runsWorker.triagePolicies
    v = _values_with_triage_policies()
    assert list(v["runsWorker"]["triagePolicies"]) == ["good.yaml"]


def test_chart_renders_triage_policies_configmap_mount_env_checksum(monkeypatch):
    monkeypatch.delenv("SOCTALK_TENANT_TRIAGE_POLICIES_DIR", raising=False)
    v = _values_with_triage_policies()
    v["runsWorker"]["triagePolicies"] = {"good.yaml": _VALID_TRIAGE_POLICY_YAML}
    manifests = _helm_template(v)

    cm = next(
        m for m in manifests
        if m["kind"] == "ConfigMap" and m["metadata"]["name"] == "soctalk-triage-policies"
    )
    assert "custom-ops-noise" in cm["data"]["good.yaml"]
    # the ConfigMap content must round-trip as valid YAML for the loader
    import yaml as _yaml
    parsed = _yaml.safe_load(cm["data"]["good.yaml"])
    assert parsed["id"] == "custom-ops-noise"
    assert parsed["guardrails"][0]["effect"] == "override"

    deploy = next(
        m for m in manifests
        if m["kind"] == "Deployment"
        and m["metadata"]["name"] == "soctalk-runs-worker"
    )
    pod = deploy["spec"]["template"]
    assert pod["metadata"]["annotations"]["checksum/triage-policies"]
    container = pod["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["SOCTALK_TRIAGE_POLICY_DIR"] == "/etc/soctalk/triage-policies"
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts["triage-policies"] == "/etc/soctalk/triage-policies"
    volumes = {vol["name"] for vol in pod["spec"]["volumes"]}
    assert "triage-policies" in volumes


def test_chart_without_triage_policies_renders_nothing_new(monkeypatch):
    monkeypatch.delenv("SOCTALK_TENANT_TRIAGE_POLICIES_DIR", raising=False)
    v = _values_with_triage_policies()
    assert v["runsWorker"]["triagePolicies"] == {}
    manifests = _helm_template(v)
    assert not any(
        m["kind"] == "ConfigMap" and m["metadata"]["name"] == "soctalk-triage-policies"
        for m in manifests
    )
    deploy = next(
        m for m in manifests
        if m["kind"] == "Deployment"
        and m["metadata"]["name"] == "soctalk-runs-worker"
    )
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    env_names = {e["name"] for e in container["env"]}
    assert "SOCTALK_TRIAGE_POLICY_DIR" not in env_names


def test_render_triage_policy_values_codex_fixes(tmp_path, monkeypatch):
    """Codex provisioning-review fixes: UUID tenant scoping works; a filename
    the chart schema would reject is skipped (not shipped to fail helm); the
    per-tenant total budget drops overflow files; content is validated from the
    same read that ships."""
    from soctalk.core.provisioning.render import render_triage_policy_values

    tenant_id = "0d4a2566-100a-42fd-8cc9-adac6e276691"
    (tmp_path / "byid.yaml").write_text(
        f"id: id-scoped\ntenant: {tenant_id}\napplies_to:\n  rule_groups: [x]\n"
    )
    (tmp_path / "odd.yamml").write_text(_VALID_TRIAGE_POLICY_YAML)  # glob-matches, schema-invalid name
    big = _VALID_TRIAGE_POLICY_YAML + "# pad\n" * 10000  # ~60KB, valid but budget fodder
    for n in range(14):
        (tmp_path / f"pad{n:02d}.yaml").write_text(
            big.replace("custom-ops-noise", f"pad-{n:02d}")
        )
    monkeypatch.setenv("SOCTALK_TENANT_TRIAGE_POLICIES_DIR", str(tmp_path))

    out = render_triage_policy_values("acme", tenant_id)
    assert "byid.yaml" in out, "UUID-scoped triage policy must ship to its tenant"
    assert "odd.yamml" not in out, "schema-rejected filename must be skipped"
    total = sum(len(v.encode()) for v in out.values())
    assert total <= 800 * 1024, "total payload must respect the ConfigMap budget"
    assert len(out) < 15, "budget must have dropped overflow files"

    # foreign tenant: neither slug nor id matches -> not shipped
    out2 = render_triage_policy_values("other", "11111111-2222-3333-4444-555555555555")
    assert "byid.yaml" not in out2


# ---------------------------------------------------------------------------
# runsWorker.wazuh — the worker's Wazuh MCP enrichment wiring (issue #109)
# ---------------------------------------------------------------------------


def test_runs_worker_wazuh_wired_to_external_siem_for_provided():
    # A provided tenant's worker must enrich against the EXTERNAL Wazuh:
    # manager API URL + indexer host from the integration row, creds from the
    # controller-managed tenant-external-siem-creds Secret, TLS per the row.
    t = _make_tenant("provided")
    integration = _make_integration(t.id)
    integration.wazuh_enabled = True
    integration.wazuh_indexer_url = "https://198.51.100.20:9200"
    integration.wazuh_api_url = "https://198.51.100.20:55000"
    integration.wazuh_verify_ssl = True

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="provided",
    )

    w = v["runsWorker"]["wazuh"]
    assert w["enabled"] is True
    assert w["apiUrl"] == "https://198.51.100.20:55000"
    assert w["indexerHost"] == "198.51.100.20"
    assert w["indexerPort"] == 9200
    assert w["credsSecret"] == "tenant-external-siem-creds"
    # Only 'provided' honours the operator's TLS preference.
    assert w["verifySsl"] is True


def test_runs_worker_wazuh_wired_to_in_cluster_siem_for_poc():
    # Managed profiles point the worker at the wazuh subchart's Services and
    # the chart-minted creds Secret; in-cluster certs are self-signed, so
    # verifySsl is forced off regardless of the DB row.
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.wazuh_enabled = True
    integration.wazuh_verify_ssl = True  # must NOT leak into the worker env

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )

    w = v["runsWorker"]["wazuh"]
    assert w["enabled"] is True
    assert w["apiUrl"] == f"https://wazuh-{t.slug}-wazuh-manager:55000"
    assert w["indexerHost"] == f"wazuh-{t.slug}-wazuh-indexer"
    assert w["indexerPort"] == 9200
    assert w["credsSecret"] == f"wazuh-{t.slug}-wazuh-creds"
    assert w["verifySsl"] is False


def test_runs_worker_wazuh_disabled_when_integration_disabled():
    t = _make_tenant("poc")
    integration = _make_integration(t.id)
    integration.wazuh_enabled = False

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )

    assert v["runsWorker"]["wazuh"]["enabled"] is False


def test_runs_worker_wazuh_disabled_when_provided_urls_missing():
    # A provided row without connection material must not enable the MCP with
    # empty targets (create_wazuh_mcp_config would warn-and-skip anyway, but
    # the render should not ask for it in the first place).
    t = _make_tenant("provided")
    integration = _make_integration(t.id)
    integration.wazuh_enabled = True
    integration.wazuh_indexer_url = None
    integration.wazuh_api_url = None

    v = render_tenant_values(
        tenant=t,
        integration=integration,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="provided",
    )

    w = v["runsWorker"]["wazuh"]
    assert w["enabled"] is False
    assert w["apiUrl"] == ""
    assert w["indexerHost"] == ""


def test_chart_fqdn_egress_covers_worker_and_adapter():  # codex P1 on #109
    """The CiliumNetworkPolicy must select BOTH the adapter and the
    runs-worker: with an FQDN external SIEM, the worker's MCP enrichment
    egress is only expressible via toFQDNs, so an adapter-only selector
    silently blocks enrichment on Cilium clusters."""
    values = _provided_values_for_chart(
        indexer_url="https://indexer.siem.acme.example:9200",
        api_url="https://manager.siem.acme.example:55000",
        soctalk_url="",
    )
    manifests = _helm_template(values)
    cnp = next(
        m for m in manifests if m.get("kind") == "CiliumNetworkPolicy"
    )
    exprs = cnp["spec"]["endpointSelector"]["matchExpressions"]
    name_expr = next(
        e for e in exprs if e["key"] == "app.kubernetes.io/name"
    )
    assert set(name_expr["values"]) == {
        "soctalk-adapter",
        "soctalk-runs-worker",
    }


def test_price_overlay_is_no_longer_rendered_into_worker_env():
    """The env overlay is retired (#125), so the chart must stop carrying it.

    Prices now resolve from the catalog when a run is created and ride on the
    run row, so a price correction takes effect on the next run instead of
    needing a helm upgrade and a pod restart. A tenant that still has an
    override keeps it — the resolver reads the column directly — but nothing
    renders it into ``SOCTALK_MODEL_PRICES`` any more.
    """
    t = _make_tenant("poc")
    priced = _make_integration(t.id)
    priced.llm_model_prices = {"deepseek-v4-flash": {"input": 0.206, "output": 0.412}}
    v = render_tenant_values(
        tenant=t,
        integration=priced,
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert "modelPrices" not in v["llm"]


# --- external SIEM egress PORTS (issue #147) --------------------------------
#
# The standard NetworkPolicy — the only one that applies on a non-Cilium
# cluster — used to hardcode 9200/55000 while deriving only the host from the
# tenant's URLs. A BYO Wazuh published on any other port (NodePort, load
# balancer, reverse proxy) was then dropped at L3/L4 while the tenant still
# reported healthy. These lock the port in.


def _endpoints_for(indexer_url: str | None, api_url: str | None):
    from soctalk.core.provisioning.render import _external_siem_endpoints

    t = _make_tenant()
    integration = _make_integration(t.id)
    integration.wazuh_indexer_url = indexer_url
    integration.wazuh_api_url = api_url
    return _external_siem_endpoints(integration)


def test_external_siem_endpoints_keep_non_standard_ports():
    """A BYO Wazuh behind a NodePort must yield its ACTUAL ports."""
    eps = _endpoints_for("https://10.0.2.2:31437", "https://10.0.2.2:30442")
    assert eps == [
        {"host": "10.0.2.2", "port": 31437},
        {"host": "10.0.2.2", "port": 30442},
    ]


def test_external_siem_endpoints_default_per_endpoint_when_port_omitted():
    """No explicit port → the well-known port for THAT endpoint, not 443."""
    eps = _endpoints_for("https://siem.example.com", "https://siem.example.com")
    assert eps == [
        {"host": "siem.example.com", "port": 9200},
        {"host": "siem.example.com", "port": 55000},
    ]


def test_external_siem_endpoints_dedupe_on_host_and_port():
    """Same host AND port from both URLs collapses to one egress rule."""
    eps = _endpoints_for("https://siem.example.com:9200", "https://siem.example.com:9200")
    assert eps == [{"host": "siem.example.com", "port": 9200}]


def test_external_siem_endpoints_skip_missing_urls():
    assert _endpoints_for(None, None) == []
    assert _endpoints_for("https://only.indexer:9200", None) == [
        {"host": "only.indexer", "port": 9200}
    ]


def test_provided_values_carry_external_siem_endpoints():
    """render_tenant_values must surface the endpoints for the chart."""
    values = _provided_values_for_chart(
        indexer_url="https://10.0.2.2:31437",
        api_url="https://10.0.2.2:30442",
        soctalk_url="https://l1.example.com",
    )
    assert values["networkPolicies"]["externalSiemEndpoints"] == [
        {"host": "10.0.2.2", "port": 31437},
        {"host": "10.0.2.2", "port": 30442},
    ]


def test_in_cluster_profiles_emit_no_external_siem_endpoints():
    """poc/persistent must be untouched — no external egress rules."""
    t = _make_tenant()
    values = render_tenant_values(
        tenant=t,
        integration=_make_integration(t.id),
        branding=_make_branding(t.id),
        mssp_id=str(uuid4()),
        install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm",
        profile="poc",
    )
    assert values["networkPolicies"]["externalSiemEndpoints"] == []


def test_external_siem_endpoints_reject_malformed_explicit_port():
    """An explicitly bad port must fail loudly, not silently become 9200.

    Substituting the well-known port would render an egress rule for a port the
    adapter never dials — the same "connects to nothing while looking healthy"
    failure this derivation exists to prevent.
    """
    import pytest

    with pytest.raises(ValueError, match="invalid port"):
        _endpoints_for("https://siem.example.com:not-a-port", None)



def test_explicit_tenant_tag_does_not_need_package_metadata(monkeypatch):
    """An explicit env tag must win WITHOUT computing the version fallback.

    The fallback was first written as `os.getenv(VAR, _default_tenant_image_tag())`,
    which Python evaluates eagerly — so an install whose package metadata is
    missing (source on PYTHONPATH rather than pip-installed) raised even when the
    operator had set the tag explicitly, leaving them no way out.
    """
    import soctalk.core.provisioning.render as render_mod

    def _boom() -> str:
        raise RuntimeError("fallback must not be evaluated when the env is set")

    monkeypatch.setattr(render_mod, "_default_tenant_image_tag", _boom)
    for var in (
        "SOCTALK_TENANT_ADAPTER_IMAGE_TAG",
        "SOCTALK_TENANT_RUNS_WORKER_IMAGE_TAG",
        "SOCTALK_TENANT_LINUX_EP_IMAGE_TAG",
    ):
        monkeypatch.setenv(var, "9.9.9")
    t = _make_tenant("poc")
    v = render_tenant_values(
        tenant=t, integration=_make_integration(t.id), branding=_make_branding(t.id),
        mssp_id=str(uuid4()), install_id=str(uuid4()),
        llm_secret_name="tenant-x-llm", profile="poc",
    )
    assert v["adapter"]["image"]["tag"] == "9.9.9"
    assert v["runsWorker"]["image"]["tag"] == "9.9.9"
    assert v["linuxep"]["image"]["tag"] == "9.9.9"
