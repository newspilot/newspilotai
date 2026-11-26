# SPDX-License-Identifier: AGPL-3.0-only
"""The client's own categories, synced from their CMS (or entered by hand). Each can override the site's
publishing mode; see newspilot.publish_mode."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from newspilot import audit
from newspilot.db.models import Category
from newspilot.tenancy import current_context

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


def upsert(session: Session, *, slug: str, name: str, external_id: str | None = None) -> Category:
    """Add a category, or update its name and CMS id. Its publishing mode is never changed here."""
    ctx = current_context(session)
    if not _SLUG.match(slug):
        raise ValueError(f"invalid category slug {slug!r} (lower-case letters, digits and hyphens)")
    cat = session.scalars(select(Category).where(Category.slug == slug)).first()
    if cat is None:
        cat = Category(tenant_id=ctx.tenant_id, slug=slug, name=name, external_id=external_id)
        session.add(cat)
        session.flush()
        audit.record(session, "category.created", target_type="category", target_id=slug)
    elif (cat.name, cat.external_id) != (name, external_id):
        cat.name, cat.external_id, cat.updated_at = name, external_id, func.now()
        session.flush()
        audit.record(session, "category.updated", target_type="category", target_id=slug)
    return cat


def get(session: Session, category_id: uuid.UUID) -> Category | None:
    current_context(session)
    return session.scalars(select(Category).where(Category.id == category_id)).first()


def by_slug(session: Session, slug: str) -> Category | None:
    current_context(session)
    return session.scalars(select(Category).where(Category.slug == slug)).first()


def all_active(session: Session) -> list[Category]:
    current_context(session)
    return list(session.scalars(select(Category).where(Category.active.is_(True)).order_by(Category.name)))
