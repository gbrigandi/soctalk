"""Unit-level tests for the native IR subsystem.

Tests that run without Postgres: reducer determinism, triage scoring,
idempotency-key stability, signature hashing, and tool-registry wiring.

DB-backed invariants (RLS enforcement, execution-log immutability,
single-active-run unique index, reopen matching) live in
test_ir_integration.py and are gated on SKIP_INTEGRATION.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest


os.environ.setdefault(
    "DATABASE_URL_APP", "postgresql+asyncpg://stub:stub@localhost:9999/stub"
)
os.environ.setdefault(
    "DATABASE_URL_MSSP", "postgresql+asyncpg://stub:stub@localhost:9999/stub"
)
os.environ.setdefault(
    "SOCTALK_JWT_SIGNING_KEY", "test-signing-key-32-bytes-plaintext"
)
os.environ.setdefault(
    "SOCTALK_ADAPTER_SIGNING_KEY", "adapter-signing-key-32-bytes-plaintext"
)


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------


def test_reducer_seeds_hypothesis_from_alert_ingested():
    from soctalk.core.ir.reducer import Facts, apply_event

    state = Facts()
    new_state = apply_event(
        state,
        "alert_ingested",
        {
            "rule_id": "5720",
            "severity": 8,
            "ai_confidence": 0.85,
            "initial_hypothesis": "credential_theft",
        },
        seq=1,
    )
    assert len(new_state.hypotheses) == 1
    assert new_state.hypotheses[0]["id"] == "root"
    assert new_state.hypotheses[0]["label"] == "credential_theft"
    assert new_state.hypotheses[0]["confidence"] == 0.85
    assert new_state.applied_seq == 1


def test_reducer_hypothesis_updated_modifies_existing():
    from soctalk.core.ir.reducer import Facts, apply_event

    state = Facts(hypotheses=[{"id": "h1", "label": "initial", "confidence": 0.5}])
    new_state = apply_event(
        state,
        "hypothesis_updated",
        {"id": "h1", "confidence": 0.82, "rationale": "new evidence"},
        seq=2,
    )
    assert new_state.hypotheses[0]["confidence"] == 0.82
    assert new_state.hypotheses[0]["rationale"] == "new evidence"


def test_reducer_analyst_correction_writes_field():
    from soctalk.core.ir.reducer import Facts, apply_event

    state = Facts(hypotheses=[{"id": "h1", "label": "x", "confidence": 0.9}])
    new_state = apply_event(
        state,
        "analyst_correction",
        {"path": "hypotheses.h1.confidence", "value": 0.3},
        seq=3,
    )
    assert new_state.hypotheses[0]["confidence"] == 0.3


def test_reducer_is_pure():
    """Applying an event does not mutate the input state."""

    from soctalk.core.ir.reducer import Facts, apply_event

    state = Facts(hypotheses=[{"id": "h1", "label": "x", "confidence": 0.5}])
    orig_id = id(state)
    new_state = apply_event(
        state, "hypothesis_updated", {"id": "h1", "confidence": 0.9}, seq=1
    )
    assert id(new_state) != orig_id
    # Original unchanged
    assert state.hypotheses[0]["confidence"] == 0.5
    # New state reflects the change
    assert new_state.hypotheses[0]["confidence"] == 0.9


def test_reducer_timeline_bounded_to_100():
    from soctalk.core.ir.reducer import Facts, apply_event

    state = Facts()
    for i in range(150):
        state = apply_event(
            state,
            "timeline_entry",
            {"ts": "2026-04-21T00:00:00Z", "summary": f"entry {i}"},
            seq=i,
        )
    assert len(state.timeline_summary) == 100
    # Last 100 retained; first ~50 dropped.
    assert state.timeline_summary[0]["summary"] == "entry 50"
    assert state.timeline_summary[-1]["summary"] == "entry 149"


def test_reducer_unknown_kind_is_no_op():
    from soctalk.core.ir.reducer import Facts, apply_event

    state = Facts(hypotheses=[{"id": "h1", "label": "x", "confidence": 0.5}])
    new_state = apply_event(state, "some_unknown_event_kind", {}, seq=7)
    # applied_seq moves forward, but everything else unchanged
    assert new_state.applied_seq == 7
    assert new_state.hypotheses == state.hypotheses


def test_reducer_replay_is_deterministic():
    """Applying the same events in the same order produces the same state."""

    from soctalk.core.ir.reducer import Facts, apply_event

    events = [
        ("alert_ingested", {"rule_id": "5720", "severity": 8, "ai_confidence": 0.8}),
        ("hypothesis_updated", {"id": "root", "confidence": 0.9, "rationale": "more"}),
        ("directive_added", {"id": "d1", "text": "always check SPF"}),
        ("timeline_entry", {"ts": "2026-04-21T00:00:00Z", "summary": "first"}),
        ("confidence_recalibrated", {"confidences": {"root": 0.75}}),
    ]

    def run_through() -> Facts:
        s = Facts()
        for i, (kind, payload) in enumerate(events, start=1):
            s = apply_event(s, kind, payload, seq=i)
        return s

    a = run_through()
    b = run_through()
    assert a.as_dict() == b.as_dict()
    assert a.hypotheses[0]["confidence"] == 0.75


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def test_assess_bands():
    from soctalk.core.ir.triage import assess

    a, c = assess(severity=10)
    assert a == "real"
    assert c >= 0.8

    a, c = assess(severity=6)
    assert a == "unclear"

    a, c = assess(severity=4)
    assert a == "likely_fp"

    a, c = assess(severity=1)
    assert a == "high_conf_fp"
    assert c >= 0.9


# ---------------------------------------------------------------------------
# Event signatures and idempotency
# ---------------------------------------------------------------------------


def test_alert_signature_coalesces_same_bucket():
    """Two events with the same (rule, asset, 5-min bucket) hash the same."""

    from soctalk.core.ir.events import alert_signature

    ts1 = datetime(2026, 4, 21, 12, 30, 15, tzinfo=timezone.utc)
    ts2 = datetime(2026, 4, 21, 12, 32, 45, tzinfo=timezone.utc)  # same bucket
    ts3 = datetime(2026, 4, 21, 12, 35, 5, tzinfo=timezone.utc)  # next bucket

    s1 = alert_signature("5720", ["host-42"], ts1)
    s2 = alert_signature("5720", ["host-42"], ts2)
    s3 = alert_signature("5720", ["host-42"], ts3)

    assert s1 == s2
    assert s1 != s3


def test_alert_signature_differs_by_asset():
    from soctalk.core.ir.events import alert_signature

    ts = datetime(2026, 4, 21, 12, 30, 15, tzinfo=timezone.utc)
    s1 = alert_signature("5720", ["host-42"], ts)
    s2 = alert_signature("5720", ["host-19"], ts)
    assert s1 != s2


def test_ioc_fingerprint_stable():
    from soctalk.core.ir.events import ioc_fingerprint

    a = ioc_fingerprint("ip", "203.0.113.7")
    b = ioc_fingerprint("ip", "203.0.113.7")
    assert a == b
    # Different type → different fingerprint
    c = ioc_fingerprint("hostname", "203.0.113.7")
    assert a != c


def test_proposal_idempotency_key_stable_across_reorder():
    from soctalk.core.ir.events import proposal_idempotency_key

    case = uuid4()
    k1 = proposal_idempotency_key(case, "block_ip", {"ip": "1.2.3.4", "ttl_days": 30})
    k2 = proposal_idempotency_key(case, "block_ip", {"ttl_days": 30, "ip": "1.2.3.4"})
    # Key ordering in params should not change the hash.
    assert k1 == k2


def test_event_idempotency_key_differs_by_payload():
    from soctalk.core.ir.events import EventKind, event_idempotency_key

    case = uuid4()
    k1 = event_idempotency_key(case, EventKind.ANALYST_MESSAGE, {"body": "hi"})
    k2 = event_idempotency_key(case, EventKind.ANALYST_MESSAGE, {"body": "hello"})
    assert k1 != k2


def test_run_scoped_key_is_lease_independent_unlike_worker_key():
    # #103: a run-scoped beat (the soft budget warning) must dedupe on
    # (run_id, kind) so a RECLAIMED run — a NEW lease re-running the graph and
    # re-crossing the threshold — collapses onto the first row instead of
    # minting a duplicate. The per-lease worker key intentionally does not.
    from soctalk.core.ir.events import (
        EventKind,
        run_scoped_event_idempotency_key,
        worker_event_idempotency_key,
    )

    run = uuid4()
    lease_a, lease_b = uuid4(), uuid4()

    rk_a = run_scoped_event_idempotency_key(run_id=run, kind=EventKind.BUDGET_WARNING)
    rk_b = run_scoped_event_idempotency_key(run_id=run, kind=EventKind.BUDGET_WARNING)
    assert rk_a == rk_b, "same run + kind must dedupe across leases"
    # A different run, or a different kind, is a different beat.
    assert rk_a != run_scoped_event_idempotency_key(
        run_id=uuid4(), kind=EventKind.BUDGET_WARNING
    )
    assert rk_a != run_scoped_event_idempotency_key(
        run_id=run, kind=EventKind.WORKER_RESULT
    )
    # Contrast: the per-lease worker key changes with the lease (by design).
    wk_a = worker_event_idempotency_key(run_id=run, lease_id=lease_a, client_ord=1)
    wk_b = worker_event_idempotency_key(run_id=run, lease_id=lease_b, client_ord=1)
    assert wk_a != wk_b


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def test_tool_registry_has_built_ins():
    from soctalk.core.ir.tools import registry

    names = {t.name for t in registry.list()}
    assert "investigation.list_iocs" in names
    assert "investigation.list_assets" in names


def test_approval_policy_defaults():
    from soctalk.core.ir.models import CapabilityClass
    from soctalk.core.ir.tools import ApprovalPolicy, DEFAULT_APPROVAL, approval_policy_for

    assert approval_policy_for(CapabilityClass.READ_LOCAL) == ApprovalPolicy.AUTONOMOUS
    assert (
        approval_policy_for(CapabilityClass.READ_EXTERNAL_SILENT)
        == ApprovalPolicy.AUTONOMOUS
    )
    assert (
        approval_policy_for(CapabilityClass.READ_EXTERNAL_ATTRIBUTED)
        == ApprovalPolicy.ANALYST_APPROVE
    )
    assert approval_policy_for(CapabilityClass.WRITE_SANDBOX) == ApprovalPolicy.ANALYST_APPROVE
    assert approval_policy_for(CapabilityClass.WRITE_EXTERNAL) == ApprovalPolicy.TYPED_REASON


def test_approval_policy_override():
    from soctalk.core.ir.models import CapabilityClass
    from soctalk.core.ir.tools import ApprovalPolicy, approval_policy_for

    overrides = {
        CapabilityClass.READ_EXTERNAL_ATTRIBUTED.value: ApprovalPolicy.AUTONOMOUS,
    }
    assert (
        approval_policy_for(CapabilityClass.READ_EXTERNAL_ATTRIBUTED, overrides)
        == ApprovalPolicy.AUTONOMOUS
    )


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------


def test_customer_case_detail_omits_mssp_only_fields():
    """Regression: the customer projection must not carry hypotheses,
    directives, policies, or MSSP routing metadata — those are
    MSSP-internal and must never reach the wire for customer callers."""

    from soctalk.core.api.ir import (
        CustomerCaseDetail,
        CustomerCaseFacts,
        CustomerCaseSummary,
    )

    # Facts shape carries only timeline_summary.
    assert "hypotheses" not in CustomerCaseFacts.model_fields
    assert "active_directives" not in CustomerCaseFacts.model_fields
    assert "active_policies" not in CustomerCaseFacts.model_fields
    assert "timeline_summary" in CustomerCaseFacts.model_fields

    # Summary and detail must not carry MSSP routing metadata.
    for cls in (CustomerCaseSummary, CustomerCaseDetail):
        assert "assignee_user_id" not in cls.model_fields, (
            f"{cls.__name__} must not expose MSSP assignee on the wire"
        )
        assert "tenant_id" not in cls.model_fields, (
            f"{cls.__name__} must not expose tenant_id on the wire"
        )

    # Detail exposes the narrowed facts.
    assert "facts" in CustomerCaseDetail.model_fields
    assert CustomerCaseDetail.model_fields["facts"].annotation is CustomerCaseFacts


def test_install_policies_returns_defaults():
    from soctalk.core.ir.policies import (
        INSTALL_POLICY_DEFAULTS,
        install_policies,
        reset_install_policy_cache,
    )

    reset_install_policy_cache()
    pol = install_policies()
    # All defaults present.
    for k in INSTALL_POLICY_DEFAULTS:
        assert k in pol
    assert pol["auto_close_enabled"] is True
    assert 0 < pol["auto_close_threshold"] <= 1
