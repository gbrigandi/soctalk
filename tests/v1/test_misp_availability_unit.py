"""Threat-intel availability must never read as 'no matches' (#122).

The incident this pins: with MISP unbound, the worker wrote a context
indistinguishable from a successful lookup that found nothing, and the
supervisor quoted it back as "MISP context returned no matches" to close a
brute-force-then-success sequence as a false positive. Absence of evidence was
consumed as evidence of absence, and the pathway gets MORE likely exactly when
infrastructure is degraded.
"""
from __future__ import annotations

import pytest


def _supervisor_text(misp_context: dict) -> str:
    """Render the MISP block the supervisor actually sees."""
    from soctalk.supervisor.node import _build_context_summary

    state = {
        "investigation": {
            "title": "sshd brute force then success",
            "severity": "high",
            "observables": [],
            "misp_context": misp_context,
        }
    }
    return _build_context_summary(state)


def test_unreachable_intel_is_not_rendered_as_zero_matches():
    text = _supervisor_text(
        {
            "status": "unavailable",
            "matches": [],
            "checked_iocs": [],
            "unavailable_reason": "MISP is configured for this tenant but its client is not bound",
        }
    )
    assert "THREAT INTELLIGENCE UNAVAILABLE" in text
    # The exact phrasing the supervisor previously cited to justify closing.
    assert "**Matches:** 0" not in text
    assert "MISSING evidence" in text


def test_a_real_empty_lookup_still_reads_as_zero_matches():
    """The honest case must keep saying what it means."""
    text = _supervisor_text(
        {"status": "ok", "matches": [], "checked_iocs": ["198.51.100.77"]}
    )
    assert "**IOCs checked:** 1, **Matches:** 0" in text
    assert "UNAVAILABLE" not in text


def test_no_misp_configured_is_not_reported_as_a_failure():
    """A tenant without MISP is a normal configuration, not a degraded one."""
    text = _supervisor_text({"status": "not_configured", "matches": [], "checked_iocs": []})
    assert "not part of this investigation's evidence" in text
    assert "UNAVAILABLE" not in text


def test_partial_outage_says_the_rest_are_unknown_not_clean():
    text = _supervisor_text(
        {
            "status": "degraded",
            "matches": [],
            "checked_iocs": ["1.1.1.1"],
            "failed_checks": ["198.51.100.77"],
        }
    )
    assert "PARTIALLY UNAVAILABLE" in text
    assert "unknown, not clean" in text


# --- the guard edge --------------------------------------------------------


@pytest.mark.parametrize(
    "status,should_interrupt",
    [
        ("unavailable", True),
        ("degraded", True),
        ("ok", False),
        ("not_configured", False),
        (None, False),
    ],
)
def test_close_is_interrupted_only_when_expected_intel_did_not_answer(
    status, should_interrupt
):
    """A close on evidence never gathered goes to a human.

    Not escalated -- nothing says the activity is malicious -- but not committed
    automatically either. Crucially NOT triggered for a tenant with no MISP,
    which would otherwise stop auto-close working for every such install.
    """
    from soctalk.triage_policy.guard import evaluate_guard

    result = evaluate_guard(
        verdict_decision="close",
        context=None,
        malicious_signal=False,
        intel_unavailable=status in ("unavailable", "degraded"),
    )
    assert result.interrupted is should_interrupt
    # The guard only ever raises suspicion; it never rewrites close to escalate here.
    assert result.final_decision == "close"


def test_escalate_is_never_touched_by_the_intel_edge():
    from soctalk.triage_policy.guard import evaluate_guard

    r = evaluate_guard(
        verdict_decision="escalate",
        context=None,
        malicious_signal=False,
        intel_unavailable=True,
    )
    assert r.interrupted is False and r.final_decision == "escalate"
