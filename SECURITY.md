# Security Policy

GPC is a local-first context layer that indexes source code, documentation and
notes from your machine. This document describes how to report vulnerabilities,
which versions receive fixes, and the security expectations users should be
aware of when running GPC.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for suspected security
vulnerabilities.**

Report privately through one of these channels:

- **Preferred**: open a private security advisory at
  <https://github.com/rmarquesa/Global-Project-Context/security/advisories/new>.
- **Alternative**: email the maintainer at the address listed on the
  [author's GitHub profile](https://github.com/rmarquesa).

Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce, or a proof-of-concept where possible.
- The GPC commit hash (`git rev-parse HEAD`) and your OS / Python version.

You should expect an acknowledgement within seven days. Coordinated disclosure
timelines will be discussed case by case; the default is 90 days.

## Supported Versions

GPC is pre-1.0 and developed against `main`. Only the latest commit on `main`
receives security fixes. Tagged releases will define a support window once
the project reaches 1.0.

| Version | Supported |
|---|---|
| `main` (latest commit) | ✅ |
| anything older | ❌ |

## Threat Model

GPC is designed to run on a single developer machine. Its trust boundary is
the local user account.

What GPC **does** protect against:

- **Accidental indexing of common secret files**. The indexer skips files such
  as `.env`, `.npmrc`, `.pypirc`, `.netrc`, private keys, certificates and
  files matching obvious credential patterns by default.
- **Accidental indexing of obvious secret-like content**. Lines that look like
  AWS access key IDs, long API keys, long tokens, long password assignments and
  PEM blocks are skipped at chunking time.
- **Embedding leakage to third parties**. The default embedding path uses a
  local Ollama installation. No source code or notes are sent to a remote API
  unless you explicitly change this.
- **Cross-project bleed**. Each project is registered with its own slug and
  Qdrant payload metadata. Retrieval is scoped to the requested project.

What GPC **does not** protect against:

- A compromised local user account. Anyone with read access to the GPC working
  directory can read the indexed chunks in Postgres and Qdrant.
- Secret detection beyond the heuristics listed above. The indexer is
  conservative but is **not** a substitute for a dedicated secret scanner. Run
  one before committing or sharing repositories.
- Network exposure of the MCP HTTP server (see below).
- Vulnerabilities in third-party services (Postgres, Qdrant, Ollama, Neo4j,
  Docker). Keep them up to date.

## Operational Guidance

### MCP HTTP exposure

`gpc-mcp-http` defaults to `127.0.0.1:8765`. **Do not bind it to a public
interface** without putting an authenticating reverse proxy in front of it. The
MCP server is read-only but still returns indexed chunks of your code.

```bash
# Safe: localhost only.
gpc-mcp-http --host 127.0.0.1 --port 8765

# Risky: reachable from the network. Add auth in front.
gpc-mcp-http --host 0.0.0.0 --port 8765
```

### Default credentials

The `.env.example` ships with development passwords for Postgres and Neo4j:

```env
GPC_POSTGRES_PASSWORD=gpcpass
GPC_NEO4J_PASSWORD=gpcneo4jpass
```

Change them before running GPC on a shared machine, and never reuse them on
production-facing infrastructure.

### Reviewing what was indexed

Before sharing a project's GPC index (for example by copying volumes or
exporting Qdrant points), verify what was indexed:

```bash
gpc-status --project <slug>
gpc-search "secret OR password OR token" --project <slug>
```

If any chunk contains sensitive content the indexer missed, reset that
project's data and tighten the ignore rules:

```bash
gpc-index /path/to/project --slug <slug> --reset
```

### Dependencies

GPC pins its Python dependencies in `requirements.txt`. The Docker compose
file pulls upstream service images. Subscribe to advisories for:

- [Postgres](https://www.postgresql.org/support/security/) (with `pgvector`).
- [Qdrant](https://github.com/qdrant/qdrant/security/advisories).
- [Neo4j](https://neo4j.com/security/).
- [Ollama](https://github.com/ollama/ollama/security/advisories).

## Disclosure History

No public advisories yet.
