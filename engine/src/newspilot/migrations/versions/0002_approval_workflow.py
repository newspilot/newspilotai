# SPDX-License-Identifier: AGPL-3.0-only
"""Leads, articles, immutable revisions and immutable human approvals.

Guarantees enforced by the database itself, so no application bug can get around them:
- A revision's content_hash is computed by the database from its text.
- Revisions and approvals are append-only.
- An approval must match its revision's content_hash, and only a person may approve: approver must be a "user:" or
  "cli:" actor, never "system:".
- An article can be marked published only for a revision that has a matching human approval.
- An article can only point at its own revisions, and only within its own tenant (composite foreign keys).
- Every table has forced row-level security, as in 0001.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import context, op

from newspilot.config import validate_role_name

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("leads", "articles", "article_revisions", "approvals")


def upgrade() -> None:
    app = validate_role_name(context.config.attributes["app_role"])

    op.execute("""
    -- convert_to() is not marked immutable, but with a fixed target encoding the result is deterministic.
    CREATE FUNCTION newspilot_content_hash(p_title text, p_standfirst text, p_body text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE
        AS $$ SELECT encode(sha256(convert_to(
                 coalesce(p_title, '') || chr(31) || coalesce(p_standfirst, '') || chr(31) || coalesce(p_body, ''),
                 'UTF8')), 'hex') $$;

    CREATE TABLE leads (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id   uuid NOT NULL REFERENCES tenants (id),
        source_url  text NOT NULL,
        source_hash varchar(64) NOT NULL,
        title       text NOT NULL,
        summary     text,
        score       double precision,
        status      varchar(16) NOT NULL DEFAULT 'new',
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_leads_tenant_source UNIQUE (tenant_id, source_hash),
        CONSTRAINT uq_leads_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_leads_status_valid
            CHECK (status IN ('new', 'shortlisted', 'drafting', 'drafted', 'rejected', 'expired'))
    );

    CREATE TABLE articles (
        id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id             uuid NOT NULL REFERENCES tenants (id),
        lead_id               uuid,
        status                varchar(16) NOT NULL DEFAULT 'draft',
        current_revision_id   uuid,
        approved_revision_id  uuid,
        published_revision_id uuid,
        hold_reason           text,
        created_at            timestamptz NOT NULL DEFAULT now(),
        updated_at            timestamptz NOT NULL DEFAULT now(),
        first_published_at    timestamptz,
        last_published_at     timestamptz,
        CONSTRAINT uq_articles_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT fk_articles_lead FOREIGN KEY (tenant_id, lead_id) REFERENCES leads (tenant_id, id),
        CONSTRAINT ck_articles_status_valid CHECK (status IN
            ('draft', 'held', 'in_review', 'approved', 'rejected', 'published', 'unpublished')),
        CONSTRAINT ck_articles_published_has_revision
            CHECK (status <> 'published' OR published_revision_id IS NOT NULL),
        CONSTRAINT ck_articles_approved_is_current
            CHECK (status <> 'approved' OR approved_revision_id = current_revision_id),
        CONSTRAINT ck_articles_held_has_reason CHECK (status <> 'held' OR hold_reason IS NOT NULL)
    );
    CREATE INDEX ix_articles_tenant_status ON articles (tenant_id, status);

    CREATE TABLE article_revisions (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id    uuid NOT NULL REFERENCES tenants (id),
        article_id   uuid NOT NULL,
        number       integer NOT NULL,
        title        text NOT NULL,
        standfirst   text,
        body_html    text NOT NULL,
        origin       varchar(16) NOT NULL,
        created_by   text NOT NULL,
        content_hash varchar(64) GENERATED ALWAYS AS (newspilot_content_hash(title, standfirst, body_html)) STORED,
        created_at   timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_revisions_article_number UNIQUE (tenant_id, article_id, number),
        CONSTRAINT uq_revisions_article_id UNIQUE (tenant_id, article_id, id),
        CONSTRAINT fk_revisions_article FOREIGN KEY (tenant_id, article_id) REFERENCES articles (tenant_id, id),
        CONSTRAINT ck_revisions_origin_valid CHECK (origin IN
            ('draft', 'humanize', 'quality_fix', 'regenerate', 'human_edit', 'import'))
    );

    -- An article may only point at its own revisions (and so only within its own tenant).
    ALTER TABLE articles
        ADD CONSTRAINT fk_articles_current_revision FOREIGN KEY (tenant_id, id, current_revision_id)
            REFERENCES article_revisions (tenant_id, article_id, id),
        ADD CONSTRAINT fk_articles_approved_revision FOREIGN KEY (tenant_id, id, approved_revision_id)
            REFERENCES article_revisions (tenant_id, article_id, id),
        ADD CONSTRAINT fk_articles_published_revision FOREIGN KEY (tenant_id, id, published_revision_id)
            REFERENCES article_revisions (tenant_id, article_id, id);

    CREATE TABLE approvals (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id    uuid NOT NULL REFERENCES tenants (id),
        article_id   uuid NOT NULL,
        revision_id  uuid NOT NULL,
        content_hash varchar(64) NOT NULL,
        decision     varchar(20) NOT NULL,
        approver     text NOT NULL,
        checklist    jsonb NOT NULL,
        note         text,
        decided_at   timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_approvals_revision FOREIGN KEY (tenant_id, article_id, revision_id)
            REFERENCES article_revisions (tenant_id, article_id, id),
        CONSTRAINT ck_approvals_decision_valid CHECK (decision IN ('approved', 'rejected', 'changes_requested')),
        -- Only a person can approve. Machines can draft, check and hold; they can never approve.
        CONSTRAINT ck_approvals_human_approver CHECK (decision <> 'approved' OR approver ~ '^(user|cli):.+')
    );
    CREATE INDEX ix_approvals_revision ON approvals (tenant_id, revision_id);

    -- An approval must be for the exact text of its revision.
    CREATE FUNCTION newspilot_approval_matches_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM article_revisions r
                       WHERE r.tenant_id = NEW.tenant_id AND r.id = NEW.revision_id
                         AND r.content_hash = NEW.content_hash) THEN
            RAISE EXCEPTION 'approval content_hash does not match revision %', NEW.revision_id;
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER approvals_match_revision BEFORE INSERT ON approvals
        FOR EACH ROW EXECUTE FUNCTION newspilot_approval_matches_revision();

    -- Publishing needs a human approval of exactly the revision being published.
    CREATE FUNCTION newspilot_publish_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.status = 'published' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'published'
                OR NEW.published_revision_id IS DISTINCT FROM OLD.published_revision_id) THEN
            IF NOT EXISTS (SELECT 1 FROM approvals a JOIN article_revisions r
                               ON r.tenant_id = a.tenant_id AND r.id = a.revision_id
                           WHERE a.tenant_id = NEW.tenant_id AND a.revision_id = NEW.published_revision_id
                             AND a.decision = 'approved' AND a.content_hash = r.content_hash) THEN
                RAISE EXCEPTION 'revision % has no human approval and cannot be published',
                    NEW.published_revision_id;
            END IF;
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER articles_publish_guard BEFORE INSERT OR UPDATE ON articles
        FOR EACH ROW EXECUTE FUNCTION newspilot_publish_guard();

    -- Revisions and approvals are permanent records.
    CREATE FUNCTION newspilot_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
    END $$;
    CREATE TRIGGER article_revisions_append_only BEFORE UPDATE OR DELETE ON article_revisions
        FOR EACH ROW EXECUTE FUNCTION newspilot_append_only();
    CREATE TRIGGER article_revisions_no_truncate BEFORE TRUNCATE ON article_revisions
        FOR EACH STATEMENT EXECUTE FUNCTION newspilot_append_only();
    CREATE TRIGGER approvals_append_only BEFORE UPDATE OR DELETE ON approvals
        FOR EACH ROW EXECUTE FUNCTION newspilot_append_only();
    CREATE TRIGGER approvals_no_truncate BEFORE TRUNCATE ON approvals
        FOR EACH STATEMENT EXECUTE FUNCTION newspilot_append_only();
    """)

    for table in TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = newspilot_current_tenant())
            WITH CHECK (tenant_id = newspilot_current_tenant());
        """)

    op.execute(f"""
    GRANT EXECUTE ON FUNCTION newspilot_content_hash(text, text, text) TO {app};
    GRANT SELECT, INSERT, UPDATE ON leads TO {app};
    GRANT SELECT, INSERT ON articles TO {app};
    GRANT UPDATE (status, current_revision_id, approved_revision_id, published_revision_id, hold_reason,
                  updated_at, first_published_at, last_published_at) ON articles TO {app};
    GRANT SELECT, INSERT ON article_revisions, approvals TO {app};
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS approvals, article_revisions, articles, leads CASCADE;
    DROP FUNCTION IF EXISTS newspilot_append_only(), newspilot_publish_guard(),
        newspilot_approval_matches_revision(), newspilot_content_hash(text, text, text);
    """)
