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
- **Accidental indexing of obvious secret-like content**. Files containing
  AWS access key IDs, long API keys, long tokens, long password assignments and
  PEM blocks are skipped before chunking, embedding or persistence. The complete
  accepted file is checked (the default discovery size limit is 512,000 bytes).
- **Accidental symlink traversal**. Discovery excludes symlink files and symlink
  ancestors by default. Explicit `follow_symlinks` opt-in only permits in-root
  targets that also pass ignore and filename policies; external targets are
  always rejected. Do not mutate the source tree concurrently with indexing.
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

### Local network defaults and MCP HTTP exposure

The `gpc-mcp-http` CLI defaults to `127.0.0.1:8765`. Docker Compose also publishes
all host ports on `127.0.0.1`, including Postgres, Qdrant (HTTP/gRPC), Neo4j
(HTTP/Bolt), the optional MCP HTTP profile and optional Grafana profile. The HTTP
process inside its container listens on `0.0.0.0` so container networking works;
that is not a public host bind. These defaults do not isolate services from other
containers on the Compose network or from other local users.

**Do not expose services publicly.** Remote access is an explicit deployment
choice: use an authenticated TLS reverse proxy (or authenticated tunnel),
nondefault service credentials and appropriate firewall rules. A separate Compose
override can replace a port's `host_ip` deliberately; keep backend ports on
loopback and expose only the authenticated proxy, not raw database or MCP ports.
Changing the `GPC_*_PORT` variables only changes port numbers, not the loopback
binding. Compose/network overrides are the operator's responsibility.

MCP retrieval does not edit source files or indexed content, but it returns
indexed chunks of your code. Optional metrics and drift flags can persist
operational data; "read-only retrieval" does not mean no database writes at all.

```bash
# Safe: localhost only.
gpc-mcp-http --host 127.0.0.1 --port 8765

# Explicit remote deployment only: protect with an authenticated proxy and firewall.
gpc-mcp-http --host 0.0.0.0 --port 8765
```

### Default credentials

The `.env.example` ships with development passwords for Postgres and Neo4j
(Compose also has development Grafana credentials):

```env
GPC_POSTGRES_PASSWORD=gpcpass
GPC_NEO4J_PASSWORD=gpcneo4jpass
```

Change all shipped credentials before running GPC on a shared machine or enabling
remote access, and never reuse them on production-facing infrastructure. Loopback
binding is not authentication; the default Qdrant/MCP setup is not a multi-tenant
or authenticated SaaS boundary.

GPC helps enforce this: at MCP server startup and in `gpc doctor`, it emits a
warning when the shipped default Postgres or Neo4j password is used against a
**non-local** host. Set `GPC_STRICT_CREDENTIALS=1` to turn that warning into a
hard failure (the server refuses to start). Local development against
`localhost` is unaffected.

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

### Decision memory

Decision capture and approval are explicit CLI writes. The MCP decision tool
retrieves approved/current evidence by default; diagnostic inactive results are
not endorsed instructions. Stored text remains untrusted retrieved data, even
after human approval. Never interpret it as higher-priority system instructions.

Actor names are local attribution labels, not authenticated principals. The
workflow is not multi-tenant authorization or a tamper-proof audit trail. Source
hash checks refer to indexed evidence, not necessarily current disk contents;
caller-provided revision labels are not verified commits. Recognized secrets are
screened during capture, but users must still review free-form decision text.
See [decision memory](docs/decision-memory.md) for lifecycle and scope limits.

### Dependencies

GPC version-bounds its Python dependencies in `requirements.txt`; the canonical
dependency list and tooling configuration live in `pyproject.toml`. The Docker
compose file pulls upstream service images. Subscribe to advisories for:

- [Postgres](https://www.postgresql.org/support/security/) (with `pgvector`).
- [Qdrant](https://github.com/qdrant/qdrant/security/advisories).
- [Neo4j](https://neo4j.com/security/).
- [Ollama](https://github.com/ollama/ollama/security/advisories).

## Disclosure History

No public advisories yet.
