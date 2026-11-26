# SPDX-License-Identifier: AGPL-3.0-only
"""Tenancy core: tenants, settings, encrypted secrets and the audit log, with row-level security.

How isolation works:
- Tables are owned by the role that runs migrations. The application connects as a separate, non-owner role
  (NEWSPILOT_APP_ROLE) that gets only the grants listed here.
- Every tenant table has ENABLE and FORCE ROW LEVEL SECURITY, so the owner is filtered too.
- The current tenant comes from the transaction-local setting newspilot.tenant_id. newspilot_current_tenant()
  turns "unset" and "" (what a setting reverts to after a transaction) into NULL, so a query without a tenant
  matches no rows. It never matches every row.
- The tenants table is filtered the same way. The owner alone may list and insert tenants, and only through the
  SECURITY DEFINER functions below, which the application may call.

Revision ID: 0001
"""

from __future__ import annotations

from alembic import context, op
from sqlalchemy import text

from newspilot.config import validate_role_name

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = ("tenant_settings", "tenant_keys", "tenant_secrets", "audit_log")


def _roles() -> tuple[str, str]:
    owner = validate_role_name(op.get_bind().execute(text("SELECT current_user")).scalar_one())
    app = validate_role_name(context.config.attributes["app_role"])
    if owner == app:
        raise RuntimeError("the application role must not be the role that owns the tables (it would bypass RLS)")
    return owner, app


def upgrade() -> None:
    owner, app = _roles()

    op.execute("""
    CREATE FUNCTION newspilot_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('newspilot.tenant_id', true), '')::uuid $$;

    CREATE TABLE tenants (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        slug              varchar(63) NOT NULL,
        name              text NOT NULL,
        status            varchar(16) NOT NULL DEFAULT 'active',
        publisher_name    text,
        publisher_contact text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_tenants_slug UNIQUE (slug),
        CONSTRAINT ck_tenants_slug_format CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
        CONSTRAINT ck_tenants_status_valid CHECK (status IN ('active', 'paused', 'closed'))
    );

    CREATE TABLE tenant_settings (
        tenant_id  uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
        key        varchar(100) NOT NULL,
        value      jsonb NOT NULL,
        updated_by text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_tenant_settings PRIMARY KEY (tenant_id, key),
        CONSTRAINT ck_tenant_settings_key_format CHECK (key ~ '^[a-z][a-z0-9_.]{0,99}$')
    );

    CREATE TABLE tenant_keys (
        tenant_id   uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
        version     integer NOT NULL,
        kek_id      varchar(100) NOT NULL,
        wrapped_key bytea NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        retired_at  timestamptz,
        CONSTRAINT pk_tenant_keys PRIMARY KEY (tenant_id, version)
    );

    CREATE TABLE tenant_secrets (
        tenant_id     uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
        name          varchar(100) NOT NULL,
        key_version   integer NOT NULL,
        nonce         bytea NOT NULL,
        ciphertext    bytea NOT NULL,
        fingerprint   varchar(16) NOT NULL,
        status        varchar(16) NOT NULL DEFAULT 'unchecked',
        checked_at    timestamptz,
        check_message text,
        updated_by    text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_tenant_secrets PRIMARY KEY (tenant_id, name),
        CONSTRAINT fk_secret_key FOREIGN KEY (tenant_id, key_version) REFERENCES tenant_keys (tenant_id, version),
        CONSTRAINT ck_tenant_secrets_name_format CHECK (name ~ '^[a-z][a-z0-9_.]{0,99}$'),
        CONSTRAINT ck_tenant_secrets_status_valid CHECK (status IN ('unchecked', 'valid', 'invalid'))
    );

    CREATE TABLE audit_log (
        id          bigserial PRIMARY KEY,
        tenant_id   uuid NOT NULL REFERENCES tenants (id),
        occurred_at timestamptz NOT NULL DEFAULT now(),
        actor       text NOT NULL,
        action      varchar(100) NOT NULL,
        target_type varchar(100),
        target_id   text,
        details     jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX ix_audit_log_tenant_time ON audit_log (tenant_id, occurred_at DESC);

    -- Append-only: no one may change or remove an audit entry, not even the owner.
    CREATE FUNCTION newspilot_audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only';
    END $$;
    CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION newspilot_audit_log_immutable();
    CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION newspilot_audit_log_immutable();
    """)

    # Row-level security on every tenant table.
    for table in TENANT_TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = newspilot_current_tenant())
            WITH CHECK (tenant_id = newspilot_current_tenant());
        """)

    # tenants: everyone sees only the current tenant's row. The owner may also list and create tenants, which
    # the SECURITY DEFINER functions below rely on.
    op.execute(f"""
    ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON tenants
        USING (id = newspilot_current_tenant())
        WITH CHECK (id = newspilot_current_tenant());
    CREATE POLICY owner_lists_tenants ON tenants FOR SELECT TO {owner} USING (true);
    CREATE POLICY owner_creates_tenants ON tenants FOR INSERT TO {owner} WITH CHECK (true);

    CREATE FUNCTION newspilot_create_tenant(p_slug text, p_name text) RETURNS uuid
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$ INSERT INTO tenants (slug, name) VALUES (p_slug, p_name) RETURNING id $$;

    CREATE FUNCTION newspilot_tenant_id(p_slug text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$ SELECT id FROM tenants WHERE slug = p_slug $$;

    CREATE FUNCTION newspilot_active_tenant_ids() RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$ SELECT id FROM tenants WHERE status = 'active' ORDER BY created_at $$;
    """)

    # Grants for the application role: only what it needs.
    op.execute(f"""
    REVOKE ALL ON FUNCTION newspilot_create_tenant(text, text), newspilot_tenant_id(text),
        newspilot_active_tenant_ids() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION newspilot_create_tenant(text, text), newspilot_tenant_id(text),
        newspilot_active_tenant_ids(), newspilot_current_tenant() TO {app};

    GRANT SELECT ON tenants TO {app};
    GRANT UPDATE (name, status, publisher_name, publisher_contact, updated_at) ON tenants TO {app};
    GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_settings, tenant_secrets TO {app};
    GRANT SELECT, INSERT ON tenant_keys TO {app};
    GRANT UPDATE (retired_at) ON tenant_keys TO {app};
    GRANT SELECT, INSERT ON audit_log TO {app};
    GRANT USAGE ON SEQUENCE audit_log_id_seq TO {app};
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS audit_log, tenant_secrets, tenant_keys, tenant_settings, tenants CASCADE;
    DROP FUNCTION IF EXISTS newspilot_audit_log_immutable(), newspilot_create_tenant(text, text),
        newspilot_tenant_id(text), newspilot_active_tenant_ids(), newspilot_current_tenant();
    """)
