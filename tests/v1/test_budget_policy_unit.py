"""Per-run dollar budget resolution and budget-key scoping (#128).

DB-free: the resolver's failure paths and the install clamp are exactly the
parts that must hold when the database is unavailable or half-migrated.
"""
from __future__ import annotations

import uuid

import pytest

from soctalk.core.ir import policies


class _ExplodingSession:
    """Stands in for a session whose query fails mid-rollout."""

    async def execute(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("relation \"tenant_policies\" does not exist")


class _FakeSession:
    """Answers the policy read, and the "does Postgres know this zone" probe.

    ``known_zones`` None means "Postgres knows every zone Python does", which is
    the normal case; a set pins it so the mismatch path is testable.
    """

    def __init__(self, rows: dict, known_zones: set[str] | None = None):
        self._rows = rows
        self._known = known_zones

    async def execute(self, statement, params=None, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        sql = str(statement)
        rows = [{"key": k, "value": v} for k, v in self._rows.items()]
        known = self._known
        outer = self

        class _R:
            def scalar(self_inner):  # noqa: ANN001
                # pg_timezone_names probe.
                if known is None:
                    return True
                return (params or {}).get("n") in known

            def mappings(self_inner):  # noqa: ANN001
                class _M:
                    def all(self_m):  # noqa: ANN001
                        return rows

                    def first(self_m):  # noqa: ANN001
                        return rows[0] if rows else None

                return _M()

        del sql, outer
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


# --- Calendar-day reset for the daily ceilings ------------------------------


def test_day_window_is_a_calendar_day_in_the_given_zone():
    """The ceiling clears in one step at local midnight, not by trickling.

    A rolling 24h sum never fully clears: each charge ages out on its own
    anniversary, so a tenant that blew its ceiling at 14:00 is still partly
    blocked at 13:00 the next day and there is no moment to point at.
    """
    from soctalk.core.cost import day_window

    for tz in ("UTC", "America/New_York", "Europe/Madrid", "Asia/Tokyo"):
        start, end = day_window(tz)
        assert start < end
        # Exactly one day apart on the local clock (23-25h absolute, because a
        # DST transition legitimately shortens or lengthens the day).
        hours = (end - start).total_seconds() / 3600
        assert 23 <= hours <= 25, (tz, hours)


def test_an_unknown_zone_falls_back_rather_than_breaking_the_cap():
    """An invalid zone would make the spend query itself raise.

    A cap that errors is a cap that stops all triage, so a bad value degrades
    to the install default instead.
    """
    from soctalk.core.cost import _is_valid_timezone, day_window

    assert _is_valid_timezone("Not/AZone") is False
    assert _is_valid_timezone("") is False
    utc_start, _ = day_window("UTC")
    bad_start, _ = day_window("Not/AZone")
    assert bad_start == utc_start


@pytest.mark.asyncio
async def test_timezone_override_is_read_from_policy_and_validated():
    from uuid import uuid4

    from soctalk.core import cost

    good = _FakeSession({cost.BUDGET_DAY_TZ_KEY: "Europe/Madrid"})
    assert await cost.resolve_budget_day_timezone(good, uuid4()) == "Europe/Madrid"

    junk = _FakeSession({cost.BUDGET_DAY_TZ_KEY: "Nope/Nope"})
    assert await cost.resolve_budget_day_timezone(junk, uuid4()) == "UTC"

    # Python knows the zone but this Postgres does not: the name would make the
    # spend query itself raise, so it must not be used (round 4, finding 3).
    py_only = _FakeSession(
        {cost.BUDGET_DAY_TZ_KEY: "Europe/Madrid"}, known_zones={"UTC"}
    )
    assert await cost.resolve_budget_day_timezone(py_only, uuid4()) == "UTC"

    wrong_type = _FakeSession({cost.BUDGET_DAY_TZ_KEY: 42})
    assert await cost.resolve_budget_day_timezone(wrong_type, uuid4()) == "UTC"

    assert await cost.resolve_budget_day_timezone(_ExplodingSession(), uuid4()) == "UTC"


def test_the_day_boundary_is_a_budget_key_and_cannot_come_from_below():
    """Moving midnight moves when a ceiling resets, so it is a budget control."""
    from soctalk.core.ir import policies

    assert "budget_day_timezone" in policies.BUDGET_KEYS
    assert policies._without_budget_keys({"budget_day_timezone": "Pacific/Auckland"}) == {}


def test_day_window_handles_dst_transitions():
    """A DST day is 23 or 25 hours, and still starts at local midnight.

    The day is advanced on the LOCAL clock for exactly this reason: adding 24
    absolute hours would land an hour off on both transition days, so the
    ceiling would reset at 23:00 or 01:00 instead of midnight.
    """
    from datetime import datetime, timezone

    from soctalk.core.cost import day_window

    # Spring forward and fall back, US Eastern 2026.
    spring = day_window("America/New_York", datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc))
    fall = day_window("America/New_York", datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc))
    assert (spring[1] - spring[0]).total_seconds() / 3600 == 23
    assert (fall[1] - fall[0]).total_seconds() / 3600 == 25

    # Southern hemisphere transitions the other way round.
    syd = day_window("Australia/Sydney", datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc))
    assert 23 <= (syd[1] - syd[0]).total_seconds() / 3600 <= 25


def test_day_window_handles_sub_hour_offsets():
    """45-minute zones exist and must not be rounded to the hour."""
    from datetime import datetime, timezone

    from soctalk.core.cost import day_window

    at = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    for tz, offset_minutes in (
        ("Asia/Kathmandu", 5 * 60 + 45),
        ("Australia/Eucla", 8 * 60 + 45),
    ):
        start, _ = day_window(tz, at)
        # Local midnight expressed in UTC lands offset minutes BEFORE midnight UTC.
        minutes_past_utc_midnight = start.hour * 60 + start.minute
        assert (minutes_past_utc_midnight + offset_minutes) % (24 * 60) == 0, tz


# --- #134: unknown fields on the money models -----------------------------
#
# Both models used to be bare BaseModel, so an unrecognised key was dropped.
# The dangerous shape is not the all-typo body (that fails closed) but the
# mixed one: half the payload applies, the response is 200, and the caller
# believes it set both ceilings.


def test_run_budget_update_rejects_unknown_fields():
    from pydantic import ValidationError

    from soctalk.core.api.run_budget import RunBudgetUpdate

    # A mixed payload must not half-apply: one good field, one typo.
    with pytest.raises(ValidationError) as exc:
        RunBudgetUpdate(token_override=1000, dollars_override=3.25)
    assert "dollars_override" in str(exc.value)

    # The correctly-spelled pair still works.
    ok = RunBudgetUpdate(token_override=1000, dollar_override=3.25)
    assert ok.dollar_override == 3.25


def test_run_unlock_request_rejects_the_plausible_misspelling():
    from pydantic import ValidationError

    from soctalk.core.api.run_budget import RunUnlockRequest

    # ``dollars_budget`` is the field name a caller reaches for first; it must
    # be a loud 422 naming the field, not a silent "keep the current ceiling"
    # whose error then blames a value the caller never sent.
    with pytest.raises(ValidationError) as exc:
        RunUnlockRequest(dollars_budget=5.0)
    assert "dollars_budget" in str(exc.value)

    assert RunUnlockRequest(dollar_budget=5.0).dollar_budget == 5.0


# --- the price gate: a model must be priced to be used ---------------------
#
# The rule: a model needs a catalog entry before it can be used, unless cost
# tracking is disabled. Enforced at CONFIGURATION time (the LLM PATCH), not at
# run time — refusing to start a run over pricing would stop triage silently,
# which is the failure the pricing feature exists to prevent.


def test_cost_tracking_is_on_unless_explicitly_disabled(monkeypatch):
    from soctalk.core.ir.policies import cost_tracking_install_default

    monkeypatch.delenv("SOCTALK_COST_TRACKING", raising=False)
    monkeypatch.delenv("SOCTALK_UNKNOWN_MODEL_COST", raising=False)
    assert cost_tracking_install_default() is True

    for off in ("off", "0", "false", "no", "disabled", "OFF"):
        monkeypatch.setenv("SOCTALK_COST_TRACKING", off)
        assert cost_tracking_install_default() is False, off


def test_zero_unknown_cost_already_means_tracking_off(monkeypatch):
    """An operator who set unpriced models to cost nothing has answered this.

    Making them set a second variable to express the same intent would be a
    papercut, so the existing local-inference knob is honoured.
    """
    from soctalk.core.ir.policies import cost_tracking_install_default

    monkeypatch.delenv("SOCTALK_COST_TRACKING", raising=False)
    for zero in ("zero", "free", "0"):
        monkeypatch.setenv("SOCTALK_UNKNOWN_MODEL_COST", zero)
        assert cost_tracking_install_default() is False, zero


def test_turning_accounting_off_is_scoped_like_a_budget_ceiling():
    """It disables every ceiling at once, so lower policy layers must not set it."""
    from soctalk.core.ir.policies import BUDGET_KEYS, COST_TRACKING_KEY

    assert COST_TRACKING_KEY in BUDGET_KEYS


def test_unpriced_message_names_every_way_out():
    """A refusal the operator cannot act on is just an outage."""
    from soctalk.core.pricing.gate import unpriced_message

    msg = unpriced_message(["model: claude-x-9"])
    assert "claude-x-9" in msg
    assert "soctalk-prices import" in msg   # seed the catalog
    assert "override" in msg                # or state the rate yourself
    assert "off" in msg.lower()             # or stop counting dollars


@pytest.mark.asyncio
async def test_price_gate_calls_the_catalog_with_a_valid_signature(monkeypatch):
    """Exercises the lookup path, not just the message.

    The first version of this gate called ``provider_id_for(provider, base_url)``
    — two arguments to a one-argument function. Every unit test passed because
    none of them reached the lookup; CI's integration suite caught it as a
    TypeError on eight unrelated LLM-PATCH tests. Cover the call itself.
    """
    from soctalk.core.pricing import gate

    seen: dict[str, object] = {}

    async def fake_lookup(db, *, provider_kind, model, provider_id=None):
        seen["kind"] = provider_kind
        seen["model"] = model
        seen["pid"] = provider_id
        return None  # unpriced

    monkeypatch.setattr(gate.catalog, "lookup", fake_lookup)

    async def tracking_on(_db, _tenant):
        return True

    monkeypatch.setattr(gate, "resolve_cost_tracking", tracking_on)

    missing = await gate.unpriced_models(
        object(),
        uuid.uuid4(),
        provider="openai-compatible",
        base_url="https://novarouteai.com/v1",
        models={"model": "claude-x-9", "fast_model": None},
    )

    assert missing == ["model: claude-x-9"]
    assert seen["model"] == "claude-x-9"
    # provider_id carries the VENDOR BEHIND A GATEWAY, read from the host — a
    # gateway's price for a model is not the vendor's price for it. First-party
    # endpoints are identified by provider_kind instead and carry no id.
    assert seen["pid"] == "novaroute"


@pytest.mark.asyncio
async def test_price_gate_is_inert_when_tracking_is_off(monkeypatch):
    from soctalk.core.pricing import gate

    async def boom(*a, **k):  # must never be reached
        raise AssertionError("catalog consulted while cost tracking is off")

    monkeypatch.setattr(gate.catalog, "lookup", boom)

    async def tracking_off(_db, _tenant):
        return False

    monkeypatch.setattr(gate, "resolve_cost_tracking", tracking_off)

    assert await gate.unpriced_models(
        object(), uuid.uuid4(),
        provider="self-hosted", base_url="http://localhost:8000/v1",
        models={"model": "some-local-gguf"},
    ) == []


# --- cost tracking must actually reach runtime enforcement -----------------
#
# Codex review, finding 5: `cost_tracking_enabled` gated only the config-time
# check. `over_budget` still halted on dollars, and the UI checkbox said
# "Dollar ceilings are not enforced" — which was false. The answer now travels
# on the run's price snapshot, so it is fixed at run creation and a mid-run
# policy change cannot re-enforce an in-flight run.


def _state(dollars_used: float, dollars_budget: float, *, tracking: bool | None):
    snapshot = {"version": 1, "models": {}}
    if tracking is not None:
        snapshot["cost_tracking"] = tracking
    return {
        "tokens_used": 0,
        "tokens_budget": 1_000_000,
        "dollars_used": dollars_used,
        "dollars_budget": dollars_budget,
        "price_snapshot": snapshot,
    }


def test_dollar_cap_halts_when_cost_tracking_is_on():
    from soctalk.graph.budget import over_budget

    assert over_budget(_state(5.0, 5.0, tracking=True)) is True


def test_dollar_cap_is_inert_when_cost_tracking_is_off():
    from soctalk.graph.budget import over_budget

    assert over_budget(_state(999.0, 5.0, tracking=False)) is False


def test_token_cap_still_bites_with_cost_tracking_off():
    """Tokens are measured, not inferred, so they bound work either way."""
    from soctalk.graph.budget import over_budget

    st = _state(0.0, 5.0, tracking=False)
    st["tokens_used"] = st["tokens_budget"]
    assert over_budget(st) is True


def test_a_snapshot_without_the_flag_still_enforces_dollars():
    """Runs created before this shipped must not silently stop being capped."""
    from soctalk.graph.budget import over_budget

    assert over_budget(_state(5.0, 5.0, tracking=None)) is True
    st = {
        "tokens_used": 0, "tokens_budget": 1_000_000,
        "dollars_used": 5.0, "dollars_budget": 5.0,
    }
    assert over_budget(st) is True


# --- phase 1: the number says where it came from ---------------------------
#
# Only OpenRouter reports an actual cost (llm.py:194). Everything else is our
# arithmetic against our own rate card, while being authoritative for
# enforcement. cost_basis/price_source were computed and then only logged, so
# after the fact a measured dollar was indistinguishable from an inferred one.


def _track_state(snapshot=None):
    return {
        "tokens_used": 0, "tokens_budget": 10**9,
        "dollars_used": 0.0, "dollars_budget": 10**6,
        "price_snapshot": snapshot,
    }


def test_a_provider_reported_cost_is_marked_as_such(monkeypatch):
    """When the provider says what it charged, no rate card is consulted."""
    from soctalk.core.pricing.usage import CanonicalUsage
    from soctalk.graph import budget

    st = _track_state()
    usage = CanonicalUsage(input_tokens=100, output_tokens=50)
    usage.actual_cost_usd = 0.25

    monkeypatch.setattr(budget, "canonical_usage", lambda _r: usage)
    monkeypatch.setattr(budget, "_model_name", lambda _r: "some-model")
    budget.track(st, object())

    assert st["cost_basis"] == "provider_reported"
    assert st["price_source"] == "provider"
    assert st["dollars_used"] == 0.25


def test_an_estimate_records_which_rate_card_produced_it(monkeypatch):
    from soctalk.core.pricing.usage import CanonicalUsage
    from soctalk.graph import budget

    snapshot = {
        "version": 1,
        "models": {
            "fast": {
                "model": "claude-haiku-4-5",
                "input_per_mtok": 1.0,
                "output_per_mtok": 5.0,
                "source": "catalog",
            }
        },
    }
    st = _track_state(snapshot)
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=0)

    monkeypatch.setattr(budget, "canonical_usage", lambda _r: usage)
    monkeypatch.setattr(budget, "_model_name", lambda _r: "claude-haiku-4-5")
    budget.track(st, object())

    assert st["cost_basis"] == "estimated"
    assert st["price_source"] == "catalog"
    assert round(st["dollars_used"], 6) == 1.0


def test_an_unpriced_model_is_recorded_as_unknown_not_silently_estimated(monkeypatch):
    """The case that over-billed 16x. It must be nameable after the fact."""
    from soctalk.core.pricing.usage import CanonicalUsage
    from soctalk.graph import budget

    st = _track_state({"version": 1, "models": {}})
    usage = CanonicalUsage(input_tokens=1000, output_tokens=1000)

    monkeypatch.setattr(budget, "canonical_usage", lambda _r: usage)
    monkeypatch.setattr(budget, "_model_name", lambda _r: "totally-unknown-model")
    budget.track(st, object())

    assert st["cost_basis"] == "estimated"
    assert st["price_source"] == "unknown"


# --- phase 2: an unpriced figure must not halt anything (#124) --------------


def test_unpriced_spend_is_visible_but_not_enforceable():
    from soctalk.graph.budget import enforceable_dollars

    st = {"dollars_used": 104.0, "dollars_unpriced": 99.0}
    # The money is not hidden: the total still says what was spent.
    assert st["dollars_used"] == 104.0
    # But only the priced part may stop a run.
    assert enforceable_dollars(st) == 5.0


def test_a_run_is_not_halted_by_guesswork():
    """The #139 shape: a model missing from the catalog billed 16x and halted
    an investigation on spend that never happened."""
    from soctalk.graph.budget import over_budget

    st = {
        "tokens_used": 0, "tokens_budget": 10**9,
        "dollars_used": 400.0, "dollars_unpriced": 400.0, "dollars_budget": 5.0,
        "price_snapshot": {"version": 1, "models": {}},
    }
    assert over_budget(st) is False


def test_priced_spend_still_halts_normally():
    from soctalk.graph.budget import over_budget

    st = {
        "tokens_used": 0, "tokens_budget": 10**9,
        "dollars_used": 6.0, "dollars_unpriced": 1.0, "dollars_budget": 5.0,
        "price_snapshot": {"version": 1, "models": {}},
    }
    assert over_budget(st) is True


def test_tokens_still_bound_an_entirely_unpriced_run():
    """Removing the dollar cap must not remove every cap."""
    from soctalk.graph.budget import over_budget

    st = {
        "tokens_used": 200_000, "tokens_budget": 200_000,
        "dollars_used": 999.0, "dollars_unpriced": 999.0, "dollars_budget": 5.0,
        "price_snapshot": {"version": 1, "models": {}},
    }
    assert over_budget(st) is True


def test_a_claim_denial_is_not_mistaken_for_a_claimed_run():
    """A denial body is truthy; the worker must not treat it as work.

    Before the guard, `return body if body else None` would have handed the
    denial dict to the run loop, which would fail downstream on a missing run
    id — turning a clear "you are over your ceiling" into a crash.
    """
    body = {"denied": True, "reason": "daily_cap", "retry_after_seconds": 900}
    assert isinstance(body, dict) and body.get("denied")

    run_body = {"run_id": "abc", "investigation_id": "def"}
    assert not run_body.get("denied")


def test_exemption_survives_a_release_and_reclaim():
    """A retried run must not start enforcing spend it already exempted.

    dollars_used is rehydrated from the run row on claim; dollars_unpriced was
    only in graph state, so a run released on a transient provider error came
    back with the exemption at zero and could halt on the very fallback dollars
    the exemption exists to disregard (Codex review, phases 1-2).
    """
    from soctalk.graph.budget import over_budget

    # What a re-claimed run looks like WITHOUT rehydration: the exemption is
    # lost and the run halts.
    lost = {
        "tokens_used": 0, "tokens_budget": 10**9,
        "dollars_used": 400.0, "dollars_budget": 5.0,
        "price_snapshot": {"version": 1, "models": {}},
    }
    assert over_budget(lost) is True

    # With the claim carrying it, the run continues, as it did before the retry.
    rehydrated = {**lost, "dollars_unpriced": 400.0}
    assert over_budget(rehydrated) is False


def test_builtin_priced_models_are_not_labelled_unknown(monkeypatch):
    """No snapshot is not the same as no price.

    A run created before snapshots existed, or one whose price resolution
    failed (start_run explicitly tolerates that), still prices from the
    built-in table. Labelling it "unknown" would have exempted the entire
    legacy path from enforcement under phase 2 — every such run silently
    uncapped. Caught by an existing chat test that stopped tripping its budget.
    """
    from soctalk.core.pricing.usage import CanonicalUsage
    from soctalk.graph import budget

    st = {
        "tokens_used": 0, "tokens_budget": 10**9,
        "dollars_used": 0.0, "dollars_budget": 10**6,
    }  # deliberately NO price_snapshot
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=0)

    monkeypatch.setattr(budget, "canonical_usage", lambda _r: usage)
    monkeypatch.setattr(budget, "_model_name", lambda _r: "claude-sonnet-4-6")
    budget.track(st, object())

    assert st["price_source"] == "builtin"
    assert st.get("dollars_unpriced", 0.0) == 0.0
    assert budget.enforceable_dollars(st) == st["dollars_used"] > 0


def test_only_a_genuine_fallback_is_unknown(monkeypatch):
    from soctalk.core.pricing.usage import CanonicalUsage
    from soctalk.graph import budget

    st = {
        "tokens_used": 0, "tokens_budget": 10**9,
        "dollars_used": 0.0, "dollars_budget": 10**6,
    }
    usage = CanonicalUsage(input_tokens=1000, output_tokens=1000)
    monkeypatch.setattr(budget, "canonical_usage", lambda _r: usage)
    monkeypatch.setattr(budget, "_model_name", lambda _r: "no-such-model-anywhere")
    budget.track(st, object())

    assert st["price_source"] == "unknown"
    assert st["dollars_unpriced"] == st["dollars_used"] > 0
    assert budget.enforceable_dollars(st) == 0.0


# --- Codex round 2: the residual row must not inherit 'unknown' -------------


def _residual_source(price_source: str | None, d_unpriced: float) -> str | None:
    """Mirror of the label chosen for the priced remainder in
    ``_record_spend_delta``. Kept as a pure function so the rule can be pinned
    without a database."""
    return "mixed" if price_source == "unknown" and d_unpriced > 0.0 else price_source


def test_priced_remainder_is_not_labelled_unknown():
    """A window of catalog $1 then unpriced $9, with no heartbeat between.

    The unpriced $9 splits off as its own row. What remains is $1 of genuinely
    priced spend — but the report's price_source is 'unknown', because that is
    what the LAST call used. Inheriting it would exempt the $1 from the daily
    ceiling too, under-enforcing by the priced amount (Codex round 2, P0).
    """
    assert _residual_source("unknown", 9.0) == "mixed"
    # And 'mixed' must not be excluded by the daily-cap rule, which keys on
    # 'unknown' exactly.
    assert _residual_source("unknown", 9.0) != "unknown"


def test_a_wholly_unpriced_window_keeps_its_label():
    """Nothing was split off, so there is no priced remainder to protect."""
    assert _residual_source("unknown", 0.0) == "unknown"


def test_a_priced_window_is_untouched():
    assert _residual_source("catalog", 0.0) == "catalog"
    assert _residual_source("builtin", 0.0) == "builtin"


def test_unlock_compares_against_enforceable_spend():
    """A run carrying unpriced spend must still be unlockable.

    Validating on the raw total made a run unlockable only by exceeding spend
    the runtime already disregards: a token-halted run carrying $400 of
    unpriced dollars could not be unlocked at all, since no ceiling under the
    install cap exceeds $400 (Codex round 5).
    """
    dollars_used, dollars_unpriced = 400.0, 400.0
    enforceable = max(0.0, dollars_used - dollars_unpriced)
    assert enforceable == 0.0

    # A sane new ceiling now passes, where comparing against 400.0 refused it.
    new_dollars = 5.0
    assert new_dollars > enforceable
    assert not new_dollars > dollars_used  # the old rule would have rejected it


def test_cost_tracking_off_disables_the_daily_dollar_ceiling_too():
    """The switch has to mean the same thing everywhere.

    Only the per-run check honoured it, so a tenant with accounting off was
    still stopped by the daily spend ceiling — and by chat's conversation cap —
    while the panel said "Dollar ceilings are not enforced" (Codex round 6).
    """
    from soctalk.core.cost import DailyCaps, DailyCapStatus, TenantDailySpend

    over = DailyCapStatus(
        spend=TenantDailySpend(tokens=10, dollars=999.0),
        caps=DailyCaps(tokens=1_000_000, dollars=50.0),
    )
    assert over.dollar_cap_hit is True
    assert over.cap_hit is True

    same_but_untracked = DailyCapStatus(
        spend=TenantDailySpend(tokens=10, dollars=999.0),
        caps=DailyCaps(tokens=1_000_000, dollars=50.0),
        cost_tracking=False,
    )
    assert same_but_untracked.dollar_cap_hit is False
    assert same_but_untracked.cap_hit is False


def test_tokens_still_cap_when_cost_tracking_is_off():
    """Turning off dollar accounting is not a request to remove every bound."""
    from soctalk.core.cost import DailyCaps, DailyCapStatus, TenantDailySpend

    st = DailyCapStatus(
        spend=TenantDailySpend(tokens=10_000_000, dollars=0.0),
        caps=DailyCaps(tokens=1_000_000, dollars=50.0),
        cost_tracking=False,
    )
    assert st.token_cap_hit is True
    assert st.cap_hit is True


def test_fleet_daily_cap_honours_the_install_switch(monkeypatch):
    """Fleet chat has no tenant policy, but an install-wide off still means off.

    It was the last path enforcing dollars through the switch (Codex round 7).
    """
    from soctalk.core.cost import MsspUserDailySpend

    monkeypatch.delenv("SOCTALK_COST_TRACKING", raising=False)
    monkeypatch.delenv("SOCTALK_UNKNOWN_MODEL_COST", raising=False)
    huge = MsspUserDailySpend(tokens=0, dollars=10**6)
    assert huge.dollar_cap_hit is True

    monkeypatch.setenv("SOCTALK_COST_TRACKING", "off")
    assert huge.dollar_cap_hit is False
    # Tokens are unaffected: they are counted, not inferred.
    tokens_over = MsspUserDailySpend(tokens=10**12, dollars=0.0)
    assert tokens_over.token_cap_hit is True
