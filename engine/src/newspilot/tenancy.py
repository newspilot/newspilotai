# SPDX-License-Identifier: AGPL-3.0-only
"""TenantContext: the only way application code touches tenant data.

    with tenant_session(sessions, ctx) as session:
        ...   # every query here sees only ctx.tenant_id's rows

The tenant is set with set_config(..., is_local=true), so it lasts exactly one transaction. Do not call
session.commit() inside the block. A later transaction on the same session would have no tenant set and would
see no rows (it fails closed, but it is a bug). The block commits when it ends, and rolls back on an exception.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SETTING = "newspilot.tenant_id"


@dataclass(frozen=True)
class TenantContext:
    """Which tenant a unit of work is for, and who is doing it (recorded in the audit log)."""

    tenant_id: uuid.UUID
    actor: str = "system"

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, uuid.UUID):
            raise TypeError("tenant_id must be a uuid.UUID")
        if not self.actor or not self.actor.strip():
            raise ValueError("actor must name a person or a system process")


@contextmanager
def tenant_session(sessions: sessionmaker[Session], ctx: TenantContext) -> Iterator[Session]:
    """A session in one transaction, scoped to ctx's tenant by row-level security."""
    with sessions() as session, session.begin():
        session.execute(text("SELECT set_config(:name, :value, true)"), {"name": SETTING, "value": str(ctx.tenant_id)})
        session.info["tenant_context"] = ctx
        yield session


def current_context(session: Session) -> TenantContext:
    """The TenantContext a session was opened with. Raises if it was not opened by tenant_session()."""
    ctx = session.info.get("tenant_context")
    if not isinstance(ctx, TenantContext):
        raise RuntimeError("this session is not scoped to a tenant; open it with tenant_session()")
    return ctx


# Operations that are not scoped to one tenant. They go through SECURITY DEFINER functions in the database, so the
# application role never needs to read across tenants.


def create_tenant(sessions: sessionmaker[Session], slug: str, name: str) -> uuid.UUID:
    with sessions() as session, session.begin():
        return session.execute(
            text("SELECT newspilot_create_tenant(:slug, :name)"), {"slug": slug, "name": name}
        ).scalar_one()


def tenant_id_for_slug(sessions: sessionmaker[Session], slug: str) -> uuid.UUID | None:
    with sessions() as session:
        return session.execute(text("SELECT newspilot_tenant_id(:slug)"), {"slug": slug}).scalar_one()


def active_tenant_ids(sessions: sessionmaker[Session]) -> list[uuid.UUID]:
    with sessions() as session:
        return list(session.execute(text("SELECT * FROM newspilot_active_tenant_ids()")).scalars())
