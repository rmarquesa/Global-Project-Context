from pathlib import Path
import re
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


def test_reset_covers_every_migrated_table_and_drops_children_first():
    """Guard the full reset inventory as new migrations add dependent tables."""
    order = {name: index for index, name in enumerate(reset.GPC_TABLES)}
    assert len(order) == len(reset.GPC_TABLES)
    migration_dir = Path(__file__).parents[1] / "migrations"
    tables = set()
    for migration in migration_dir.glob("*.sql"):
        for name, definition in re.findall(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(gpc_\w+)\s*\((.*?)\);",
            migration.read_text(),
            re.IGNORECASE | re.DOTALL,
        ):
            tables.add(name)
            assert name in order, f"Reset omits {name} from {migration.name}"
            for parent in re.findall(r"references\s+(gpc_\w+)", definition):
                if parent != name:
                    assert order[name] < order[parent], (name, parent)
    assert tables == set(order) - {"gpc_schema_migrations"}
