"""Postgres-backed job claiming and the generation-one match workflow."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from harness.scorer import score_event_log
from scheduler.fixtures import FIXTURES, fixture_pairs, sandbox_program
from scheduler.sandbox import SandboxResult, run as run_sandbox


LEASE_SECONDS = 900
SANDBOX_POLICY_VERSION = "infra-namespace-v1"


@dataclass(frozen=True)
class GenerationSummary:
    generation_id: UUID
    generation_number: int
    match_count: int
    score_count: int
    passed: bool


def connect(database_url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(
        database_url or os.environ.get("DATABASE_URL", "postgresql://gauntlet:gauntlet@localhost:5432/gauntlet_ecology"),
        row_factory=dict_row,
    )


def enqueue_job(connection: psycopg.Connection, *, job_type: str, idempotency_key: str, payload_ref: str) -> dict[str, Any]:
    """Insert a job exactly once and return its authoritative row."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (type, idempotency_key, payload_ref)
            VALUES (%s, %s, %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING *
            """,
            (job_type, idempotency_key, payload_ref),
        )
        row = cursor.fetchone()
    connection.commit()
    assert row is not None
    return row


def claim_job(connection: psycopg.Connection, *, worker_id: str, lease_seconds: int = LEASE_SECONDS) -> dict[str, Any] | None:
    """Atomically lease one queued/expired job, using SKIP LOCKED for workers."""
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM jobs
                    WHERE status = 'queued'
                       OR (status = 'leased' AND lease_until < now())
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE jobs AS j
                SET status = 'leased', lease_owner = %s,
                    lease_until = now() + %s::interval,
                    attempts = j.attempts + 1, error_class = NULL,
                    updated_at = now()
                FROM candidate
                WHERE j.id = candidate.id
                RETURNING j.*
                """,
                (worker_id, f"{lease_seconds} seconds"),
            )
            return cursor.fetchone()


def _mark_running(connection: psycopg.Connection, job_id: UUID, worker_id: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs SET status = 'running', updated_at = now()
            WHERE id = %s AND status = 'leased' AND lease_owner = %s AND lease_until > now()
            """,
            (job_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("job lease was lost before execution")
    connection.commit()


def _finish_job(connection: psycopg.Connection, job_id: UUID, worker_id: str, *, error: Exception | None = None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs
            SET status = %s, lease_owner = NULL, lease_until = NULL,
                error_class = %s, updated_at = now()
            WHERE id = %s AND lease_owner = %s
            """,
            ("succeeded" if error is None else "failed", None if error is None else type(error).__name__, job_id, worker_id),
        )
    connection.commit()


def _challenge(connection: psycopg.Connection, semver: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM challenge_versions WHERE semver = %s", (semver,))
        row = cursor.fetchone()
    if row is None:
        raise ValueError(f"challenge version {semver!r} does not exist; apply schema/seed.sql first")
    return row


def bootstrap_generation(connection: psycopg.Connection, *, challenge_semver: str, number: int) -> dict[str, Any]:
    """Create generation one and the four checked-in fixture strategies idempotently."""
    challenge = _challenge(connection, challenge_semver)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM generations WHERE number = %s", (number,))
        generation = cursor.fetchone()
        if generation is None:
            cursor.execute(
                """
                INSERT INTO generations
                    (number, challenge_version_id, state, random_seed, population_settings, parent_selection_policy, opened_at)
                VALUES (%s, %s, 'open', %s, %s, 'bootstrap-fixture-pairs', now())
                RETURNING *
                """,
                (number, challenge["id"], number, json.dumps({"source": "harness fixtures", "pair_count": 2})),
            )
            generation = cursor.fetchone()
        elif generation["challenge_version_id"] != challenge["id"]:
            raise ValueError(f"generation {number} belongs to a different challenge version")
        assert generation is not None
        for fixture in FIXTURES:
            cursor.execute(
                """
                SELECT s.* FROM strategies AS s
                WHERE s.generation_id = %s AND s.source_bundle_uri = %s
                """,
                (generation["id"], fixture.source_uri),
            )
            strategy = cursor.fetchone()
            if strategy is not None:
                continue
            cursor.execute(
                """
                INSERT INTO agents (role, display_name, creation_generation_id)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (fixture.role, fixture.display_name, generation["id"]),
            )
            agent = cursor.fetchone()
            assert agent is not None
            cursor.execute(
                """
                INSERT INTO strategies
                    (agent_id, generation_id, manifest_version, source_bundle_hash, source_bundle_uri,
                     rationale, model_provenance, validation_status, policy_verdict)
                VALUES (%s, %s, 'fixture-v1', %s, %s, 'checked-in deterministic harness fixture',
                        %s, 'valid', 'allowed')
                RETURNING *
                """,
                (agent["id"], generation["id"], fixture.source_hash, fixture.source_uri, json.dumps({"kind": "fixture", "path": str(fixture.path.relative_to(fixture.path.parents[2]))})),
            )
        cursor.execute(
            """
            INSERT INTO selection_decisions
                (generation_id, eligible_match_ids, aggregate_metrics, diversity_score, ranking_seed, selected_parent_strategy_ids)
            SELECT %s, '{}', %s, 0, %s, '{}'
            WHERE NOT EXISTS (SELECT 1 FROM selection_decisions WHERE generation_id = %s)
            """,
            (generation["id"], json.dumps({"bootstrap": True, "reason": "generation 1 has no parents"}), generation["random_seed"], generation["id"]),
        )
    connection.commit()
    return generation


def _strategy_by_uri(connection: psycopg.Connection, generation_id: UUID, uri: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM strategies WHERE generation_id = %s AND source_bundle_uri = %s", (generation_id, uri))
        strategy = cursor.fetchone()
    if strategy is None:
        raise RuntimeError(f"fixture strategy is missing: {uri}")
    return strategy


def schedule_fixture_matches(connection: psycopg.Connection, generation: dict[str, Any]) -> list[UUID]:
    """Create the two deterministic fixture matches exactly once."""
    match_ids: list[UUID] = []
    with connection.cursor() as cursor:
        for offset, (attacker, defender) in enumerate(fixture_pairs()):
            attacker_strategy = _strategy_by_uri(connection, generation["id"], attacker.source_uri)
            defender_strategy = _strategy_by_uri(connection, generation["id"], defender.source_uri)
            cursor.execute(
                """
                INSERT INTO matches
                    (attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version)
                VALUES (%s, %s, %s, %s, %s, 'scheduled', %s)
                ON CONFLICT (attacker_strategy_id, defender_strategy_id, seed, challenge_version_id) DO NOTHING
                RETURNING id
                """,
                (attacker_strategy["id"], defender_strategy["id"], generation["id"], generation["challenge_version_id"], generation["random_seed"] + offset, SANDBOX_POLICY_VERSION),
            )
            match = cursor.fetchone()
            if match is None:
                cursor.execute(
                    """
                    SELECT id FROM matches WHERE attacker_strategy_id = %s AND defender_strategy_id = %s
                      AND seed = %s AND challenge_version_id = %s
                    """,
                    (attacker_strategy["id"], defender_strategy["id"], generation["random_seed"] + offset, generation["challenge_version_id"]),
                )
                match = cursor.fetchone()
            assert match is not None
            match_ids.append(match["id"])
    connection.commit()
    return match_ids


def _canonical_hash(previous: str | None, payload: dict[str, Any]) -> str:
    body = (previous or "") + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key == "token" else _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    return value


def _insert_events(connection: psycopg.Connection, match_id: UUID, event_log: dict[str, Any]) -> str:
    secret = str(event_log["challenge"]["secret_flag"])
    previous: str | None = None
    with connection.cursor() as cursor:
        for sequence, event in enumerate(event_log["events"]):
            payload = _redact(event, secret)
            event_hash = _canonical_hash(previous, payload)
            cursor.execute(
                """
                INSERT INTO events (match_id, sequence, virtual_timestamp, actor, action_type, redacted_payload, prev_hash, hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (match_id, sequence) DO NOTHING
                """,
                (match_id, sequence, sequence, str(event.get("source", "system")), event["type"], json.dumps(payload), previous, event_hash),
            )
            previous = event_hash
    return previous or _canonical_hash(None, {})


def _record_score(connection: psycopg.Connection, match_id: UUID, event_log: dict[str, Any], evidence_root_hash: str) -> None:
    score = score_event_log(event_log)
    penalties = {
        "harness_total": score["total"],
        "leaked": score["leaked"],
        "reasons": score["reasons"],
        "attack_techniques": score["attack_quality"]["techniques"],
        "benign_checks": score["availability"],
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO scores
                (match_id, attacker_points, defender_points, availability_points, policy_penalties,
                 exploit_classification, scorer_version, evidence_root_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (match_id) DO NOTHING
            """,
            (
                match_id,
                score["confidentiality"]["attacker"] + score["attack_quality"]["attacker"],
                score["confidentiality"]["defender"],
                score["availability"]["defender"],
                json.dumps(penalties),
                "secret-leak" if score["leaked"] else "no-secret-leak",
                score["scorer_version"],
                evidence_root_hash,
            ),
        )


def execute_match(connection: psycopg.Connection, match_id: UUID, *, sandbox_runner: Callable[[str], SandboxResult] = run_sandbox) -> None:
    """Run one scheduled match once; completed rows are a durable idempotency boundary."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.*, a.source_bundle_uri AS attacker_uri, d.source_bundle_uri AS defender_uri
            FROM matches AS m
            JOIN strategies AS a ON a.id = m.attacker_strategy_id
            JOIN strategies AS d ON d.id = m.defender_strategy_id
            WHERE m.id = %s FOR UPDATE
            """,
            (match_id,),
        )
        match = cursor.fetchone()
        if match is None:
            raise ValueError("match does not exist")
        if match["status"] == "completed":
            connection.commit()
            return
        cursor.execute("UPDATE matches SET status = 'running' WHERE id = %s", (match_id,))
        cursor.execute(
            """INSERT INTO executions (match_id, stage, attempt, runner_image_digest, resource_limits, started_at)
               VALUES (%s, 'attack', 1, 'sha256:pending', '{}', now())
               ON CONFLICT (match_id, stage, attempt) DO NOTHING""",
            (match_id,),
        )
    connection.commit()

    attacker = next((item for item in FIXTURES if item.source_uri == match["attacker_uri"]), None)
    defender = next((item for item in FIXTURES if item.source_uri == match["defender_uri"]), None)
    if attacker is None or defender is None:
        raise ValueError("only checked-in fixture strategies are executable in generation one")
    secret = f"FLAG{{generation-{match['generation_id']}-match-{match_id}}}"
    sandbox_result = sandbox_runner(sandbox_program(attacker, defender, secret=secret))
    evidence_root_hash = _insert_events(connection, match_id, sandbox_result.event_log)
    _record_score(connection, match_id, sandbox_result.event_log, evidence_root_hash)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE executions
            SET runner_image_digest = %s, resource_limits = %s, ended_at = now(), exit_reason = %s,
                output_hash = %s, private_log_uri = %s, attestation_signature = %s
            WHERE match_id = %s AND stage = 'attack' AND attempt = 1
            """,
            (sandbox_result.runner_image_digest, json.dumps(sandbox_result.resource_limits), sandbox_result.exit_reason,
             sandbox_result.output_hash, f"database://matches/{match_id}/events", sandbox_result.output_hash, match_id),
        )
        cursor.execute("UPDATE matches SET status = 'completed', completed_at = now() WHERE id = %s", (match_id,))
    connection.commit()


def run_generation(connection: psycopg.Connection, *, challenge_semver: str = "1.0.0", number: int = 1) -> GenerationSummary:
    generation = bootstrap_generation(connection, challenge_semver=challenge_semver, number=number)
    match_ids = schedule_fixture_matches(connection, generation)
    for match_id in match_ids:
        execute_match(connection, match_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM matches WHERE generation_id = %s", (generation["id"],))
        match_count = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM scores AS s JOIN matches AS m ON m.id = s.match_id WHERE m.generation_id = %s", (generation["id"],))
        score_count = cursor.fetchone()["count"]
        cursor.execute("UPDATE generations SET state = 'closed', closed_at = COALESCE(closed_at, now()) WHERE id = %s", (generation["id"],))
    connection.commit()
    return GenerationSummary(generation["id"], generation["number"], match_count, score_count, match_count == score_count and match_count == 2)


def process_one(connection: psycopg.Connection, *, worker_id: str) -> GenerationSummary | None:
    job = claim_job(connection, worker_id=worker_id)
    if job is None:
        return None
    _mark_running(connection, job["id"], worker_id)
    try:
        if job["type"] != "run_generation":
            raise ValueError(f"unsupported job type {job['type']!r}")
        payload = json.loads(job["payload_ref"])
        summary = run_generation(connection, challenge_semver=payload["challenge_semver"], number=int(payload["number"]))
    except Exception as error:
        _finish_job(connection, job["id"], worker_id, error=error)
        raise
    _finish_job(connection, job["id"], worker_id)
    return summary
