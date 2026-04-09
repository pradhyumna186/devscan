"""
Alembic async migration environment.

Run migrations from inside the Docker container:
    docker-compose exec app alembic revision --autogenerate -m "description"
    docker-compose exec app alembic upgrade head
    docker-compose exec app alembic downgrade -1

Or against the host-mapped port (5433) by temporarily overriding DATABASE_URL:
    DATABASE_URL="postgresql+asyncpg://devscan:devscan_pass@localhost:5433/devscan" \
        alembic upgrade head
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# ── Import Base and all models so their tables are registered ──────────────
from app.database import Base
import app.models  # noqa: F401  — registers Repo, PRReview, Issue, WebhookEvent

# ── Alembic Config ─────────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Prefer DATABASE_URL from environment over alembic.ini sqlalchemy.url."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://devscan:devscan_pass@localhost:5432/devscan",
    )


# ── Offline mode ───────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Run migrations against a URL without a live DB connection.
    Produces SQL script output only.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online (async) mode ────────────────────────────────────────────────────

def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,   # detect column type changes
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(get_url(), echo=False)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ── Entry point ────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
