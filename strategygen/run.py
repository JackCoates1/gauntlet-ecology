"""Persistence and orchestration for one real generated attacker/defender pair."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from scheduler.sandbox import run as run_sandbox
from scheduler.worker import SANDBOX_POLICY_VERSION, execute_match

from .generator import CODEX_MODEL, GenerationAttempt, MAX_ATTEMPTS_PER_ROLE, generate_attempt, invoke_codex
from .sandbox_program import sandbox_program


@dataclass(frozen=True)
class RealGenerationSummary:
    generation_id: UUID
    generation_number: int
    attacker_strategy_id: UUID | None
    defender_strategy_id: UUID | None
    match_id: UUID | None
    score: dict[str, Any] | None

    @property
    def passed(self) -> bool:
        return self.match_id is not None and self.score is not None


def create_real_generation(
    connection: psycopg.Connection,
    *,
    challenge_semver: str,
    number: int,
    population_settings: dict[str, Any] | None = None,
    parent_selection_policy: str = "codex-real-strategygen-v1",
) -> dict[str, Any]:
    """Open a generation reserved for real Codex source, not bootstrap fixtures."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM challenge_versions WHERE semver = %s", (challenge_semver,))
        challenge = cursor.fetchone()
        if challenge is None:
            raise ValueError(f"challenge version {challenge_semver!r} does not exist; apply schema/seed.sql first")
        cursor.execute("SELECT * FROM generations WHERE number = %s", (number,))
        existing = cursor.fetchone()
        if existing is not None:
            if existing["challenge_version_id"] != challenge["id"]:
                raise ValueError(f"generation {number} belongs to a different challenge version")
            return existing
        cursor.execute(
            """
            INSERT INTO generations
                (number, challenge_version_id, state, random_seed, population_settings, parent_selection_policy, opened_at)
            VALUES (%s, %s, 'open', %s, %s, %s, now())
            RETURNING *
            """,
            (number, challenge["id"], number, json.dumps(population_settings or {"source": "codex exec", "roles": ["attacker", "defender"]}), parent_selection_policy),
        )
        generation = cursor.fetchone()
    connection.commit()
    assert generation is not None
    return generation


def _agent(connection: psycopg.Connection, generation_id: UUID, role: str) -> dict[str, Any]:
    name = f"Codex generated {role}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agents WHERE creation_generation_id = %s AND role = %s AND display_name = %s", (generation_id, role, name))
        agent = cursor.fetchone()
        if agent is None:
            cursor.execute("INSERT INTO agents (role, display_name, creation_generation_id) VALUES (%s, %s, %s) RETURNING *", (role, name, generation_id))
            agent = cursor.fetchone()
    connection.commit()
    assert agent is not None
    return agent


def record_attempt(
    connection: psycopg.Connection,
    *,
    generation: dict[str, Any],
    agent: dict[str, Any],
    attempt: GenerationAttempt,
    parent_strategy_ids: tuple[UUID, ...] = (),
) -> dict[str, Any]:
    """Store every output, including invalid ones, before any sandbox execution."""
    source_hash = "sha256:" + hashlib.sha256(attempt.source.encode()).hexdigest()
    provenance = {**attempt.provenance, "attempt": attempt.attempt, "validation_reason": attempt.validation.reason}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO strategies
                (agent_id, generation_id, parent_strategy_ids, manifest_version, source_bundle_hash, source_bundle_uri,
                 rationale, model_provenance, validation_status, policy_verdict)
            VALUES (%s, %s, %s, 'codex-python-v1', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id, generation_id, source_bundle_hash) DO UPDATE
              SET model_provenance = EXCLUDED.model_provenance,
                  validation_status = EXCLUDED.validation_status,
                  policy_verdict = EXCLUDED.policy_verdict,
                  rationale = EXCLUDED.rationale,
                  parent_strategy_ids = EXCLUDED.parent_strategy_ids
            RETURNING *
            """,
            (agent["id"], generation["id"], list(parent_strategy_ids), source_hash, attempt.artifact_path.resolve().as_uri(), "Codex-generated strategy; static validation precedes a sandbox-only import smoke test.", json.dumps(provenance), "valid" if attempt.validation.valid else "invalid", "allowed" if attempt.validation.valid else "pending"),
        )
        strategy = cursor.fetchone()
        cursor.execute("UPDATE generations SET model_budget_counters = model_budget_counters || %s::jsonb WHERE id = %s", (json.dumps({f"{attempt.role}_attempt_{attempt.attempt}_wall_time_ms": attempt.wall_time_ms}), generation["id"]))
    connection.commit()
    assert strategy is not None
    return strategy


def update_smoke_validation(connection: psycopg.Connection, strategy_id: UUID, *, error: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """UPDATE strategies SET validation_status = %s, policy_verdict = %s,
               model_provenance = model_provenance || %s::jsonb WHERE id = %s""",
            ("valid" if error is None else "invalid", "allowed" if error is None else "pending", json.dumps({"smoke_validation": "passed" if error is None else "failed", "smoke_error": error}), strategy_id),
        )
    connection.commit()


def _smoke_attempt(attempt: GenerationAttempt) -> str | None:
    """Import and invoke source only in the proven sandbox, never on the scheduler host."""
    try:
        run_sandbox(sandbox_program(
            attacker_source=attempt.source if attempt.role == "attacker" else "def attack(request):\n    return None\n",
            defender_source=attempt.source if attempt.role == "defender" else "def read_note(note_id, token):\n    return 'SMOKE'\n",
            mode="smoke", smoke_role=attempt.role,
        ))
    except Exception as error:
        return f"sandbox smoke failed: {type(error).__name__}: {error}"
    return None


def _existing_valid_role(connection: psycopg.Connection, generation_id: UUID, role: str) -> dict[str, Any] | None:
    """Return a durable successful candidate, including its saved source."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.* FROM strategies AS s
            JOIN agents AS a ON a.id = s.agent_id
            WHERE s.generation_id = %s AND a.role = %s
              AND s.validation_status = 'valid' AND s.policy_verdict = 'allowed'
            ORDER BY s.created_at, s.id LIMIT 1
            """,
            (generation_id, role),
        )
        strategy = cursor.fetchone()
    if strategy is None:
        return None
    uri = str(strategy["source_bundle_uri"])
    if not uri.startswith("file://"):
        return None
    source_path = Path(uri.removeprefix("file://"))
    if not source_path.exists():
        return None
    return {**strategy, "source": source_path.read_text()}


def _attempts_already_recorded(connection: psycopg.Connection, generation_id: UUID, role: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*) AS count FROM strategies AS s
               JOIN agents AS a ON a.id = s.agent_id
               WHERE s.generation_id = %s AND a.role = %s""",
            (generation_id, role),
        )
        return int(cursor.fetchone()["count"])


def _generate_role(
    connection: psycopg.Connection,
    generation: dict[str, Any],
    role: str,
    *,
    attempts: int,
    prior_generation_context: str | None = None,
    parent_strategy_ids: tuple[UUID, ...] = (),
    invoker: Callable[[str], tuple[str, int, str | None]] = invoke_codex,
    provider: str = "codex exec",
    model: str = CODEX_MODEL,
    smoke_validator: Callable[[GenerationAttempt], str | None] = _smoke_attempt,
) -> dict[str, Any] | None:
    existing = _existing_valid_role(connection, generation["id"], role)
    if existing is not None:
        return existing
    agent = _agent(connection, generation["id"], role)
    already_recorded = _attempts_already_recorded(connection, generation["id"], role)
    for number in range(already_recorded + 1, attempts + 1):
        attempt = generate_attempt(role, number, invoker=invoker, provider=provider, model=model, prior_generation_context=prior_generation_context)
        strategy = record_attempt(connection, generation=generation, agent=agent, attempt=attempt, parent_strategy_ids=parent_strategy_ids)
        if not attempt.validation.valid:
            continue
        smoke_error = smoke_validator(attempt)
        update_smoke_validation(connection, strategy["id"], error=smoke_error)
        if smoke_error is None:
            return {**strategy, "source": attempt.source}
    return None


def schedule_generated_match(connection: psycopg.Connection, *, generation: dict[str, Any], attacker_strategy: dict[str, Any], defender_strategy: dict[str, Any]) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO matches
               (attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version)
               VALUES (%s, %s, %s, %s, %s, 'scheduled', %s)
               ON CONFLICT (attacker_strategy_id, defender_strategy_id, seed, challenge_version_id)
               DO UPDATE SET attacker_strategy_id = EXCLUDED.attacker_strategy_id
               RETURNING id""",
            (attacker_strategy["id"], defender_strategy["id"], generation["id"], generation["challenge_version_id"], generation["random_seed"], SANDBOX_POLICY_VERSION),
        )
        match = cursor.fetchone()
    connection.commit()
    assert match is not None
    return match["id"]


def generate_and_run(connection: psycopg.Connection, *, challenge_semver: str = "1.0.0", number: int = 2, attempts: int = MAX_ATTEMPTS_PER_ROLE) -> RealGenerationSummary:
    """Generate a pair, preserve invalid attempts, then execute one valid pair through worker.execute_match."""
    if attempts < 1 or attempts > MAX_ATTEMPTS_PER_ROLE:
        raise ValueError(f"attempts must be between 1 and {MAX_ATTEMPTS_PER_ROLE}")
    generation = create_real_generation(connection, challenge_semver=challenge_semver, number=number)
    attacker = _generate_role(connection, generation, "attacker", attempts=attempts)
    defender = _generate_role(connection, generation, "defender", attempts=attempts)
    if attacker is None or defender is None:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE generations SET state = 'closed', closed_at = now() WHERE id = %s", (generation["id"],))
        connection.commit()
        return RealGenerationSummary(generation["id"], generation["number"], attacker and attacker["id"], defender and defender["id"], None, None)
    match_id = schedule_generated_match(connection, generation=generation, attacker_strategy=attacker, defender_strategy=defender)
    execute_match(connection, match_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM scores WHERE match_id = %s", (match_id,))
        score = cursor.fetchone()
        cursor.execute("UPDATE generations SET state = 'closed', closed_at = now() WHERE id = %s", (generation["id"],))
    connection.commit()
    return RealGenerationSummary(generation["id"], generation["number"], attacker["id"], defender["id"], match_id, score)
