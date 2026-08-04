"""Unit tests for replay event payload builders (issue #72, Phase 0).

DB-free: run in CI's SKIP_INTEGRATION pass.
"""

from __future__ import annotations

from soctalk.core.ir import replay_events as re_
from soctalk.core.ir.events import REDUCER_APPLIES, EventKind
from soctalk.core.ir.models import Visibility
from soctalk.core.ir.reducer import Facts, apply_event

VALID_VISIBILITIES = {v.value for v in Visibility}


def test_replay_kinds_are_not_reducer_applied():
    for kind in (
        EventKind.POLICY_RESOLVED,
        EventKind.SUPERVISOR_DECISION,
        EventKind.WORKER_STARTED,
        EventKind.WORKER_RESULT,
        EventKind.VERDICT_RENDERED,
        EventKind.GUARD_EVALUATED,
    ):
        assert kind not in REDUCER_APPLIES


def test_reducer_noops_replay_kinds():
    facts = Facts()
    for kind in (
        EventKind.POLICY_RESOLVED,
        EventKind.SUPERVISOR_DECISION,
        EventKind.GUARD_EVALUATED,
        EventKind.VERDICT_RENDERED,
        EventKind.BUDGET_WARNING,
    ):
        out = apply_event(facts, kind.value, {"anything": "x"}, seq=7)
        assert out.hypotheses == facts.hypotheses
        assert out.applied_seq == 7


def test_budget_warning_is_analyst_facing_and_reducer_noop(monkeypatch):
    # #103 soft warning: operational spend detail → mssp_only, never reduced.
    ev = re_.budget_warning(
        tokens_used=6000, tokens_budget=8000,
        dollars_used=0.19, dollars_budget=0.25, ratio=0.75,
    )
    assert ev.kind == EventKind.BUDGET_WARNING
    assert ev.visibility == Visibility.MSSP_ONLY.value
    assert EventKind.BUDGET_WARNING not in REDUCER_APPLIES
    assert ev.payload["tokens_used"] == 6000
    assert ev.payload["dollars_used"] == 0.19  # rounded to 4dp
    assert ev.payload["ratio"] == 0.75
    assert ev.payload["payload_version"] == re_.PAYLOAD_VERSION


def test_budget_warning_never_raises_on_odd_input():
    # Builder discipline: clip/coerce, never raise on the pipeline's hot path.
    ev = re_.budget_warning(
        tokens_used="9313", tokens_budget=8000,  # type: ignore[arg-type]
        dollars_used=0.123456789, dollars_budget=0.25, ratio=0.2,
    )
    assert ev.payload["tokens_used"] == 9313
    assert ev.payload["dollars_used"] == 0.1235
    # Genuine garbage coerces to 0 rather than raising on the emit path.
    junk = re_.budget_warning(
        tokens_used=None, tokens_budget="oops",  # type: ignore[arg-type]
        dollars_used=None, dollars_budget="x", ratio=None,  # type: ignore[arg-type]
    )
    assert junk.payload["tokens_used"] == 0
    assert junk.payload["dollars_used"] == 0.0
    assert junk.payload["ratio"] == 0.0


def test_all_builders_emit_valid_visibility():
    events = [
        re_.policy_resolved(
            triage_policy={"id": "tp-1", "version": 2},
            deterministic_disposition="close_operational",
            vetoes_checked=["ioc_present"],
            vetoes_fired=[],
        ),
        re_.supervisor_decision(
            {"next_action": "INVESTIGATE", "action_reasoning": "x", "tp_confidence": 0.4},
            iteration=1,
        ),
        re_.worker_started("wazuh", action="INVESTIGATE"),
        re_.worker_result("wazuh", ok=True, summary="214 events", counts={"logs": 214}),
        re_.verdict_rendered({"decision": "close", "confidence": 0.93}),
        re_.guard_evaluated(
            stage="verdict_guard",
            decision_in="close",
            decision_out="close",
            effect="pass",
        ),
        re_.case_closed(path="reasoning", reason="likely FP", run_id="r1"),
    ]
    for ev in events:
        assert ev.visibility in VALID_VISIBILITIES
        assert ev.payload["payload_version"] == re_.PAYLOAD_VERSION


def test_visibility_split_customer_receipt_vs_mssp_detail():
    assert (
        re_.verdict_rendered({}).visibility == Visibility.CUSTOMER_SAFE.value
    )
    assert (
        re_.guard_evaluated(
            stage="server_floor", decision_in="close_fp",
            decision_out="escalate", effect="override",
        ).visibility
        == Visibility.CUSTOMER_SAFE.value
    )
    assert re_.case_closed(path="operational", reason=None).visibility == (
        Visibility.CUSTOMER_SAFE.value
    )
    assert (
        re_.supervisor_decision({}, iteration=1).visibility
        == Visibility.MSSP_ONLY.value
    )
    assert re_.worker_result("misp", ok=True).visibility == Visibility.MSSP_ONLY.value


def test_builders_never_raise_on_garbage():
    re_.policy_resolved(triage_policy=None, deterministic_disposition=None)
    re_.supervisor_decision(None, iteration=0)
    re_.verdict_rendered(None)
    re_.worker_result("x", ok=False, counts={"bad": "not-a-number"})  # type: ignore[dict-item]
    ev = re_.guard_evaluated(
        stage="verdict_guard", decision_in=None, decision_out=None, effect="pass",
        fired=None, reasons=None,
    )
    assert ev.payload["fired"] == []


def test_clipping_is_bounded():
    long = "z" * 10_000
    ev = re_.supervisor_decision(
        {"next_action": "VERDICT", "action_reasoning": long}, iteration=3
    )
    assert len(ev.payload["action_reasoning"]) <= 2000
    ev2 = re_.verdict_rendered({"key_evidence": [long] * 50})
    assert len(ev2.payload["key_evidence"]) <= 20
    assert all(len(item) <= 400 for item in ev2.payload["key_evidence"])


def test_verdict_decision_accepts_enum_or_string():
    from soctalk.models.enums import VerdictDecision

    ev = re_.verdict_rendered({"decision": VerdictDecision.CLOSE})
    assert ev.payload["decision"] == "close"
    ev2 = re_.verdict_rendered({"decision": "escalate"})
    assert ev2.payload["decision"] == "escalate"
