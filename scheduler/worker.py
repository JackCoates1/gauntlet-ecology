"""Postgres-backed job claiming and the generation-one match workflow."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from harness.challenge import DEFAULT_SCENARIO_CONFIG, validate_scenario_config
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
        database_url or os.environ.get("DATABASE_URL", "postgresql://gauntlet@127.0.0.1:5432/gauntlet_ecology"),
        row_factory=dict_row,
    )


def enqueue_job(connection: psycopg.Connection, *, job_type: str, idempotency_key: str, payload_ref: str) -> dict[str, Any]:
    """Insert a job exactly once, reviving only a previously failed retry."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (type, idempotency_key, payload_ref)
            VALUES (%s, %s, %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET
              status = CASE WHEN jobs.status IN ('failed', 'cancelled') THEN 'queued' ELSE jobs.status END,
              lease_owner = CASE WHEN jobs.status IN ('failed', 'cancelled') THEN NULL ELSE jobs.lease_owner END,
              lease_until = CASE WHEN jobs.status IN ('failed', 'cancelled') THEN NULL ELSE jobs.lease_until END,
              error_class = CASE WHEN jobs.status IN ('failed', 'cancelled') THEN NULL ELSE jobs.error_class END,
              updated_at = CASE WHEN jobs.status IN ('failed', 'cancelled') THEN now() ELSE jobs.updated_at END
            RETURNING *
            """,
            (job_type, idempotency_key, payload_ref),
        )
        row = cursor.fetchone()
    connection.commit()
    assert row is not None
    return row


def reclaim_own_running_job(connection: psycopg.Connection, *, idempotency_key: str, worker_id: str) -> dict[str, Any] | None:
    """Requeue an interrupted local CLI job without stealing another worker's lease.

    Normal workers rely on lease expiry.  A loop CLI is commonly restarted with
    the same stable ``worker_id`` after its process was killed, so this narrow
    recovery path avoids an unnecessary wait while retaining ownership safety.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs
            SET status = 'queued', lease_owner = NULL, lease_until = NULL,
                updated_at = now()
            WHERE idempotency_key = %s AND status IN ('leased', 'running')
              AND lease_owner = %s
            RETURNING *
            """,
            (idempotency_key, worker_id),
        )
        row = cursor.fetchone()
    connection.commit()
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
                       OR (status IN ('leased', 'running') AND lease_until < now())
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
        # PostgreSQL text/JSONB cannot store a literal NUL. Generated attack
        # probes are adversarial input, so retain the evidence in a safe,
        # explicit representation rather than letting persistence abort.
        return value.replace(secret, "[REDACTED]").replace("\x00", "[NUL]")
    return value


def _insert_events(connection: psycopg.Connection, match_id: UUID, event_log: dict[str, Any]) -> str:
    secret = str(event_log["challenge"]["secret_flag"])
    # Hashes are globally unique in the schema.  Seed each per-match chain so
    # two reproducible matches with identical redacted events do not collide.
    previous: str | None = "sha256:" + hashlib.sha256(f"match:{match_id}".encode()).hexdigest()
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
        "attack_request_patterns": score["attack_quality"]["request_patterns"],
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


def load_scenario_config(connection: psycopg.Connection, challenge_version_id: UUID) -> tuple[dict[str, int] | None, dict[str, Any]]:
    """Load a challenge version's stored scenario_config and metadata row.

    Returns the normalized scenario config (or None for the compatibility
    baseline) plus the raw challenge-version row.  Raises ValueError when a
    stored config is malformed so bad builder output fails loudly at match
    time instead of silently running the default scenario.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM challenge_versions WHERE id = %s", (challenge_version_id,))
        challenge = cursor.fetchone()
    if challenge is None:
        raise ValueError(f"challenge version {challenge_version_id} does not exist")
    raw = challenge["scenario_config"]
    if raw is None or raw == {}:
        return None, challenge
    # schema/seed.sql's baseline row stores metadata (challenge id, contract
    # descriptions) alongside execution controls, and it predates decoy and
    # token-length controls entirely.  The strict validator only accepts the
    # three execution controls with every field present, so extract the
    # execution subset and fill any absent control from the documented
    # baseline defaults before validating.  Unknown execution-looking fields
    # still fail validation, so malformed builder output cannot sneak through.
    execution_controls = {
        key: raw[key]
        for key in ("request_budget", "decoy_note_count", "token_length")
        if key in raw
    }
    defaults = DEFAULT_SCENARIO_CONFIG
    for key, default in defaults.items():
        execution_controls.setdefault(key, default)
    return validate_scenario_config(execution_controls), challenge


def execute_match(connection: psycopg.Connection, match_id: UUID, *, sandbox_runner: Callable[[str], SandboxResult] = run_sandbox) -> None:
    """Run one scheduled match once; completed rows are a durable idempotency boundary."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.*, a.source_bundle_uri AS attacker_uri, d.source_bundle_uri AS defender_uri,
                   a.validation_status AS attacker_validation_status, d.validation_status AS defender_validation_status
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

    secret = f"FLAG{{generation-{match['generation_id']}-match-{match_id}}}"
    # A match runs under its challenge version's stored scenario and its own
    # persisted seed.  Previously both were silently dropped, so scheduled
    # matches always executed the default baseline.
    scenario_config, _challenge = load_scenario_config(connection, match["challenge_version_id"])
    seed = int(match["seed"])
    attacker = next((item for item in FIXTURES if item.source_uri == match["attacker_uri"]), None)
    defender = next((item for item in FIXTURES if item.source_uri == match["defender_uri"]), None)
    if attacker is not None and defender is not None:
        program = sandbox_program(attacker, defender, secret=secret)
    else:
        if match["attacker_validation_status"] != "valid" or match["defender_validation_status"] != "valid":
            raise ValueError("only validation-approved generated strategies may be executed")
        from strategygen.sandbox_program import sandbox_program as generated_sandbox_program

        def read_generated(uri: str, role: str) -> str:
            if not uri.startswith("file://"):
                raise ValueError(f"unsupported {role} strategy source URI: {uri}")
            return Path(uri.removeprefix("file://")).read_text()

        program = generated_sandbox_program(
            attacker_source=read_generated(match["attacker_uri"], "attacker"),
            defender_source=read_generated(match["defender_uri"], "defender"),
            mode="match",
            secret=secret,
            scenario_config=scenario_config,
            seed=seed,
        )
    sandbox_result = sandbox_runner(program)
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


def generation_summary(connection: psycopg.Connection, *, number: int) -> GenerationSummary | None:
    """Read the durable result for an already-idempotent generation invocation."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, number FROM generations WHERE number = %s", (number,))
        generation = cursor.fetchone()
        if generation is None:
            return None
        cursor.execute("SELECT count(*) AS count FROM matches WHERE generation_id = %s", (generation["id"],))
        match_count = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM scores AS s JOIN matches AS m ON m.id = s.match_id WHERE m.generation_id = %s", (generation["id"],))
        score_count = cursor.fetchone()["count"]
    return GenerationSummary(generation["id"], generation["number"], match_count, score_count, match_count == score_count and match_count == 2)


def process_one(
    connection: psycopg.Connection,
    *,
    worker_id: str,
    handlers: dict[str, Callable[[psycopg.Connection, dict[str, Any]], Any]] | None = None,
) -> Any | None:
    """Claim and execute one durable job, with optional additive job handlers."""
    job = claim_job(connection, worker_id=worker_id)
    if job is None:
        return None
    _mark_running(connection, job["id"], worker_id)
    try:
        payload = json.loads(job["payload_ref"])
        if job["type"] == "run_generation":
            summary = run_generation(connection, challenge_semver=payload["challenge_semver"], number=int(payload["number"]))
        elif handlers is not None and job["type"] in handlers:
            summary = handlers[job["type"]](connection, payload)
        else:
            raise ValueError(f"unsupported job type {job['type']!r}")
    except Exception as error:
        # A failed event/score insert can leave PostgreSQL's current
        # transaction aborted. Clear it before persisting the job failure so a
        # lease is never stranded merely because its work raised an error.
        connection.rollback()
        _finish_job(connection, job["id"], worker_id, error=error)
        raise
    _finish_job(connection, job["id"], worker_id)
    return summary
