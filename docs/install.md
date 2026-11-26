# Installing NewsPilot AI

NewsPilot AI is **pre-alpha**. Today the engine provides the storage layer (tenancy, encrypted secrets, the
approval workflow, publishing settings) and an administration command line. The pipeline that finds and drafts
stories, the publish adapters and the dashboard are being built now; see the [roadmap](roadmap.md).

## Ways to run it

| Way | You run | Best for | Status |
|---|---|---|---|
| [From source](#from-source) | `git clone` and `pip install -e engine` | contributors and early testers | **Available now** |
| Python package | `pipx install newspilot`, `uv tool install newspilot` or `pip install newspilot` | Python users, servers without Docker | Planned: first release |
| Docker image | `docker pull ghcr.io/newspilot/newspilotai:<version>` | most self-hosters | Planned: first release |
| Docker Compose | `docker compose up -d` (engine and PostgreSQL together) | a single-server install | [Development database](#a-development-database-with-docker) **available now**; full stack planned: first release |
| One-click templates | a "Deploy" button for Coolify, then Railway, Render and DigitalOcean | publishers who do not want a terminal | Planned |
| Dev container / Codespaces | "Open in GitHub Codespaces" | trying it, or contributing, without installing anything | Planned |
| WordPress connector plugin | install from the wordpress.org plugin directory | WordPress sites | Planned, with the WordPress publish adapter |
| Content API SDKs | `pip install` (Python) or `npm install` (TypeScript) | developers with their own frontend | Planned, with the content API |
| Helm chart | `helm install` | Kubernetes operators | Later, if there is demand |

Every release will be versioned, with a changelog. Docker images will be built by GitHub Actions from the tagged
source, with signed build provenance and a software bill of materials, so you can check that an image matches the
code. The commands in the table use names that are planned and not yet published. Until a release exists, install
from source.

If you would rather not host it yourself, a hosted service is planned. It runs the same engine.

## From source

Requirements: Git, Python 3.12 or newer, and PostgreSQL 16 or newer.

```bash
git clone https://github.com/newspilot/newspilotai.git
cd newspilotai/engine
python -m venv .venv && . .venv/bin/activate
pip install -e .
newspilot --help
```

Then set up the database (two roles: one that owns the tables, one the engine connects as). See
[database.md](database.md) for the commands.

```bash
export NEWSPILOT_MIGRATION_DATABASE_URL=postgresql://newspilot_owner:<owner password>@localhost:5432/newspilot
export NEWSPILOT_DATABASE_URL=postgresql://newspilot_app:<app password>@localhost:5432/newspilot
export NEWSPILOT_MASTER_KEY=$(newspilot secret new-master-key)   # store this safely; it decrypts your keys

newspilot db upgrade
newspilot tenant create my-site "My Site"
newspilot secret set my-site llm.api_key        # prompts for the value; it is never echoed
newspilot setting list my-site                  # publishing settings, with help text
```

Keep the master key in your host's secret manager or a git-ignored env file, never in the repository. Back it up
separately from the database: without it, the stored keys cannot be decrypted.

## A development database with Docker

`deploy/dev/docker-compose.yml` starts PostgreSQL with both roles already created:

```bash
cp deploy/dev/.env.example deploy/dev/.env        # then put your own passwords in .env
docker compose -f deploy/dev/docker-compose.yml --env-file deploy/dev/.env up -d
```

It listens on `127.0.0.1:5432` only. It is for development, not production.

## Updating

From source: `git pull`, then `pip install -e .` and `newspilot db upgrade`. Migrations run in place and keep
your data. Read the release notes before upgrading across a version that lists breaking changes.

## Getting help

Open an issue on GitHub for bugs and questions. Report security problems privately, as described in
[SECURITY.md](../SECURITY.md).
