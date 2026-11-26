# SPDX-License-Identifier: AGPL-3.0-only
"""Per-tenant, non-secret settings stored as JSON values in tenant_settings.

Anything that looks like a credential is refused here, so it has to go through SecretStore and stays encrypted.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from newspilot import audit
from newspilot.db.models import TenantSetting
from newspilot.tenancy import current_context

SECRET_LIKE = re.compile(r"(?:^|[._])(?:key|apikey|api_key|token|secret|password|passwd|credential|private)(?:$|[._])")


class SecretInSettingsError(ValueError):
    """A credential was offered as a plain setting."""


def _check_key(key: str) -> None:
    if SECRET_LIKE.search(key):
        raise SecretInSettingsError(f"{key!r} looks like a credential: store it with SecretStore, not as a setting")


def get(session: Session, key: str, default: Any = None) -> Any:
    current_context(session)
    value = session.scalar(select(TenantSetting.value).where(TenantSetting.key == key))
    return default if value is None else value


def get_all(session: Session) -> dict[str, Any]:
    current_context(session)
    return {row.key: row.value for row in session.scalars(select(TenantSetting).order_by(TenantSetting.key))}


def put(session: Session, key: str, value: Any) -> None:
    _check_key(key)
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
