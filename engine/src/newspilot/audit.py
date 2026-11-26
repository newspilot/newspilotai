# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only audit log. The database refuses UPDATE, DELETE and TRUNCATE on audit_log."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from newspilot.db.models import AuditEntry
from newspilot.tenancy import current_context


def record(
    session: Session,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEntry:
    """Record an action by the session's actor. Never put secret values in details."""
    ctx = current_context(session)
    entry = AuditEntry(
        tenant_id=ctx.tenant_id,
        actor=ctx.actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )
    session.add(entry)
    session.flush()
    return entry


def recent(session: Session, limit: int = 50) -> list[AuditEntry]:
    current_context(session)
    stmt = select(AuditEntry).order_by(AuditEntry.occurred_at.desc(), AuditEntry.id.desc()).limit(limit)
    return list(session.scalars(stmt))
