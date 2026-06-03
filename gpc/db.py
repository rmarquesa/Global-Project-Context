"""Shared, long-lived backing-store handles for the MCP server.

The MCP server is a long-lived process that answers many tool calls. Opening a
brand-new Postgres connection, Qdrant client and Neo4j driver on every call pays
the connect/auth/handshake cost each time. This module centralizes three
reusable handles:

* ``pg_connection()`` — a pooled Postgres connection (psycopg_pool). Falls back
  to a direct short-lived connection when psycopg_pool is not installed, so the
  server still works on older installs.
* ``get_qdrant()`` — a process-wide ``QdrantClient`` singleton.
* ``get_neo4j_driver()`` — a process-wide Neo4j driver singleton. Neo4j drivers
  are explicitly designed to be created once per process and pool internally.

All handles are created lazily on first use and closed at interpreter exit, so
importing this module (and the modules that use it) stays cheap and tests that
never touch a backing store do not open connections.
"""

from __future__ import annotations

import atexit
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import tuple_row

from gpc.config import (
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    PG_POOL_MAX_SIZE,
    PG_POOL_TIMEOUT,
    POSTGRES_DSN,
    QDRANT_HOST,
    QDRANT_PORT,
)

try:  # Optional dependency — degrade gracefully to direct connections.
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - exercised only on older installs
    ConnectionPool = None  # type: ignore[assignment,misc]


class Neo4jDependencyError(RuntimeError):
    pass


_pool: Any = None
_pool_lock = threading.Lock()

_qdrant: Any = None
_qdrant_lock = threading.Lock()

_neo4j_driver_singleton: Any = None
_neo4j_lock = threading.Lock()


def _get_pool() -> Any:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            pool = ConnectionPool(
                POSTGRES_DSN,
                min_size=0,
                max_size=PG_POOL_MAX_SIZE,
                timeout=PG_POOL_TIMEOUT,
                name="gpc-pg",
                open=False,
            )
            pool.open()
            atexit.register(_close_pool)
            _pool = pool
    return _pool


def _close_pool() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass


@contextmanager
def pg_connection(*, row_factory: Any = tuple_row) -> Iterator[psycopg.Connection]:
    """Yield a Postgres connection, pooled when psycopg_pool is available.

    The connection is committed and returned to the pool on clean exit, or
    rolled back if the body raises — same transaction semantics as a plain
    ``with psycopg.connect(...) as conn``. ``row_factory`` defaults to
    ``tuple_row`` to match ``psycopg.connect``; pass ``dict_row`` for callers
    that index rows by column name.
    """

    if ConnectionPool is None:
        with psycopg.connect(POSTGRES_DSN, row_factory=row_factory) as conn:
            yield conn
        return

    pool = _get_pool()
    with pool.connection() as conn:
        # Pooled connections are reused, so set the row factory on every
        # checkout rather than relying on a prior caller's choice.
        conn.row_factory = row_factory
        yield conn


def get_qdrant() -> Any:
    """Return the process-wide QdrantClient, creating it on first use."""

    global _qdrant
    if _qdrant is not None:
        return _qdrant
    with _qdrant_lock:
        if _qdrant is None:
            from qdrant_client import QdrantClient

            client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
            atexit.register(_close_qdrant)
            _qdrant = client
    return _qdrant


def _close_qdrant() -> None:
    global _qdrant
    client, _qdrant = _qdrant, None
    if client is not None:
        try:
            client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass


def get_neo4j_driver() -> Any:
    """Return the process-wide Neo4j driver, creating it on first use."""

    global _neo4j_driver_singleton
    if _neo4j_driver_singleton is not None:
        return _neo4j_driver_singleton
    with _neo4j_lock:
        if _neo4j_driver_singleton is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:  # pragma: no cover - optional dep
                raise Neo4jDependencyError(
                    "Missing neo4j Python driver. Install dependencies with "
                    "`./venv/bin/pip install -r requirements.txt`."
                ) from exc

            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            atexit.register(_close_neo4j)
            _neo4j_driver_singleton = driver
    return _neo4j_driver_singleton


def _close_neo4j() -> None:
    global _neo4j_driver_singleton
    driver, _neo4j_driver_singleton = _neo4j_driver_singleton, None
    if driver is not None:
        try:
            driver.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass
