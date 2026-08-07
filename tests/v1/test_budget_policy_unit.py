"""Per-run dollar budget resolution and budget-key scoping (#128).

DB-free: the resolver's failure paths and the install clamp are exactly the
parts that must hold when the database is unavailable or half-migrated.
"""
from __future__ import annotations

import pytest

from soctalk.core.ir import policies


class _ExplodingSession:
    """Stands in for a session whose query fails mid-rollout."""

    async def execute(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("relation \"tenant_policies\" does not exist")


class _FakeSession:
    def __init__(self, rows: dict):
        self._rows = rows

    async def execute(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        rows = [{"key": k, "value": v} for k, v in self._rows.items()]

        class _R:
            def mappings(self_inner):  # noqa: ANN001
                class _M:
                    def all(self_m):  # noqa: ANN001
                        return rows

                return _M()

        return _R()


def test_dollar_budget_install_default_matches_the_graph_default():
    """An install that sets no policy must behave as it did before the key."""
    assert policies.run_dollar_budget_default() == 5.0


def test_dollar_budget_cap_is_env_tunable(monkeypatch):
    monkeypatch.setenv("SOCTALK_RUN_DOLLAR_BUDGET_MAX", "25")
    assert policies.run_dollar_budget_max() == 25.0
    # Junk and non-positive values fall back rather than disabling the cap.
    monkeypatch.setenv("SOCTALK_RUN_DOLLAR_BUDGET_MAX", "not-a-number")
    assert policies.run_dollar_budget_max() == 1_000.0
    monkeypatch.setenv("SOCTALK_RUN_DOLLAR_BUDGET_MAX", "0")
    assert policies.run_dollar_budget_max() == 1_000.0


@pytest.mark.asyncio
async def test_dollar_budget_resolution_never_breaks_run_creation():
    """A failed lookup yields the default, not an exception.

    dollars_budget is NOT NULL with a plausible default, so unlike
    price_snapshot a failure here is invisible downstream — it must not be
    allowed to stop a run from being created (Codex review, finding 8).
    """
    from uuid import uuid4

    got = await policies.resolve_run_dollar_budget(_ExplodingSession(), uuid4())
    assert got == 5.0


@pytest.mark.asyncio
async def test_tenant_override_is_clamped_to_the_install_cap(monkeypatch):
    from uuid import uuid4

    monkeypatch.setenv("SOCTALK_RUN_DOLLAR_BUDGET_MAX", "10")
    db = _FakeSession({policies.RUN_DOLLAR_BUDGET_KEY: 999.0})
    assert await policies.resolve_run_dollar_budget(db, uuid4()) == 10.0


@pytest.mark.asyncio
async def test_non_finite_override_cannot_disable_the_cap():
    """inf or NaN would read as "set" while removing the ceiling entirely."""
    from uuid import uuid4

    for bad in (float("inf"), float("nan")):
        db = _FakeSession({policies.RUN_DOLLAR_BUDGET_KEY: bad})
        assert await policies.resolve_run_dollar_budget(db, uuid4()) == 5.0


def test_budget_keys_are_stripped_from_the_lower_policy_layers():
    """Ceilings are install/tenant scope only.

    RLS scopes which policy ROWS a tenant touches, not which keys or values
    they carry, so stripping is what actually prevents a raised ceiling
    (Codex review, finding 5).
    """
    layer = {
        "max_dollars_per_investigation": 10_000,
        "max_tokens_per_investigation": 10_000_000,
        "max_dollars_per_24h": 99_999,
        "auto_close_enabled": False,
    }
    kept = policies._without_budget_keys(layer)
    assert kept == {"auto_close_enabled": False}
    assert policies._without_budget_keys(None) == {}
