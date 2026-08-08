"""Alembic migration environment configuration."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlmodel import SQLModel

# Import all models — v0 legacy (investigations/events/etc.) + v1 native IR.
# Autogenerate walks SQLModel.metadata, so every table-bearing module must
# be imported here or it will be silently dropped from migrations.
from soctalk.persistence.models import (  # noqa: F401
    AnalyzerStats,
    Event,
    InvestigationReadModel,
    IOCStats,
    MetricsHourly,
    PendingReview,
    RuleStats,
)

# v1 native IR + tenancy + auth models. Imported so they're registered
# with SQLModel.metadata — not strictly required for the current
# raw-SQL migrations, but kept so ``alembic revision --autogenerate``
# can see the full surface if we need it again.
import soctalk.core.ir.models  # noqa: F401
import soctalk.core.tenancy.models  # noqa: F401
import soctalk.core.auth.models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use SQLModel's metadata for autogenerate support
target_metadata = SQLModel.metadata


def get_url() -> str:
    """Get database URL from environment, converting async to sync driver."""
    url = os.getenv("DATABASE_URL") or os.getenv(
        # Fall back to the DDL role used by the integration suite / CI so
        # in-process reversibility tests work when only the role-specific
        # vars are set. Final default is a runnable admin URL (never the
        # non-existent bare ``soctalk`` role).
        "DATABASE_URL_ADMIN",
        "postgresql+asyncpg://soctalk_admin:soctalk_admin@localhost:5432/soctalk",
    )
    # Convert async driver to sync driver for Alembic
    # asyncpg -> psycopg2 (or just postgresql for default driver)
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# Arbitrary but fixed: every runner must pick the same key or the lock does
# nothing. Derived from "soctalk-alembic" so it will not collide with an
# application-level advisory lock chosen independently.
_MIGRATION_LOCK_KEY = 8104729163554821001


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = create_engine(
        get_url(),
        poolclass=pool.NullPool,
    )

    from sqlalchemy import text

    with connectable.connect() as connection:
        # Serialise concurrent migration runners (#135). Migrations run in the
        # API Deployment's db-init initContainer, so every replica starts one,
        # and values.schema.json puts no maximum on replicas — a fresh install
        # at replicas: 2 raced two Alembic runs with nothing to stop them.
        #
        # This must be the FIRST statement on the connection, before the
        # alembic_version pre-create below: `CREATE TABLE IF NOT EXISTS` is not
        # atomic against a concurrent create, and two racing runners fail there
        # with a duplicate pg_type row long before alembic itself is involved.
        # A lock taken after that block leaves the real race untouched (caught
        # by racing two runners against one empty database, not by reading).
        #
        # Session-level, not transaction-level: alembic commits per revision,
        # so a xact lock would be released after the first one. The loser
        # blocks here until the winner finishes, then proceeds and finds itself
        # already at head — a slow start instead of a half-applied schema.
        connection.execute(
            text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY}
        )
        connection.commit()

        # Some revision ids in this chain exceed alembic's default
        # VARCHAR(32) for ``alembic_version.version_num`` (e.g.
        # ``add_llm_settings_to_user_settings`` = 37 chars). Pre-create
        # the version table with a wider column on empty DBs; on
        # already-migrated DBs widen it in place. Both paths must land
        # before alembic tries to write to the table.
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS alembic_version (
              version_num VARCHAR(64) NOT NULL,
              CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
        """))
        connection.execute(text(
            "ALTER TABLE alembic_version "
            "ALTER COLUMN version_num TYPE VARCHAR(64)"
        ))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        try:
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY}
            )
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
