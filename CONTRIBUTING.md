# Contributing to Refactra

Thank you for improving Refactra: MySQL to SQLAlchemy. Keep every change
focused, reviewable, and safe for source-migration workflows.

## Development setup

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check src/refactra_mysql tests
python -m compileall -q src/refactra_mysql tests
python -m build
```

## Change requirements

- Add or update regression tests for behavioral changes.
- Preserve the default no-implicit-in-place-write safeguards.
- Keep provider model identifiers configurable rather than hardcoded.
- Document changes to CLI flags, environment variables, or generated output.
- Keep commits focused and use clear, professional commit messages.
- Do not include credentials, proprietary source, production data, generated
  reports, or application-specific absolute paths.

## Pull requests

Describe the problem, the chosen approach, the validation performed, and any
remaining limitations. Generated code and AI-assisted changes require the same
review and test standard as manually written code.

## Security reports

Do not disclose suspected vulnerabilities in a public issue. Follow the private
reporting process in [SECURITY.md](SECURITY.md).
