"""Post-verdict guard: pure functions that decide whether the LLM's verdict commits.

The guard fires only on deterministic edges; the ambiguous middle passes through:

- ``authz.class == contradicted`` caps a close at escalate — authorization records ARE
  present but fail to cover (expired, out-of-window, wrong scope, CAB-unapproved) or a
  prohibition forbids the action. The mismatch is the finding; a human responder is the
  right verifier. This is the engine's judgment enforced, not a keyword branch.
- malicious signal caps a close at escalate — the "authorization never overrides an
  IOC" sentence in the verdict prompt, promoted from instruction to enforcement.

``absent`` (no record of the right kind) deliberately does NOT override in this
increment: the verdict prompt already steers absent-evidence cases to needs_more_info
and the model is free to close on non-authorization grounds. Everything here is
side-effect free and unit-tested case-for-case, same discipline as the expectedness
parity test.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel

from soctalk.authorization.engine import (
    AuthzClass,
    derive_authz_class,
    resolved_entity,
    select_facts,
)
from soctalk.models.authorization import (
    AuthorizationComponents,
    AuthorizationContext,
    AuthorizationEntityKind,
    AuthorizationTrack,
)

GUARDRAIL_AUTHZ_CONTRADICTED = "authorization_contradicted_close"
GUARDRAIL_IOC_OVER_CLOSE = "ioc_over_close"
# Closing on evidence that was never gathered (#122). Distinct from the IOC edge:
# there the evidence exists and is damning; here it is simply absent, so the
# draft is INTERRUPTED for a human rather than escalated outright.
GUARDRAIL_INTEL_UNAVAILABLE = "intel_unavailable_close"
# Sign-off scope note (#45): this rule governs LLM-close COMMITS in the graph
# plane, where asset data classification is knowable (authorization facts). The
# ingest plane cannot apply it — no facts exist at ingest — so a memoized close
# cached BEFORE the rule existed can still replay for a sensitive shape until
# its TTL. Forward-protected: an interrupted close memoizes as ``escalate``, so
# post-rule recurrences of a sign-off shape always reach review. Full ingest
# coverage arrives with shape provenance / CMDB classification (M2 Phase b).
GUARDRAIL_SIGNOFF_CLOSE = "sensitive_asset_close_signoff"


class GuardOverride(BaseModel):
    """One fired guardrail — the audit record of an override or interrupt."""

    guardrail: str
    effect: Literal["override", "interrupt"] = "override"
    from_decision: str
    to_decision: str
    reason: str


class GuardResult(BaseModel):
    final_decision: str
    authz_class: AuthzClass | None = None
    components: AuthorizationComponents | None = None
    overrides: list[GuardOverride] = []
    # interrupt (#45): the draft decision stands, but a human signs off before it
    # takes effect — distinct from an override in both routing and audit.
    interrupted: bool = False
    # The state-contract context the conditions were evaluated against (#44) —
    # exposed so shadow triage policies are judged against the exact same facts.
    condition_ctx: dict[str, Any] | None = None

    @property
    def overridden(self) -> bool:
        return any(o.effect == "override" for o in self.overrides)


def asset_data_classification(context: AuthorizationContext | None) -> str | None:
    """The trust-resolved data classification of the activity's asset, or None
    when unknown (no context, FIM track, or no asset record). Uses the engine's
    own selection + resolution so the sign-off rule and the evaluation can never
    disagree about which record speaks for the asset."""
    if context is None or context.activity.track != AuthorizationTrack.ACCOUNT:
        return None
    if not context.activity.host:
        return None
    selected = select_facts(context.facts, context.activity.track, context.tenant)
    asset = resolved_entity(selected, AuthorizationEntityKind.ASSET, context.activity.host)
    if asset is None or asset.data_classification is None:
        return None
    return str(asset.data_classification).lower()


def condition_context(
    *,
    verdict_decision: str,
    verdict_confidence: float | None,
    authz_class: AuthzClass,
    components: AuthorizationComponents | None,
    context: AuthorizationContext | None,
    malicious_signal: bool,
    active_incident: bool,
) -> dict[str, Any]:
    """The read-only state contract (#43/#44) declarative conditions run against.
    Every field here must be declared in ``conditions.STATE_CONTRACT``."""
    asset_env = asset_crit = None
    if context is not None and context.activity.track == AuthorizationTrack.ACCOUNT:
        selected = select_facts(context.facts, context.activity.track, context.tenant)
        asset = resolved_entity(
            selected, AuthorizationEntityKind.ASSET, context.activity.host or ""
        )
        if asset is not None:
            asset_env = asset.environment
            asset_crit = asset.criticality
    return {
        "authz": {
            "class": authz_class,
            "in_scope": components.in_scope if components else False,
            "sanctioned_or_routine": (
                components.sanctioned_or_routine if components else False
            ),
            "actor_genuine": components.actor_genuine if components else True,
            "policy_allowed": components.policy_allowed if components else True,
        },
        "verdict": verdict_decision,
        "verdict_confidence": verdict_confidence,
        "asset": {
            "data_classification": asset_data_classification(context),
            "environment": asset_env,
            "criticality": asset_crit,
        },
        "enrichment": {"ioc": malicious_signal},
        "correlation": {"active_incident": active_incident},
    }


def evaluate_guard(
    *,
    verdict_decision: str,
    context: AuthorizationContext | None,
    malicious_signal: bool,
    close_signoff_data_classes: Sequence[str] = (),
    guardrails: Sequence[dict[str, Any]] = (),
    verdict_confidence: float | None = None,
    active_incident: bool = False,
    intel_unavailable: bool = False,
) -> GuardResult:
    """The guard's whole decision, as a pure function.

    Only a ``close`` draft is ever touched; escalate and needs_more_info always commit
    (the guard only ever RAISES suspicion, mirroring the engine, which only ever lowers
    it by finding covering evidence). The IOC edge is checked first — the floor always
    outranks authorization reasoning. A close that would otherwise COMMIT is
    interrupted for human sign-off when the activity's asset carries one of the
    triage policy's ``close_signoff_data_classes`` (#45): the draft stays intact, a human
    disposes — even a fully covered close on such an asset is not automatic.
    """
    authz_class, components = derive_authz_class(context)
    overrides: list[GuardOverride] = []
    interrupted = False
    if verdict_decision == "close" and malicious_signal:
        overrides.append(
            GuardOverride(
                guardrail=GUARDRAIL_IOC_OVER_CLOSE,
                from_decision="close",
                to_decision="escalate",
                reason=(
                    "malicious indicators present — authorization/benign evidence "
                    "never overrides an IOC"
                ),
            )
        )
    elif verdict_decision == "close" and authz_class == "contradicted":
        overrides.append(
            GuardOverride(
                guardrail=GUARDRAIL_AUTHZ_CONTRADICTED,
                from_decision="close",
                to_decision="escalate",
                reason=(
                    "authorization records present but do not cover this activity — "
                    "acting outside the terms of an authorization is the finding"
                ),
            )
        )
    # A close drafted while threat intelligence the tenant EXPECTS was
    # unreachable is a close on absent evidence. It is not escalated -- nothing
    # says the activity is malicious -- but it must not commit automatically
    # either, because the model cannot distinguish "unknown to MISP" from "MISP
    # never answered" and the failure mode gets MORE likely exactly when
    # infrastructure is degraded (#122).
    if verdict_decision == "close" and intel_unavailable and not overrides:
        interrupted = True

    ctx = condition_context(
        verdict_decision=verdict_decision,
        verdict_confidence=verdict_confidence,
        authz_class=authz_class,
        components=components,
        context=context,
        malicious_signal=malicious_signal,
        active_incident=active_incident,
    )
    final_decision = "escalate" if overrides else verdict_decision

    # Declarative guardrails (#44) — evaluated AFTER the code edges, first match
    # wins, additive only: an override may only move the decision UP the
    # close < needs_more_info < escalate ladder (the schema's ``to`` enum plus
    # this rank check make suppression inexpressible).
    if not overrides:
        from soctalk.triage_policy.conditions import evaluate_condition
        from soctalk.triage_policy.models import DECISION_RANK

        for i, rule in enumerate(guardrails):
            if not isinstance(rule, dict) or not evaluate_condition(
                rule.get("when"), ctx
            ):
                continue
            effect = rule.get("effect")
            to = str(rule.get("to") or "")
            reason = str(rule.get("reason") or "triage policy guardrail")[:512]
            if effect == "override":
                if DECISION_RANK.get(to, -1) <= DECISION_RANK.get(verdict_decision, 99):
                    continue  # raise-only: a non-raising override never fires
                overrides.append(
                    GuardOverride(
                        guardrail=f"triage_policy_guardrail_{i}",
                        from_decision=verdict_decision,
                        to_decision=to,
                        reason=reason,
                    )
                )
                final_decision = to
                break
            if effect == "interrupt":
                interrupted = True
                overrides.append(
                    GuardOverride(
                        guardrail=f"triage_policy_guardrail_{i}",
                        effect="interrupt",
                        from_decision=verdict_decision,
                        to_decision="human_review",
                        reason=reason,
                    )
                )
                break

    # Built-in sign-off interrupt (#45) — only when nothing above fired.
    if not overrides and verdict_decision == "close" and close_signoff_data_classes:
        data_class = asset_data_classification(context)
        if data_class is not None and data_class in {
            str(c).lower() for c in close_signoff_data_classes
        }:
            interrupted = True
            overrides.append(
                GuardOverride(
                    guardrail=GUARDRAIL_SIGNOFF_CLOSE,
                    effect="interrupt",
                    from_decision="close",
                    to_decision="human_review",
                    reason=(
                        f"close on a {data_class}-classified asset requires human "
                        "sign-off — the draft stands, a human disposes"
                    ),
                )
            )
    return GuardResult(
        final_decision=final_decision,
        authz_class=authz_class,
        components=components,
        overrides=overrides,
        interrupted=interrupted,
        condition_ctx=ctx,
    )


def shadow_guardrail_audits(
    shadow_triage_policies: Sequence[dict[str, Any]], ctx: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Would-fire records for SHADOW triage policies' guardrails against the same
    contract context the active guard used (#44). Pure; never mutates anything —
    the caller appends these to the audit trail only.

    Mirrors ACTIVE semantics exactly (Codex #44 finding: divergent shadow data
    corrupts the activation evidence): first matching rule per triage policy wins, and
    a non-raising override is skipped just as the live guard would skip it — a
    rule that would be ignored when active must not be logged as would-fire.
    """
    from soctalk.triage_policy.conditions import evaluate_condition
    from soctalk.triage_policy.models import DECISION_RANK

    if not ctx:
        return []
    draft = str(ctx.get("verdict") or "")
    audits: list[dict[str, Any]] = []
    for pb in shadow_triage_policies:
        if not isinstance(pb, dict):
            continue
        for i, rule in enumerate(pb.get("guardrails") or []):
            if not isinstance(rule, dict) or not evaluate_condition(
                rule.get("when"), ctx
            ):
                continue
            to = str(rule.get("to") or "")
            if rule.get("effect") == "override" and DECISION_RANK.get(
                to, -1
            ) <= DECISION_RANK.get(draft, 99):
                continue  # raise-only, same as the live guard
            audits.append(
                {
                    "shadow": True,
                    "triage_policy": pb.get("id"),
                    "guardrail": f"triage_policy_guardrail_{i}",
                    "would_effect": rule.get("effect"),
                    "would_to": to,
                    "reason": str(rule.get("reason") or "")[:512],
                }
            )
            break  # first match wins, same as the live guard
    return audits


def decision_value(raw: Any) -> str:
    """A verdict/supervisor decision as a plain lowercase string. Pydantic
    ``model_dump()`` keeps (str, Enum) instances in the state dict, so a naive
    ``str(v)`` yields ``"VerdictDecision.close"``."""
    if hasattr(raw, "value"):
        return str(raw.value).lower()
    return str(raw or "").lower()
