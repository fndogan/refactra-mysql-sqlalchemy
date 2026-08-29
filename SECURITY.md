# Security policy

## Supported versions

Security fixes are made on the default branch and the latest `0.1.x` release.
Older snapshots and generated migration output are not maintained as separate
supported products.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting feature when it is available. Otherwise, email
`dev@fndogan.com` with the subject `Refactra security report` before publishing
details.

Include:

- the affected version or commit;
- the affected command and configuration;
- minimal reproduction steps;
- the security impact and expected behavior;
- any suggested mitigation.

Do not include real API keys, production data, customer information, database
dumps, or confidential source code. Use synthetic examples and redact logs.
Allow time for investigation and coordinated remediation before disclosure.

## Security boundaries

Refactra analyzes and rewrites source code; it does not establish semantic
equivalence or make generated migrations safe for production automatically.
Treat all generated code, reports, diffs, and benchmark scaffolds as untrusted
until they have passed human review and project-specific tests.

## AI provider data handling

The AI conversion commands send selected function source and extracted model
definitions to the configured Anthropic or OpenAI API. The project does not
redact secrets or confidential values before transmission. Review the selected
provider's current data-use and retention terms, and remove sensitive material
before running AI conversion.

`--dry-run` prevents local output writes, but AI conversion still makes provider
requests and transmits the selected source context.

API keys are accepted through `AI_API_KEY` in the environment or an ignored
`.env` file. They are not accepted as command-line arguments, because command
arguments can appear in shell history and process listings.

Generated JSON/TXT reports may contain source snippets, diffs, and filesystem
paths supplied to the CLI. Treat reports as potentially sensitive and review
them before sharing or committing them.

## Safe operation

- Work on a dedicated branch or a copy of the source tree.
- Inspect source and model context before enabling AI conversion.
- Prefer `--dry-run` or preview mode before applying local changes.
- Use `--output-dir` for first-pass conversion; use `--in-place` or `--apply`
  only after reviewing the target.
- Run syntax checks, project tests, and database-backed equivalence checks.
- Run generated database tests only against isolated test data, never production.
- Review generated reports before sharing or committing them.
- Revoke and rotate any credential that is exposed in source, logs, or reports.
