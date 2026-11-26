# SPDX-License-Identifier: AGPL-3.0-only
"""Optional publishing limits, read from the site's own settings (see settings_registry, group "publishing").

The engine has no built-in limit: with nothing set, check() always allows. When a site owner sets a limit and it is
reached, the article is not held or rejected. It simply waits, and check() says when it can go out. Limits apply
only to an article's first publication; corrections and updates to live articles are never delayed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from newspilot import settings
from newspilot.db.models import Article, Category, Lead
from newspilot.tenancy import current_context

WINDOW = dt.timedelta(hours=24)


@dataclass(frozen=True)
class LimitCheck:
    ok: bool
    reason: str | None = None
    # Earliest time the limit that blocked this article allows another one (if it can be known).
    retry_after: dt.datetime | None = None


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def lock_for_publishing(session: Session) -> None:
    """Serialise publishing within a tenant for the rest of the transaction, so two workers cannot both pass a
    limit at the same moment."""
    ctx = current_context(session)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"publish:{ctx.tenant_id}"})


def check(session: Session, article_id: uuid.UUID) -> LimitCheck:
    current_context(session)
    article = session.get(Article, article_id)
    if article is None:
        raise LookupError(f"article {article_id} not found")
    if article.first_published_at is not None:
        return LimitCheck(True)  # an update to a live article, never delayed

    now = session.scalar(select(func.now()))
    since = now - WINDOW
    new_in_window = select(Article).where(Article.first_published_at > since, Article.id != article.id).subquery()

    def oldest_plus_window(stmt) -> dt.datetime | None:
        oldest = session.scalar(stmt)
        return oldest + WINDOW if oldest else None

    # Minimum gap between new articles.
    gap = settings.get(session, "publish.min_gap_minutes")
    if gap:
        last = session.scalar(select(func.max(Article.first_published_at)).where(Article.id != article.id))
        if last is not None and now - last < dt.timedelta(minutes=gap):
            return LimitCheck(False, f"at most one new article every {gap} minutes", last + dt.timedelta(minutes=gap))

    # Whole site.
    daily = settings.get(session, "publish.daily_limit")
    if daily:
        count = session.scalar(select(func.count()).select_from(new_in_window))
        if count >= daily:
            first = select(func.min(new_in_window.c.first_published_at))
            return LimitCheck(
                False, f"site limit of {daily} new articles per 24 hours reached", oldest_plus_window(first)
            )

    # The article's category.
    per_category = settings.get(session, "publish.category_daily_limits") or {}
    if article.category_id and per_category:
        slug = session.scalar(select(Category.slug).where(Category.id == article.category_id))
        limit = per_category.get(slug)
        if limit:
            in_cat = select(new_in_window.c.first_published_at).where(
                new_in_window.c.category_id == article.category_id
            )
            count = session.scalar(select(func.count()).select_from(in_cat.subquery()))
            if count >= limit:
                first = select(func.min(in_cat.subquery().c.first_published_at))
                return LimitCheck(
                    False,
                    f"limit of {limit} new articles per 24 hours in category {slug!r} reached",
                    oldest_plus_window(first),
                )

    # The outlet the lead came from.
    per_source = settings.get(session, "publish.source_daily_limit")
    if per_source and article.lead_id:
        source_url = session.scalar(select(Lead.source_url).where(Lead.id == article.lead_id))
        host = _host(source_url or "")
        if host:
            rows = session.execute(
                select(new_in_window.c.first_published_at, Lead.source_url).join(
                    Lead, Lead.id == new_in_window.c.lead_id
                )
            ).all()
            same = sorted(t for t, url in rows if _host(url) == host)
            if len(same) >= per_source:
                return LimitCheck(
                    False, f"limit of {per_source} new articles per 24 hours from {host} reached", same[0] + WINDOW
                )

    return LimitCheck(True)
