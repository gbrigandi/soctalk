"""CLI for the install-level model price catalog (#125).

Usage:

    soctalk-prices show [--provider-kind KIND]
    soctalk-prices import <file.json> [--provenance imported] [--source URL] [--apply]
    soctalk-prices pull <base-url> [--provider-kind KIND] [--apply]

Deliberately a CLI rather than a CRUD screen. The catalog is install-global
reference data: one row changes what every tenant in the install is billed at,
and a price typed into a form loses the ``source``, ``as_of`` and
``license_status`` that are what make it trustworthy. Prices arrive in batches
from a vendor page, an export, or a gateway's own ``/models``, so the write
path is an import that carries provenance by construction.

Nothing applies without ``--apply``. The default is a diff plus blast radius:
which entries would change, and which tenants are configured for the models
affected. A price edit moves budget enforcement, so seeing who it lands on
before it lands is the point.

Connects with ``DATABASE_URL_MSSP`` (the BYPASSRLS role), because the catalog
is global and a tenant-scoped connection would see nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from soctalk.core.pricing import catalog

# What an import file looks like. One list, one entry per priced model:
#
#   [
#     {"provider_kind": "openrouter", "provider_id": "deepseek",
#      "model": "deepseek-v4-flash", "input_per_mtok": 0.14,
#      "output_per_mtok": 0.28, "as_of": "2026-08-07"}
#   ]
#
# Dollars per million tokens on the way in because that is what vendors
# publish; stored as integer micro-dollars.
REQUIRED_FIELDS = ("provider_kind", "model", "input_per_mtok", "output_per_mtok")


def _db_url() -> str:
    url = os.getenv("DATABASE_URL_MSSP") or os.getenv("DATABASE_URL") or ""
    if not url:
        print("DATABASE_URL_MSSP is not set", file=sys.stderr)
        raise SystemExit(2)
    return url


def _session_factory(url: str):
    engine = create_async_engine(url, future=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _parse_entries(raw: Any) -> list[dict[str, Any]]:
    """Validate an import payload into catalog-shaped rows.

    Fails on the whole file rather than importing the good half: a partial
    price import is harder to reason about than a rejected one.
    """
    if not isinstance(raw, list):
        raise ValueError("import file must be a JSON array of entries")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {i} is not an object")
        for field in REQUIRED_FIELDS:
            if field not in entry:
                raise ValueError(f"entry {i} is missing {field}")
        kind = str(entry["provider_kind"])
        if kind not in catalog.PROVIDER_KINDS:
            raise ValueError(
                f"entry {i}: unknown provider_kind {kind!r}; "
                f"known: {sorted(catalog.PROVIDER_KINDS)}"
            )
        try:
            dims = catalog.dimensions_from_dollars(
                float(entry["input_per_mtok"]),
                float(entry["output_per_mtok"]),
                cache_read_per_mtok=(
                    float(entry["cache_read_per_mtok"])
                    if entry.get("cache_read_per_mtok") is not None
                    else None
                ),
                cache_write_per_mtok=(
                    float(entry["cache_write_per_mtok"])
                    if entry.get("cache_write_per_mtok") is not None
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"entry {i}: {exc}") from None
        catalog.validate_dimensions(dims)
        # Bound as a real date, not a string: asyncpg types parameters strictly
        # and a CAST in the SQL does not rescue a str bound to a date column.
        as_of = entry.get("as_of")
        if as_of is not None:
            try:
                as_of = date.fromisoformat(str(as_of))
            except ValueError:
                raise ValueError(f"entry {i}: as_of must be YYYY-MM-DD") from None
        out.append(
            {
                "provider_kind": kind,
                "provider_id": entry.get("provider_id") or None,
                "model": str(entry["model"]),
                "dimensions": dims,
                "as_of": as_of,
                "notes": entry.get("notes"),
            }
        )
    return out


async def _existing(db: AsyncSession, entry: dict[str, Any]) -> dict[str, Any] | None:
    row = (
        await db.execute(
            text(
                """
                SELECT id, dimensions, provenance, as_of FROM model_prices
                 WHERE provider_kind = :kind
                   AND model = :model
                   AND provider_id IS NOT DISTINCT FROM :pid
                 LIMIT 1
                """
            ),
            {
                "kind": entry["provider_kind"],
                "model": entry["model"],
                "pid": entry["provider_id"],
            },
        )
    ).mappings().first()
    return dict(row) if row else None


async def _tenants_using(db: AsyncSession, model: str) -> list[str]:
    """Tenant slugs configured for a model, for the blast radius.

    A price change is not abstract: it moves the dollar budget and the daily
    cap for whoever is pointed at that model, so the operator sees who before
    deciding.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT t.slug
                  FROM integration_configs i
                  JOIN tenants t ON t.id = i.tenant_id
                 WHERE :m IN (
                         COALESCE(i.llm_fast_model, i.llm_model),
                         COALESCE(i.llm_reasoning_model, i.llm_model),
                         -- The bare primary too: chat runs on it, and with both
                         -- overrides set the COALESCEs above never reach it —
                         -- so an operator was told a price change affected no
                         -- tenants while it changed what their chat cost
                         -- (Codex review of the chat-role change).
                         i.llm_model
                       )
                    OR EXISTS (
                         -- Per-tier models live in llm_tiers JSONB and are
                         -- consumed by render and runtime, but the scalar
                         -- columns above never see them — so a price change
                         -- to a tier-only model reported no affected tenants
                         -- (Codex, round 2).
                         SELECT 1
                           FROM jsonb_each(COALESCE(i.llm_tiers, '{}'::jsonb)) AS tier(k, v)
                          WHERE v->>'model' = :m
                       )
                 ORDER BY t.slug
                """
            ),
            {"m": model},
        )
    ).scalars().all()
    return list(rows)


def _fmt(dims: dict[str, Any]) -> str:
    inp = catalog.dollars_per_mtok(dims, "input_per_mtok_microusd")
    out = catalog.dollars_per_mtok(dims, "output_per_mtok_microusd")
    return f"${inp:g}/${out:g}"


async def _apply(
    db: AsyncSession,
    entries: list[dict[str, Any]],
    *,
    provenance: str,
    source: str | None,
    license_status: str | None,
    apply: bool,
) -> int:
    changed = 0
    for entry in entries:
        prior = await _existing(db, entry)
        label = (
            f"{entry['provider_kind']}"
            f"{'/' + entry['provider_id'] if entry['provider_id'] else ''}"
            f" {entry['model']}"
        )
        if prior is None:
            print(f"  + {label}  {_fmt(entry['dimensions'])}")
        elif prior["dimensions"] != entry["dimensions"]:
            print(
                f"  ~ {label}  {_fmt(prior['dimensions'])} -> "
                f"{_fmt(entry['dimensions'])}"
            )
        else:
            continue
        changed += 1
        affected = await _tenants_using(db, entry["model"])
        if affected:
            print(f"      affects tenants: {', '.join(affected)}")
        if not apply:
            continue
        now = datetime.now(UTC)
        if prior is None:
            await db.execute(
                text(
                    """
                    INSERT INTO model_prices
                      (id, provider_kind, provider_id, model, dimensions,
                       currency, provenance, source, as_of, license_status,
                       notes, created_at, updated_at)
                    VALUES
                      (:id, :kind, :pid, :model, CAST(:dims AS jsonb),
                       'USD', :prov, :src, :as_of, :lic,
                       :notes, :now, :now)
                    """
                ),
                {
                    "id": str(uuid4()),
                    "kind": entry["provider_kind"],
                    "pid": entry["provider_id"],
                    "model": entry["model"],
                    "dims": json.dumps(entry["dimensions"]),
                    "prov": provenance,
                    "src": source,
                    "as_of": entry["as_of"],
                    "lic": license_status,
                    "notes": entry["notes"],
                    "now": now,
                },
            )
        else:
            await db.execute(
                text(
                    """
                    UPDATE model_prices
                       SET dimensions = CAST(:dims AS jsonb),
                           provenance = :prov,
                           source = :src,
                           as_of = :as_of,
                           license_status = :lic,
                           updated_at = :now
                     WHERE id = :id
                    """
                ),
                {
                    "dims": json.dumps(entry["dimensions"]),
                    "prov": provenance,
                    "src": source,
                    "as_of": entry["as_of"],
                    "lic": license_status,
                    "now": now,
                    "id": prior["id"],
                },
            )
    if apply:
        await db.commit()
    return changed


async def cmd_show(args: argparse.Namespace) -> int:
    factory = _session_factory(_db_url())
    async with factory() as db:
        sql = "SELECT provider_kind, provider_id, model, dimensions, provenance, as_of FROM model_prices"
        params: dict[str, Any] = {}
        if args.provider_kind:
            sql += " WHERE provider_kind = :kind"
            params["kind"] = args.provider_kind
        sql += " ORDER BY provider_kind, provider_id NULLS FIRST, model"
        rows = (await db.execute(text(sql), params)).mappings().all()
    if not rows:
        print("catalog is empty")
        return 0
    for r in rows:
        who = f"{r['provider_kind']}{'/' + r['provider_id'] if r['provider_id'] else ''}"
        as_of = f" as of {r['as_of']}" if r["as_of"] else ""
        print(f"{who:28s} {r['model']:38s} {_fmt(r['dimensions']):>18s}  [{r['provenance']}{as_of}]")
    print(f"\n{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    return 0


async def cmd_import(args: argparse.Namespace) -> int:
    try:
        source_path = Path(args.file) if args.file else bundled_seed_path()
        if source_path is None:
            print(
                "no price file given and no bundled seed found; pass a path to "
                "a price file",
                file=sys.stderr,
            )
            return 2
        raw = json.loads(source_path.read_text())
        entries = _parse_entries(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"cannot import: {exc}", file=sys.stderr)
        return 2
    factory = _session_factory(_db_url())
    async with factory() as db:
        print(f"{'applying' if args.apply else 'dry run'}: {len(entries)} entries from {source_path}")
        changed = await _apply(
            db,
            entries,
            provenance=args.provenance,
            source=args.source,
            license_status=args.license_status,
            apply=args.apply,
        )
    if changed == 0:
        print("no changes")
    elif not args.apply:
        print(f"\n{changed} change(s) would be made. Re-run with --apply.")
    else:
        print(f"\n{changed} change(s) applied.")
    return 0


async def cmd_pull(args: argparse.Namespace) -> int:
    """Pull prices a gateway publishes about itself.

    Only OpenAI-style ``/models`` payloads carrying a ``pricing`` block are
    understood, which is what OpenRouter-shaped gateways serve. Prices are
    per-token there and per-million here.

    Provider-declared rates are lower trust than the catalog: a buggy or
    hostile gateway could declare cheap rates and cause real overspend, so this
    still requires ``--apply`` and still shows the blast radius first.
    """
    base = args.base_url.rstrip("/")
    url = base if base.endswith("/models") else f"{base}/models"
    headers = {}
    key = os.getenv("SOCTALK_PRICES_PULL_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = httpx.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"cannot pull from {url}: {exc}", file=sys.stderr)
        return 2

    entries: list[dict[str, Any]] = []
    models = payload if isinstance(payload, list) else (payload or {}).get("data") or []
    for m in models:
        pricing = (m or {}).get("pricing") or {}
        try:
            inp = float(pricing["prompt"]) * 1_000_000
            out = float(pricing["completion"]) * 1_000_000
        except (KeyError, TypeError, ValueError):
            continue
        model = m.get("id")
        if not model:
            continue
        entries.append(
            {
                "provider_kind": args.provider_kind,
                "provider_id": args.provider_id,
                "model": str(model),
                "dimensions": catalog.dimensions_from_dollars(inp, out),
                "as_of": datetime.now(UTC).date(),
                "notes": None,
            }
        )
    if not entries:
        print(f"no priced models found at {url}")
        return 1
    if args.only:
        wanted = set(args.only)
        entries = [e for e in entries if e["model"] in wanted]

    factory = _session_factory(_db_url())
    async with factory() as db:
        print(f"{'applying' if args.apply else 'dry run'}: {len(entries)} priced models from {url}")
        changed = await _apply(
            db,
            entries,
            provenance="provider_declared",
            source=url,
            license_status=None,
            apply=args.apply,
        )
    if changed == 0:
        print("no changes")
    elif not args.apply:
        print(f"\n{changed} change(s) would be made. Re-run with --apply.")
    else:
        print(f"\n{changed} change(s) applied.")
    return 0


def bundled_seed_path() -> Path | None:
    """The seed shipped with SocTalk, in either layout.

    Installed, it lands at ``soctalk/data/pricing`` (pyproject force-include).
    In a source checkout it is still at the repo root, so both are checked --
    otherwise this works for operators and silently returns None for anyone
    developing against the tree, which is exactly backwards for testing.
    """
    here = Path(__file__).resolve()
    for parent_index in (2, 4):  # installed package, then source checkout
        candidate = here.parents[parent_index] / "data" / "pricing" / "seed-prices.json"
        if candidate.exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="soctalk-prices", description="Install-level model price catalog."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="list catalog entries")
    p_show.add_argument("--provider-kind")
    p_show.set_defaults(fn=cmd_show)

    p_imp = sub.add_parser("import", help="import a price set from a JSON file")
    # Optional: with no path, use the seed that ships in the package. A fresh
    # install has an empty catalog (v1_0042 creates the table and seeds nothing),
    # so every model resolves `unknown` and bills at the fail-expensive
    # fallback until this is run. Requiring the operator to locate a file
    # inside site-packages made the obvious first step needlessly hard.
    p_imp.add_argument(
        "file",
        nargs="?",
        default=None,
        help="price file to import; omit to use the seed bundled with SocTalk",
    )
    p_imp.add_argument("--provenance", default="imported", choices=sorted(catalog.PROVENANCES))
    p_imp.add_argument("--source", help="where these prices came from")
    p_imp.add_argument("--license-status")
    p_imp.add_argument("--apply", action="store_true", help="write; default is a dry run")
    p_imp.set_defaults(fn=cmd_import)

    p_pull = sub.add_parser("pull", help="pull prices a gateway publishes about itself")
    p_pull.add_argument("base_url")
    p_pull.add_argument("--provider-kind", default="openai_compatible", choices=sorted(catalog.PROVIDER_KINDS))
    p_pull.add_argument("--provider-id")
    p_pull.add_argument("--only", nargs="*", help="limit to these model ids")
    p_pull.add_argument("--apply", action="store_true", help="write; default is a dry run")
    p_pull.set_defaults(fn=cmd_pull)

    args = parser.parse_args(argv)
    return asyncio.run(args.fn(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
