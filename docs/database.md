# Database setup

NewsPilot AI needs PostgreSQL 16 or newer and **two roles**:

| Role | Used by | Can do |
|---|---|---|
| `newspilot_owner` | `newspilot db upgrade` only | Owns the tables and runs migrations |
| `newspilot_app` | the running engine | Only what the migrations grant it. Row-level security always applies. |

The engine refuses to migrate if both are the same role, because a table owner would otherwise sidestep the
isolation rules. Never give `newspilot_app` `SUPERUSER` or `BYPASSRLS`.

## Creating the roles

Run this once as a database administrator. Replace the passwords with your own, and keep them out of version
control.

```sql
CREATE ROLE newspilot_owner LOGIN PASSWORD '<owner password>';
CREATE ROLE newspilot_app LOGIN PASSWORD '<app password>' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE DATABASE newspilot OWNER newspilot_owner;
REVOKE ALL ON DATABASE newspilot FROM PUBLIC;
GRANT CONNECT ON DATABASE newspilot TO newspilot_app;
\c newspilot
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO newspilot_owner;
```

To use another name for the application role, set `NEWSPILOT_APP_ROLE` when you run migrations.

For local development, `deploy/dev/docker-compose.yml` does all of this for you.

## Environment

| Variable | Value |
|---|---|
| `NEWSPILOT_MIGRATION_DATABASE_URL` | the owner role's URL (used only by `newspilot db upgrade`) |
| `NEWSPILOT_DATABASE_URL` | the application role's URL (used by everything else) |
| `NEWSPILOT_MASTER_KEY` | from `newspilot secret new-master-key`. Store it in your host's secret manager. |
| `NEWSPILOT_MASTER_KEY_ID` | optional label for that key (default `local-1`) |

**Keep the master key safe and backed up separately from the database.** Without it, the stored secrets (API
keys, CMS passwords) cannot be decrypted, and the backup alone cannot decrypt them either. That is the point of
keeping them apart.

## First run

```bash
newspilot db upgrade
newspilot tenant create my-site "My Site"
newspilot secret set my-site llm.api_key        # prompts for the value; it is never echoed or logged
newspilot secret list my-site
```
