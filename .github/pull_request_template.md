## What and why

<!-- One change per pull request. What does it do, and why is it needed? -->

## Checklist

- [ ] Tests added or updated, and `ruff check` and `pytest` pass
- [ ] No secrets, keys, tokens or `.env` files (CI scans every commit)
- [ ] Guardrails unchanged: human approval, fail-closed checks, source and quote limits, image licensing and
      caps all still apply (or the change is explained above)
- [ ] The core stays subject-neutral (`scripts/check_neutral.py`)
- [ ] Any new table holding tenant data has `tenant_id` and row-level security (the tenancy tests cover this)
