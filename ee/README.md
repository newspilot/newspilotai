# ee/: hosted-service code

This directory is reserved for code that only the hosted NewsPilot AI service uses, such as billing,
custom-domain provisioning, the multi-workspace Slack app and SSO. It is covered by [`ee/LICENSE`](LICENSE), not
the AGPL.

It is empty for now. Rules for when it is used:

- The open-source engine must work completely without `ee/`. Nothing outside `ee/` may import from it.
- Every quality gate, guardrail and security fix lives outside `ee/`. The paid edition sells convenience
  (hosting, managed keys, multi-site, team features), not safety.
