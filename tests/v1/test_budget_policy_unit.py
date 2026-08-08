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


# --- 24h ceilings, per tenant (#129) ---------------------------------------


def test_daily_cap_status_reports_headroom_and_which_dimension_tripped():
    from soctalk.core.cost import DailyCaps, DailyCapStatus, TenantDailySpend

    caps = DailyCaps(tokens=10_000_000, dollars=50.0)
    under = DailyCapStatus(TenantDailySpend(tokens=9_000_000, dollars=12.0), caps)
    assert not under.cap_hit
    assert under.tokens_remaining == 1_000_000
    assert under.dollars_remaining == 38.0
    assert under.reason is None

    by_tokens = DailyCapStatus(TenantDailySpend(tokens=10_000_000, dollars=1.0), caps)
    assert by_tokens.cap_hit and by_tokens.token_cap_hit
    assert "tokens" in (by_tokens.reason or "")

    by_dollars = DailyCapStatus(TenantDailySpend(tokens=1, dollars=50.0), caps)
    assert by_dollars.cap_hit and by_dollars.dollar_cap_hit
    assert "spend" in (by_dollars.reason or "")
    # Headroom never goes negative, even past the cap.
    assert by_dollars.dollars_remaining == 0.0


@pytest.mark.asyncio
async def test_daily_caps_fall_back_to_install_defaults_when_unresolvable():
    """Failing open on a spend ceiling is the expensive direction."""
    from uuid import uuid4

    from soctalk.core import cost

    caps = await cost.resolve_tenant_daily_caps(_ExplodingSession(), uuid4())
    assert caps.tokens == cost.tenant_daily_token_cap()
    assert caps.dollars == cost.tenant_daily_dollar_cap()


@pytest.mark.asyncio
async def test_daily_cap_override_is_clamped_and_junk_is_ignored(monkeypatch):
    from uuid import uuid4

    from soctalk.core import cost

    monkeypatch.setenv("SOCTALK_TENANT_DAILY_DOLLAR_CAP_MAX", "100")
    db = _FakeSession({cost.DAILY_DOLLAR_CAP_KEY: 999_999, cost.DAILY_TOKEN_CAP_KEY: "junk"})
    caps = await cost.resolve_tenant_daily_caps(db, uuid4())
    assert caps.dollars == 100.0
    # An unreadable token cap leaves the install default in place.
    assert caps.tokens == cost.tenant_daily_token_cap()


def test_daily_status_is_what_reports_a_cap_hit_not_the_spend_object():
    """TenantDailySpend.cap_hit only knows the install env values.

    Caught live: the unlock endpoint used it, so a tenant whose 24h ceiling was
    lowered by policy to $0.01 while env said $50 was reported as having no cap
    problem — the exact case where an operator most needs telling, because the
    run they just unlocked will not be claimed.
    """
    from soctalk.core.cost import DailyCaps, DailyCapStatus, TenantDailySpend

    spend = TenantDailySpend(tokens=100, dollars=0.05265)
    # Env-shaped view: nothing looks wrong.
    assert not spend.dollar_cap_hit
    # Resolved per-tenant view: the ceiling is blown.
    status = DailyCapStatus(spend, DailyCaps(tokens=10_000_000, dollars=0.01))
    assert status.cap_hit and status.dollar_cap_hit
    assert "0.01" in (status.reason or "")


# --- Codex review fixes ----------------------------------------------------


def test_provider_reported_cost_is_validated_before_it_is_trusted():
    """This figure overrides our arithmetic AND gates the budget halt.

    A negative, NaN or infinite value from a provider would under-bill, and an
    inf would stop over_budget from ever tripping. A bad number is discarded so
    the call degrades to our own estimate, not to no ceiling at all
    (Codex review, finding 7).
    """
    from soctalk.core.pricing.usage import canonical_usage

    for bad in (float("nan"), float("inf"), -1.0):
        u = canonical_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": bad}}
        )
        assert u.actual_cost_usd is None, bad
    good = canonical_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.5}}
    )
    assert good.actual_cost_usd == 0.5
    # Zero is a legitimate actual (free tier), not a bad number.
    zero = canonical_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0}}
    )
    assert zero.actual_cost_usd == 0.0


def test_a_non_finite_budget_cannot_survive_the_halt_check():
    """Why NaN must never reach dollars_budget: it disables the ceiling.

    over_budget compares used >= budget; every comparison against NaN is
    False, so a run carrying a NaN budget can never halt (Codex review,
    finding 6 — the unlock endpoint now rejects it at the edge).
    """
    from soctalk.graph import budget

    state = {
        "tokens_used": 0,
        "tokens_budget": 200_000,
        "dollars_used": 1_000_000.0,
        "dollars_budget": float("nan"),
    }
    assert budget.over_budget(state) is False


# --- Codex review round 3 ---------------------------------------------------


def test_duplicate_model_keys_are_rejected_after_normalization():
    """Keys are stripped, so "gpt-4o" and " gpt-4o " collapse to one.

    Keeping the last silently would mis-price a tenant while the caller
    believes both entries took effect (Codex review round 3, finding 3).
    """
    from soctalk.core.tenancy.models import validate_llm_model_prices

    with pytest.raises(ValueError, match="duplicate"):
        validate_llm_model_prices(
            {
                "gpt-4o": {"input": 1, "output": 2},
                " gpt-4o ": {"input": 9, "output": 9},
            }
        )
    ok = validate_llm_model_prices(
        {"a": {"input": 1, "output": 2}, "b": {"input": 3, "output": 4}}
    )
    assert set(ok) == {"a", "b"}


def test_money_fields_reject_bool_on_every_write_path():
    """A JSON `true` must never become a $1.00 ceiling, on any endpoint."""
    from pydantic import ValidationError

    from soctalk.core.api.run_budget import RunBudgetUpdate, RunUnlockRequest

    for model, field in (
        (RunBudgetUpdate, "dollar_override"),
        (RunBudgetUpdate, "daily_dollar_override"),
        (RunUnlockRequest, "dollar_budget"),
    ):
        with pytest.raises(ValidationError):
            model(**{field: True})
        # A real number still works.
        assert getattr(model(**{field: 2.5}), field) == 2.5


def test_investigation_list_reports_whether_more_pages_exist():
    """The UI's Next button keys off has_more, which was never returned."""
    from soctalk.core.api.investigations_bridge import InvestigationList

    assert InvestigationList(items=[], total=0, page=1, page_size=25).has_more is False
    full = InvestigationList(items=[], total=60, page=1, page_size=25, has_more=True)
    assert full.has_more is True
