"""Response-playbook schema + the envelope condition contract (issue #49).

A response playbook is data, not code: it matches on the disposition envelope,
names tier-0 capabilities from the vetted allowlist, and can express nothing
else. ``extra="forbid"`` everywhere — a typo'd field rejects the whole file at
load (fail closed), never silently does nothing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from soctalk.response.capabilities import ON_CLOSE_ALLOWED, RESPONSE_CAPABILITIES
from soctalk.triage_policy.conditions import validate_condition

# The disposition envelope's schema version. Bump on any breaking change to
# the envelope shape — the envelope is a PUBLIC contract (webhook receivers
# parse it), not an internal detail.
# v2: MITRE normalized to the codebase convention — mitre.techniques now carries
# the canonical Txxxx ids (was names), mitre.tactics added, names demoted to the
# non-contract mitre.technique_names display field.
ENVELOPE_VERSION = 2

# The documented read-only surface a response ``when:`` condition may
# reference — same discipline as the triage-policy STATE_CONTRACT: additions
# are deliberate API decisions, never reflected in from arbitrary state.
# List-valued fields (rule.groups, rule.ids, mitre.techniques, mitre.tactics) are
# membership targets: ``{"in": ["sudo", {"var": "rule.groups"}]}``.
RESPONSE_STATE_CONTRACT: frozenset[str] = frozenset(
    {
        "disposition",
        "worker_disposition",
        "floor_vetoed",
        "verdict_confidence",
        "severity",
        "rule.groups",
        "rule.ids",
        # ATT&CK. techniques = canonical Txxxx ids (core/ir/triage.py convention;
        # the human-readable technique names are display-only and NOT here).
        # tactics = the tactic strings the source emits verbatim (Wazuh sends
        # NAMES like "Lateral Movement", not TA refs) — match on those.
        "mitre.techniques",
        "mitre.tactics",
    }
)


class ResponseAction(BaseModel):
    """One capability invocation: a vetted name, an optional sandboxed
    condition over the envelope contract, and opaque params the capability
    handler interprets (fail closed there too)."""

    model_config = ConfigDict(extra="forbid")

    capability: str
    when: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _legality(self) -> ResponseAction:
        if self.capability not in RESPONSE_CAPABILITIES:
            raise ValueError(
                f"capability {self.capability!r} is not in the vetted allowlist"
            )
        if self.when is not None:
            validate_condition(self.when, RESPONSE_STATE_CONTRACT)
        return self


class ResponseMatch(BaseModel):
    """Envelope-matching rules, OR'd — same semantics as TriagePolicyMatch.
    Empty (the default) matches everything: the disposition phase lists
    (on_escalate/on_close) already scope when the playbook fires.

    ATT&CK matchers: ``mitre_techniques`` are Txxxx technique ids (never technique
    names — the codebase convention, core/ir/triage.py); ``mitre_tactics`` are the
    tactic strings the source emits (Wazuh sends names like "Lateral Movement")."""

    model_config = ConfigDict(extra="forbid")

    rule_groups: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    mitre_techniques: list[str] = Field(default_factory=list)
    mitre_tactics: list[str] = Field(default_factory=list)


class ResponseBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_escalate: list[ResponseAction] = Field(default_factory=list, max_length=8)
    on_close: list[ResponseAction] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _close_tier(self) -> ResponseBlock:
        for action in self.on_close:
            if action.capability not in ON_CLOSE_ALLOWED:
                raise ValueError(
                    f"on_close permits only {sorted(ON_CLOSE_ALLOWED)} in phase 1 "
                    f"(got {action.capability!r}) — a close is the "
                    "suppression-shaped direction"
                )
        return self


class ResponsePlaybook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    version: int = 1
    # "*" applies to every tenant; a concrete value is the tenant slug or UUID
    # (the dispatcher matches against both — L1 knows the tenant per request,
    # unlike the env-scoped runs-worker).
    tenant: str = "*"
    # File-loaded playbooks default to shadow (the parser sets it): matched and
    # audited, nothing enqueued, until the author flips to active — the same
    # activation discipline as triage policies (#44).
    status: Literal["active", "shadow"] = "active"
    priority: int = 100
    applies_to: ResponseMatch = Field(default_factory=ResponseMatch)
    response: ResponseBlock = Field(default_factory=ResponseBlock)

    def actions_for(self, disposition: str) -> list[ResponseAction]:
        if disposition == "escalate":
            return self.response.on_escalate
        if disposition == "close_fp":
            return self.response.on_close
        return []
