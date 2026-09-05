"""Unit coverage for the average-fitness ranking and benchmark-panel selection.

These exercise evolution/engine.py's SQL directly against a real Postgres
schema, without touching the sandboxed VM or Codex -- the crux of the fix is
the ranking arithmetic and panel-selection query, not match execution (which
is covered end to end by scheduler/tests/test_generation_integration.py).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from evolution.engine import _benchmark_opponents, _ranked_parents


@pytest.fixture()
def connection(engine_database_url):
    with psycopg.connect(engine_database_url, row_factory=dict_row) as conn:
        yield conn


def _challenge_id(connection: psycopg.Connection) -> UUID:
    return connection.execute("SELECT id FROM challenge_versions WHERE semver = '1.0.0'").fetchone()["id"]


def _make_generation(connection: psycopg.Connection, number: int) -> dict:
    challenge_id = _challenge_id(connection)
    return connection.execute(
        """INSERT INTO generations (number, challenge_version_id, state, random_seed, parent_selection_policy)
           VALUES (%s, %s, 'closed', %s, 'test') RETURNING *""",
        (number, challenge_id, number),
    ).fetchone()


def _make_strategy(connection: psycopg.Connection, generation: dict, role: str, *, uri: str | None = None) -> dict:
    agent = connection.execute(
        "INSERT INTO agents (role, display_name, creation_generation_id) VALUES (%s, %s, %s) RETURNING *",
        (role, f"{role}-{uuid4()}", generation["id"]),
    ).fetchone()
    return connection.execute(
        """INSERT INTO strategies (agent_id, generation_id, manifest_version, source_bundle_hash, source_bundle_uri, validation_status, policy_verdict)
           VALUES (%s, %s, '1', %s, %s, 'valid', 'allowed') RETURNING *""",
        (agent["id"], generation["id"], f"sha256:{uuid4()}", uri or f"file:///tmp/{uuid4()}.py"),
    ).fetchone()


def _make_match(
    connection: psycopg.Connection,
    generation: dict,
    attacker: dict,
    defender: dict,
    *,
    attacker_points: float,
    defender_points: float = 0,
    availability_points: float = 0,
) -> UUID:
    challenge_id = _challenge_id(connection)
    seed = connection.execute("SELECT COALESCE(max(seed), 0) + 1 AS next FROM matches").fetchone()["next"]
    match = connection.execute(
        """INSERT INTO matches (attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version)
           VALUES (%s, %s, %s, %s, %s, 'completed', '1') RETURNING id""",
        (attacker["id"], defender["id"], generation["id"], challenge_id, seed),
    ).fetchone()
    connection.execute(
        """INSERT INTO scores (match_id, attacker_points, defender_points, availability_points, scorer_version, evidence_root_hash)
           VALUES (%s, %s, %s, %s, '1.0', %s)""",
        (match["id"], attacker_points, defender_points, availability_points, f"sha256:{uuid4()}"),
    )
    return match["id"]


def test_ranked_parents_uses_average_not_sum(connection):
    # Strategy A plays once and scores 70. Strategy B plays three times and
    # scores 50 each (sum 150, average 50). Summing points would previously
    # favour B purely for having played more matches; averaging must favour
    # A, whose single result is genuinely stronger.
    generation = _make_generation(connection, 101)
    defender = _make_strategy(connection, generation, "defender")
    strategy_a = _make_strategy(connection, generation, "attacker")
    strategy_b = _make_strategy(connection, generation, "attacker")

    match_ids = [_make_match(connection, generation, strategy_a, defender, attacker_points=70)]
    match_ids += [_make_match(connection, generation, strategy_b, defender, attacker_points=50) for _ in range(3)]

    ranked = _ranked_parents(connection, eligible_match_ids=tuple(match_ids), panel_size=1)
    top_attacker = next(row for row in ranked if row["role"] == "attacker")
    assert top_attacker["strategy_id"] == strategy_a["id"]
    assert top_attacker["fitness"] == 70
    assert top_attacker["sample_size"] == 1


def test_benchmark_opponents_excludes_fixture_sourced_strategies(connection):
    # The sandbox has no interpreter to run a generated candidate against a
    # canned fixture "match" (see scheduler/fixtures.py), so the benchmark
    # panel must only ever offer real, generated (file://) opponents.
    generation = _make_generation(connection, 102)
    attacker = _make_strategy(connection, generation, "attacker")
    fixture_defender = _make_strategy(connection, generation, "defender", uri="fixture://harness/fixtures/secure_defender.py")
    generated_defender = _make_strategy(connection, generation, "defender")

    match_ids = [
        _make_match(connection, generation, attacker, fixture_defender, attacker_points=10, defender_points=60, availability_points=25),
        _make_match(connection, generation, attacker, generated_defender, attacker_points=5, defender_points=60, availability_points=25),
    ]

    panel = _benchmark_opponents(connection, eligible_match_ids=tuple(match_ids), panel_size=3)
    assert panel["defender"] == (generated_defender["id"],)
