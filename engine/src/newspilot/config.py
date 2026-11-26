# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime configuration, read from environment variables.

Only connection details and the master key come from the environment. Everything else about a site lives in
the database (tenant settings and tenant secrets).

  NEWSPILOT_DATABASE_URL            the application's connection: a NON-owner role (row-level security applies)
  NEWSPILOT_MIGRATION_DATABASE_URL  the owner role that runs migrations (never used by the running app)
  NEWSPILOT_APP_ROLE                name of the application role that migrations grant to (default newspilot_app)
  NEWSPILOT_MASTER_KEY              base64 32-byte key that wraps each tenant's data key (see secrets.py)
  NEWSPILOT_MASTER_KEY_ID           a label for that key, stored with each wrapped data key (default local-1)
"""

from __future__ import annotations

import os
import re

DEFAULT_APP_ROLE = "newspilot_app"
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def database_url() -> str:
    return _required("NEWSPILOT_DATABASE_URL")


def migration_database_url() -> str:
    return _required("NEWSPILOT_MIGRATION_DATABASE_URL")


def app_role() -> str:
    return validate_role_name(os.environ.get("NEWSPILOT_APP_ROLE", "").strip() or DEFAULT_APP_ROLE)


def validate_role_name(name: str) -> str:
    """Role names end up in GRANT statements, so only plain lower-case identifiers are accepted."""
    if not _ROLE_NAME.match(name):
        raise ConfigError(f"invalid PostgreSQL role name {name!r} (use lower-case letters, digits and _)")
    return name
