import os
from collections.abc import Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def database_url() -> str:
    # Credentials are not baked into the repo: they come from the DATABASE_URL
    # env var (see deploy/gauntlet-ecology-api.service EnvironmentFile) or,
    # for local dev, from /root/.pgpass.
    return os.environ.get(
        "DATABASE_URL", "postgresql://gauntlet_app@127.0.0.1:5432/gauntlet_ecology"
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
