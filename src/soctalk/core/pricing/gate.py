"""A model must be priced before it may be used.

The rule: **a model needs an entry in the price catalog to be used, unless cost
tracking is disabled.** Without it, an unpriced model was billed at the
fail-expensive fallback and those invented dollars were authoritative for
enforcement — they decremented real budgets and halted real runs. Measured at
16x on a live install (#139), and the inversion itself is #124: an estimate
treated as a fact.

Enforced at configuration time rather than at run time, deliberately. Refusing
to *start a run* over pricing would stop triage silently, which is the failure
the pricing feature exists to prevent (``core/ir/runtime.py`` keeps price
resolution non-fatal for exactly that reason). Refusing to *save a model* puts
the error in front of the operator who chose it, while they are choosing it,
with the model name in hand.

Disabling cost tracking is a deliberate deployment choice — local inference
where dollars are meaningless, or an air-gapped install with no price feed. It
is a policy (``cost_tracking_enabled``), so it is settable per tenant from the
UI rather than being an env var only a shell can reach.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.ir.policies import resolve_cost_tracking
from soctalk.core.pricing import catalog
from soctalk.core.pricing.resolve import (
    override_key,
    provider_id_for,
    provider_kind_for,
)

logger = structlog.get_logger(__name__)


async def unpriced_models(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    provider: str | None,
    base_url: str | None,
    models: dict[str, str | None],
    overrides: dict | None = None,
) -> list[str]:
    """Which of ``models`` have no price, as ``"role: model"`` strings.

    ``models`` maps a role name (``model``, ``fast_model``, ...) to the model
    ID. Empty roles are skipped — an unset fast model falls back to the primary
    one, so it is not separately unpriced.

    A tenant price override counts as priced: the operator has stated the rate,
    which is the thing the catalog would otherwise supply.

    Returns [] when cost tracking is off, so callers need no second check.
    """
    if not await resolve_cost_tracking(db, tenant_id):
        return []

    kind = provider_kind_for(provider, base_url)
    pid = provider_id_for(base_url)
    overlay = overrides or {}

    missing: list[str] = []
    for role, model in models.items():
        name = (model or "").strip()
        if not name:
            continue
        # A qualified override counts, and so does a bare one: the resolver
        # tries them in that order, so the gate must accept either (#141
        # phase 3). Without this, setting a per-backend price still left the
        # model looking unpriced and the save was refused.
        if (
            name in overlay
            or override_key(kind, pid, name) in overlay
            or override_key(kind, None, name) in overlay
        ):
            continue
        try:
            row = await catalog.lookup(
                db, provider_kind=kind, model=name, provider_id=pid
            )
        except Exception as exc:  # noqa: BLE001
            # A catalog that cannot be read is an infrastructure fault, not a
            # configuration error. Blocking the operator's edit on it would
            # turn a transient database problem into "you may not change your
            # model", so degrade to permissive and say so.
            logger.warning("price_gate_lookup_failed", model=name, error=str(exc))
            return []
        if row is None:
            missing.append(f"{role}: {name}")

    if missing:
        # An ENTIRELY empty catalog is a different fault from one unknown model
        # among many known ones: it means the install never seeded, and the
        # operator will read "no price is known for gpt-4o" as "gpt-4o is
        # exotic" rather than "pricing was never set up". Still refuse — the
        # rule is the rule — but say which situation this is.
        try:
            total = await catalog.count(db)
        except Exception:  # noqa: BLE001
            total = None
        if total == 0:
            logger.warning(
                "price_gate_catalog_empty",
                hint="run 'soctalk-prices import --apply'; db-init does this "
                "automatically on a normal install",
            )
    return missing


def unpriced_message(missing: list[str]) -> str:
    """The operator-facing explanation, naming every way out."""
    listed = ", ".join(missing)
    return (
        f"No price is known for {listed}. A model must have a price catalog "
        "entry before it can be used, so that budgets are enforced on real "
        "cost rather than an invented one. Either add it to the catalog "
        "(soctalk-prices import --apply), set a per-tenant price override, or "
        "turn cost accounting off for this tenant in the Agent Run Budget "
        "panel."
    )
