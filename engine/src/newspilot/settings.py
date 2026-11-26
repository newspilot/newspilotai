# SPDX-License-Identifier: AGPL-3.0-only
"""Per-tenant, non-secret settings stored as JSON values in tenant_settings.

Settings defined in newspilot.settings_registry are validated and fall back to their defaults; other keys are stored
as given. Anything that looks like a credential is refused here, so it has to go through SecretStore and stays
encrypted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from newspilot import audit
from newspilot.db.models import TenantSetting
from newspilot.settings_registry import REGISTRY, InvalidSettingError, SettingDef, definition
from newspilot.tenancy import current_context

__all__ = ["InvalidSettingError", "SecretInSettingsError", "delete", "describe", "get", "get_all", "put"]

SECRET_LIKE = re.compile(r"(?:^|[._])(?:key|apikey|api_key|token|secret|password|passwd|credential|private)(?:$|[._])")
_MISSING = object()


class SecretInSettingsError(ValueError):
    """A credential was offered as a plain setting."""


def _check_key(key: str) -> None:
    if SECRET_LIKE.search(key):
        raise SecretInSettingsError(f"{key!r} looks like a credential: store it with SecretStore, not as a setting")


def get(session: Session, key: str, default: Any = _MISSING) -> Any:
    """The stored value; else the given default; else the registry's default; else None."""
    current_context(session)
    value = session.scalar(select(TenantSetting.value).where(TenantSetting.key == key))
    if value is not None:
        return value
    if default is not _MISSING:
        return default
    d = definition(key)
    return d.default if d else None


def get_all(session: Session) -> dict[str, Any]:
    current_context(session)
    return {row.key: row.value for row in session.scalars(select(TenantSetting).order_by(TenantSetting.key))}


def put(session: Session, key: str, value: Any) -> None:
    """Store a setting. A registered setting is validated first; a value of None clears it (back to its default)."""
    _check_key(key)
    d = definition(key)
    if d is not None:
        value = d.coerce(value)
        if value is None:
            delete(session, key)
            return
    ctx = current_context(session)
    stmt = insert(TenantSetting).values(tenant_id=ctx.tenant_id, key=key, value=value, updated_by=ctx.actor)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TenantSetting.tenant_id, TenantSetting.key],
            set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by, "updated_at": func.now()},
        )
    )
    audit.record(session, "setting.changed", target_type="setting", target_id=key, details={"value": value})


def delete(session: Session, key: str) -> bool:
    current_context(session)
    row = session.scalar(select(TenantSetting).where(TenantSetting.key == key))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    audit.record(session, "setting.deleted", target_type="setting", target_id=key)
    return True


@dataclass(frozen=True)
class SettingView:
    """A registered setting as the dashboard shows it: its definition, the current value and whether it is set."""

    definition: SettingDef
    value: Any
    is_set: bool


def describe(session: Session) -> list[SettingView]:
    stored = get_all(session)
    return [
        SettingView(d, stored.get(d.key, d.default), d.key in stored)
        for d in REGISTRY.values()  # definition order: how the dashboard presents them
    ]
