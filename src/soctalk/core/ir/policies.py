"""Policy loader: install YAML defaults + per-tenant Postgres overrides.

Precedence (lower overrides higher): install < tenant <
investigation_template < investigation_local. MVP implements install
and tenant; the per-investigation layers are returned unchanged if
empty.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Install defaults
# ---------------------------------------------------------------------------


INSTALL_POLICY_DEFAULTS: dict[str, Any] = {
    # Auto-close
    "auto_close_enabled": True,
    "auto_close_threshold": 0.90,
    "auto_close_requires_ioc_anchor": True,
    "reopen_window_days": 30,
    # Safety-floor members (issue #46) — enforced by the executor at every
    # auto-close site (rules band, memoized close, worker close_fp incl. the
    # triage policy operational disposition), never expressible in triage policy data:
    # ``auto_close_kill`` — per-tenant kill switch: True flips every automatic
    #   close to promote/escalate, no rollout needed (the install-wide analogue
    #   is the SOCTALK_AUTO_CLOSE_KILL env on the API).
    # ``auto_close_volume_cap`` — rolling cap on automatic closes per tenant
    #   per ``auto_close_volume_window_hours``; at/above the cap further closes
    #   are vetoed to promotion/escalation (audited) — a runaway close loop
    #   must degrade to "humans look", not mass suppression. <= 0 disables.
    "auto_close_kill": False,
    "auto_close_volume_cap": 500,
    "auto_close_volume_window_hours": 24,
    # Run budget
    "max_tokens_per_investigation": 200_000,
    "max_dollars_per_investigation": 5.0,
    "max_tool_calls_per_investigation": 200,
    # Alert triage
    "alert_severity_threshold": 3,  # >= 3 creates an investigation by default
    "coalesce_window_minutes": 5,
    # Settle window (issue #28): a promoted investigation's run is not
    # claimable for this many seconds, so correlated events landing right
    # after promotion attach before the first LLM look. 0 disables (default
    # until correlation attach lands and multi-alert investigations exist).
    # Alerts at/above settle_bypass_severity claim immediately.
    "settle_window_seconds": 0,
    "settle_bypass_severity": 12,
    # Entity-overlap correlation (issue #27): a real alert sharing a
    # high-strength typed entity with an active investigation attaches to
    # it instead of creating a new one. Off by default until validated on
    # tenant data (a bad match predicate over-groups).
    "entity_correlation_enabled": False,
    # Verdict memoization (issue #29): a recurring alert shape previously
    # LLM-verdicted as a high-confidence FP closes by reference without an
    # LLM run. Off by default until validated (a stale memo suppresses a
    # real alert). Reopen (#15) still applies to memoized closes.
    "verdict_memoization_enabled": False,
    # Engagement deconfliction (#31): match ingest alerts against declared
    # pentest/red-team windows (source ip + host + technique + time). In-scope
    # => recorded in an auditable declared-test lane and skips the LLM run, but
    # is NEVER closed/FP; out-of-scope tester activity is forced to a real look.
    # Off by default; a declared window changes triage behaviour, so it's opt-in.
    "engagement_deconfliction_enabled": False,
    # Canonical entity graph (issue #24): land each alert's typed entities +
    # observation relationships into the memory graph. Off by default (new
    # per-alert write volume); enable per-tenant.
    "entity_graph_enabled": False,
    # Learned correlation scorer (issue #30): REVIEW-ONLY suggestions for
    # attaches the deterministic predicate missed. Never auto-attaches; off
    # by default and only enabled per-tenant after the offline spike gate
    # (soctalk.evals.correlation) proves precision.
    "correlation_scorer_enabled": False,
    # Visibility
    # ``customer_safe_promotion`` controls how a freshly-promoted investigation
    # gets its initial visibility:
    #   * ``auto``     — every promoted investigation is born ``customer_safe``,
    #                    so the tenant's portal renders it immediately
    #                    without analyst gating. Right default for
    #                    PoC / single-MSSP installs and the wholesale
    #                    flow where the value prop is "tenant sees
    #                    their own alerts." Analysts can still demote
    #                    to ``mssp_only`` for noise.
    #   * ``explicit`` — investigation is born ``mssp_only``; tenant only sees
    #                    it after an analyst explicitly promotes via
    #                    /api/mssp/investigations/{id}/visibility. Right for
    #                    enterprise installs where analyst triage is
    #                    a contractual gate.
    #   * ``disabled`` — never auto-promote; analyst-promotion endpoint
    #                    is also rejected. ``mssp_only`` is permanent.
    # Note: auto-CLOSED false-positive investigations stay ``mssp_only``
    # regardless of this policy — surfacing FPs to the tenant is
    # anti-helpful.
    "default_visibility": "customer_safe",
    "customer_safe_promotion": "auto",  # 'auto' | 'explicit' | 'disabled'
    # Tool approvals
    "tool_approval_overrides": {},  # capability_class -> ApprovalPolicy
}


def _install_policy_path() -> Path | None:
    path = os.getenv("SOCTALK_IR_POLICY_FILE")
    if not path:
        return None
    p = Path(path)
    return p if p.exists() else None


@lru_cache(maxsize=1)
def install_policies() -> dict[str, Any]:
    """Install-scope policies = defaults merged with optional YAML overrides.

    Cached for process lifetime; restart to pick up changes.
    """

    merged = dict(INSTALL_POLICY_DEFAULTS)
    path = _install_policy_path()
    if path:
        try:
            with path.open() as f:
                yaml_overrides = yaml.safe_load(f) or {}
            if not isinstance(yaml_overrides, dict):
                raise ValueError("install policy YAML must be a mapping at root")
            merged.update(yaml_overrides)
        except Exception:  # noqa: BLE001
            # Fail open with defaults rather than crash boot. Ops sees
            # the load error in logs.
            import structlog

            structlog.get_logger().exception(
                "install_policy_load_failed", path=str(path)
            )
    return merged


def reset_install_policy_cache() -> None:
    """For tests that change SOCTALK_IR_POLICY_FILE at runtime."""

    install_policies.cache_clear()


# ---------------------------------------------------------------------------
# Tenant overrides
# ---------------------------------------------------------------------------


async def tenant_policies(db: AsyncSession, tenant_id: UUID) -> dict[str, Any]:
    rows = (
        await db.execute(
            text(
                "SELECT key, value FROM tenant_policies WHERE tenant_id = :t"
            ),
            {"t": str(tenant_id)},
        )
    ).mappings().all()
    return {r["key"]: r["value"] for r in rows}


async def set_tenant_policy(
    db: AsyncSession, tenant_id: UUID, key: str, value: Any
) -> None:
    import json

    await db.execute(
        text(
            """
            INSERT INTO tenant_policies (tenant_id, key, value, updated_at)
            VALUES (:t, :k, CAST(:v AS JSONB), now())
            ON CONFLICT (tenant_id, key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = now()
            """
        ),
        {"t": str(tenant_id), "k": key, "v": json.dumps(value)},
    )


async def delete_tenant_policy(db: AsyncSession, tenant_id: UUID, key: str) -> None:
    """Remove a tenant override so the install default applies again.

    Clearing an override DELETES the row rather than storing a JSON ``null``:
    "absent = no override" is the precedence contract the resolver and audit
    (before/after) rely on.
    """
    await db.execute(
        text("DELETE FROM tenant_policies WHERE tenant_id = :t AND key = :k"),
        {"t": str(tenant_id), "k": key},
    )


# ---------------------------------------------------------------------------
# Run token budget (#103): install default -> tenant override, clamped to an
# install hard cap, resolved at run creation (no worker rollout).
# ---------------------------------------------------------------------------

RUN_TOKEN_BUDGET_KEY = "max_tokens_per_investigation"


def run_token_budget_default() -> int:
    """Install default run token budget (the install policy value)."""
    return int(install_policies().get(RUN_TOKEN_BUDGET_KEY, 200_000))


def run_token_budget_max() -> int:
    """Install hard cap a tenant override cannot exceed.

    ``SOCTALK_RUN_TOKEN_BUDGET_MAX`` (positive int) or a generous default that
    preserves the pre-#103 ceiling; lower it via the system chart to enforce a
    real cap. Chart-wired in soctalk-system.
    """
    raw = os.environ.get("SOCTALK_RUN_TOKEN_BUDGET_MAX", "")
    if raw.strip():
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 100_000_000


async def resolve_run_token_budget(db: AsyncSession, tenant_id: UUID) -> int:
    """Effective per-run token budget for a tenant at run-creation time.

    install default -> tenant override (via the policy layer), then clamped to
    the install hard cap. The clamp is authoritative here (not just at the PATCH
    validator): the cap is env-sourced, so rolling API pods could validate
    against different caps, and a previously-stored override may exceed a
    later-lowered cap. Stamped once onto the run row, so changing the override
    never mutates an in-flight run.
    """
    eff = await effective_policy(db, tenant_id)
    budget = int(eff.get(RUN_TOKEN_BUDGET_KEY, run_token_budget_default()))
    return max(1, min(budget, run_token_budget_max()))


# ---------------------------------------------------------------------------
# Effective policy (precedence evaluator)
# ---------------------------------------------------------------------------


# Install-level hard caps: tenants cannot relax these. Map of key →
# comparator; if tenant attempts to set a more permissive value, it's
# silently clamped at evaluation time.
HARD_CAPS: dict[str, str] = {
    # "max_tokens_per_investigation": "lt",   # tenant value must be <= install
    # Not enforcing specific caps in MVP; infrastructure present.
}


async def effective_policy(
    db: AsyncSession,
    tenant_id: UUID,
    investigation_template: dict[str, Any] | None = None,
    investigation_local: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge install → tenant → investigation_template → investigation_local, lower wins."""

    install = install_policies()
    tenant = await tenant_policies(db, tenant_id)
    template = investigation_template or {}
    local = investigation_local or {}

    merged: dict[str, Any] = dict(install)
    merged.update(tenant)
    merged.update(template)
    merged.update(local)
    # Apply hard caps if any.
    for key, rule in HARD_CAPS.items():
        if rule == "lt" and merged.get(key, 0) > install.get(key, 0):
            merged[key] = install[key]
    return merged


__all__ = [
    "RUN_TOKEN_BUDGET_KEY",
    "delete_tenant_policy",
    "effective_policy",
    "install_policies",
    "reset_install_policy_cache",
    "resolve_run_token_budget",
    "run_token_budget_default",
    "run_token_budget_max",
    "set_tenant_policy",
    "tenant_policies",
]
