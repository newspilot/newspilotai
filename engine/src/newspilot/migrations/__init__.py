# SPDX-License-Identifier: AGPL-3.0-only
"""Alembic migrations for the engine's schema. Run them with `newspilot db upgrade`."""

from __future__ import annotations

from alembic import command
from alembic.config import Config


def alembic_config(database_url: str | None = None, app_role: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "newspilot:migrations")
    if database_url:
        cfg.attributes["database_url"] = database_url
    if app_role:
        cfg.attributes["app_role"] = app_role
    return cfg


def upgrade(database_url: str | None = None, app_role: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url, app_role), revision)
