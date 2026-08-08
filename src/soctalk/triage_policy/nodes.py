"""Deterministic triage policy nodes for the SecOps graph (issue #43).

Three plain LangGraph nodes — no LLM calls, no I/O beyond state:

- ``resolve_triage_policy_node``: entry node; matches the investigation against the
  built-in registry and writes the active triage policy into state.
- ``gather_authorization_context_node``: the required step the pre-verdict gate
  reroutes to. Connectors are separate work, so today it gathers from the sources
  that exist — the claim payload / fixture context already on the investigation —
  validates it, stamps the engine components, and records absence explicitly
  (absence is first-class evidence, never implicit approval).
- ``verdict_guard_node``: runs the guard over the verdict draft; on override it
  rewrites the decision, appends the audit record to ``state["playbook_audit"]``,
  and annotates the recommendation so the analyst sees both the LLM draft and why
  it did not stand.
- ``operational_close_node``: the ``close_operational`` deterministic disposition —
  closes an operational-class alert without any LLM call, reached only when
  ``route_from_resolve_triage_policy`` found no security-indicator veto.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from soctalk.authorization.engine import evaluate_authorization
from soctalk.authorization.render import has_malicious_signal, parse_authorization_context
from soctalk.core.ir import replay_events
from soctalk.graph.event_sink import emit as emit_replay
from soctalk.triage_policy.guard import (
    decision_value,
    evaluate_guard,
    shadow_guardrail_audits,
)
from soctalk.triage_policy.models import (
    CLOSE_OPERATIONAL,
    GATHER_AUTHORIZATION_CONTEXT,
    KNOWN_DISPOSITIONS,
)
from soctalk.triage_policy.operational import (
    VETO_MALICIOUS,
    VETO_MITRE,
    VETO_OBSERVABLES,
    VETO_SEVERITY,
    VETO_UNATTESTED_CLASS,
    operational_close_vetoes,
)
from soctalk.triage_policy.registry import match_shadow_triage_policies, match_triage_policy

logger = structlog.get_logger()

OPERATIONAL_VETO_CHECKLIST = [
    VETO_MITRE,
    VETO_OBSERVABLES,
    VETO_SEVERITY,
    VETO_MALICIOUS,
    VETO_UNATTESTED_CLASS,
]


async def resolve_triage_policy_node(state: dict[str, Any]) -> dict[str, Any]:
    """Match the alert against the triage policy registry; write the winner into state.
    Matching SHADOW triage policies are recorded alongside (#44) — their guardrails get
    would-fire audit records at the guard, but they never govern anything."""
    investigation = state.get("investigation", {}) or {}
    triage_policy = match_triage_policy(investigation)
    if triage_policy is not None:
        state["triage_policy"] = state["playbook"] = triage_policy.model_dump()
        logger.info(
            "triage_policy_resolved",
            triage_policy=triage_policy.id,
            version=triage_policy.version,
            required_steps=triage_policy.required_steps,
        )
    else:
        logger.debug("triage_policy_none_matched")
    shadow = match_shadow_triage_policies(investigation)
    if shadow:
        state["triage_policy_shadow"] = state["playbook_shadow"] = [p.model_dump() for p in shadow]
        logger.info(
            "triage_policy_shadow_matched", triage_policies=[p.id for p in shadow]
        )

    # Replay beat (#72): the policy gate's ruling, including the operational
    # veto checklist when a deterministic disposition is on the table. The
    # vetoes are computed HERE (same args as the router) and stashed so the
    # routing function reads this result instead of recomputing — the router
    # stays pure and can never disagree with the emitted event.
    tp = (state.get("triage_policy") or {}) if triage_policy is not None else {}
    disposition = tp.get("deterministic_disposition")
    vetoes: list[str] | None = None
    if disposition and disposition in KNOWN_DISPOSITIONS:
        vetoes = operational_close_vetoes(
            investigation,
            class_rule_groups=(tp.get("applies_to") or {}).get("rule_groups"),
        )
        state["operational_vetoes"] = vetoes
    emit_replay(
        replay_events.policy_resolved(
            triage_policy=tp or None,
            deterministic_disposition=disposition,
            vetoes_checked=OPERATIONAL_VETO_CHECKLIST if vetoes is not None else [],
            vetoes_fired=vetoes or [],
        )
    )
    return state


async def gather_authorization_context_node(state: dict[str, Any]) -> dict[str, Any]:
    """Load authorization evidence into state and mark the required step as run.

    The step is recorded as run UNCONDITIONALLY (first line, before any work) — the
    pre-verdict gate reroutes here whenever the step is missing, so a step that could
    fail without recording itself would loop the graph forever.
    """
    steps: list[str] = state.setdefault(
        "triage_policy_steps_run", state.setdefault("playbook_steps_run", [])
    )
    if GATHER_AUTHORIZATION_CONTEXT not in steps:
        steps.append(GATHER_AUTHORIZATION_CONTEXT)

    emit_replay(replay_events.worker_started("authorization_context"))
    investigation = state.get("investigation", {}) or {}
    ctx = parse_authorization_context(investigation)
    if ctx is None:
        # Absence is the gathered answer, stated explicitly: no record of the right
        # kind exists in any connected source. The verdict prompt's absent-vs-
        # contradicted rubric takes it from here.
        state["authorization_gathered"] = {"status": "absent", "facts": 0}
        logger.info("authorization_context_gathered", status="absent")
        emit_replay(
            replay_events.worker_result(
                "authorization_context",
                ok=True,
                summary="no authorization record of the right kind in any connected source",
                counts={"facts": 0},
            )
        )
        return state

    components = evaluate_authorization(ctx.activity, ctx.facts, ctx.tenant)
    ctx.components = components
    investigation["authorization_context"] = ctx.model_dump(mode="json")
    state["investigation"] = investigation
    state["authorization_gathered"] = {
        "status": "present",
        "facts": len(ctx.facts),
        "components": components.model_dump(),
    }
    logger.info(
        "authorization_context_gathered",
        status="present",
        facts=len(ctx.facts),
        engine_decision=components.decision,
    )
    emit_replay(
        replay_events.worker_result(
            "authorization_context",
            ok=True,
            summary=f"authorization evidence gathered: engine decision {components.decision}",
            counts={"facts": len(ctx.facts)},
        )
    )
    return state


async def operational_close_node(state: dict[str, Any]) -> dict[str, Any]:
    """Deterministically close an operational-class alert — no LLM involved.

    Reached only via ``route_from_resolve_triage_policy`` after the security-indicator
    check came back clean. Writes a supervisor-shaped CLOSE decision (the worker's
    disposition mapping and the close node already understand that shape) plus a
    playbook_audit record, so the close reason and the audit trail both say this
    was a triage policy disposition, not a model judgment. The terminal safety floor in
    the runs-worker still applies to the resulting ``close_fp``, unchanged.
    """
    triage_policy = (state.get("triage_policy") or state.get("playbook")) or {}
    reasoning = (
        f"triage policy {triage_policy.get('id')}: operational alert class (agent health) "
        "with no security indicators — deterministic close, no LLM invoked"
    )
    state["supervisor_decision"] = {
        "next_action": "CLOSE",
        "action_reasoning": reasoning,
        "tp_confidence": 0.0,
        "confidence_reasoning": "deterministic triage policy disposition",
    }
    state.setdefault("triage_policy_audit", state.setdefault("playbook_audit", [])).append(
        {
            "at": datetime.now(UTC).isoformat(),
            "triage_policy": triage_policy.get("id"),
            "effect": "deterministic_disposition",
            "disposition": CLOSE_OPERATIONAL,
            "reason": reasoning,
        }
    )
    state["operational_close"] = True
    logger.info("operational_close_applied", triage_policy=triage_policy.get("id"))
    emit_replay(
        replay_events.guard_evaluated(
            stage="operational",
            decision_in=CLOSE_OPERATIONAL,
            decision_out=CLOSE_OPERATIONAL,
            effect="pass",
            checklist=OPERATIONAL_VETO_CHECKLIST,
        )
    )
    return state


async def verdict_guard_node(state: dict[str, Any]) -> dict[str, Any]:
    """Post-verdict guard: LLM proposed, this node disposes.

    Runs on every verdict (the floor edges are unconditional; triage policy presence only
    gates the pre-verdict reroute). Provider errors and missing verdicts pass through
    untouched — the worker's failed-run contract owns those.
    """
    verdict = state.get("verdict") or {}
    if state.get("verdict_error") or not verdict:
        return state

    triage_policy = (state.get("triage_policy") or state.get("playbook")) or {}
    triage_policy_id = triage_policy.get("id")
    investigation = state.get("investigation", {}) or {}
    # Each verdict pass gets a fresh guard ruling — a stale interrupt flag from a
    # prior pass (e.g. human MORE_INFO -> supervisor -> new verdict) must not
    # route an uninterrupted draft to review.
    state.pop("verdict_interrupted", None)
    state.pop("verdict_overridden_by_guard", None)
    draft_decision = decision_value(verdict.get("decision"))
    try:
        verdict_confidence = float(verdict.get("confidence"))
    except (TypeError, ValueError):
        verdict_confidence = None
    correlation = state.get("correlation") or {}
    result = evaluate_guard(
        verdict_decision=draft_decision,
        context=parse_authorization_context(investigation),
        malicious_signal=has_malicious_signal(investigation),
        close_signoff_data_classes=triage_policy.get("close_signoff_data_classes") or (),
        guardrails=triage_policy.get("guardrails") or (),
        verdict_confidence=verdict_confidence,
        active_incident=bool(
            isinstance(correlation, dict) and correlation.get("active_incident")
        ),
        # Only when intel was EXPECTED and did not answer. A tenant with no MISP
        # configured is a normal setup whose closes are unaffected; "degraded"
        # counts because the specific indicator in question may be one of the
        # lookups that failed (#122).
        intel_unavailable=(
            (investigation.get("misp_context") or {}).get("status")
            in ("unavailable", "degraded")
        ),
    )
    state["authz_class"] = result.authz_class

    # Shadow triage policies (#44): would-fire records against the exact same context,
    # audit-only — this is the evidence that graduates a shadow triage policy to active.
    shadow_audits = shadow_guardrail_audits(
        (state.get("triage_policy_shadow") or state.get("playbook_shadow")) or (),
        result.condition_ctx,
    )
    for entry in shadow_audits:
        state.setdefault("triage_policy_audit", state.setdefault("playbook_audit", [])).append(
            {"at": datetime.now(UTC).isoformat(), **entry}
        )
        logger.info("triage_policy_shadow_would_fire", **entry)

    if not result.overrides:
        logger.debug(
            "verdict_guard_pass", decision=draft_decision, authz_class=result.authz_class
        )
        # Replay beat (#72): passes are recorded, not just vetoes — the
        # receipt must show the gate was open, not absent.
        emit_replay(
            replay_events.guard_evaluated(
                stage="verdict_guard",
                decision_in=draft_decision,
                decision_out=draft_decision,
                effect="pass",
            )
        )
        return state

    audit = {
        "at": datetime.now(UTC).isoformat(),
        "triage_policy": triage_policy_id,
        "authz_class": result.authz_class,
        "components": result.components.model_dump() if result.components else None,
        "llm_draft_decision": draft_decision,
        "final_decision": result.final_decision,
        "overrides": [o.model_dump() for o in result.overrides],
    }
    state.setdefault("triage_policy_audit", state.setdefault("playbook_audit", [])).append(audit)

    if result.interrupted:
        # The draft stands untouched — a human disposes. Routing (and the
        # worker's disposition mapping) read the flag, not the verdict.
        state["verdict_interrupted"] = True
    if result.overridden:
        reasons = "; ".join(
            o.reason for o in result.overrides if o.effect == "override"
        )
        verdict = dict(verdict)
        verdict["decision"] = result.final_decision
        verdict["recommendation"] = (
            f"[GUARD OVERRIDE: LLM drafted '{draft_decision}', enforced '"
            f"{result.final_decision}' — {reasons}] "
            f"{verdict.get('recommendation') or ''}"
        ).strip()[:2048]
        state["verdict"] = verdict
        state["verdict_overridden_by_guard"] = True
        if result.final_decision == "needs_more_info":
            # A guard-manufactured needs_more_info must count as a retry, same
            # contract as the verdict node's own NMI (Codex #44 High): the LLM
            # never incremented the counter for its close draft, and without
            # this the supervisor→verdict→override loop repeats to the
            # recursion limit instead of forcing human review on the retry.
            state["verdict_retry_count"] = state.get("verdict_retry_count", 0) + 1

    for o in result.overrides:
        logger.warning(
            "verdict_guard_" + o.effect,
            guardrail=o.guardrail,
            triage_policy=triage_policy_id,
            from_decision=o.from_decision,
            to_decision=o.to_decision,
            authz_class=result.authz_class,
        )
    emit_replay(
        replay_events.guard_evaluated(
            stage="verdict_guard",
            decision_in=draft_decision,
            decision_out=result.final_decision if result.overridden else draft_decision,
            effect="interrupt" if result.interrupted else "override",
            fired=[o.guardrail for o in result.overrides],
            reasons=[o.reason for o in result.overrides],
        )
    )
    return state
