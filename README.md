# NewsPilot AI

**Human-approved, source-cited news automation.**

NewsPilot AI watches the sources you choose, researches each story across several of them, drafts it in
your house style and checks it against those sources. **Nothing is published until an editor approves the
final text.** It works for any publisher and any subject. The behaviour for a particular subject comes from
configuration, not from code.

> **Status: pre-alpha.** The engine is being built in the open. It is not ready for production use yet. See
> [`docs/roadmap.md`](docs/roadmap.md).

## Principles

These are defaults the code enforces, not options you have to remember to turn on:

- **Human approval by default.** A person approves the final text, and the approver, the time and a checklist
  are recorded. Any regeneration resets the approval.
- **Auto-publish only by choice, and labelled.** A site can opt in, for the whole site or for chosen categories.
  A person still approves each story idea, every automated check must pass, and readers are told that a person
  did not review the final text.
- **Fail closed.** If a quality check, a budget meter or a licence check cannot give an answer, the article
  waits. It is never published "unchecked".
- **Sources, not scraping.** Every claim is traced to a source. The engine respects robots.txt and opt-outs,
  limits how much of any one source it quotes, and treats paywalled sources as links only.
- **Licensed images only.** Images come only from sources with a machine-checked licence (or that you upload).
  Each one carries stored provenance. Crediting an image is not the same as licensing it.
- **Real bylines.** Articles name a real person who takes editorial responsibility. There are no synthetic
  personas.
- **No "publish at scale".** Daily caps, per-source caps and an information-gain check keep output to stories
  that add something.

## Install

NewsPilot AI is pre-alpha, so for now you run it **from source**:

```bash
git clone https://github.com/newspilot/newspilotai.git
cd newspilotai/engine && python -m venv .venv && . .venv/bin/activate && pip install -e .
newspilot --help
```

Then follow [`docs/install.md`](docs/install.md) to set up the database and your first site. A Python package
(`pipx install newspilot`), a Docker image, a Docker Compose bundle and one-click deploy templates are planned
for the first release. The full list, with the status of each, is in [`docs/install.md`](docs/install.md).

## How it is organised

| Path | What |
|---|---|
| `engine/` | The Python engine: tenancy, storage, pipeline, quality gates, publish adapters |
| `docs/` | Architecture, roadmap and design notes |
| `ee/` | Code for the hosted service only, under a separate commercial licence (empty for now) |

Everything a single site needs, including every quality gate, is open source. Subject-specific behaviour
(sections, prompts, house style, entity lists, data sources) lives in **site profiles** and **plugins**, which
are kept outside the core.

## Development

Requirements: Python 3.12+ and PostgreSQL 16+.

```bash
cd engine
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

The database tests need a PostgreSQL superuser URL in `NEWSPILOT_TEST_ADMIN_URL`. Each test run creates and
drops its own database and roles. Without the variable, those tests are skipped.

To run the engine locally, see [`docs/database.md`](docs/database.md). It covers the two database roles, the
master key, and a Docker Compose file for a development database.

## Licence

NewsPilot AI is licensed under the [GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`), except
for the `ee/` directory, which has its [own licence](ee/LICENSE). Under the AGPL, if you run a modified version
as a network service, you must offer its source code to that service's users. See [`NOTICE`](NOTICE).

Contributions are welcome under the [Contributor Licence Agreement](CLA.md); see
[`CONTRIBUTING.md`](CONTRIBUTING.md). To report a security problem, see [`SECURITY.md`](SECURITY.md).
