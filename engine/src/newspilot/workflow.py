# SPDX-License-Identifier: AGPL-3.0-only
"""The article lifecycle and the approval gate.

    draft ──submit──▶ in_review ──approve / auto_approve──▶ approved ──publish──▶ published ──unpublish──▶ unpublished
      │                   │  ▲                                  │                    │
      └──hold──▶ held ────┘  └────────── any new revision resets the approval ───────┘

Two publishing modes (see newspilot.publish_mode), set per site and optionally per category:
- "human" (default): approve() — a person approves the exact final text, with a checklist.
- "auto": auto_approve() — the system approves once a person has approved the lead and every automated check has
  passed. If anything is missing or failed, the article is held for a person instead. Auto-published articles are
  marked "not_human_reviewed" so readers can be told.

Rules (the database enforces the most important ones as well, see migration 0002):
- Every change to the text is a new, immutable revision. A new revision cancels any approval: the article goes
  back to in_review and must be approved again.
- An approval is for one exact revision, identified by its content hash.
- Only an approved revision can be published, and only while it is still the current revision.
- An editor can unpublish at any time.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from newspilot import audit, publish_limits
from newspilot.db.models import Approval, Article, ArticleRevision, Lead
from newspilot.publish_mode import effective_mode
from newspilot.tenancy import HUMAN_ACTOR_PREFIXES, current_context, is_human

__all__ = ["HUMAN_ACTOR_PREFIXES", "is_human"]  # re-exported for callers of the workflow

# What an editor confirms before approving. A site can add items; these four cannot be removed.
REQUIRED_CHECKLIST = ("facts_checked", "quotes_checked", "images_licensed", "sources_credited")
# What the automated checks must all report before an article may auto-publish. The database requires the same keys.
# Publishing limits are not an approval check: they are optional site settings applied at publish time
# (newspilot.publish_limits), and an article over a limit waits rather than being held.
AUTO_CHECKS = ("quality_gate", "information_gain", "images_licensed", "sources_credited")


class WorkflowError(RuntimeError):
    """An action is not allowed in the article's current state."""


class ApprovalError(WorkflowError):
    """An approval was refused (not a person, stale revision, incomplete checklist)."""


class PublishLimitReached(WorkflowError):
    """A publishing limit the site owner set is reached. The article stays approved and can go out later."""

    def __init__(self, reason: str, retry_after=None) -> None:
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


def _article(session: Session, article_id: uuid.UUID, *, lock: bool = True) -> Article:
    current_context(session)
    stmt = select(Article).where(Article.id == article_id)
    if lock:
        stmt = stmt.with_for_update()
    article = session.scalars(stmt).first()
    if article is None:
        raise WorkflowError(f"article {article_id} not found")  # also what another tenant's article looks like
    return article


def _touch(article: Article) -> None:
    article.updated_at = func.now()


def create_article(
    session: Session,
    *,
    title: str,
    body_html: str,
    standfirst: str | None = None,
    origin: str = "draft",
    lead_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
) -> Article:
    """A new article with its first revision, in status draft. It takes its lead's category unless given one."""
    ctx = current_context(session)
    if category_id is None and lead_id is not None:
        category_id = session.scalar(select(Lead.category_id).where(Lead.id == lead_id))
    article = Article(tenant_id=ctx.tenant_id, lead_id=lead_id, category_id=category_id, status="draft")
    session.add(article)
    session.flush()
    _add_revision(session, article, title=title, standfirst=standfirst, body_html=body_html, origin=origin)
    audit.record(session, "article.created", target_type="article", target_id=str(article.id))
    return article


def _add_revision(
    session: Session, article: Article, *, title: str, standfirst: str | None, body_html: str, origin: str
) -> ArticleRevision:
    ctx = current_context(session)
    if not title.strip() or not body_html.strip():
        raise WorkflowError("a revision needs a title and a body")
    number = (
        session.scalar(select(func.max(ArticleRevision.number)).where(ArticleRevision.article_id == article.id)) or 0
    ) + 1
    rev = ArticleRevision(
        tenant_id=ctx.tenant_id,
        article_id=article.id,
        number=number,
        title=title,
        standfirst=standfirst,
        body_html=body_html,
        origin=origin,
        created_by=ctx.actor,
    )
    session.add(rev)
    session.flush()
    article.current_revision_id = rev.id
    return rev


def add_revision(
    session: Session,
    article_id: uuid.UUID,
    *,
    title: str,
    body_html: str,
    standfirst: str | None = None,
    origin: str,
) -> ArticleRevision:
    """Save new text. Any approval is cancelled and the article needs review again."""
    article = _article(session, article_id)
    if article.status == "rejected":
        raise WorkflowError("a rejected article cannot be revised; create a new one")
    rev = _add_revision(session, article, title=title, standfirst=standfirst, body_html=body_html, origin=origin)
    approval_reset = article.status in ("approved", "published", "unpublished")
    if article.status in ("approved", "published", "unpublished", "in_review"):
        article.status = "in_review"
    _touch(article)
    session.flush()
    audit.record(
        session,
        "article.revised",
        target_type="article",
        target_id=str(article.id),
        details={"revision": rev.number, "origin": origin, "approval_reset": approval_reset},
    )
    return rev


def submit_for_review(session: Session, article_id: uuid.UUID) -> Article:
    article = _article(session, article_id)
    if article.status not in ("draft", "held"):
        raise WorkflowError(f"cannot submit an article that is {article.status}")
    article.status, article.hold_reason = "in_review", None
    _touch(article)
    session.flush()
    audit.record(session, "article.submitted", target_type="article", target_id=str(article.id))
    return article


def hold(session: Session, article_id: uuid.UUID, reason: str) -> Article:
    """Stop an article from moving on (for example: a check could not decide, or found a problem)."""
    if not reason.strip():
        raise WorkflowError("a hold needs a reason")
    article = _article(session, article_id)
    if article.status not in ("draft", "in_review", "approved"):
        raise WorkflowError(f"cannot hold an article that is {article.status}")
    article.status, article.hold_reason = "held", reason
    _touch(article)
    session.flush()
    audit.record(session, "article.held", target_type="article", target_id=str(article.id), details={"reason": reason})
    return article


def approve(
    session: Session,
    article_id: uuid.UUID,
    revision_id: uuid.UUID,
    checklist: Mapping[str, bool],
    *,
    note: str | None = None,
    required: tuple[str, ...] = (),
) -> Approval:
    """A person approves one exact revision.

    revision_id must be the revision the editor actually read: if the text changed since, the approval is refused.
    """
    ctx = current_context(session)
    if not is_human(ctx.actor):
        raise ApprovalError(f"{ctx.actor!r} is not a person; only a person can approve an article")
    article = _article(session, article_id)
    if article.status != "in_review":
        raise ApprovalError(f"only an article in review can be approved (this one is {article.status})")
    if revision_id != article.current_revision_id:
        raise ApprovalError("the text changed after it was reviewed; review the latest revision")
    missing = [item for item in (*REQUIRED_CHECKLIST, *required) if checklist.get(item) is not True]
    if missing:
        raise ApprovalError(f"checklist incomplete: {', '.join(missing)}")

    rev = session.get(ArticleRevision, revision_id)
    approval = Approval(
        tenant_id=ctx.tenant_id,
        article_id=article.id,
        revision_id=rev.id,
        content_hash=rev.content_hash,
        decision="approved",
        approver=ctx.actor,
        checklist=dict(checklist),
        note=note,
    )
    session.add(approval)
    article.status, article.approved_revision_id = "approved", rev.id
    _touch(article)
    session.flush()
    audit.record(
        session,
        "article.approved",
        target_type="article",
        target_id=str(article.id),
        details={"revision": rev.number, "content_hash": rev.content_hash},
    )
    return approval


def auto_approve(session: Session, article_id: uuid.UUID, checks: Mapping[str, bool]) -> Approval | None:
    """The system approves an article in "auto" mode, if it may. Returns None when a person has to decide.

    - In "human" mode nothing happens: the article stays in review for a person.
    - In "auto" mode, if the lead was not approved by a person, or any automated check did not pass (or did not
      report), the article is held with the reason, for a person to look at. It fails closed.
    """
    ctx = current_context(session)
    if is_human(ctx.actor):
        raise ApprovalError("a person approving an article should use approve(), which records their checklist")
    article = _article(session, article_id)
    if article.status != "in_review":
        raise ApprovalError(f"only an article in review can be approved (this one is {article.status})")
    if effective_mode(session, article.category_id) != "auto":
        return None
    lead = session.get(Lead, article.lead_id) if article.lead_id else None
    if lead is None or not (lead.approved_by and is_human(lead.approved_by)):
        hold(session, article_id, "auto-publish needs a person to approve the lead first")
        return None
    failed = [name for name in AUTO_CHECKS if checks.get(name) is not True]
    if failed:
        hold(session, article_id, f"automated checks did not pass: {', '.join(failed)}")
        return None

    rev = session.get(ArticleRevision, article.current_revision_id)
    approval = Approval(
        tenant_id=ctx.tenant_id,
        article_id=article.id,
        revision_id=rev.id,
        content_hash=rev.content_hash,
        decision="auto_approved",
        approver=ctx.actor,
        checklist=dict(checks),
        note=f"lead approved by {lead.approved_by}",
    )
    session.add(approval)
    article.status, article.approved_revision_id = "approved", rev.id
    _touch(article)
    session.flush()
    audit.record(
        session,
        "article.auto_approved",
        target_type="article",
        target_id=str(article.id),
        details={"revision": rev.number, "content_hash": rev.content_hash, "lead_approved_by": lead.approved_by},
    )
    return approval


def reject(session: Session, article_id: uuid.UUID, reason: str, *, changes_requested: bool = False) -> Article:
    """Turn an article down, or (changes_requested=True) send it back for another revision."""
    ctx = current_context(session)
    if not reason.strip():
        raise WorkflowError("a rejection needs a reason")
    article = _article(session, article_id)
    if article.status not in ("draft", "held", "in_review", "approved"):
        raise WorkflowError(f"cannot reject an article that is {article.status}")
    rev = session.get(ArticleRevision, article.current_revision_id)
    session.add(
        Approval(
            tenant_id=ctx.tenant_id,
            article_id=article.id,
            revision_id=rev.id,
            content_hash=rev.content_hash,
            decision="changes_requested" if changes_requested else "rejected",
            approver=ctx.actor,
            checklist={},
            note=reason,
        )
    )
    article.status = "draft" if changes_requested else "rejected"
    article.hold_reason = None
    _touch(article)
    session.flush()
    action = "article.changes_requested" if changes_requested else "article.rejected"
    audit.record(session, action, target_type="article", target_id=str(article.id), details={"reason": reason})
    return article


def mark_published(session: Session, article_id: uuid.UUID, *, override_limits: bool = False) -> Article:
    """Record that the approved current revision is now live.

    A publisher should call this in the same transaction as the publish itself, before sending the article to the
    site, so a refusal (for example PublishLimitReached) means nothing went out. A first publication respects the
    site's own publishing limits, if it set any. A person may override them (it is recorded); the system may not.
    """
    ctx = current_context(session)
    publish_limits.lock_for_publishing(session)
    article = _article(session, article_id)
    if article.status not in ("approved", "unpublished"):
        raise WorkflowError(f"cannot publish an article that is {article.status}")
    if article.approved_revision_id is None or article.approved_revision_id != article.current_revision_id:
        raise ApprovalError("the current revision has not been approved")
    limit = publish_limits.check(session, article.id)
    if not limit.ok:
        if not (override_limits and is_human(ctx.actor)):
            raise PublishLimitReached(limit.reason, limit.retry_after)
        audit.record(
            session,
            "article.limit_overridden",
            target_type="article",
            target_id=str(article.id),
            details={"limit": limit.reason},
        )
    decisions = set(
        session.scalars(select(Approval.decision).where(Approval.revision_id == article.current_revision_id))
    )
    # What readers are told depends on how this exact text was approved. A person's approval wins.
    article.disclosure = "human_reviewed" if "approved" in decisions else "not_human_reviewed"
    article.status = "published"
    article.published_revision_id = article.current_revision_id
    article.first_published_at = article.first_published_at or func.now()
    article.last_published_at = func.now()
    _touch(article)
    session.flush()
    audit.record(
        session,
        "article.published",
        target_type="article",
        target_id=str(article.id),
        details={"revision_id": str(article.published_revision_id), "disclosure": article.disclosure},
    )
    return article


def unpublish(session: Session, article_id: uuid.UUID, reason: str) -> Article:
    """Take an article down. Always allowed for a published article, so complaints can be acted on at once."""
    if not reason.strip():
        raise WorkflowError("an unpublish needs a reason (it goes in the audit log)")
    article = _article(session, article_id)
    if article.status != "published" and not (article.status == "in_review" and article.published_revision_id):
        raise WorkflowError(f"article is not live (it is {article.status})")
    article.status = "unpublished"
    _touch(article)
    session.flush()
    audit.record(
        session, "article.unpublished", target_type="article", target_id=str(article.id), details={"reason": reason}
    )
    return article


def revisions(session: Session, article_id: uuid.UUID) -> list[ArticleRevision]:
    current_context(session)
    stmt = select(ArticleRevision).where(ArticleRevision.article_id == article_id).order_by(ArticleRevision.number)
    return list(session.scalars(stmt))


def approvals(session: Session, article_id: uuid.UUID) -> list[Approval]:
    current_context(session)
    stmt = select(Approval).where(Approval.article_id == article_id).order_by(Approval.decided_at)
    return list(session.scalars(stmt))
