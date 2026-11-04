from pathlib import Path
from unittest.mock import MagicMock

from gpc import reset


def test_reset_explicitly_drops_decision_history_before_parent(monkeypatch):
    """Contract test only: no live reset or PostgreSQL engine execution."""
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.statusmessage = "DROP TABLE"
    context = MagicMock()
    context.__enter__.return_value = connection
    monkeypatch.setattr(reset, "pg_connection", lambda: context)

    dropped = reset.drop_postgres_schema()

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    child = "drop table if exists gpc_decision_events cascade"
    parent = "drop table if exists gpc_decisions cascade"
    assert statements.count(child) == 1
    assert statements.index(child) < statements.index(parent)
    assert dropped.index("gpc_decision_events") < dropped.index("gpc_decisions")
    connection.commit.assert_called_once_with()


def test_decision_migration_recreates_cascading_history_foreign_key():
    migration = (
        Path(__file__).parents[1] / "migrations/0011_decision_memory.sql"
    ).read_text()
    assert "create table if not exists gpc_decision_events" in migration
    assert "references gpc_decisions(id) on delete cascade" in migration
    assert reset.GPC_TABLES.count("gpc_decision_events") == 1
