# SPDX-License-Identifier: AGPL-3.0-only
"""The approval gate. Nothing reaches "published" without a person approving that exact text, and the database
enforces it even when application code is bypassed."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from newspilot import audit, workflow
from newspilot.db import Approval, Article, ArticleRevision
from newspilot.tenancy import TenantContext, tenant_session
from newspilot.workflow import ApprovalError, WorkflowError

pytestmark = pytest.mark.usefixtures("database")

CHECKS = {"facts_checked": True, "quotes_checked": True, "images_licensed": True, "sources_credited": True}


@pytest.fixture
def editor(two_tenants):
    a, _ = two_tenants
    return dataclasses.replace(a, actor="user:editor-1")


@pytest.fixture
def machine(two_tenants):
    a, _ = two_tenants
    return dataclasses.replace(a, actor="system:pipeline")


def _draft(sessions, ctx, body="<p>First version.</p>"):
    with tenant_session(sessions, ctx) as s:
        article = workflow.create_article(s, title="A headline", body_html=body, standfirst="A standfirst")
        workflow.submit_for_review(s, article.id)
        return article.id, article.current_revision_id


def _status(sessions, ctx, article_id):
    with tenant_session(sessions, ctx) as s:
        return s.get(Article, article_id)


def test_happy_path(sessions, machine, editor):
    article_id, rev_id = _draft(sessions, machine)
    with tenant_session(sessions, editor) as s:
        approval = workflow.approve(s, article_id, rev_id, CHECKS, note="Checked against both sources")
        assert approval.approver == "user:editor-1" and len(approval.content_hash) == 64
        workflow.mark_published(s, article_id)
    art = _status(sessions, editor, article_id)
    assert art.status == "published" and art.published_revision_id == rev_id and art.first_published_at
    with tenant_session(sessions, editor) as s:
        actions = [e.action for e in audit.recent(s)][:4]
    assert actions == ["article.published", "article.approved", "article.submitted", "article.created"]


def test_content_hash_is_computed_by_the_database(sessions, machine):
    article_id, rev_id = _draft(sessions, machine, body="<p>Body.</p>")
    with tenant_session(sessions, machine) as s:
        rev = s.get(ArticleRevision, rev_id)
        expected = s.execute(
            text("SELECT newspilot_content_hash('A headline', 'A standfirst', '<p>Body.</p>')")
        ).scalar_one()
    assert rev.content_hash == expected


def test_machines_cannot_approve(sessions, machine):
    article_id, rev_id = _draft(sessions, machine)
    with tenant_session(sessions, machine) as s, pytest.raises(ApprovalError, match="not a person"):
        workflow.approve(s, article_id, rev_id, CHECKS)
    for actor in ("system:pipeline", "user:", "anonymous", "cli: "):
        assert not workflow.is_human(actor), actor


def test_database_refuses_machine_approval(sessions, machine):
    article_id, rev_id = _draft(sessions, machine)
    with pytest.raises(IntegrityError, match="ck_approvals_human_approver"), tenant_session(sessions, machine) as s:
        rev = s.get(ArticleRevision, rev_id)
        s.add(
            Approval(
                tenant_id=machine.tenant_id,
                article_id=article_id,
                revision_id=rev_id,
                content_hash=rev.content_hash,
                decision="approved",
                approver="system:pipeline",
                checklist=CHECKS,
            )
        )
        s.flush()


def test_checklist_must_be_complete(sessions, machine, editor):
    article_id, rev_id = _draft(sessions, machine)
    for missing in workflow.REQUIRED_CHECKLIST:
        partial = {**CHECKS, missing: False}
        with tenant_session(sessions, editor) as s, pytest.raises(ApprovalError, match=missing):
            workflow.approve(s, article_id, rev_id, partial)
    with tenant_session(sessions, editor) as s, pytest.raises(ApprovalError, match="disclosure_present"):
        workflow.approve(s, article_id, rev_id, CHECKS, required=("disclosure_present",))


def test_stale_review_is_refused(sessions, machine, editor):
    article_id, old_rev = _draft(sessions, machine)
    with tenant_session(sessions, machine) as s:
        workflow.add_revision(s, article_id, title="A headline", body_html="<p>Changed.</p>", origin="quality_fix")
    with tenant_session(sessions, editor) as s, pytest.raises(ApprovalError, match="text changed"):
        workflow.approve(s, article_id, old_rev, CHECKS)


def test_new_revision_resets_approval(sessions, machine, editor):
    article_id, rev_id = _draft(sessions, machine)
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev_id, CHECKS)
    with tenant_session(sessions, machine) as s:
        workflow.add_revision(s, article_id, title="A headline", body_html="<p>Regenerated.</p>", origin="regenerate")
    art = _status(sessions, editor, article_id)
    assert art.status == "in_review" and art.approved_revision_id == rev_id != art.current_revision_id
    with tenant_session(sessions, editor) as s, pytest.raises(WorkflowError):
        workflow.mark_published(s, article_id)


def test_editing_a_live_article_needs_a_new_approval(sessions, machine, editor):
    article_id, rev1 = _draft(sessions, machine)
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev1, CHECKS)
        workflow.mark_published(s, article_id)
        rev2 = workflow.add_revision(
            s, article_id, title="A headline", body_html="<p>Correction.</p>", origin="human_edit"
        ).id
    art = _status(sessions, editor, article_id)
    assert art.status == "in_review" and art.published_revision_id == rev1  # the approved text stays live
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev2, CHECKS)
        workflow.mark_published(s, article_id)
    assert _status(sessions, editor, article_id).published_revision_id == rev2


def test_database_refuses_publishing_without_approval(sessions, machine):
    article_id, rev_id = _draft(sessions, machine)
    with pytest.raises(DBAPIError, match="no human approval"), tenant_session(sessions, machine) as s:
        s.execute(
            text("UPDATE articles SET status = 'published', published_revision_id = :r WHERE id = :a"),
            {"r": rev_id, "a": article_id},
        )


def test_database_refuses_publishing_a_different_revision_than_approved(sessions, machine, editor):
    article_id, rev1 = _draft(sessions, machine)
    with tenant_session(sessions, machine) as s:
        rev2 = workflow.add_revision(s, article_id, title="A headline", body_html="<p>v2</p>", origin="humanize").id
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev2, CHECKS)
    with pytest.raises(DBAPIError, match="no human approval"), tenant_session(sessions, machine) as s:
        s.execute(
            text("UPDATE articles SET status = 'published', published_revision_id = :r WHERE id = :a"),
            {"r": rev1, "a": article_id},
        )


def test_database_refuses_approval_for_different_text(sessions, machine, editor):
    article_id, rev_id = _draft(sessions, machine)
    with pytest.raises(DBAPIError, match="does not match revision"), tenant_session(sessions, editor) as s:
        s.add(
            Approval(
                tenant_id=editor.tenant_id,
                article_id=article_id,
                revision_id=rev_id,
                content_hash="0" * 64,
                decision="approved",
                approver="user:editor-1",
                checklist=CHECKS,
            )
        )
        s.flush()


def test_approved_status_must_point_at_current_revision(sessions, machine, editor):
    article_id, rev1 = _draft(sessions, machine)
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev1, CHECKS)
    # Bypassing the workflow: a new revision while the status stays "approved".
    with (
        pytest.raises(IntegrityError, match="ck_articles_approved_is_current"),
        tenant_session(sessions, machine) as s,
    ):
        art = s.get(Article, article_id)
        rev = ArticleRevision(
            tenant_id=machine.tenant_id,
            article_id=article_id,
            number=99,
            title="x",
            body_html="<p>x</p>",
            origin="regenerate",
            created_by="system:pipeline",
        )
        s.add(rev)
        s.flush()
        art.current_revision_id = rev.id
        s.flush()


def test_revisions_and_approvals_are_append_only(sessions, machine, editor):
    article_id, rev_id = _draft(sessions, machine)
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev_id, CHECKS)
    for sql in (
        "UPDATE article_revisions SET body_html = 'rewritten'",
        "DELETE FROM article_revisions",
        "UPDATE approvals SET approver = 'user:someone-else'",
        "DELETE FROM approvals",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"), tenant_session(sessions, editor) as s:
            s.execute(text(sql))


def test_owner_cannot_rewrite_history_either(owner_engine, sessions, machine):
    article_id, _ = _draft(sessions, machine)
    with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as c:
        c.execute(text("SELECT set_config('newspilot.tenant_id', :t, true)"), {"t": str(machine.tenant_id)})
        c.execute(text("UPDATE article_revisions SET body_html = 'rewritten' WHERE article_id = :a"), {"a": article_id})


def test_unpublish_any_time(sessions, machine, editor):
    article_id, rev_id = _draft(sessions, machine)
    with tenant_session(sessions, editor) as s:
        workflow.approve(s, article_id, rev_id, CHECKS)
        workflow.mark_published(s, article_id)
        with pytest.raises(WorkflowError, match="reason"):
            workflow.unpublish(s, article_id, " ")
        workflow.unpublish(s, article_id, "Complaint from the subject; reviewing")
    assert _status(sessions, editor, article_id).status == "unpublished"
    with tenant_session(sessions, editor) as s:  # republishing the same approved text is allowed
        workflow.mark_published(s, article_id)
    assert _status(sessions, editor, article_id).status == "published"


def test_hold_reject_and_changes_requested(sessions, machine, editor):
    article_id, _ = _draft(sessions, machine)
    with tenant_session(sessions, machine) as s:
        workflow.hold(s, article_id, "information gain below threshold")
    held = _status(sessions, machine, article_id)
    assert held.status == "held" and held.hold_reason
    with tenant_session(sessions, editor) as s:
        workflow.reject(s, article_id, "Adds nothing to the source", changes_requested=True)
    assert _status(sessions, editor, article_id).status == "draft"
    with tenant_session(sessions, editor) as s:
        workflow.reject(s, article_id, "Not for us")
        assert [a.decision for a in workflow.approvals(s, article_id)] == ["changes_requested", "rejected"]
    with tenant_session(sessions, machine) as s, pytest.raises(WorkflowError, match="rejected"):
        workflow.add_revision(s, article_id, title="t", body_html="<p>b</p>", origin="regenerate")


def test_other_tenant_cannot_touch_the_article(sessions, two_tenants, machine, editor):
    _, b = two_tenants
    article_id, rev_id = _draft(sessions, machine)
    other_editor = TenantContext(b.tenant_id, actor="user:editor-2")
    with tenant_session(sessions, other_editor) as s, pytest.raises(WorkflowError, match="not found"):
        workflow.approve(s, article_id, rev_id, CHECKS)
    with tenant_session(sessions, other_editor) as s, pytest.raises(WorkflowError, match="not found"):
        workflow.unpublish(s, article_id, "attempt")
    with tenant_session(sessions, other_editor) as s:
        assert workflow.revisions(s, article_id) == []


def test_article_cannot_point_at_another_articles_revision(sessions, machine):
    article_a, _ = _draft(sessions, machine)
    _, rev_b = _draft(sessions, machine)
    with pytest.raises(IntegrityError, match="fk_articles_current_revision"), tenant_session(sessions, machine) as s:
        s.execute(text("UPDATE articles SET current_revision_id = :r WHERE id = :a"), {"r": rev_b, "a": article_a})
