"""Typed payload builders for pipeline replay events (issue #72, Phase 0).

The flight-recorder surfaces render ONLY persisted events, so these builders
are the single place where replay payload shapes, visibility, and size
discipline live. Three rules:

1. Builders never raise on odd input — clip, coerce, and record. Replay
   events are enrichment; they must never break the pipeline that emits them.
2. Visibility is part of the contract. The customer-facing receipt
   (verdict, guard rulings, policy resolution, terminal closes) is
   ``customer_safe``; operational detail (supervisor reasoning, worker
   summaries) stays ``mssp_only``. Tenant RLS enforces the split — the
   tenant UI renders missing beats as unavailable, never inferred.
3. Payloads are versioned (``payload_version``) and size-capped: free text
   and lists are clipped to fixed bounds (``_TEXT_CAP``/``_ITEM_CAP``/
   ``_LIST_CAP``). Clipping is silent by design — beats are summaries, and
   the full material lives on the run/review records they point at.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from soctalk.core.ir.events import EventKind
from soctalk.core.ir.models import Visibility

PAYLOAD_VERSION = 1

_TEXT_CAP = 2000
_ITEM_CAP = 400
_LIST_CAP = 20


def _clip(value: Any, cap: int = _TEXT_CAP) -> str:
    s = "" if value is None else str(value)
    return s[:cap]


def _clip_list(values: Any, item_cap: int = _ITEM_CAP) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [_clip(v, item_cap) for v in list(values)[:_LIST_CAP]]


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ReplayEvent(BaseModel):
    """One pipeline beat, ready for ``append_event`` (or the worker sink)."""

    kind: EventKind
    payload: dict[str, Any]
    visibility: str


def _base(extra: dict[str, Any]) -> dict[str, Any]:
    return {"payload_version": PAYLOAD_VERSION, **extra}


def policy_resolved(
    *,
    triage_policy: dict[str, Any] | None,
    deterministic_disposition: str | None,
    vetoes_checked: list[str] | None = None,
    vetoes_fired: list[str] | None = None,
    plane: str = "graph",
) -> ReplayEvent:
    """The policy gate's ruling. ``plane`` is ``graph`` or ``ingest``."""

    tp = triage_policy or {}
    return ReplayEvent(
        kind=EventKind.POLICY_RESOLVED,
        visibility=Visibility.CUSTOMER_SAFE.value,
        payload=_base(
            {
                "plane": plane,
                "triage_policy": _clip(tp.get("id"), 200) or None,
                "triage_policy_version": tp.get("version"),
                "deterministic_disposition": _clip(deterministic_disposition, 100)
                or None,
                "vetoes_checked": _clip_list(vetoes_checked),
                "vetoes_fired": _clip_list(vetoes_fired),
            }
        ),
    )


def supervisor_decision(
    decision: dict[str, Any] | None, *, iteration: int
) -> ReplayEvent:
    """One routing decision. Reasoning is analyst-facing → mssp_only."""

    d = decision or {}
    return ReplayEvent(
        kind=EventKind.SUPERVISOR_DECISION,
        visibility=Visibility.MSSP_ONLY.value,
        payload=_base(
            {
                "iteration": int(iteration),
                "next_action": _clip(d.get("next_action"), 40),
                "action_reasoning": _clip(d.get("action_reasoning")),
                "tp_confidence": _f(d.get("tp_confidence")),
            }
        ),
    )


def budget_warning(
    *,
    tokens_used: int,
    tokens_budget: int,
    dollars_used: float,
    dollars_budget: float,
    ratio: float,
) -> ReplayEvent:
    """Soft budget warning (#103): spend crossed ``ratio`` of the per-run cap.

    Analyst-facing (mssp_only). Fired once per run, before the hard halt at
    100%; the run keeps going.
    """
    return ReplayEvent(
        kind=EventKind.BUDGET_WARNING,
        visibility=Visibility.MSSP_ONLY.value,
        payload=_base(
            {
                "tokens_used": int(tokens_used),
                "tokens_budget": int(tokens_budget),
                "dollars_used": round(float(dollars_used), 4),
                "dollars_budget": round(float(dollars_budget), 4),
                "ratio": round(float(ratio), 2),
            }
        ),
    )


def worker_started(worker: str, *, action: str | None = None) -> ReplayEvent:
    return ReplayEvent(
        kind=EventKind.WORKER_STARTED,
        visibility=Visibility.MSSP_ONLY.value,
        payload=_base({"worker": _clip(worker, 60), "action": _clip(action, 40) or None}),
    )


def worker_result(
    worker: str,
    *,
    ok: bool,
    summary: str | None = None,
    counts: dict[str, int] | None = None,
) -> ReplayEvent:
    safe_counts = {
        _clip(k, 60): int(v)
        for k, v in (counts or {}).items()
        if isinstance(v, (int, float))
    }
    return ReplayEvent(
        kind=EventKind.WORKER_RESULT,
        visibility=Visibility.MSSP_ONLY.value,
        payload=_base(
            {
                "worker": _clip(worker, 60),
                "ok": bool(ok),
                "summary": _clip(summary) or None,
                "counts": safe_counts,
            }
        ),
    )


def verdict_rendered(verdict: dict[str, Any] | None) -> ReplayEvent:
    """The full verdict receipt. Verdict fields already reach tenants via
    closure narratives and review rows, so the payload is customer_safe."""

    v = verdict or {}
    decision = v.get("decision")
    decision = getattr(decision, "value", decision)
    return ReplayEvent(
        kind=EventKind.VERDICT_RENDERED,
        visibility=Visibility.CUSTOMER_SAFE.value,
        payload=_base(
            {
                "decision": _clip(decision, 40),
                "confidence": _f(v.get("confidence")),
                "evidence_strength": _clip(v.get("evidence_strength"), 40) or None,
                "potential_impact": _clip(v.get("potential_impact"), 40) or None,
                "urgency": _clip(v.get("urgency"), 40) or None,
                "threat_assessment": _clip(v.get("threat_assessment")),
                "key_evidence": _clip_list(v.get("key_evidence")),
                "gaps_in_evidence": _clip_list(v.get("gaps_in_evidence")),
                "alternative_explanations": _clip_list(
                    v.get("alternative_explanations")
                ),
                "recommendation": _clip(v.get("recommendation")),
                "reasoning_model": _clip(v.get("reasoning_model"), 100) or None,
            }
        ),
    )


def guard_evaluated(
    *,
    stage: str,
    decision_in: str | None,
    decision_out: str | None,
    effect: str,
    fired: list[str] | None = None,
    reasons: list[str] | None = None,
    checklist: list[str] | None = None,
) -> ReplayEvent:
    """A guard ruling — the receipt beat. ``stage`` is one of
    ``verdict_guard`` | ``worker_floor`` | ``server_floor`` | ``operational``
    | ``ingest``; ``effect`` is ``pass`` | ``override`` | ``interrupt``."""

    return ReplayEvent(
        kind=EventKind.GUARD_EVALUATED,
        visibility=Visibility.CUSTOMER_SAFE.value,
        payload=_base(
            {
                "stage": _clip(stage, 40),
                "decision_in": _clip(decision_in, 40) or None,
                "decision_out": _clip(decision_out, 40) or None,
                "effect": _clip(effect, 20),
                "fired": _clip_list(fired),
                "reasons": _clip_list(reasons),
                "checklist": _clip_list(checklist),
            }
        ),
    )


def human_review_requested(
    *,
    reason: str | None,
    verdict_decision: str | None,
    verdict_confidence: float | None,
) -> ReplayEvent:
    """The human-lane entry beat. Reason text is analyst-facing →
    mssp_only; the full review context lives on pending_reviews."""

    return ReplayEvent(
        kind=EventKind.HUMAN_REVIEW_REQUESTED,
        visibility=Visibility.MSSP_ONLY.value,
        payload=_base(
            {
                "reason": _clip(reason) or None,
                "verdict_decision": _clip(verdict_decision, 40) or None,
                "verdict_confidence": _f(verdict_confidence),
            }
        ),
    )


def human_decision(decision: str | None) -> ReplayEvent:
    """The human ruling beat. Reviewer identity and free-text feedback are
    deliberately excluded — compact and safe wherever the row is visible."""

    return ReplayEvent(
        kind=EventKind.HUMAN_DECISION,
        visibility=Visibility.MSSP_ONLY.value,
        payload=_base({"decision": _clip(decision, 40) or None}),
    )


def case_closed(
    *,
    path: str,
    reason: str | None,
    run_id: str | None = None,
) -> ReplayEvent:
    """Terminal close beat, appended by the plane that DECIDES the close
    (ingest triage or ``complete_run``), in the same transaction as the row
    transition. ``path`` is the disposition-path taxonomy the fleet view
    aggregates on: ``ingest_memoized`` | ``ingest_rules`` | ``operational``
    | ``reasoning``. Reuses ``EventKind.AUTO_CLOSED``; the reducer
    deliberately ignores it (row-side state owns closes)."""

    return ReplayEvent(
        kind=EventKind.AUTO_CLOSED,
        visibility=Visibility.CUSTOMER_SAFE.value,
        payload=_base(
            {
                "path": _clip(path, 40),
                "reason": _clip(reason) or None,
                "run_id": _clip(run_id, 60) or None,
            }
        ),
    )


__all__ = [
    "PAYLOAD_VERSION",
    "ReplayEvent",
    "case_closed",
    "guard_evaluated",
    "human_decision",
    "human_review_requested",
    "policy_resolved",
    "supervisor_decision",
    "verdict_rendered",
    "worker_result",
    "worker_started",
]
