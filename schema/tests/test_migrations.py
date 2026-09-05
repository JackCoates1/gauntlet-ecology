import os
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[2]
# NOTE: this test DROPs and recreates the schema — it must never default to the
# live database. The `_test` database is created by:
# docker compose -f docker-compose.dev.yml exec postgres createdb -U gauntlet gauntlet_ecology_test
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://gauntlet@127.0.0.1:5432/gauntlet_ecology_test"
)


def apply_migrations(connection: psycopg.Connection) -> None:
    for migration in sorted((ROOT / "schema" / "migrations").glob("*.sql")):
        connection.execute(migration.read_text())


def test_migrations_apply_to_a_fresh_schema() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        apply_migrations(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    assert {
        "challenge_versions",
        "generations",
        "agents",
        "strategies",
        "matches",
        "executions",
        "events",
        "scores",
        "selection_decisions",
        "jobs",
    } <= tables
