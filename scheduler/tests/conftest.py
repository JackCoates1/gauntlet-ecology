import os
from pathlib import Path

import psycopg
import pytest


ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://gauntlet:gauntlet@localhost:5432/gauntlet_ecology_test"
)


@pytest.fixture(scope="session")
def scheduler_database_url() -> str:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        for migration in sorted((ROOT / "schema" / "migrations").glob("*.sql")):
            connection.execute(migration.read_text())
        connection.execute((ROOT / "schema" / "seed.sql").read_text())
    return DATABASE_URL
