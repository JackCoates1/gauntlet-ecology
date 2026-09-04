import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://gauntlet:gauntlet@localhost:5432/gauntlet_ecology"
)


def reset_database() -> dict[str, str]:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        for migration in sorted((ROOT / "schema" / "migrations").glob("*.sql")):
            connection.execute(migration.read_text())
        connection.execute((ROOT / "schema" / "seed.sql").read_text())

        challenge_id = connection.execute(
            "SELECT id FROM challenge_versions WHERE semver = '1.0.0'"
        ).fetchone()[0]
        generation_id, attacker_agent_id, defender_agent_id = uuid4(), uuid4(), uuid4()
        attacker_strategy_id, defender_strategy_id, match_id = uuid4(), uuid4(), uuid4()
        connection.execute(
            """
            INSERT INTO generations (id, number, challenge_version_id, state, random_seed, parent_selection_policy)
            VALUES (%s, 1, %s, 'closed', 42, 'fitness-plus-diversity')
            """,
            (generation_id, challenge_id),
        )
        connection.execute(
            """
            INSERT INTO agents (id, role, display_name, creation_generation_id)
            VALUES (%s, 'attacker', 'Red Team One', %s),
                   (%s, 'defender', 'Blue Team One', %s)
            """,
            (attacker_agent_id, generation_id, defender_agent_id, generation_id),
        )
        connection.execute(
            """
            INSERT INTO strategies (id, agent_id, generation_id, manifest_version, source_bundle_hash, source_bundle_uri, validation_status, policy_verdict)
            VALUES (%s, %s, %s, '1', 'sha256:attacker', 'object://bundles/attacker', 'valid', 'allowed'),
                   (%s, %s, %s, '1', 'sha256:defender', 'object://bundles/defender', 'valid', 'allowed')
            """,
            (
                attacker_strategy_id,
                attacker_agent_id,
                generation_id,
                defender_strategy_id,
                defender_agent_id,
                generation_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO matches (id, attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version)
            VALUES (%s, %s, %s, %s, %s, 99, 'completed', '1')
            """,
            (match_id, attacker_strategy_id, defender_strategy_id, generation_id, challenge_id),
        )
        connection.execute(
            """
            INSERT INTO executions (match_id, stage, attempt, runner_image_digest, exit_reason)
            VALUES (%s, 'attack', 1, 'sha256:runner', 'completed')
            """,
            (match_id,),
        )
        connection.execute(
            """
            INSERT INTO scores (match_id, attacker_points, defender_points, availability_points, scorer_version, evidence_root_hash)
            VALUES (%s, 7.5, 3, 2, '1.0.0', 'sha256:evidence')
            """,
            (match_id,),
        )
    return {"generation_id": str(generation_id), "match_id": str(match_id)}


@pytest.fixture(scope="session")
def seeded_ids() -> dict[str, str]:
    return reset_database()


@pytest.fixture(scope="session")
def client(seeded_ids: dict[str, str]):
    # Import after the test database is prepared so the application pool uses its URL.
    os.environ["DATABASE_URL"] = DATABASE_URL
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client
