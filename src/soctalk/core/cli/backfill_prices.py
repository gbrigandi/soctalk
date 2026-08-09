"""Bring existing tenants onto the priced-model rule (#141 phase 4).

    soctalk-backfill-prices [--apply]

Every tenant configured before the price gate existed may be running a model
the catalog cannot price. Two outcomes, and the distinction is the point:

* **Priced by the catalog** — nothing to do. The tenant keeps tracking catalog
  corrections, which is what a tenant with no override should do. Deliberately
  NOT copied into a per-tenant override: that would pin today's rates and
  silently stop future corrections reaching them.

* **Not priced** — cost accounting is switched OFF for that tenant, with an
  audit entry. Blocking their upgrade is hostile, and inventing a rate from a
  vendor row under a different provider_kind is the exact failure this whole
  effort exists to remove. Off is honest: it says "we are not counting dollars
  here", the UI says so, token ceilings still apply, and the operator can turn
  it back on the moment they supply rates.

Run AFTER the catalog is seeded. ``db-init`` runs ``alembic upgrade head``
before ``soctalk-prices import --apply``, so a catalog-dependent backfill
inside a migration would read an empty table and switch off accounting for
every tenant on the install.

Connects with ``DATABASE_URL_MSSP`` (BYPASSRLS): this reads every tenant's
config, which a tenant-scoped connection cannot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from soctalk.core.ir.policies import COST_TRACKING_KEY
from soctalk.core.pricing import gate
from soctalk.core.tenancy.models import IntegrationConfig, Tenant


def _url() -> str:
    url = os.getenv("DATABASE_URL_MSSP") or os.getenv("DATABASE_URL") or ""
    if not url:
        print("DATABASE_URL_MSSP is not set", file=sys.stderr)
        raise SystemExit(2)
    return url


async def _run(apply: bool) -> int:
    engine = create_async_engine(_url())
    Session = async_sessionmaker(engine, expire_on_commit=False)

    priced: list[str] = []
    grandfathered: list[tuple[str, list[str]]] = []
    already_off: list[str] = []

    async with Session() as s:
        tenants = (
            await s.execute(select(Tenant.id, Tenant.slug).order_by(Tenant.slug))
        ).all()
        for tenant_id, slug in tenants:
            cfg = (
                await s.execute(
                    select(IntegrationConfig).where(
                        IntegrationConfig.tenant_id == tenant_id
                    )
                )
            ).scalars().first()
            if cfg is None:
                continue

            # A tenant that has already opted OUT is left alone: they made that
            # choice deliberately and this command must not re-litigate it.
            #
            # An explicit ON is NOT an opt-out. Skipping every existing row
            # treated "accounting is on" as "leave them unpriced and live",
            # which is the opposite of what it says (Codex review of phases
            # 4-5). Those tenants fall through and are handled like any other.
            existing = (
                await s.execute(
                    text(
                        "SELECT value FROM tenant_policies "
                        "WHERE tenant_id = :t AND key = :k"
                    ),
                    {"t": str(tenant_id), "k": COST_TRACKING_KEY},
                )
            ).scalar_one_or_none()
            if existing is not None and str(existing).strip().strip('"').lower() in {
                "false",
                "0",
                "off",
                "no",
                "disabled",
            }:
                already_off.append(slug)
                continue

            unpriced = await gate.unpriced_config(s, UUID(str(tenant_id)), cfg)
            if not unpriced:
                priced.append(slug)
                continue

            grandfathered.append((slug, unpriced))
            if apply:
                # A durable record of WHY, and of which models. The policy row
                # only says "off"; without this the reason lives in one
                # command's stdout and is gone (Codex review of phases 4-5).
                await s.execute(
                    text(
                        # id and actor_id are NOT NULL with no default, so
                        # omitting them made --apply fail before the policy row
                        # was ever written. My testing only ever ran the dry
                        # run, which never reaches this statement (Codex review
                        # of phases 4-5, round 2).
                        "INSERT INTO audit_log (id, tenant_id, actor_principal, "
                        "  actor_id, action, resource_type, resource_id, after, notes) "
                        "VALUES (gen_random_uuid(), :t, 'system:backfill-prices', "
                        "  'system', 'cost_tracking.disabled', 'tenant', :t, "
                        "  CAST(:after AS jsonb), :notes)"
                    ),
                    {
                        "t": str(tenant_id),
                        "after": json.dumps(
                            {"cost_tracking_enabled": False, "unpriced": unpriced}
                        ),
                        "notes": (
                            "cost accounting switched off: no price is known for "
                            + ", ".join(unpriced)
                        ),
                    },
                )
                await s.execute(
                    text(
                        "INSERT INTO tenant_policies (tenant_id, key, value, updated_at) "
                        "VALUES (:t, :k, 'false', now()) "
                        "ON CONFLICT (tenant_id, key) DO UPDATE "
                        "SET value = EXCLUDED.value, updated_at = now()"
                    ),
                    {"t": str(tenant_id), "k": COST_TRACKING_KEY},
                )
        if apply:
            await s.commit()

    print(f"priced, left tracking the catalog: {len(priced)}")
    for slug in priced:
        print(f"  = {slug}")
    print(f"already opted out, untouched: {len(already_off)}")
    for slug in already_off:
        print(f"  . {slug}")
    print(f"unpriced, cost accounting {'switched off' if apply else 'WOULD be switched off'}: "
          f"{len(grandfathered)}")
    for slug, models in grandfathered:
        print(f"  ! {slug}: {', '.join(models)}")

    if grandfathered:
        print(
            "\nThese tenants need rates before dollar ceilings can apply again: "
            "set a per-tenant price override, or add the model to the catalog "
            "and re-enable accounting in the Agent Run Budget panel."
        )
    if not apply and grandfathered:
        print("\nDry run. Re-run with --apply to write.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="soctalk-backfill-prices",
        description="Grandfather tenants whose models the catalog cannot price.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write; default is a dry run showing what would change",
    )
    args = ap.parse_args(argv)
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
