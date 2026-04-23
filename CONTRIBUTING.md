# Contributing to GPC

Thank you for considering a contribution to Global Project Context. This guide
explains how to set up a development environment, the conventions the project
follows, and how to propose a change.

GPC is early-stage and local-first. Bug reports, documentation fixes, smoke
tests and small features are all welcome.

## Code of Conduct

By participating in this project you agree to act respectfully and in good
faith. Harassment, discrimination, or hostile behaviour will not be tolerated.
Report concerns privately to the maintainer (see [SECURITY.md](SECURITY.md) for
the contact channel).

## Ways to Contribute

- **Report bugs** through GitHub Issues. Include the GPC version (commit hash),
  Python version, OS, the command you ran, and the relevant section of
  `.gpc/index.log` or the MCP server logs.
- **Improve documentation** in `docs/` or `README.md`. Documentation PRs do not
  require sign-off from a second reviewer.
- **Add tests** to `tests/smoke/`. Coverage of the indexer, retrieval path and
  MCP tool contracts is the highest-value area.
- **Fix open issues** labelled `good first issue` or `help wanted`.
- **Propose a feature** by opening an issue first. Discuss the design before
  writing code, especially for changes that touch the MCP surface, the
  indexer's ignore rules, or storage schemas.

## Development Setup

### Prerequisites

- Python 3.12 or newer.
- Docker and Docker Compose.
- A local [Ollama](https://ollama.com) installation with the
  `nomic-embed-text` model pulled.

### Bootstrap

```bash
git clone https://github.com/rmarquesa/Global-Project-Context.git
cd Global-Project-Context
cp .env.example .env
./install.sh --skip-clients
```

`--skip-clients` avoids touching your real Codex/Claude/Copilot configurations
during development. Drop the flag once you want to validate end-to-end with a
real client.

### Verify the environment

```bash
gpc doctor
./venv/bin/python -m tests.smoke.embedding_smoke_test
./venv/bin/python -m tests.smoke.search_test
./venv/bin/python -m tests.smoke.mcp_smoke_test
```

All three smoke tests should report success against the local Docker services.

## Project Layout

| Path | Purpose |
|---|---|
| `gpc/` | Reusable Python package: indexer, retrieval, MCP server, CLI. |
| `gpc_mcp_server.py` | Stable MCP entrypoint wrapper. |
| `scripts/` | Internal admin scripts (migrations, Qdrant init, client installer). |
| `migrations/` | Postgres SQL migrations applied by `gpc migrate`. |
| `tests/smoke/` | End-to-end smoke tests against live local services. |
| `docs/` | Public documentation. |
| `examples/hooks/` | Reference Git hooks (Graphify integration). |
| `site/` | Static landing page. |

A more detailed inventory lives in [docs/scripts.md](docs/scripts.md).

## Code Style

- **Formatter**: keep code compatible with [Black](https://black.readthedocs.io/)
  defaults (line length 88, double quotes).
- **Linter**: code should pass [Ruff](https://docs.astral.sh/ruff/) with the
  default ruleset.
- **Type hints**: required on public functions in the `gpc/` package.
- **Imports**: standard library first, third-party second, local last; one blank
  line between groups.
- **Docstrings**: short imperative summary; only add longer prose when the
  behaviour is non-obvious.

Run before committing:

```bash
./venv/bin/python -m ruff check gpc scripts tests
./venv/bin/python -m black --check gpc scripts tests
```

## Testing

GPC ships smoke tests rather than unit tests because most behaviour depends on
real Postgres, Qdrant, Ollama and Neo4j services.

| Test | Validates |
|---|---|
| `tests.smoke.embedding_smoke_test` | Ollama is reachable and producing 768-dim vectors. |
| `tests.smoke.search_test` | Qdrant returns hydrated chunks for a known query. |
| `tests.smoke.registry_smoke_test` | Project resolution by slug, alias and `cwd`. |
| `tests.smoke.graph_projection_smoke_test` | Neo4j projection round-trips. |
| `tests.smoke.mcp_smoke_test` | The MCP server exposes the expected tools over stdio. |

Add a new smoke test when introducing or changing:

- An MCP tool's input/output shape.
- The indexer's discovery or chunking rules.
- A storage schema (Postgres migration or Qdrant payload).
- A new retrieval path.

## Commit Conventions

Use short imperative commit subjects in present tense:

```
indexer: skip files larger than max_file_bytes
docs: clarify gpc init slug behaviour
mcp: bound retrieve_tokens by max_chars
```

Subjects under 72 characters. Use the body for *why*, not *what*. Reference
issues with `Fixes #123` when applicable.

Squash work-in-progress commits before opening a pull request.

## Pull Request Process

1. Fork and create a topic branch off `main`.
2. Make your change. Keep PRs focused — one logical change per PR.
3. Update or add documentation if your change is user-visible.
4. Run the relevant smoke tests locally.
5. Open the PR with a description that covers:
   - What changed.
   - Why (link to the issue if there is one).
   - How you validated it.
   - Anything reviewers should pay extra attention to.
6. Be ready to iterate on review feedback.

The maintainer will normally respond within a week. Documentation-only PRs may
be merged faster.

## What Is Out of Scope

The following changes will not be accepted without a prior design discussion:

- Adding a remote embedding provider as the default. GPC defaults to local
  Ollama for privacy and offline operation.
- Exposing write-capable MCP tools without job tracking, cancellation and
  explicit safety boundaries.
- Replacing Postgres as the source of truth for indexed metadata.
- Bundling additional language-model runtimes inside the Docker compose.

Open an issue first if you believe one of these is genuinely needed.

## License

By contributing you agree that your contributions will be licensed under the
GNU Affero General Public License v3.0 or later, the same license as the rest
of the project. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
