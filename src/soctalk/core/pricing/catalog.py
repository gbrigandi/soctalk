"""The install-level model price catalog (#125).

One row per (provider kind, provider identity, model), holding what that model
costs at that provider. Global on purpose: a price is a property of the market
rather than of a customer, so the table carries no ``tenant_id`` and no tenant
policy, and writes are a platform-admin act.

Rates are integer micro-dollars per million tokens. Integers because float
dollars do not survive arithmetic honestly, and per-million because that is the
unit vendors publish and operators think in.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel

from soctalk.core.pricing.names import base_model_id

# The dimensions this application understands today. A row may carry more (the
# column is JSONB precisely so a new pricing axis is not a migration), but only
# these are read, and only these are validated on write.
KNOWN_DIMENSIONS = (
    "input_per_mtok_microusd",
    "output_per_mtok_microusd",
    "cache_read_per_mtok_microusd",
    "cache_write_per_mtok_microusd",
)

# The two that must be present for a row to price anything at all.
REQUIRED_DIMENSIONS = ("input_per_mtok_microusd", "output_per_mtok_microusd")

PROVIDER_KINDS = frozenset(
    {"anthropic", "openai", "openrouter", "openai_compatible", "self_hosted"}
)

PROVENANCES = frozenset({"curated", "operator", "provider_declared", "imported"})

MICRO = 1_000_000


class ModelPrice(SQLModel, table=True):
    """A price for one model at one provider. Install-global; no tenant_id."""

    __tablename__ = "model_prices"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    provider_kind: str = Field(sa_column=Column(Text, nullable=False))
    provider_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    model: str = Field(sa_column=Column(Text, nullable=False))
    dimensions: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    currency: str = Field(default="USD", sa_column=Column(Text, nullable=False))
    provenance: str = Field(sa_column=Column(Text, nullable=False))
    source: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    as_of: date | None = Field(default=None)
    license_status: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


def validate_dimensions(raw: Any) -> dict[str, Any]:
    """Validate a dimensions payload, returning it normalized.

    Raises ``ValueError`` rather than persisting something the pricing path
    would later fail to read: a price that silently fails to parse leaves the
    tenant on the unknown path, which is the exact outcome this table exists to
    prevent.

    Unrecognised keys are kept, not rejected. A vendor inventing a new axis
    should not need a schema change to be recorded; it simply is not read until
    the application learns it.
    """
    if not isinstance(raw, dict):
        raise ValueError("dimensions must be an object")
    for field in REQUIRED_DIMENSIONS:
        if field not in raw:
            raise ValueError(f"dimensions is missing {field}")
    out: dict[str, Any] = dict(raw)
    for key in KNOWN_DIMENSIONS:
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"dimensions.{key} must be a number of micro-dollars")
        if value < 0:
            raise ValueError(f"dimensions.{key} must not be negative")
        # Stored as integers; a float that is not a whole number of micro-dollars
        # is a caller mistaking the unit, which is worth saying rather than
        # rounding away.
        if float(value) != int(value):
            raise ValueError(
                f"dimensions.{key} must be whole micro-dollars, not {value!r}"
            )
        out[key] = int(value)
    return out


def dimensions_from_dollars(
    input_per_mtok: float,
    output_per_mtok: float,
    *,
    cache_read_per_mtok: float | None = None,
    cache_write_per_mtok: float | None = None,
) -> dict[str, int]:
    """Build a dimensions map from the dollars-per-million vendors publish.

    The conversion lives here so every caller — import, operator edit, provider
    self-describe — rounds the same way instead of each inventing its own.
    """
    dims = {
        "input_per_mtok_microusd": round(input_per_mtok * MICRO),
        "output_per_mtok_microusd": round(output_per_mtok * MICRO),
    }
    if cache_read_per_mtok is not None:
        dims["cache_read_per_mtok_microusd"] = round(cache_read_per_mtok * MICRO)
    if cache_write_per_mtok is not None:
        dims["cache_write_per_mtok_microusd"] = round(cache_write_per_mtok * MICRO)
    return dims


def dollars_per_mtok(dimensions: dict[str, Any], key: str) -> float | None:
    """One dimension as dollars per million tokens, or None if absent."""
    raw = dimensions.get(key)
    if raw is None:
        return None
    return float(raw) / MICRO


async def count(db: AsyncSession) -> int:
    """How many entries the catalog holds, install-wide.

    Used to tell "this one model is unknown" from "pricing was never seeded",
    which read identically at the point of failure but need different fixes.
    """
    row = await db.execute(text("SELECT count(*) FROM model_prices"))
    return int(row.scalar_one() or 0)


async def lookup(
    db: AsyncSession,
    *,
    provider_kind: str,
    model: str,
    provider_id: str | None = None,
) -> ModelPrice | None:
    """The catalog entry for a model at a provider, or None.

    Tried most specific first: an entry naming the vendor beats a generic one
    for the same protocol, because a gateway's price for a model is not the
    vendor's price for it.

    Each candidate is tried as the exact ID, then as the version-stripped family
    ID (#139). Exact first so a genuinely distinct dated SKU can be priced
    separately if one is ever seeded; family second so the dated IDs providers
    actually return — and that operators are told to pin — do not miss the
    catalog and get billed at the fail-expensive unknown-model rate.

    Runs outside tenant context on purpose — the catalog is install-global, and
    a tenant-scoped read would find nothing.
    """
    # Exact ID, then the version-stripped family. dict.fromkeys keeps order and
    # collapses the duplicate when the model carries no version suffix.
    candidates = list(dict.fromkeys([model, base_model_id(model)]))

    for candidate in candidates:
        if not candidate:
            continue
        if provider_id:
            row = (
                await db.execute(
                    text(
                        """
                        SELECT * FROM model_prices
                         WHERE provider_kind = :kind
                           AND provider_id = :pid
                           AND model = :model
                         LIMIT 1
                        """
                    ),
                    {"kind": provider_kind, "pid": provider_id, "model": candidate},
                )
            ).mappings().first()
            if row is not None:
                return ModelPrice(**dict(row))

        row = (
            await db.execute(
                text(
                    """
                    SELECT * FROM model_prices
                     WHERE provider_kind = :kind
                       AND provider_id IS NULL
                       AND model = :model
                     LIMIT 1
                    """
                ),
                {"kind": provider_kind, "model": candidate},
            )
        ).mappings().first()
        if row is not None:
            return ModelPrice(**dict(row))

    return None
