import os
from collections.abc import Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql://gauntlet:gauntlet@localhost:5432/gauntlet_ecology"
    )


pool = ConnectionPool(
    conninfo=database_url(),
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row},
    open=False,
)


def get_connection() -> Iterator[Connection]:
    with pool.connection() as connection:
        yield connection
