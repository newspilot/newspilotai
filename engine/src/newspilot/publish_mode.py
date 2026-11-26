# SPDX-License-Identifier: AGPL-3.0-only
"""Publishing modes: who signs off the final text.

- "human" (the default for every new site): a person approves the final text of each article.
- "auto": an article may be published without a person reading the final text, if a person approved its lead
  (for example in Slack) and every automated check passed. Readers are told it was not reviewed by a person.

A site has one mode. Each category can override it ("human" or "auto"), or follow the site (None). Only a person can
change a mode, and every change is in the audit log. The database checks the mode again at publish time.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from newspilot import audit
from newspilot.db.models import PUBLISH_MODES, Category, Tenant
from newspilot.tenancy import current_context, is_human


class PublishModeError(PermissionError):
    """A publishing mode change was refused."""


def _require_person(session: Session) -> str:
    actor = current_context(session).actor
    if not is_human(actor):
        raise PublishModeError(f"{actor!r} is not a person; only a person can change how articles are published")
    return actor


def site_mode(session: Session) -> str:
    ctx = current_context(session)
    return session.scalar(select(Tenant.publish_mode).where(Tenant.id == ctx.tenant_id)) or "human"


def set_site_mode(session: Session, mode: str) -> None:
    if mode not in PUBLISH_MODES:
        raise ValueError(f"mode must be one of {PUBLISH_MODES}")
    actor = _require_person(session)
    ctx = current_context(session)
    tenant = session.get(Tenant, ctx.tenant_id)
    old = tenant.publish_mode
    tenant.publish_mode, tenant.publish_mode_set_by, tenant.publish_mode_set_at = mode, actor, func.now()
    session.flush()
    audit.record(
        session,
        "publish_mode.site",
        target_type="tenant",
        target_id=str(ctx.tenant_id),
        details={"from": old, "to": mode},
    )


def set_category_mode(session: Session, category_id: uuid.UUID, mode: str | None) -> None:
    """mode None means the category follows the site's mode."""
    if mode is not None and mode not in PUBLISH_MODES:
        raise ValueError(f"mode must be one of {PUBLISH_MODES} or None")
    actor = _require_person(session)
    cat = session.scalars(select(Category).where(Category.id == category_id).with_for_update()).first()
    if cat is None:
        raise LookupError(f"category {category_id} not found")
    old = cat.publish_mode
    cat.publish_mode, cat.publish_mode_set_by, cat.publish_mode_set_at = mode, actor, func.now()
    cat.updated_at = func.now()
    session.flush()
    audit.record(
        session, "publish_mode.category", target_type="category", target_id=cat.slug, details={"from": old, "to": mode}
    )


def effective_mode(session: Session, category_id: uuid.UUID | None) -> str:
    """The mode that applies to an article in this category (the same rule the database uses)."""
    ctx = current_context(session)
    return session.execute(
        text("SELECT newspilot_effective_publish_mode(:t, :c)"), {"t": ctx.tenant_id, "c": category_id}
    ).scalar_one()
