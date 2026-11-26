# SPDX-License-Identifier: AGPL-3.0-only
"""Tenant isolation. These tests are the safety net for the most damaging bug a multi-tenant product can have:
one customer seeing or changing another customer's data."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, ProgrammingError

from newspilot import audit, settings
from newspilot.db import AuditEntry, Tenant, TenantSetting
from newspilot.tenancy import (
    TenantContext,
    active_tenant_ids,
    current_context,
    tenant_id_for_slug,
    tenant_session,
)

pytestmark = pytest.mark.usefixtures("database")


def test_every_tenant_table_has_forced_rls(owner_engine):
    """Any table with a tenant_id column (and tenants itself) must have RLS enabled AND forced, with a policy."""
    with owner_engine.connect() as c:
        rows = c.execute(
            text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND (c.relname = 'tenants' OR EXISTS (
                    SELECT 1 FROM information_schema.columns col
                    WHERE col.table_schema = 'public' AND col.table_name = c.relname AND col.column_name = 'tenant_id'))
            """)
        ).all()
    assert {r.relname for r in rows} >= {"tenants", "tenant_settings", "tenant_keys", "tenant_secrets", "audit_log"}
    for r in rows:
        assert r.relrowsecurity, f"{r.relname}: row-level security is not enabled"
        assert r.relforcerowsecurity, f"{r.relname}: row-level security is not forced"
        assert r.policies >= 1, f"{r.relname}: no policy"


def test_app_role_cannot_bypass_rls(owner_engine, database):
    with owner_engine.connect() as c:
        role = c.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": database["app_role"]}
        ).one()
        owns = c.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = :r"),
            {"r": database["app_role"]},
        ).scalar_one()
    assert not role.rolsuper and not role.rolbypassrls
    assert owns == 0, "the application role must not own any table"


def test_tenant_sees_only_its_own_rows(sessions, two_tenants):
    a, b = two_tenants
    with tenant_session(sessions, a) as s:
        settings.put(s, "site.name", "A's site")
    with tenant_session(sessions, b) as s:
        settings.put(s, "site.name", "B's site")

    with tenant_session(sessions, a) as s:
        assert settings.get(s, "site.name") == "A's site"
        assert {row.tenant_id for row in s.scalars(select(TenantSetting))} == {a.tenant_id}
        assert {e.tenant_id for e in audit.recent(s)} == {a.tenant_id}
        assert [t.id for t in s.scalars(select(Tenant))] == [a.tenant_id]


def test_cannot_write_rows_for_another_tenant(sessions, two_tenants):
    a, b = two_tenants
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_session(sessions, a) as s:
        s.add(TenantSetting(tenant_id=b.tenant_id, key="planted", value=1, updated_by="attacker"))
        s.flush()


def test_cannot_change_another_tenants_rows(sessions, two_tenants):
    a, b = two_tenants
    with tenant_session(sessions, b) as s:
        settings.put(s, "colour", "blue")
    with tenant_session(sessions, a) as s:
        result = s.execute(update(TenantSetting).where(TenantSetting.key == "colour").values(value="red"))
        assert result.rowcount == 0
        result = s.execute(update(Tenant).where(Tenant.id == b.tenant_id).values(name="taken over"))
        assert result.rowcount == 0
    with tenant_session(sessions, b) as s:
        assert settings.get(s, "colour") == "blue"
        assert s.get(Tenant, b.tenant_id).name == "Tenant B"


def test_no_tenant_set_means_no_rows(sessions, two_tenants):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        settings.put(s, "x", 1)
    # pool_size=1: this is the same pooled connection the tenant transaction just used.
    with sessions() as s:
        assert s.execute(text("SELECT current_setting('newspilot.tenant_id', true)")).scalar_one() in ("", None)
        assert s.scalars(select(TenantSetting)).all() == []
        assert s.scalars(select(Tenant)).all() == []
        assert s.scalars(select(AuditEntry)).all() == []


def test_tenant_setting_does_not_leak_to_next_transaction(sessions, two_tenants):
    a, b = two_tenants
    with tenant_session(sessions, a) as s:
        assert s.execute(text("SELECT newspilot_current_tenant()")).scalar_one() == a.tenant_id
    with tenant_session(sessions, b) as s:
        assert s.execute(text("SELECT newspilot_current_tenant()")).scalar_one() == b.tenant_id
    with sessions() as s:
        assert s.execute(text("SELECT newspilot_current_tenant()")).scalar_one() is None


def test_owner_is_filtered_too(owner_engine, sessions, two_tenants):
    """FORCE ROW LEVEL SECURITY: connecting as the table owner by mistake still shows no tenant data."""
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        settings.put(s, "y", 2)
    with owner_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM tenant_settings")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 0


def test_app_cannot_create_or_delete_tenants_directly(sessions, two_tenants):
    a, _ = two_tenants
    with pytest.raises(ProgrammingError, match="permission denied"), sessions() as s, s.begin():
        s.execute(text("INSERT INTO tenants (slug, name) VALUES ('sneaky', 'Sneaky')"))
    with pytest.raises(ProgrammingError, match="permission denied"), tenant_session(sessions, a) as s:
        s.execute(text("DELETE FROM tenants"))


def test_audit_log_is_append_only(sessions, owner_engine, two_tenants):
    a, _ = two_tenants
    with tenant_session(sessions, a) as s:
        audit.record(s, "test.event", details={"n": 1})
    with pytest.raises(ProgrammingError, match="permission denied"), tenant_session(sessions, a) as s:
        s.execute(text("UPDATE audit_log SET action = 'rewritten'"))
    with pytest.raises(ProgrammingError, match="permission denied"), tenant_session(sessions, a) as s:
        s.execute(text("DELETE FROM audit_log"))
    # Even the owner, who has full table privileges, is stopped by the trigger.
    with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as c:
        c.execute(text("SELECT set_config('newspilot.tenant_id', :t, true)"), {"t": str(a.tenant_id)})
        c.execute(text("DELETE FROM audit_log"))


def test_cross_tenant_lookups(sessions, two_tenants):
    a, b = two_tenants
    with tenant_session(sessions, a) as s:
        slug = s.get(Tenant, a.tenant_id).slug
    assert tenant_id_for_slug(sessions, slug) == a.tenant_id
    assert tenant_id_for_slug(sessions, "does-not-exist") is None
    assert {a.tenant_id, b.tenant_id} <= set(active_tenant_ids(sessions))


def test_context_validation_and_scoping(sessions):
    with pytest.raises(TypeError):
        TenantContext("not-a-uuid")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TenantContext(uuid.uuid4(), actor=" ")
    with sessions() as s, pytest.raises(RuntimeError, match="not scoped"):
        current_context(s)
