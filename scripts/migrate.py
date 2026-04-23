import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

from gpc.config import POSTGRES_DSN, ROOT_DIR


MIGRATIONS_DIR = ROOT_DIR / "migrations"
MIGRATIONS_TABLE = "gpc_schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str


def ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        f"""
        create table if not exists {MIGRATIONS_TABLE} (
            version text primary key,
            name text not null,
            checksum text not null,
            applied_at timestamptz not null default now()
        )
        """
    )


def load_migrations() -> list[Migration]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        stem = path.stem
        version, _, name = stem.partition("_")
        if not version or not name:
            raise ValueError(f"Invalid migration filename: {path.name}")

        sql = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                name=name.replace("_", " "),
                path=path,
                checksum=hashlib.sha256(sql).hexdigest(),
            )
        )
    return migrations


def applied_migrations(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        f"select version, name, checksum from {MIGRATIONS_TABLE} order by version"
    ).fetchall()
    return {version: (name, checksum) for version, name, checksum in rows}


def check_for_checksum_mismatches(
    migrations: list[Migration], applied: dict[str, tuple[str, str]]
) -> None:
    for migration in migrations:
        applied_row = applied.get(migration.version)
        if not applied_row:
            continue

        _, applied_checksum = applied_row
        if applied_checksum != migration.checksum:
            raise SystemExit(
                "Migration checksum mismatch for "
                f"{migration.path.name}. Applied migrations must not be edited."
            )


def apply_migration(conn: psycopg.Connection, migration: Migration) -> None:
    sql = migration.path.read_text()
    with conn.transaction():
        conn.execute(sql)
        conn.execute(
            f"""
            insert into {MIGRATIONS_TABLE} (version, name, checksum)
            values (%s, %s, %s)
            """,
            (migration.version, migration.name, migration.checksum),
        )
    print(f"Applied {migration.path.name}")


def migrate_up() -> None:
    migrations = load_migrations()
    if not migrations:
        raise SystemExit("No migrations found.")

    with psycopg.connect(POSTGRES_DSN) as conn:
        ensure_migrations_table(conn)
        applied = applied_migrations(conn)
        check_for_checksum_mismatches(migrations, applied)

        pending = [migration for migration in migrations if migration.version not in applied]
        if not pending:
            print("No pending migrations.")
            return

        for migration in pending:
            apply_migration(conn, migration)


def migration_status() -> None:
    migrations = load_migrations()
    with psycopg.connect(POSTGRES_DSN) as conn:
        ensure_migrations_table(conn)
        applied = applied_migrations(conn)
        check_for_checksum_mismatches(migrations, applied)

    for migration in migrations:
        state = "applied" if migration.version in applied else "pending"
        print(f"{migration.version} {state} {migration.path.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GPC Postgres migrations.")
    parser.add_argument(
        "command",
        choices=("up", "status"),
        nargs="?",
        default="up",
        help="Migration command to run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "up":
        migrate_up()
    elif args.command == "status":
        migration_status()


if __name__ == "__main__":
    main()
