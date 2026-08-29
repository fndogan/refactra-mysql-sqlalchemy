# Refactra: MySQL to SQLAlchemy

[![CI](https://github.com/fndogan/refactra-mysql-sqlalchemy/actions/workflows/ci.yml/badge.svg)](https://github.com/fndogan/refactra-mysql-sqlalchemy/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Package: 0.1.0](https://img.shields.io/badge/package-0.1.0-0A7EA4.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Status: Beta](https://img.shields.io/badge/status-beta-orange.svg)](#project-status)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-Ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![SQLAlchemy 2.0](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg)](https://docs.sqlalchemy.org/en/20/)
[![Codemods: LibCST](https://img.shields.io/badge/codemods-LibCST-4B32C3.svg)](https://libcst.readthedocs.io/en/latest/)
[![Security policy](https://img.shields.io/badge/security-policy-0A7EA4.svg)](SECURITY.md)
[![Contributing](https://img.shields.io/badge/contributing-guide-2EA44F.svg)](CONTRIBUTING.md)

Refactra's Python migration toolkit for analyzing raw MySQL usage, applying
deterministic LibCST codemods, converting selected functions to SQLAlchemy with
Anthropic or OpenAI models, and reviewing the result with static quality checks.

Built with [LibCST](https://libcst.readthedocs.io/en/latest/),
[SQLAlchemy 2.0](https://docs.sqlalchemy.org/en/20/), and optional AI conversion
through the [Anthropic API](https://platform.claude.com/docs/en/api/overview) or
[OpenAI API](https://developers.openai.com/api/reference/overview).

## Project status

Refactra MySQL to SQLAlchemy is currently in beta. Generated changes require
code review, project-specific tests, and database-backed validation before
deployment.

## Project identity

| Item | Value |
| --- | --- |
| Brand and project family | **Refactra** |
| Project | **Refactra: MySQL to SQLAlchemy** |
| Repository | `fndogan/refactra-mysql-sqlalchemy` |
| Python distribution | `refactra-mysql-sqlalchemy` |
| Python package | `refactra_mysql` |
| Source layout | `src/refactra_mysql/` |
| Command-line interface | `refactra-mysql` |
| Owner and maintainer | [Furkan Dogan](https://github.com/fndogan) |

## Key capabilities

- Inventory raw SQL usage and migration complexity across Python source trees.
- Apply deterministic, formatting-preserving LibCST codemods.
- Convert selected functions through Anthropic or OpenAI providers.
- Classify dynamic, transactional, and high-risk SQL before conversion.
- Validate syntax, imports, model references, call signatures, and N+1 risks.
- Generate semantic-equivalence and performance-test scaffolds for human review.

## What it does

The intended workflow is:

1. Analyze Python source for raw SQL patterns.
2. Apply mechanical cleanup to a separate output directory.
3. Convert eligible functions to SQLAlchemy with a supported AI provider.
4. Validate syntax, imports, call signatures, model usage, and potential N+1 queries.

The toolkit supports PyMySQL, mysql-connector, and similar DB-API-style source
patterns. It does not promise automatic semantic equivalence: transaction
behavior, dynamic SQL, application conventions, and database-specific behavior
still need human review.

## Requirements

- Python 3.11 or newer
- Existing SQLAlchemy models
- An Anthropic or OpenAI API key for AI conversion only
- A dedicated branch or copy of the application being migrated

## Installation

Anthropic support is included in the default installation:

```bash
git clone https://github.com/fndogan/refactra-mysql-sqlalchemy.git
cd refactra-mysql-sqlalchemy
python -m pip install -e .
```

For OpenAI support:

```bash
python -m pip install -e '.[openai]'
```

For development and tests:

```bash
python -m pip install -e '.[dev]'
```

## Configuration

Copy the example configuration and edit it locally:

```bash
cp .env.example .env
```

`.env` is ignored by Git. Never commit API keys.

| Variable | Purpose | Default |
| --- | --- | --- |
| `SOURCE_DIR` | Comma-separated source directories | required by analysis/codemods |
| `MODELS_FILE` | SQLAlchemy models file | required by conversion |
| `OUTPUT_DIR` | Converted output directory | `./output` |
| `REPORTS_DIR` | Generated report directory | `./reports` |
| `AI_PROVIDER` | `anthropic` or `openai` | `anthropic` |
| `AI_API_KEY` | Provider API key | required by AI commands |
| `AI_MODEL` | Current model ID from provider documentation | required by AI commands |
| `SYSTEM_PROMPT_FILE` | Local first-pass prompt override | packaged example |
| `DYNAMIC_PROMPT_FILE` | Local reviewed-pass prompt override | packaged example |
| `AI_PROMPT_CACHING` | Anthropic prompt caching | `true` |
| `RATE_LIMIT_RPM` | Request limit | `5` |
| `RATE_LIMIT_INPUT_TPM` | Input-token limit | `10000` |
| `RATE_LIMIT_OUTPUT_TPM` | Output-token limit | `4000` |
| `LOG_LEVEL` | Logging level | `INFO` |

Model identifiers are intentionally not hardcoded because provider model catalogs
change. Choose a currently supported ID from the provider's official documentation.

The packaged prompts are intentionally generic examples. Keep private or
application-specific prompt files outside version control and point
`SYSTEM_PROMPT_FILE` or `DYNAMIC_PROMPT_FILE` to their local paths. The
`.private-prompts/` directory is ignored for local development.

## Safe quick start

Use a separate output directory throughout the first pass:

```bash
# 1. Inventory raw SQL without modifying source
refactra-mysql analyze \
  --source-dir ./legacy_app \
  --output ./reports/analysis.json

# 2. Preview deterministic codemods
refactra-mysql codemods \
  --source-dir ./legacy_app \
  --output-dir ./output \
  --dry-run

# 3. Apply codemods to the output tree
refactra-mysql codemods \
  --source-dir ./legacy_app \
  --output-dir ./output

# 4. Preview AI conversion; API calls are still made in dry-run mode
refactra-mysql convert \
  --source-dir ./output \
  --models-file ./app/models.py \
  --output-dir ./converted \
  --dry-run

# 5. Apply AI conversion to another output tree
refactra-mysql convert \
  --source-dir ./output \
  --models-file ./app/models.py \
  --output-dir ./converted

# 6. Validate the result
refactra-mysql syntax --source-dir ./converted
refactra-mysql validate \
  --source-dir ./converted \
  --models-file ./app/models.py
refactra-mysql n1 ./converted --models ./app/models.py
```

`codemods` and `convert` refuse implicit in-place writes. Use `--in-place` only
when that is the reviewed target. `post-process` and the dynamic second pass are
preview-only unless `--apply` is supplied.

## AI data and privacy

AI conversion sends selected function source and extracted SQLAlchemy model
definitions to the configured Anthropic or OpenAI API. There is no automatic
secret or confidential-data redaction. Before running conversion:

- inspect source and model definitions for credentials or sensitive values;
- confirm the provider's current data-use and retention terms;
- use only code you are authorized to transmit;
- treat generated reports as sensitive because they can contain source, diffs,
  and filesystem paths supplied to the CLI.

`AI_API_KEY` is read from the environment or an ignored `.env` file. The CLI does
not accept keys as command arguments, avoiding exposure in process listings and
shell history. See [SECURITY.md](SECURITY.md) for the complete policy.

## Commands

| Command | Description |
| --- | --- |
| `analyze` | Inventory raw SQL patterns |
| `codemods` | Apply deterministic LibCST transformations |
| `convert` | Convert eligible functions with Anthropic or OpenAI |
| `convert-dynamic` | Preview or apply a second pass for selected skipped functions |
| `post-process` | Preview or apply import/dead-code cleanup |
| `validate` | Check syntax, imports, and model references |
| `syntax` | Compile all Python files in a directory |
| `n1` | Detect query-in-loop and known relationship access patterns |
| `consistency` | Check function-call signatures |
| `fix-consistency` | Preview or repair selected stale call signatures |
| `quality` | Produce code-quality metrics |
| `coverage` | Compare original and converted function coverage |
| `models` | Validate SQLAlchemy model definitions |
| `compare` | Produce before/after comparisons |
| `semantic` | Generate semantic-equivalence test scaffolds |
| `benchmark` | Generate database performance test scaffolds |

Run `refactra-mysql <command> --help` for command-specific options. Direct
module execution through `python -m refactra_mysql` is also supported.

## N+1 detection

Direct session queries inside loops are detected without extra configuration.
Relationship access is reported only when backed by model introspection or
explicit hints, avoiding false positives from ordinary plural Python attributes.

```bash
refactra-mysql n1 ./converted --models ./app/models.py
refactra-mysql n1 ./converted --hints orders memberships
refactra-mysql n1 ./converted --models ./app/models.py --ci
```

## Semantic-equivalence scaffolds

The generator creates compilable pytest scaffolds for matching top-level
functions. Your project must provide `db_session` and `raw_connection` fixtures.
Review every generated argument and run against isolated test data:

```bash
refactra-mysql semantic \
  --original-dir ./legacy_app \
  --converted-dir ./converted \
  --output-file ./tests/test_semantic_equivalence.py
```

These are scaffolds, not proof of equivalence. Add assertions for ordering,
transactions, exceptions, side effects, and database-specific behavior.

## Performance benchmark scaffolds

The benchmark generator produces project-neutral pytest scaffolds without
hardcoded application imports:

```bash
refactra-mysql benchmark \
  --converted-dir ./converted \
  --output-file ./tests/test_perf_benchmark.py
```

Provide `db_session`, `raw_connection`, and `migration_pairs` fixtures in the
target project's `conftest.py`. The mapping keys use
`module.path:function_name`; each value is an
`(original_callable, converted_callable)` pair. Run benchmarks only against
isolated staging data.

## Development

```bash
python -m compileall -q src/refactra_mysql tests
python -m ruff check src/refactra_mysql tests
python -m mypy src/refactra_mysql tests
python -m pytest -q
python -m build
```

CI runs the compile, test, and package-build checks on Python 3.11 and 3.12.

## Contributing

Issues and pull requests are welcome. Review [CONTRIBUTING.md](CONTRIBUTING.md)
for the development workflow and change requirements. Report security problems
privately as described in [SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE). Copyright remains with the named
copyright holder; the MIT grant allows use, copying, modification, distribution,
sublicensing, and sale subject to the license notice and terms.

Created and maintained by [Furkan Dogan](https://github.com/fndogan).
