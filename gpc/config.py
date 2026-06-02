import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]

load_dotenv(ROOT_DIR / ".env")


PROJECT_NAME = "GPC"
PROJECT_DESCRIPTION = "Global Project Context"

COLLECTION_NAME = "gpc_memory"
VECTOR_SIZE = int(os.getenv("GPC_VECTOR_SIZE", "0"))

EMBEDDING_PROVIDER = os.getenv("GPC_EMBEDDING_PROVIDER", "ollama").strip().lower()
OLLAMA_HOST = os.getenv("GPC_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_EMBEDDING_MODEL = os.getenv(
    "GPC_OLLAMA_EMBEDDING_MODEL",
    "nomic-embed-text:latest",
)
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("GPC_OLLAMA_TIMEOUT_SECONDS", "60"))
# Bound the per-query embedding cache. Set to 0 to disable caching entirely.
EMBEDDING_CACHE_SIZE = int(os.getenv("GPC_EMBEDDING_CACHE_SIZE", "256"))

QDRANT_HOST = os.getenv("GPC_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("GPC_QDRANT_PORT", "6333"))

DEFAULT_POSTGRES_DSN = "postgresql://gpc:gpcpass@localhost:5433/gpc"
POSTGRES_DSN = os.getenv("GPC_POSTGRES_DSN", DEFAULT_POSTGRES_DSN)

# Postgres connection pool sizing for the long-lived MCP server. The pool is
# created lazily on first use, never pre-opens connections (min_size=0), and is
# bypassed entirely when psycopg_pool is unavailable (see gpc/db.py).
PG_POOL_MAX_SIZE = int(os.getenv("GPC_PG_POOL_MAX_SIZE", "8"))
PG_POOL_TIMEOUT = float(os.getenv("GPC_PG_POOL_TIMEOUT", "10"))

NEO4J_URI = os.getenv("GPC_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("GPC_NEO4J_USER", "neo4j")
DEFAULT_NEO4J_PASSWORD = "gpcneo4jpass"
NEO4J_PASSWORD = os.getenv("GPC_NEO4J_PASSWORD", DEFAULT_NEO4J_PASSWORD)

MCP_HTTP_HOST = os.getenv("GPC_MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_PORT = int(os.getenv("GPC_MCP_HTTP_PORT", "8765"))
MCP_HTTP_PATH = os.getenv("GPC_MCP_HTTP_PATH", "/mcp")

# Fail closed instead of warning when default credentials are used against a
# non-local host. Opt-in so local dev keeps working out of the box.
STRICT_CREDENTIALS = os.getenv("GPC_STRICT_CREDENTIALS", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", None}


def _dsn_host(dsn: str) -> str | None:
    try:
        return urlsplit(dsn).hostname
    except ValueError:
        return None


def _uri_host(uri: str) -> str | None:
    try:
        return urlsplit(uri).hostname
    except ValueError:
        return None


def insecure_credential_warnings() -> list[str]:
    """Return human-readable warnings for default secrets on non-local hosts.

    Shipping default passwords is fine for a localhost dev box, but a remote
    deployment that never overrode them runs on publicly known credentials.
    """

    warnings: list[str] = []

    pg_host = _dsn_host(POSTGRES_DSN)
    if (
        POSTGRES_DSN == DEFAULT_POSTGRES_DSN or "gpcpass" in POSTGRES_DSN
    ) and pg_host not in _LOCAL_HOSTS:
        warnings.append(
            f"Postgres is reachable at non-local host {pg_host!r} but still uses the "
            "shipped default password. Set GPC_POSTGRES_DSN with a unique secret."
        )

    neo_host = _uri_host(NEO4J_URI)
    if NEO4J_PASSWORD == DEFAULT_NEO4J_PASSWORD and neo_host not in _LOCAL_HOSTS:
        warnings.append(
            f"Neo4j is reachable at non-local host {neo_host!r} but still uses the "
            "shipped default password. Set GPC_NEO4J_PASSWORD with a unique secret."
        )

    return warnings


def enforce_credentials() -> list[str]:
    """Emit credential warnings; raise when STRICT_CREDENTIALS is enabled.

    Call this from network-exposed entrypoints (the MCP server, ``gpc doctor``)
    rather than at import time, so tests and CLI helpers stay quiet.
    """

    import sys

    warnings = insecure_credential_warnings()
    for message in warnings:
        print(f"[gpc.config] WARNING: {message}", file=sys.stderr)
    if warnings and STRICT_CREDENTIALS:
        raise RuntimeError(
            "Refusing to start with default credentials on a non-local host "
            "(GPC_STRICT_CREDENTIALS is enabled): " + " ".join(warnings)
        )
    return warnings
