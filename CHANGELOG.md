# Changelog

All notable changes to GPC are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `pyproject.toml` with project metadata, pinned runtime dependency floors, and
  centralized `ruff` / `black` configuration.
- `gpc/db.py`: a shared connection layer. The long-lived MCP server now reuses a
  pooled Postgres connection (`psycopg_pool`), a single `QdrantClient`, and a
  single Neo4j driver instead of opening fresh handles on every tool call. Falls
  back to direct connections when `psycopg_pool` is not installed.
- Per-query embedding cache (`embed_query`) so identical repeated searches skip
  the Ollama round-trip. Bounded by `GPC_EMBEDDING_CACHE_SIZE` (default 256).
- Credential safety check: the server now warns (or, with
  `GPC_STRICT_CREDENTIALS=1`, refuses to start) when default Postgres/Neo4j
  passwords are used against a non-local host.
- Unit tests for `registry`, `search`, `graph_query`, and the new credential and
  embedding-cache logic — runnable in CI without live services.
- CI now runs the unit test suite (`pytest`, excluding live-service smoke tests).
- `gpc/__init__.py` now exposes `__version__`.

### Fixed
- MCP tool names changed from `gpc.X` to `gpc_X` so Claude Desktop accepts them
  (its tool-name pattern `^[a-zA-Z0-9_-]{1,64}$` rejects dots).

### Changed
- `install.sh` now checks for Python 3.10+ before creating the virtualenv.
