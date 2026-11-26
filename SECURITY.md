# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately through GitHub instead: on
this repository's **Security** tab, choose **Report a vulnerability**. Only the maintainers can see the report.

Please include what you found, how to reproduce it and what an attacker could do with it. We aim to acknowledge
a report within a few working days, and we will keep you updated until it is fixed.

## Scope

We are particularly interested in reports about:

- data from one tenant becoming visible to, or changeable by, another tenant;
- exposure of stored secrets (API keys, CMS credentials, tokens);
- ways to publish without the required approval, or to get around a quality, licence or budget check;
- authentication and authorisation flaws in the API or the dashboard.

## Supported versions

The project is in pre-alpha, so only the latest commit on `main` is supported. Security fixes will never be
restricted to the paid edition.
