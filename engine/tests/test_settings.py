# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from newspilot import audit, settings
from newspilot.tenancy import tenant_session

pytestmark = pytest.mark.usefixtures("database")


def test_put_get_and_upsert(sessions, two_tenants):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        settings.put(s, "publish.daily_cap", 5)
        settings.put(s, "sections", [{"slug": "local", "name": "Local"}])
        settings.put(s, "publish.daily_cap", 10)
        assert settings.get(s, "publish.daily_cap") == 10
        assert settings.get(s, "missing", "fallback") == "fallback"
        assert settings.get_all(s) == {"publish.daily_cap": 10, "sections": [{"slug": "local", "name": "Local"}]}
        assert [e.action for e in audit.recent(s)][:3] == ["setting.changed"] * 3
        assert settings.delete(s, "sections") is True
        assert settings.delete(s, "sections") is False


@pytest.mark.parametrize(
    "key", ["openrouter_api_key", "llm.api_key", "slack.token", "smtp_password", "cms.secret", "private"]
)
def test_credentials_are_refused(sessions, two_tenants, key):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s, pytest.raises(settings.SecretInSettingsError):
        settings.put(s, key, "value")


@pytest.mark.parametrize("key", ["keyword_list", "monkey_mode", "tokenizer", "publish.daily_cap"])
def test_ordinary_keys_are_allowed(sessions, two_tenants, key):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        settings.put(s, key, True)
        assert settings.get(s, key) is True
