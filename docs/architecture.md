# Architecture

NewsPilot AI is one engine that runs a single site (self-hosted) or many sites (hosted service) from the same
code. The self-hosted edition is simply the multi-tenant engine with one tenant. That way the two editions
cannot drift apart.

## Building blocks

| Part | Choice | Why |
|---|---|---|
| Storage | One PostgreSQL database. Every tenant-owned row has a `tenant_id`, with row-level security as a backstop. | Tenancy built in from day one avoids the most expensive rewrite later. |
| Schema | Owned by the engine, migrated with Alembic | The engine, not a CMS, is the system of record. A CMS is a place to publish to. |
| Settings | `tenant_settings` (JSONB) in Postgres | Durable and per tenant |
| Secrets | `tenant_secrets`, envelope-encrypted: a per-tenant data key wrapped by a master key | A database dump alone never exposes customer keys. |
| Jobs | A Postgres-backed queue (Procrastinate first). Each article is a workflow keyed by `(tenant_id, source_item_hash)`. | Idempotent and retriable, with no extra infrastructure for self-hosters |
| Cache | Redis, optional, for caches only | Nothing in Redis is a source of truth. |
| LLM and search | OpenRouter, plus search providers. A per-tenant budget is reserved before each call and **fails closed**. | Cost caps that cannot be bypassed by a missing meter |
| Publishing | Adapters behind one interface: `create_or_update`, `delete`, `health_check` | WordPress, Ghost, Webflow, headless CMSs, webhooks and the content API |
| Approval | A state machine in the engine. The dashboard is the main review surface; Slack and email notify. | Approval cannot depend on one chat tool. |

## Tenancy and row-level security

- Tables are owned by a migration role. The application connects as a separate **non-owner** role, and every
  tenant table has `ENABLE` and `FORCE ROW LEVEL SECURITY`, so even the owner is filtered.
- The current tenant is set per **transaction** with `set_config('newspilot.tenant_id', …, true)`. A pooled
  connection cannot carry one tenant's setting into another tenant's request.
- Policies read the setting with `NULLIF(current_setting('newspilot.tenant_id', true), '')`. With no tenant set,
  queries see no rows; they never see every row.
- Application code goes through `TenantContext`. Tests seed two tenants and assert that neither can read or
  write the other's rows, and that every table with a `tenant_id` column has a forced policy.

## Pipeline

```
scout → score → research → draft → humanize → quality gate → information-gain hold → review → publish
```

Every step records what it did, what it cost and why. Any step that cannot decide holds the article for an
editor; it never publishes it.

- **Research:** at least two independent sources, or one source plus first-party data.
- **Quality gate:** a fact check against the source pack by a different model family from the writer, plus
  deterministic checks (length, banned phrases, attribution).
- **Originality:** an information-gain hold, similarity limits against each source, a cap on quote length, and
  caps per source outlet per day.
- **Images:** licensed sources only, with provenance stored (source, licence, licence URL, author, page, time of
  check). AI illustrations are labelled in their metadata and never show real people or events
  photorealistically.
- **Review:** an editor approves the final text with a checklist. Any regeneration resets the approval. The
  approver is stored with the article.

## Subject-neutral core, configurable sites

The core never contains anything specific to one subject, site or market. All of that is configuration:

- **Site profile (data):** sections and their rules, story formats and lengths, house style, prompts, entity
  lists and feeds. It is stored per tenant and can be exported and imported as a file.
- **Plugins (code, optional):** structured data sources and story templates for a particular subject. Plugins
  live outside the core repository and talk to the engine through a small, versioned interface.

CI runs `scripts/check_neutral.py` to catch subject-specific terms in the core.

## Layout

```
engine/          Python package `newspilot`: domain, pipeline, gates, adapters, storage
  src/newspilot/
  tests/
docs/            design notes (database.md: roles, environment, first run)
deploy/dev/      Docker Compose for a local development database
scripts/         repository checks
ee/              hosted-service code under a commercial licence (never imported by the core)
```

The API, worker and dashboard apps will join as `apps/` when they are built.
