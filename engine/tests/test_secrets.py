# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select

from newspilot import audit
from newspilot.db import TenantKey, TenantSecret
from newspilot.secret_store import LocalKeyProvider, SecretError, SecretStore, SecretValue, new_master_key
from newspilot.tenancy import tenant_session

pytestmark = pytest.mark.usefixtures("database")

VALUE = "test-value-" + "x" * 24  # a fake credential: never a real one in tests


def test_round_trip_and_redaction(sessions, two_tenants, keys):
    a, _ = two_tenants
    store = SecretStore(keys)
    with tenant_session(sessions, a) as s:
        info = store.put(s, "llm.api_key", VALUE)
        assert info.status == "unchecked" and len(info.fingerprint) == 16
    with tenant_session(sessions, a) as s:
        value = store.get(s, "llm.api_key")
    assert value.reveal() == VALUE
    assert VALUE not in repr(value) and VALUE not in str(value) and VALUE not in f"{value}"


def test_value_is_never_stored_in_plain_text(sessions, two_tenants, keys):
    a, _ = two_tenants
    store = SecretStore(keys)
    with tenant_session(sessions, a) as s:
        store.put(s, "cms.password", VALUE)
        row = s.get(TenantSecret, (a.tenant_id, "cms.password"))
        key = s.scalars(select(TenantKey)).one()
        assert VALUE.encode() not in row.ciphertext
        assert len(key.wrapped_key) == 12 + 32 + 16  # nonce + data key + GCM tag: never the raw key
        details = [e.details for e in audit.recent(s)]
        assert all(VALUE not in str(d) for d in details)


def test_other_tenant_cannot_read(sessions, two_tenants, keys):
    a, b = two_tenants
    store = SecretStore(keys)
    with tenant_session(sessions, a) as s:
        store.put(s, "search.api_key", VALUE)
    with tenant_session(sessions, b) as s:
        assert store.get(s, "search.api_key") is None
        assert store.list(s) == []


def test_ciphertext_moved_to_another_tenant_does_not_decrypt(sessions, two_tenants, keys):
    a, b = two_tenants
    store = SecretStore(keys)
    with tenant_session(sessions, a) as s:
        store.put(s, "shared.name", VALUE)
        stolen = s.get(TenantSecret, (a.tenant_id, "shared.name"))
        nonce, ciphertext = stolen.nonce, stolen.ciphertext
    with tenant_session(sessions, b) as s:
        store.put(s, "shared.name", "b-own-value-" + "y" * 20)
        row = s.get(TenantSecret, (b.tenant_id, "shared.name"))
        row.nonce, row.ciphertext = nonce, ciphertext
    with tenant_session(sessions, b) as s, pytest.raises(SecretError):
        store.get(s, "shared.name")


def test_ciphertext_moved_to_another_name_does_not_decrypt(sessions, two_tenants, keys):
    a, _ = two_tenants
    store = SecretStore(keys)
    with tenant_session(sessions, a) as s:
        store.put(s, "first", VALUE)
        store.put(s, "second", "other-value-" + "z" * 20)
        first = s.get(TenantSecret, (a.tenant_id, "first"))
        second = s.get(TenantSecret, (a.tenant_id, "second"))
        second.nonce, second.ciphertext = first.nonce, first.ciphertext
    with tenant_session(sessions, a) as s, pytest.raises(SecretError):
        store.get(s, "second")


def test_wrong_master_key_fails_closed(sessions, two_tenants, keys):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        SecretStore(keys).put(s, "k", VALUE)
    impostor = LocalKeyProvider(secrets.token_bytes(32), key_id=keys.key_id)
    with tenant_session(sessions, a) as s, pytest.raises(SecretError, match="wrong master key"):
        SecretStore(impostor).get(s, "k")
    other_id = LocalKeyProvider(secrets.token_bytes(32), key_id="another-key")
    with tenant_session(sessions, a) as s, pytest.raises(SecretError, match="was wrapped with"):
        SecretStore(other_id).get(s, "k")


def test_replace_check_delete_and_audit(sessions, two_tenants, keys):
    a, _ = two_tenants
    store = SecretStore(keys)
    with tenant_session(sessions, a) as s:
        first = store.put(s, "k", VALUE)
        same = store.put(s, "k", SecretValue(VALUE))
        assert same.fingerprint == first.fingerprint
        checked = store.mark_checked(s, "k", ok=True, message="provider accepted the key")
        assert checked.status == "valid" and checked.checked_at is not None
        changed = store.put(s, "k", "different-" + "q" * 20)
        assert changed.fingerprint != first.fingerprint
        assert changed.status == "unchecked"  # a new value must be checked again
        assert store.delete(s, "k") is True
        assert store.delete(s, "k") is False
        actions = [e.action for e in audit.recent(s)]
    assert actions[:5] == ["secret.deleted", "secret.replaced", "secret.checked", "secret.replaced", "secret.created"]


def test_empty_secret_rejected(sessions, two_tenants, keys):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s, pytest.raises(ValueError):
        SecretStore(keys).put(s, "k", "")


def test_listing_needs_no_master_key(sessions, two_tenants, keys):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        SecretStore(keys).put(s, "k", VALUE)
    with tenant_session(sessions, a) as s:
        infos = SecretStore(None).list(s)
    assert [i.name for i in infos] == ["k"]
    assert not hasattr(infos[0], "value")


def test_master_key_format(monkeypatch):
    key = new_master_key()
    monkeypatch.setenv("NEWSPILOT_MASTER_KEY", key)
    assert LocalKeyProvider.from_env().key_id == "local-1"
    monkeypatch.setenv("NEWSPILOT_MASTER_KEY", "dG9vLXNob3J0")  # base64 of "too-short"
    with pytest.raises(Exception, match="32 bytes"):
        LocalKeyProvider.from_env()
