# SPDX-License-Identifier: AGPL-3.0-only
"""Alembic environment. Migrations run as the OWNER role (NEWSPILOT_MIGRATION_DATABASE_URL), never as the
application role. The application role's name (NEWSPILOT_APP_ROLE) is passed to migrations so they can grant to
it.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from newspilot import config as np_config
from newspilot.db.engine import normalize_url
from newspilot.db.models import Base

alembic_config = context.config
target_metadata = Base.metadata

alembic_config.attributes.setdefault("app_role", np_config.app_role())


def _url() -> str:
    return normalize_url(alembic_config.attributes.get("database_url") or np_config.migration_database_url())


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = alembic_config.attributes.get("connection")
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=target_metadata, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
