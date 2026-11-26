# SPDX-License-Identifier: AGPL-3.0-only
"""Leads: stories found in a source. Each source item becomes at most one lead per tenant.

The idempotency key is a hash of the normalised source URL, so the same story found twice (with different
tracking parameters, a fragment or a trailing slash) is recognised as the same lead. That is what makes scouting
safe to retry.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from newspilot import audit
from newspilot.db.models import LEAD_STATUSES, Lead
from newspilot.tenancy import current_context, is_human

TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "cmpid"}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not a web address: {url!r}")
    host = parts.hostname.lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(query), ""))


def source_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def get_or_create(
    session: Session, *, source_url: str, title: str, summary: str | None = None, score: float | None = None
) -> tuple[Lead, bool]:
    """The lead for this source item, and whether it was created now. Safe to call repeatedly."""
    ctx = current_context(session)
    key = source_hash(source_url)
    stmt = (
        insert(Lead)
        .values(
            tenant_id=ctx.tenant_id, source_url=source_url, source_hash=key, title=title, summary=summary, score=score
        )
        .on_conflict_do_nothing(index_elements=[Lead.tenant_id, Lead.source_hash])
        .returning(Lead.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()
    lead = session.scalars(select(Lead).where(Lead.source_hash == key)).one()
    if new_id is not None:
        audit.record(session, "lead.created", target_type="lead", target_id=str(lead.id))
    return lead, new_id is not None


def _lead(session: Session, lead_id) -> Lead:
    current_context(session)
    lead = session.scalars(select(Lead).where(Lead.id == lead_id).with_for_update()).first()
    if lead is None:
        raise LookupError(f"lead {lead_id} not found")
    return lead


def approve(session: Session, lead_id, category_id=None) -> Lead:
    """A person approves the story idea (for example with a Slack button), optionally choosing its category.

    In "auto" publishing mode this is the human step: the article can then publish once every automated check
    passes. In "human" mode a person still approves the final text as well.
    """
    actor = current_context(session).actor
    if not is_human(actor):
        raise PermissionError(f"{actor!r} is not a person; only a person can approve a lead")
    lead = _lead(session, lead_id)
    if lead.status not in ("new", "shortlisted"):
        raise ValueError(f"cannot approve a lead that is {lead.status}")
    lead.status, lead.approved_by, lead.approved_at, lead.updated_at = "approved", actor, func.now(), func.now()
    if category_id is not None:
        lead.category_id = category_id
    session.flush()
    audit.record(
        session,
        "lead.approved",
        target_type="lead",
        target_id=str(lead.id),
        details={"category_id": str(category_id) if category_id else None},
    )
    return lead


def set_status(session: Session, lead_id, status: str) -> Lead:
    """Move a lead along the pipeline. Approving goes through approve(), which records the person."""
    if status not in LEAD_STATUSES:
        raise ValueError(f"unknown lead status {status!r}")
    if status == "approved":
        raise ValueError("use leads.approve() to approve a lead")
    lead = _lead(session, lead_id)
    lead.status, lead.updated_at = status, func.now()
    session.flush()
    audit.record(session, "lead.status", target_type="lead", target_id=str(lead.id), details={"status": status})
    return lead
