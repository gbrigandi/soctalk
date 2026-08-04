"""Per-run budget seed precedence in the runs-worker (issue #5).

A per-tenant cap is rendered into ``SOCTALK_CASE_RUN_{DOLLAR,TOKEN}_BUDGET`` env.
Both must give the env TOP precedence over the claim row so the tenant override
actually takes effect — previously the token seed was taken from the claim
unconditionally and the env was ignored for claimed runs (Codex).
"""

from __future__ import annotations

import pytest

from soctalk.runs_worker.main import _dollars_budget_kv, _tokens_budget_kv


def test_tokens_claim_wins_over_env(monkeypatch):
    # #103: the CLAIM row is authoritative (server resolves the per-tenant
    # budget and stamps it), so a positive claim wins even when the legacy
    # env is set. The env is only an install-global fallback now.
    monkeypatch.setenv("SOCTALK_CASE_RUN_TOKEN_BUDGET", "50000")
    assert _tokens_budget_kv(200000) == {"tokens_budget": 200000}


def test_tokens_env_fallback_when_claim_absent(monkeypatch):
    # No valid claim budget -> the install-global env fallback applies.
    monkeypatch.setenv("SOCTALK_CASE_RUN_TOKEN_BUDGET", "50000")
    assert _tokens_budget_kv(0) == {"tokens_budget": 50000}


def test_tokens_falls_back_to_claim_when_env_absent(monkeypatch):
    monkeypatch.delenv("SOCTALK_CASE_RUN_TOKEN_BUDGET", raising=False)
    assert _tokens_budget_kv(200000) == {"tokens_budget": 200000}


def test_tokens_non_positive_env_ignored(monkeypatch):
    # An operator typo (=0 / garbage) in the fallback env must not zero the
    # budget when there's no claim; fall through to the ensure() default.
    monkeypatch.setenv("SOCTALK_CASE_RUN_TOKEN_BUDGET", "0")
    assert _tokens_budget_kv(0) == {}
    monkeypatch.setenv("SOCTALK_CASE_RUN_TOKEN_BUDGET", "notanint")
    assert _tokens_budget_kv(0) == {}


def test_tokens_empty_when_nothing_positive(monkeypatch):
    monkeypatch.delenv("SOCTALK_CASE_RUN_TOKEN_BUDGET", raising=False)
    assert _tokens_budget_kv(0) == {}
    assert _tokens_budget_kv(None) == {}


def test_dollars_env_overrides_claim(monkeypatch):
    # Parity check for the pre-existing dollar path.
    monkeypatch.setenv("SOCTALK_CASE_RUN_DOLLAR_BUDGET", "2.5")
    assert _dollars_budget_kv(5.0) == {"dollars_budget": 2.5}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])


# --- Enforcement chain (#103): the stamped/seeded budget is what halts a run ---


def test_ensure_preserves_a_seeded_budget():
    # The worker seeds state["tokens_budget"] from the claim (the resolved,
    # stamped per-tenant budget). ensure() must NOT overwrite it with the env
    # default — otherwise the per-tenant override would never enforce.
    from soctalk.graph import budget as token_budget

    state = {"tokens_budget": 40_000}
    token_budget.ensure(state)
    assert state["tokens_budget"] == 40_000


def test_over_budget_halts_at_the_seeded_budget():
    from soctalk.graph import budget as token_budget

    state = {
        "tokens_used": 0,
        "tokens_budget": 40_000,
        "dollars_used": 0.0,
        "dollars_budget": 5.0,
    }
    assert token_budget.over_budget(state) is False
    state["tokens_used"] = 39_999
    assert token_budget.over_budget(state) is False, "under budget keeps running"
    state["tokens_used"] = 40_000
    assert token_budget.over_budget(state) is True, "hard halt at 100% of the budget"


def test_soft_warn_fires_at_75_percent_not_before():
    from soctalk.graph import budget as token_budget

    base = {"tokens_budget": 40_000, "dollars_used": 0.0, "dollars_budget": 5.0}
    assert token_budget.crossed_soft_warn({**base, "tokens_used": 29_999}) is False
    assert token_budget.crossed_soft_warn({**base, "tokens_used": 30_000}) is True  # 75%
    # Once over budget, the hard halt supersedes the soft warning.
    assert token_budget.crossed_soft_warn({**base, "tokens_used": 40_000}) is False


def test_soft_warn_ratio_env_override(monkeypatch):
    from soctalk.graph import budget as token_budget

    monkeypatch.setenv("SOCTALK_BUDGET_WARN_RATIO", "0.5")
    assert token_budget.soft_warn_ratio() == 0.5
    base = {"tokens_budget": 40_000, "dollars_used": 0.0, "dollars_budget": 5.0}
    assert token_budget.crossed_soft_warn({**base, "tokens_used": 20_000}) is True
    # Out-of-range values are ignored -> back to the 0.75 default.
    monkeypatch.setenv("SOCTALK_BUDGET_WARN_RATIO", "1.5")
    assert token_budget.soft_warn_ratio() == 0.75


def test_soft_warn_fires_on_the_dollar_cap_too():
    # Parity with the hard halt: the warning trips on EITHER the token OR the
    # dollar ratio, so a cheap-token/expensive-model run still warns.
    from soctalk.graph import budget as token_budget

    base = {"tokens_budget": 40_000, "tokens_used": 0, "dollars_budget": 5.0}
    assert token_budget.crossed_soft_warn({**base, "dollars_used": 3.74}) is False
    assert token_budget.crossed_soft_warn({**base, "dollars_used": 3.75}) is True  # 75%
