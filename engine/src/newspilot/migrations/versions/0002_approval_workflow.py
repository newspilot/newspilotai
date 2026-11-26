# SPDX-License-Identifier: AGPL-3.0-only
"""Categories, leads, articles, immutable revisions and approvals, with two publishing modes.

Publishing modes. A site has a mode, and each of its categories can override it:
- "human" (the default): a person approves the final text of every article.
- "auto": an article may publish without a person reading the final text, but only if a person approved its lead
  (for example in Slack), every automated check passed, and the article carries the "not reviewed by a person"
  disclosure. Only a person can switch a site or a category to "auto".

Guarantees enforced by the database itself, so no application bug can get around them:
- A revision's content_hash is computed by the database from its text.
- Revisions and approvals are append-only.
- An approval matches its revision's content_hash.
- "approved" decisions come only from people ("user:"/"cli:" actors). "auto_approved" decisions come only from the
  system, must record that every automated check passed, and are accepted only while the article's effective mode
  is "auto" and its lead was approved by a person.
- An article can be marked published only for a revision with a human approval, or with an auto approval while the
  mode is still "auto" and the article carries the "not_human_reviewed" disclosure.
- An article can only point at its own revisions and categories, within its own tenant (composite foreign keys).
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

TABLES = ("categories", "leads", "articles", "article_revisions", "approvals")
HUMAN = "'^(user|cli):.+'"


def upgrade() -> None:
    app = validate_role_name(context.config.attributes["app_role"])

    op.execute(f"""
    -- convert_to() is not marked immutable, but with a fixed target encoding the result is deterministic.
    CREATE FUNCTION newspilot_content_hash(p_title text, p_standfirst text, p_body text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE
        AS $$ SELECT encode(sha256(convert_to(
                 coalesce(p_title, '') || chr(31) || coalesce(p_standfirst, '') || chr(31) || coalesce(p_body, ''),
                 'UTF8')), 'hex') $$;

    -- The site-wide publishing mode. "auto" can only be set by a person.
    ALTER TABLE tenants
        ADD COLUMN publish_mode varchar(8) NOT NULL DEFAULT 'human',
        ADD COLUMN publish_mode_set_by text,
        ADD COLUMN publish_mode_set_at timestamptz,
        ADD CONSTRAINT ck_tenants_publish_mode_valid CHECK (publish_mode IN ('human', 'auto')),
        ADD CONSTRAINT ck_tenants_auto_set_by_person
            CHECK (publish_mode = 'human' OR (publish_mode_set_by ~ {HUMAN} AND publish_mode_set_at IS NOT NULL));

    -- The client's own categories (from their CMS). publish_mode NULL means: follow the site's mode.
    CREATE TABLE categories (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id           uuid NOT NULL REFERENCES tenants (id),
        slug                varchar(100) NOT NULL,
        name                text NOT NULL,
        external_id         text,
        publish_mode        varchar(8),
        publish_mode_set_by text,
        publish_mode_set_at timestamptz,
        active              boolean NOT NULL DEFAULT true,
        created_at          timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_categories_tenant_slug UNIQUE (tenant_id, slug),
        CONSTRAINT uq_categories_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_categories_publish_mode_valid CHECK (publish_mode IS NULL OR publish_mode IN ('human', 'auto')),
        CONSTRAINT ck_categories_auto_set_by_person CHECK (publish_mode IS DISTINCT FROM 'auto'
            OR (publish_mode_set_by ~ {HUMAN} AND publish_mode_set_at IS NOT NULL))
    );

    CREATE TABLE leads (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id   uuid NOT NULL REFERENCES tenants (id),
        category_id uuid,
        source_url  text NOT NULL,
        source_hash varchar(64) NOT NULL,
        title       text NOT NULL,
        summary     text,
        score       double precision,
        status      varchar(16) NOT NULL DEFAULT 'new',
        approved_by text,
        approved_at timestamptz,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_leads_tenant_source UNIQUE (tenant_id, source_hash),
        CONSTRAINT uq_leads_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT fk_leads_category FOREIGN KEY (tenant_id, category_id) REFERENCES categories (tenant_id, id),
        CONSTRAINT ck_leads_status_valid CHECK (status IN
            ('new', 'shortlisted', 'approved', 'drafting', 'drafted', 'rejected', 'expired')),
        -- A lead approval (for example from Slack) is always a person's, and records when.
        CONSTRAINT ck_leads_approved_by_person CHECK (approved_by IS NULL
            OR (approved_by ~ {HUMAN} AND approved_at IS NOT NULL)),
        CONSTRAINT ck_leads_approved_status_has_approver CHECK (status <> 'approved' OR approved_by IS NOT NULL)
    );

    CREATE TABLE articles (
        id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id             uuid NOT NULL REFERENCES tenants (id),
        lead_id               uuid,
        category_id           uuid,
        status                varchar(16) NOT NULL DEFAULT 'draft',
        current_revision_id   uuid,
        approved_revision_id  uuid,
        published_revision_id uuid,
        hold_reason           text,
        -- What readers are told: pending until approval; human_reviewed after a person approves the final text;
        -- not_human_reviewed when it was auto-published.
        disclosure            varchar(24) NOT NULL DEFAULT 'pending',
        created_at            timestamptz NOT NULL DEFAULT now(),
        updated_at            timestamptz NOT NULL DEFAULT now(),
        first_published_at    timestamptz,
        last_published_at     timestamptz,
        CONSTRAINT uq_articles_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT fk_articles_lead FOREIGN KEY (tenant_id, lead_id) REFERENCES leads (tenant_id, id),
        CONSTRAINT fk_articles_category FOREIGN KEY (tenant_id, category_id) REFERENCES categories (tenant_id, id),
        CONSTRAINT ck_articles_status_valid CHECK (status IN
            ('draft', 'held', 'in_review', 'approved', 'rejected', 'published', 'unpublished')),
        CONSTRAINT ck_articles_disclosure_valid
            CHECK (disclosure IN ('pending', 'human_reviewed', 'not_human_reviewed')),
        CONSTRAINT ck_articles_published_has_revision
            CHECK (status <> 'published' OR published_revision_id IS NOT NULL),
        CONSTRAINT ck_articles_published_has_disclosure CHECK (status <> 'published' OR disclosure <> 'pending'),
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
        -- clock_timestamp(), not now(): decisions made in one transaction still have a definite order.
        decided_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT fk_approvals_revision FOREIGN KEY (tenant_id, article_id, revision_id)
            REFERENCES article_revisions (tenant_id, article_id, id),
        CONSTRAINT ck_approvals_decision_valid
            CHECK (decision IN ('approved', 'auto_approved', 'rejected', 'changes_requested')),
        -- A human approval is a person's.
        CONSTRAINT ck_approvals_human_approver CHECK (decision <> 'approved' OR approver ~ {HUMAN}),
        -- An auto approval is the system's, and records that every automated check passed.
        CONSTRAINT ck_approvals_auto_approver CHECK (decision <> 'auto_approved' OR approver ~ '^system:.+'),
        CONSTRAINT ck_approvals_auto_checks_passed CHECK (decision <> 'auto_approved' OR checklist @>
            '{{"quality_gate": true, "information_gain": true, "images_licensed": true,
               "sources_credited": true, "within_caps": true}}'::jsonb)
    );
    CREATE INDEX ix_approvals_revision ON approvals (tenant_id, revision_id);

    -- The mode that applies to an article: its category's own mode, else the site's.
    CREATE FUNCTION newspilot_effective_publish_mode(p_tenant uuid, p_category uuid) RETURNS text
        LANGUAGE sql STABLE
        AS $$ SELECT coalesce(
                 (SELECT c.publish_mode FROM categories c WHERE c.tenant_id = p_tenant AND c.id = p_category),
                 (SELECT t.publish_mode FROM tenants t WHERE t.id = p_tenant),
                 'human') $$;

    -- Whether an article may take the auto path right now: mode "auto" and a person approved its lead.
    CREATE FUNCTION newspilot_auto_path_allowed(p_tenant uuid, p_article uuid) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$ SELECT EXISTS (
                 SELECT 1 FROM articles a JOIN leads l ON l.tenant_id = a.tenant_id AND l.id = a.lead_id
                 WHERE a.tenant_id = p_tenant AND a.id = p_article
                   AND l.approved_by ~ {HUMAN}
                   AND newspilot_effective_publish_mode(a.tenant_id, a.category_id) = 'auto') $$;

    -- An approval must be for the exact text of its revision; an auto approval needs the auto path to be open.
    CREATE FUNCTION newspilot_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM article_revisions r
                       WHERE r.tenant_id = NEW.tenant_id AND r.id = NEW.revision_id
                         AND r.content_hash = NEW.content_hash) THEN
            RAISE EXCEPTION 'approval content_hash does not match revision %', NEW.revision_id;
        END IF;
        IF NEW.decision = 'auto_approved' AND NOT newspilot_auto_path_allowed(NEW.tenant_id, NEW.article_id) THEN
            RAISE EXCEPTION 'auto approval refused: the article is not in auto mode or its lead has no human approval';
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER approvals_guard BEFORE INSERT ON approvals
        FOR EACH ROW EXECUTE FUNCTION newspilot_approval_guard();

    -- Publishing needs, for exactly the revision being published, either a human approval, or an auto approval while
    -- the auto path is still open. The reader-facing label must be true: "human_reviewed" only with a human approval.
    -- The guard also runs when the label of a live article changes, so it cannot be relabelled later.
    CREATE FUNCTION newspilot_publish_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        human_ok boolean;
        auto_ok boolean;
    BEGIN
        IF NEW.status = 'published' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'published'
                OR NEW.published_revision_id IS DISTINCT FROM OLD.published_revision_id
                OR NEW.disclosure IS DISTINCT FROM OLD.disclosure) THEN
            -- The latest decision on this exact text counts: a rejection after an approval revokes it.
            SELECT a.decision = 'approved', a.decision = 'auto_approved'
            INTO human_ok, auto_ok
            FROM approvals a JOIN article_revisions r ON r.tenant_id = a.tenant_id AND r.id = a.revision_id
            WHERE a.tenant_id = NEW.tenant_id AND a.revision_id = NEW.published_revision_id
              AND a.content_hash = r.content_hash
            ORDER BY a.decided_at DESC
            LIMIT 1;
            IF coalesce(human_ok, false) THEN
                RETURN NEW;
            END IF;
            IF coalesce(auto_ok, false) AND NEW.disclosure = 'not_human_reviewed'
               AND newspilot_auto_path_allowed(NEW.tenant_id, NEW.id) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'revision % has no valid approval for publishing with disclosure %',
                NEW.published_revision_id, NEW.disclosure;
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
    GRANT EXECUTE ON FUNCTION newspilot_content_hash(text, text, text), newspilot_effective_publish_mode(uuid, uuid),
        newspilot_auto_path_allowed(uuid, uuid) TO {app};
    GRANT UPDATE (publish_mode, publish_mode_set_by, publish_mode_set_at) ON tenants TO {app};
    GRANT SELECT, INSERT ON categories TO {app};
    GRANT UPDATE (name, external_id, publish_mode, publish_mode_set_by, publish_mode_set_at, active, updated_at)
        ON categories TO {app};
    GRANT SELECT, INSERT, UPDATE ON leads TO {app};
    GRANT SELECT, INSERT ON articles TO {app};
    GRANT UPDATE (status, category_id, current_revision_id, approved_revision_id, published_revision_id, hold_reason,
                  disclosure, updated_at, first_published_at, last_published_at) ON articles TO {app};
    GRANT SELECT, INSERT ON article_revisions, approvals TO {app};
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS approvals, article_revisions, articles, leads, categories CASCADE;
    DROP FUNCTION IF EXISTS newspilot_append_only(), newspilot_publish_guard(), newspilot_approval_guard(),
        newspilot_auto_path_allowed(uuid, uuid), newspilot_effective_publish_mode(uuid, uuid),
        newspilot_content_hash(text, text, text);
    ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_auto_set_by_person,
        DROP CONSTRAINT IF EXISTS ck_tenants_publish_mode_valid,
        DROP COLUMN IF EXISTS publish_mode_set_at, DROP COLUMN IF EXISTS publish_mode_set_by,
        DROP COLUMN IF EXISTS publish_mode;
    """)
