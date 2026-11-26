# SPDX-License-Identifier: AGPL-3.0-only
"""Publishing modes: "human" (default) and opt-in "auto", per site with per-category overrides.

Auto-publish is allowed only when a person approved the lead and every automated check passed; otherwise the
article is held for a person. Readers are told when a person did not review the final text. The database enforces
the same rules."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from newspilot import audit, categories, leads, publish_mode, workflow
from newspilot.db import Approval, Article, ArticleRevision, Lead
from newspilot.publish_mode import PublishModeError
from newspilot.tenancy import tenant_session
from newspilot.workflow import ApprovalError

pytestmark = pytest.mark.usefixtures("database")

ALL_PASS = dict.fromkeys(workflow.AUTO_CHECKS, True)
CHECKS = dict.fromkeys(workflow.REQUIRED_CHECKLIST, True)


@pytest.fixture
def ctxs(two_tenants):
    a, _ = two_tenants
    return {
        "owner": dataclasses.replace(a, actor="user:site-owner"),
        "editor": dataclasses.replace(a, actor="user:slack-U123"),
        "system": dataclasses.replace(a, actor="system:pipeline"),
    }


def _category(sessions, ctx, slug):
    with tenant_session(sessions, ctx) as s:
        return categories.upsert(s, slug=slug, name=slug.title(), external_id=f"cms-{slug}").id


def _lead(sessions, ctxs, *, approve=True, category_id=None, n=[0]):  # noqa: B006 - a counter for unique URLs
    n[0] += 1
    with tenant_session(sessions, ctxs["system"]) as s:
        lead, _ = leads.get_or_create(s, source_url=f"https://example.org/story-{n[0]}", title=f"Story {n[0]}")
    if approve:
        with tenant_session(sessions, ctxs["editor"]) as s:  # the Slack button
            leads.approve(s, lead.id, category_id=category_id)
    return lead.id


def _article_in_review(sessions, ctxs, lead_id):
    with tenant_session(sessions, ctxs["system"]) as s:
        art = workflow.create_article(s, title="Headline", body_html="<p>Body</p>", lead_id=lead_id)
        workflow.submit_for_review(s, art.id)
        return art.id


def _get(sessions, ctx, article_id):
    with tenant_session(sessions, ctx) as s:
        return s.get(Article, article_id)


def test_new_sites_start_in_human_mode(sessions, ctxs):
    with tenant_session(sessions, ctxs["system"]) as s:
        assert publish_mode.site_mode(s) == "human"
        assert publish_mode.effective_mode(s, None) == "human"


def test_auto_publish_happy_path(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with tenant_session(sessions, ctxs["system"]) as s:
        approval = workflow.auto_approve(s, article_id, ALL_PASS)
        assert approval.decision == "auto_approved" and "user:slack-U123" in approval.note
        workflow.mark_published(s, article_id)
    art = _get(sessions, ctxs["system"], article_id)
    assert art.status == "published" and art.disclosure == "not_human_reviewed"


def test_human_mode_leaves_it_for_a_person(sessions, ctxs):
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with tenant_session(sessions, ctxs["system"]) as s:
        assert workflow.auto_approve(s, article_id, ALL_PASS) is None
    assert _get(sessions, ctxs["system"], article_id).status == "in_review"
    with tenant_session(sessions, ctxs["editor"]) as s:
        workflow.approve(s, article_id, s.get(Article, article_id).current_revision_id, CHECKS)
        workflow.mark_published(s, article_id)
    assert _get(sessions, ctxs["editor"], article_id).disclosure == "human_reviewed"


def test_auto_needs_a_person_to_approve_the_lead(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs, approve=False))
    with tenant_session(sessions, ctxs["system"]) as s:
        assert workflow.auto_approve(s, article_id, ALL_PASS) is None
    art = _get(sessions, ctxs["system"], article_id)
    assert art.status == "held" and "approve the lead" in art.hold_reason


@pytest.mark.parametrize("failing", workflow.AUTO_CHECKS)
def test_any_failed_or_missing_check_holds_the_article(sessions, ctxs, failing):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    for checks in ({**ALL_PASS, failing: False}, {k: v for k, v in ALL_PASS.items() if k != failing}):
        article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
        with tenant_session(sessions, ctxs["system"]) as s:
            assert workflow.auto_approve(s, article_id, checks) is None
        art = _get(sessions, ctxs["system"], article_id)
        assert art.status == "held" and failing in art.hold_reason


def test_category_overrides_the_site(sessions, ctxs):
    results = _category(sessions, ctxs["owner"], "results")
    analysis = _category(sessions, ctxs["owner"], "analysis")
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "human")
        publish_mode.set_category_mode(s, results, "auto")  # routine format: auto
        publish_mode.set_category_mode(s, analysis, "human")  # always a person
        assert publish_mode.effective_mode(s, results) == "auto"
        assert publish_mode.effective_mode(s, analysis) == "human"
        assert publish_mode.effective_mode(s, None) == "human"

    auto_article = _article_in_review(sessions, ctxs, _lead(sessions, ctxs, category_id=results))
    human_article = _article_in_review(sessions, ctxs, _lead(sessions, ctxs, category_id=analysis))
    with tenant_session(sessions, ctxs["system"]) as s:
        assert s.get(Article, auto_article).category_id == results  # inherited from the lead
        assert workflow.auto_approve(s, auto_article, ALL_PASS) is not None
        assert workflow.auto_approve(s, human_article, ALL_PASS) is None

    with tenant_session(sessions, ctxs["owner"]) as s:  # site auto, but this category always needs a person
        publish_mode.set_site_mode(s, "auto")
        publish_mode.set_category_mode(s, results, None)  # back to following the site
        assert publish_mode.effective_mode(s, results) == "auto"
        assert publish_mode.effective_mode(s, analysis) == "human"


def test_only_a_person_changes_modes(sessions, ctxs):
    cat = _category(sessions, ctxs["owner"], "briefs")
    with tenant_session(sessions, ctxs["system"]) as s, pytest.raises(PublishModeError):
        publish_mode.set_site_mode(s, "auto")
    with tenant_session(sessions, ctxs["system"]) as s, pytest.raises(PublishModeError):
        publish_mode.set_category_mode(s, cat, "auto")
    with (
        pytest.raises(IntegrityError, match="ck_tenants_auto_set_by_person"),
        tenant_session(sessions, ctxs["system"]) as s,
    ):
        s.execute(
            text(
                "UPDATE tenants SET publish_mode = 'auto', publish_mode_set_by = 'system:x', "
                "publish_mode_set_at = now()"
            )
        )
    with (
        pytest.raises(IntegrityError, match="ck_categories_auto_set_by_person"),
        tenant_session(sessions, ctxs["system"]) as s,
    ):
        s.execute(
            text(
                "UPDATE categories SET publish_mode = 'auto', publish_mode_set_by = 'system:x', "
                "publish_mode_set_at = now()"
            )
        )
    with tenant_session(sessions, ctxs["owner"]) as s:
        actions = [e.action for e in audit.recent(s)]
    assert "publish_mode.site" not in actions


def test_only_a_person_approves_a_lead(sessions, ctxs):
    lead_id = _lead(sessions, ctxs, approve=False)
    with tenant_session(sessions, ctxs["system"]) as s, pytest.raises(PermissionError):
        leads.approve(s, lead_id)
    with tenant_session(sessions, ctxs["system"]) as s, pytest.raises(ValueError, match="leads.approve"):
        leads.set_status(s, lead_id, "approved")
    with (
        pytest.raises(IntegrityError, match="ck_leads_approved_by_person"),
        tenant_session(sessions, ctxs["system"]) as s,
    ):
        s.execute(text("UPDATE leads SET approved_by = 'system:x', approved_at = now() WHERE id = :l"), {"l": lead_id})


def test_a_person_cannot_use_the_auto_path(sessions, ctxs):
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with tenant_session(sessions, ctxs["editor"]) as s, pytest.raises(ApprovalError, match="approve()"):
        workflow.auto_approve(s, article_id, ALL_PASS)


def _raw_auto_approval(s, ctx, article_id, *, approver="system:pipeline", checklist=None):
    rev = s.get(ArticleRevision, s.get(Article, article_id).current_revision_id)
    s.add(
        Approval(
            tenant_id=ctx.tenant_id,
            article_id=article_id,
            revision_id=rev.id,
            content_hash=rev.content_hash,
            decision="auto_approved",
            approver=approver,
            checklist=checklist or ALL_PASS,
        )
    )
    s.flush()


def test_database_refuses_auto_approval_in_human_mode(sessions, ctxs):
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with pytest.raises(DBAPIError, match="auto approval refused"), tenant_session(sessions, ctxs["system"]) as s:
        _raw_auto_approval(s, ctxs["system"], article_id)


def test_database_refuses_auto_approval_without_human_lead(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs, approve=False))
    with pytest.raises(DBAPIError, match="auto approval refused"), tenant_session(sessions, ctxs["system"]) as s:
        _raw_auto_approval(s, ctxs["system"], article_id)


def test_database_auto_approval_rules(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with (
        pytest.raises(IntegrityError, match="ck_approvals_auto_approver"),
        tenant_session(sessions, ctxs["system"]) as s,
    ):
        _raw_auto_approval(s, ctxs["system"], article_id, approver="user:someone")
    partial = {k: v for k, v in ALL_PASS.items() if k != "within_caps"}
    with (
        pytest.raises(IntegrityError, match="ck_approvals_auto_checks_passed"),
        tenant_session(sessions, ctxs["system"]) as s,
    ):
        _raw_auto_approval(s, ctxs["system"], article_id, checklist=partial)


def test_switching_back_to_human_stops_pending_auto_publishes(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with tenant_session(sessions, ctxs["system"]) as s:
        workflow.auto_approve(s, article_id, ALL_PASS)
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "human")
    with pytest.raises(DBAPIError, match="no valid approval"), tenant_session(sessions, ctxs["system"]) as s:
        workflow.mark_published(s, article_id)


def test_auto_published_article_cannot_be_relabelled_as_reviewed(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        publish_mode.set_site_mode(s, "auto")
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with tenant_session(sessions, ctxs["system"]) as s:
        workflow.auto_approve(s, article_id, ALL_PASS)
        workflow.mark_published(s, article_id)
    with pytest.raises(DBAPIError, match="no valid approval"), tenant_session(sessions, ctxs["system"]) as s:
        s.execute(text("UPDATE articles SET disclosure = 'human_reviewed' WHERE id = :a"), {"a": article_id})


def test_rejection_after_approval_revokes_it(sessions, ctxs):
    article_id = _article_in_review(sessions, ctxs, _lead(sessions, ctxs))
    with tenant_session(sessions, ctxs["editor"]) as s:
        workflow.approve(s, article_id, s.get(Article, article_id).current_revision_id, CHECKS)
        workflow.reject(s, article_id, "Second thoughts")
    with pytest.raises(DBAPIError, match="no valid approval"), tenant_session(sessions, ctxs["system"]) as s:
        s.execute(
            text(
                "UPDATE articles SET status = 'published', published_revision_id = current_revision_id, "
                "disclosure = 'human_reviewed' WHERE id = :a"
            ),
            {"a": article_id},
        )


def test_lead_category_and_approver_recorded(sessions, ctxs):
    cat = _category(sessions, ctxs["owner"], "local")
    lead_id = _lead(sessions, ctxs, category_id=cat)
    with tenant_session(sessions, ctxs["system"]) as s:
        lead = s.get(Lead, lead_id)
    assert lead.status == "approved" and lead.approved_by == "user:slack-U123" and lead.category_id == cat
    with tenant_session(sessions, ctxs["editor"]) as s, pytest.raises(ValueError, match="cannot approve"):
        leads.approve(s, lead_id)
