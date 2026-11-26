# Roadmap

Each phase ends with an evidence gate: the next phase starts only once the previous one has shown it works.

## Phase 0: prove the engine (now)

- Repository foundations: licence, CI, secret scanning, the subject-neutrality check
- Tenancy core: schema, row-level security, `TenantContext`, encrypted secrets, append-only audit log
- Pipeline tables and the approval state machine, with "human" (default) and opt-in "auto" publishing modes
- Budget ledger (fails closed), model routing and an allowlist of tested models
- Pipeline steps: scout, score, research, draft, quality gate, information-gain hold
- Licensed-image pipeline with stored provenance
- A first publish adapter, and a thin review dashboard with Slack and email notifications
- An evaluation suite: frozen source items with deterministic checks and an LLM judge, run on every prompt or
  model change

**Gate:** real publications running on the engine. The test is 100+ approved articles, a measured cost per
article, and healthy search indexing with no manual actions.

## Phase 1: first public release

Docker Compose and one-click templates, documentation, published evaluation results, and the CLA process
opened to outside contributors.

## Phase 2 onwards

More publish adapters (WordPress, Ghost, Webflow), bring-your-own keys checked on save, newsletter and social
repackaging, hosted themes and custom domains, and plugins for structured story templates.

## Never

"Unlimited articles"; auto-publish by default (it is opt-in, per site or category, and labelled); synthetic or fake bylines; photorealistic AI images of real
people or events; fetching content past paywalls; the same story pushed across several sites owned by one
client.
