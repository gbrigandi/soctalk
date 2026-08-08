"""POST /api/mssp/tenants payload strictness (issue #110). DB-free."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from soctalk.core.api.tenants import TenantCreate


def test_simple_create_rejects_wizard_fields():
    # Sending the WIZARD payload to the identity-only create used to 201 a
    # default-poc tenant with profile/external_siem silently dropped, burning
    # the slug. extra="forbid" turns that into a 422 naming the stray fields.
    with pytest.raises(ValidationError) as ei:
        TenantCreate(
            slug="orion-soc",
            display_name="Orion Labs",
            profile="provided",
            external_siem={"indexer_url": "https://x:9200"},
            llm_api_key="sk-x",
        )
    msg = str(ei.value)
    assert "profile" in msg
    assert "external_siem" in msg


def test_simple_create_still_accepts_its_own_fields():
    p = TenantCreate(
        slug="orion-soc",
        display_name="Orion Labs",
        llm_base_url="https://api.anthropic.com",
        llm_model="claude-sonnet-4-6",
        wazuh_url=None,
        branding_app_name="Orion",
    )
    assert p.slug == "orion-soc"
