# SPDX-License-Identifier: AGPL-3.0-only
"""Publishing limits are the site owner's own settings: none by default, and when set, articles wait rather than
being held, updates to live articles are never delayed, and a person may override (recorded)."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text

from newspilot import audit, categories, leads, publish_limits, settings, settings_registry, workflow
from newspilot.db import Article
from newspilot.settings_registry import InvalidSettingError
from newspilot.tenancy import tenant_session
from newspilot.workflow import PublishLimitReached

CHECKS = dict.fromkeys(workflow.REQUIRED_CHECKLIST, True)


# ── settings definitions (no database needed) ─────────────────────────────────────────────────────────────────


def test_every_publishing_limit_is_off_by_default_and_explained():
    title, intro = settings_registry.GROUPS["publishing"]
    assert "Google" in intro and "scaled content abuse" in intro and "no publishing limit unless you set" in intro
    for d in settings_registry.by_group()["publishing"]:
        assert d.default in (None, {}), f"{d.key} must be off by default"
        assert d.label and len(d.help) > 40


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("publish.daily_limit", "5", 5),
        ("publish.daily_limit", "none", None),
        ("publish.daily_limit", "", None),
        ("publish.min_gap_minutes", "0", 0),
        ("publish.category_daily_limits", "results=6, analysis=2", {"results": 6, "analysis": 2}),
        ("publish.category_daily_limits", {"local": "3"}, {"local": 3}),
    ],
)
def test_values_are_parsed(key, raw, expected):
    assert settings_registry.definition(key).coerce(raw) == expected


@pytest.mark.parametrize(
    "key,raw",
    [
        ("publish.daily_limit", "0"),
        ("publish.daily_limit", "-2"),
        ("publish.daily_limit", "lots"),
        ("publish.daily_limit", True),
        ("publish.min_gap_minutes", "99999"),
        ("publish.category_daily_limits", "results"),
        ("publish.category_daily_limits", "Not A Slug=3"),
        ("publish.category_daily_limits", "results=0"),
    ],
)
def test_bad_values_are_refused(key, raw):
    with pytest.raises(InvalidSettingError):
        settings_registry.definition(key).coerce(raw)


# ── limits against the database ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def ctxs(two_tenants):
    a, _ = two_tenants
    return {
        "owner": dataclasses.replace(a, actor="user:site-owner"),
        "system": dataclasses.replace(a, actor="system:publisher"),
    }


def _approved(sessions, ctxs, *, source="https://outlet-one.example/story", category_id=None, n=[0]):  # noqa: B006
    n[0] += 1
    with tenant_session(sessions, ctxs["system"]) as s:
        lead, _ = leads.get_or_create(s, source_url=f"{source}-{n[0]}", title=f"Story {n[0]}")
        art = workflow.create_article(
            s, title=f"Story {n[0]}", body_html="<p>Body</p>", lead_id=lead.id, category_id=category_id
        )
        workflow.submit_for_review(s, art.id)
    with tenant_session(sessions, ctxs["owner"]) as s:
        workflow.approve(s, art.id, s.get(Article, art.id).current_revision_id, CHECKS)
    return art.id


def _publish(sessions, ctx, article_id, **kw):
    with tenant_session(sessions, ctx) as s:
        workflow.mark_published(s, article_id, **kw)


def _set(sessions, ctxs, key, value):
    with tenant_session(sessions, ctxs["owner"]) as s:
        settings.put(s, key, value)


@pytest.mark.usefixtures("database")
def test_no_limits_by_default(sessions, ctxs):
    for _ in range(12):
        _publish(sessions, ctxs["system"], _approved(sessions, ctxs))


@pytest.mark.usefixtures("database")
def test_site_daily_limit_makes_articles_wait(sessions, ctxs):
    _set(sessions, ctxs, "publish.daily_limit", "2")
    first, second, third = (_approved(sessions, ctxs) for _ in range(3))
    _publish(sessions, ctxs["system"], first)
    _publish(sessions, ctxs["system"], second)
    with pytest.raises(PublishLimitReached, match="2 new articles per 24 hours") as exc:
        _publish(sessions, ctxs["system"], third)
    assert exc.value.retry_after is not None
    with tenant_session(sessions, ctxs["system"]) as s:
        assert s.get(Article, third).status == "approved"  # waiting, not held


@pytest.mark.usefixtures("database")
def test_updates_to_live_articles_are_never_delayed(sessions, ctxs):
    _set(sessions, ctxs, "publish.daily_limit", "1")
    live = _approved(sessions, ctxs)
    _publish(sessions, ctxs["system"], live)
    with tenant_session(sessions, ctxs["owner"]) as s:
        rev = workflow.add_revision(s, live, title="Corrected", body_html="<p>Fixed</p>", origin="human_edit")
        workflow.approve(s, live, rev.id, CHECKS)
    _publish(sessions, ctxs["system"], live)  # over the limit, but it is a correction


@pytest.mark.usefixtures("database")
def test_a_person_can_override_and_it_is_recorded(sessions, ctxs):
    _set(sessions, ctxs, "publish.daily_limit", "1")
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs))
    urgent = _approved(sessions, ctxs)
    with pytest.raises(PublishLimitReached):
        _publish(sessions, ctxs["system"], urgent, override_limits=True)  # the system cannot override
    _publish(sessions, ctxs["owner"], urgent, override_limits=True)
    with tenant_session(sessions, ctxs["owner"]) as s:
        assert "article.limit_overridden" in [e.action for e in audit.recent(s)]


@pytest.mark.usefixtures("database")
def test_category_limit(sessions, ctxs):
    with tenant_session(sessions, ctxs["owner"]) as s:
        results = categories.upsert(s, slug="results", name="Results").id
        other = categories.upsert(s, slug="features", name="Features").id
    _set(sessions, ctxs, "publish.category_daily_limits", "results=1")
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs, category_id=results))
    with pytest.raises(PublishLimitReached, match="category 'results'"):
        _publish(sessions, ctxs["system"], _approved(sessions, ctxs, category_id=results))
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs, category_id=other))  # not limited


@pytest.mark.usefixtures("database")
def test_source_outlet_limit(sessions, ctxs):
    _set(sessions, ctxs, "publish.source_daily_limit", "1")
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs, source="https://www.outlet-one.example/a"))
    with pytest.raises(PublishLimitReached, match="outlet-one.example"):
        _publish(sessions, ctxs["system"], _approved(sessions, ctxs, source="https://outlet-one.example/b"))
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs, source="https://outlet-two.example/c"))


@pytest.mark.usefixtures("database")
def test_minimum_gap(sessions, ctxs):
    _set(sessions, ctxs, "publish.min_gap_minutes", "30")
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs))
    nxt = _approved(sessions, ctxs)
    with pytest.raises(PublishLimitReached, match="every 30 minutes") as exc:
        _publish(sessions, ctxs["system"], nxt)
    assert exc.value.retry_after is not None
    # Once the gap has passed (simulated by moving the earlier publish back), it goes out.
    with tenant_session(sessions, ctxs["system"]) as s:
        s.execute(
            text(
                "UPDATE articles SET first_published_at = now() - interval '31 minutes' "
                "WHERE first_published_at IS NOT NULL"
            )
        )
    _publish(sessions, ctxs["system"], nxt)


@pytest.mark.usefixtures("database")
def test_clearing_a_limit_returns_to_no_limit(sessions, ctxs):
    _set(sessions, ctxs, "publish.daily_limit", "1")
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs))
    _set(sessions, ctxs, "publish.daily_limit", "none")
    with tenant_session(sessions, ctxs["owner"]) as s:
        assert settings.get(s, "publish.daily_limit") is None
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs))


@pytest.mark.usefixtures("database")
def test_check_reports_without_publishing(sessions, ctxs):
    _set(sessions, ctxs, "publish.daily_limit", "1")
    _publish(sessions, ctxs["system"], _approved(sessions, ctxs))
    waiting = _approved(sessions, ctxs)
    with tenant_session(sessions, ctxs["system"]) as s:
        result = publish_limits.check(s, waiting)
    assert not result.ok and result.reason and result.retry_after
