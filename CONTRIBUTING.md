# Contributing

Thank you for your interest in NewsPilot AI. The project is in pre-alpha. Outside contributions open once the
[Contributor Licence Agreement](CLA.md) has been reviewed and the signing tool is set up. Until then, issues
and discussion are welcome.

## Ground rules

1. **The guardrails stay on.** Changes must not weaken human approval, fail-closed checks, source and quote
   limits, image licensing or caps. If a change touches one of them, explain why in the pull request.
2. **The core is subject-neutral.** Nothing in `engine/` may be written for one subject, site or market (for
   example sport, technology, finance or one country). Put subject-specific behaviour in a site profile or a
   plugin. CI runs `scripts/check_neutral.py` to catch obvious cases.
3. **No secrets, ever.** Never commit keys, tokens, passwords or `.env` files, not even in tests. CI scans every
   commit with gitleaks, and you can run the same scan locally with `pre-commit`.
4. **Every table that holds tenant data has a `tenant_id` and row-level security.** The tenancy tests fail if a
   table does not.

## Workflow

```bash
pip install pre-commit && pre-commit install     # runs gitleaks and ruff before each commit
cd engine && pip install -e ".[dev]"
ruff check . && pytest
```

Open a pull request against `main` and fill in the template. Keep each pull request to one change.

## Licence

By contributing, you agree that your contributions are made under the [CLA](CLA.md) and distributed under the
project's licence (see [`NOTICE`](NOTICE)).
