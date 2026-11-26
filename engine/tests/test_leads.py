# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from newspilot import leads
from newspilot.tenancy import tenant_session


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://Example.com/story/", "https://example.com/story"),
        ("http://example.com/story?utm_source=x&id=7", "https://example.com/story?id=7"),
        ("https://example.com/story#comments", "https://example.com/story"),
        ("https://example.com/s?b=2&a=1&fbclid=z", "https://example.com/s?a=1&b=2"),
    ],
)
def test_same_story_normalises_to_the_same_key(a, b):
    assert leads.source_hash(a) == leads.source_hash(b)


def test_different_stories_differ():
    assert leads.source_hash("https://example.com/a") != leads.source_hash("https://example.com/b")
    assert leads.source_hash("https://example.com/s?id=1") != leads.source_hash("https://example.com/s?id=2")


@pytest.mark.parametrize("bad", ["ftp://example.com/x", "not a url", "javascript:alert(1)"])
def test_only_web_addresses(bad):
    with pytest.raises(ValueError):
        leads.normalize_url(bad)


@pytest.mark.usefixtures("database")
def test_get_or_create_is_idempotent_per_tenant(sessions, two_tenants):
    a, b = two_tenants
    url = "https://example.com/news/item-1?utm_campaign=feed"
    with tenant_session(sessions, a) as s:
        first, created = leads.get_or_create(s, source_url=url, title="Item 1")
        again, created_again = leads.get_or_create(s, source_url="https://example.com/news/item-1", title="Item 1")
    assert created and not created_again and first.id == again.id
    with tenant_session(sessions, b) as s:  # another tenant gets its own lead for the same story
        other, created_other = leads.get_or_create(s, source_url=url, title="Item 1")
    assert created_other and other.id != first.id
    with tenant_session(sessions, a) as s:
        assert leads.set_status(s, first.id, "shortlisted").status == "shortlisted"
        with pytest.raises(ValueError):
            leads.set_status(s, first.id, "published")
    with tenant_session(sessions, b) as s, pytest.raises(LookupError):
        leads.set_status(s, first.id, "rejected")
