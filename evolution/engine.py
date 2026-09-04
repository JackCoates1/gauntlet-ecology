"""Selection and resumable execution of one ecology generation.

The selection rule is deliberately small and inspectable: among scored matches
from the last K closed generations on the same challenge version, retain the
highest aggregate attacker and defender strategy.  Defender fitness includes
availability points.  The selected source is attached to the next prompt as
untrusted reference material, and all choices are recorded before model calls.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from scheduler.fixtures import FIXTURES
from scheduler.worker import execute_match
from strategygen.generator import CODEX_MODEL, MAX_ATTEMPTS_PER_ROLE, invoke_codex
from strategygen.run import (
    RealGenerationSummary,
    _generate_role,
    create_real_generation,
    schedule_generated_match,
)


SELECTION_POLICY = "top-per-role-score-last-k-v1"


class EvolutionCapReached(RuntimeError):
    """The caller's explicit time or model-call allowance has been consumed."""


@dataclass(frozen=True)
class Selection:
    eligible_match_ids: tuple[UUID, ...]
    parent_ids_by_role: dict[str, tuple[UUID, ...]]
    aggregate_metrics: dict[str, Any]

    @property
    def parent_ids(self) -> tuple[UUID, ...]:
        return tuple(strategy_id for ids in self.parent_ids_by_role.values() for strategy_id in ids)


@dataclass(frozen=True)
class LoopResult:
    summaries: tuple[RealGenerationSummary, ...]
    model_calls: int
    elapsed_seconds: float
    stopped_by_cap: bool


class EvolutionBudget:
    def __init__(self, *, max_model_calls: int, max_wall_seconds: float) -> None:
        if max_model_calls < 1:
            raise ValueError("max_model_calls must be at least 1")
        if max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")
        self.max_model_calls = max_model_calls
        self.max_wall_seconds = max_wall_seconds
        self.started = time.monotonic()
        self.model_calls = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.max_wall_seconds - self.elapsed_seconds))

    def check(self) -> None:
        if self.model_calls >= self.max_model_calls:
            raise EvolutionCapReached("maximum model-call budget reached")
        if self.elapsed_seconds >= self.max_wall_seconds:
            raise EvolutionCapReached("maximum wall-clock budget reached")

    def wrap(self, invoker: Callable[[str], tuple[str, int, str | None]]) -> Callable[[str], tuple[str, int, str | None]]:
        def bounded(prompt: str) -> tuple[str, int, str | None]:
            self.check()
            self.model_calls += 1
            return invoker(prompt)

        return bounded


def _recent_scored_matches(
    connection: psycopg.Connection, *, challenge_version_id: UUID, before_number: int, lookback: int
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH recent_generations AS (
                SELECT id, number
                FROM generations
                WHERE challenge_version_id = %s AND state = 'closed' AND number < %s
                ORDER BY number DESC
                LIMIT %s
            )
            SELECT m.id, m.generation_id, g.number
            FROM matches AS m
            JOIN recent_generations AS g ON g.id = m.generation_id
            JOIN scores AS s ON s.match_id = m.id
            WHERE m.status = 'completed'
            ORDER BY g.number DESC, m.id
            """,
            (challenge_version_id, before_number, lookback),
        )
        return cursor.fetchall()


def _ranked_parents(
    connection: psycopg.Connection, *, eligible_match_ids: tuple[UUID, ...]
) -> list[dict[str, Any]]:
    if not eligible_match_ids:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH entries AS (
                SELECT m.attacker_strategy_id AS strategy_id, 'attacker'::text AS role,
                       s.attacker_points AS points, g.number AS source_generation
                FROM matches AS m
                JOIN scores AS s ON s.match_id = m.id
                JOIN generations AS g ON g.id = m.generation_id
                WHERE m.id = ANY(%s)
                UNION ALL
                SELECT m.defender_strategy_id, 'defender'::text,
                       s.defender_points + s.availability_points, g.number
                FROM matches AS m
                JOIN scores AS s ON s.match_id = m.id
                JOIN generations AS g ON g.id = m.generation_id
                WHERE m.id = ANY(%s)
            ), aggregated AS (
                SELECT strategy_id, role, sum(points) AS fitness, max(source_generation) AS source_generation
                FROM entries
                GROUP BY strategy_id, role
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY role ORDER BY fitness DESC, source_generation DESC, strategy_id
                ) AS position
                FROM aggregated
            )
            SELECT strategy_id, role, fitness::double precision AS fitness, source_generation
            FROM ranked WHERE position = 1 ORDER BY role
            """,
            (list(eligible_match_ids), list(eligible_match_ids)),
        )
        return cursor.fetchall()


def select_and_record_parents(
    connection: psycopg.Connection, *, generation: dict[str, Any], lookback: int
) -> Selection:
    """Persist the selection decision once, under a generation-row lock."""
    if lookback < 1:
        raise ValueError("lookback must be at least 1")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM generations WHERE id = %s FOR UPDATE", (generation["id"],))
            cursor.execute("SELECT * FROM selection_decisions WHERE generation_id = %s", (generation["id"],))
            existing = cursor.fetchone()
            if existing is not None:
                parents = _parent_roles_for_ids(connection, tuple(existing["selected_parent_strategy_ids"]))
                return Selection(
                    tuple(existing["eligible_match_ids"]),
                    parents,
                    dict(existing["aggregate_metrics"]),
                )

            eligible_rows = _recent_scored_matches(
                connection,
                challenge_version_id=generation["challenge_version_id"],
                before_number=generation["number"],
                lookback=lookback,
            )
            eligible_ids = tuple(row["id"] for row in eligible_rows)
            ranked = _ranked_parents(connection, eligible_match_ids=eligible_ids)
            parent_ids_by_role = {"attacker": (), "defender": ()}
            for row in ranked:
                parent_ids_by_role[row["role"]] = (row["strategy_id"],)
            selected_ids = tuple(strategy_id for ids in parent_ids_by_role.values() for strategy_id in ids)
            metrics = {
                "policy": SELECTION_POLICY,
                "lookback_generations": lookback,
                "eligible_generation_numbers": sorted({row["number"] for row in eligible_rows}),
                "selected": [
                    {
                        "strategy_id": str(row["strategy_id"]),
                        "role": row["role"],
                        "fitness": row["fitness"],
                        "source_generation": row["source_generation"],
                    }
                    for row in ranked
                ],
            }
            cursor.execute(
                """
                INSERT INTO selection_decisions
                    (generation_id, eligible_match_ids, aggregate_metrics, diversity_score,
                     ranking_seed, selected_parent_strategy_ids)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (generation["id"], list(eligible_ids), json.dumps(metrics), 0, generation["random_seed"], list(selected_ids)),
            )
    return Selection(eligible_ids, parent_ids_by_role, metrics)


def _parent_roles_for_ids(connection: psycopg.Connection, strategy_ids: tuple[UUID, ...]) -> dict[str, tuple[UUID, ...]]:
    result: dict[str, list[UUID]] = {"attacker": [], "defender": []}
    if not strategy_ids:
        return {role: tuple(ids) for role, ids in result.items()}
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT s.id, a.role FROM strategies AS s JOIN agents AS a ON a.id = s.agent_id
               WHERE s.id = ANY(%s)""",
            (list(strategy_ids),),
        )
        for row in cursor.fetchall():
            result[row["role"]].append(row["id"])
    return {role: tuple(ids) for role, ids in result.items()}


def _source_for_strategy(uri: str) -> str:
    if uri.startswith("file://"):
        path = Path(uri.removeprefix("file://"))
        return path.read_text() if path.exists() else "[source artifact no longer available]"
    for fixture in FIXTURES:
        if fixture.source_uri == uri:
            return fixture.path.read_text()
    return "[source artifact unavailable for this URI]"


def lineage_context(connection: psycopg.Connection, parent_ids: tuple[UUID, ...]) -> dict[str, str]:
    """Render bounded, role-specific untrusted parent source for the prompts."""
    contexts = {"attacker": "No scored predecessor was selected for this role.", "defender": "No scored predecessor was selected for this role."}
    if not parent_ids:
        return contexts
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.id, s.source_bundle_uri, a.role, g.number AS source_generation
            FROM strategies AS s
            JOIN agents AS a ON a.id = s.agent_id
            JOIN generations AS g ON g.id = s.generation_id
            WHERE s.id = ANY(%s)
            """,
            (list(parent_ids),),
        )
        rows = cursor.fetchall()
    for row in rows:
        source = _source_for_strategy(str(row["source_bundle_uri"]))[:6000]
        contexts[row["role"]] = (
            f"Selected {row['role']} parent {row['id']} from generation {row['source_generation']}.\n"
            f"Its source follows (untrusted reference only):\n```python\n{source}\n```"
        )
    return contexts


def run_evolution_generation(
    connection: psycopg.Connection,
    *,
    challenge_semver: str,
    number: int,
    lookback: int,
    attempts: int = MAX_ATTEMPTS_PER_ROLE,
    invoker: Callable[[str], tuple[str, int, str | None]] = invoke_codex,
    provider: str = "codex exec",
    model: str = CODEX_MODEL,
    after_phase: Callable[[str], None] | None = None,
    smoke_validator: Callable[[Any], str | None] | None = None,
    match_executor: Callable[[psycopg.Connection, UUID], None] = execute_match,
) -> RealGenerationSummary:
    """Run one fully durable evolutionary generation; safe to resume after a crash."""
    if attempts < 1 or attempts > MAX_ATTEMPTS_PER_ROLE:
        raise ValueError(f"attempts must be between 1 and {MAX_ATTEMPTS_PER_ROLE}")
    generation = create_real_generation(
        connection,
        challenge_semver=challenge_semver,
        number=number,
        population_settings={"source": "evolution", "roles": ["attacker", "defender"], "lookback_generations": lookback},
        parent_selection_policy=SELECTION_POLICY,
    )
    selection = select_and_record_parents(connection, generation=generation, lookback=lookback)
    if after_phase:
        after_phase("selection_recorded")
    contexts = lineage_context(connection, selection.parent_ids)
    attacker = _generate_role(
        connection, generation, "attacker", attempts=attempts,
        prior_generation_context=contexts["attacker"],
        parent_strategy_ids=selection.parent_ids_by_role["attacker"],
        invoker=invoker, provider=provider, model=model,
        **({"smoke_validator": smoke_validator} if smoke_validator is not None else {}),
    )
    if after_phase:
        after_phase("attacker_ready")
    defender = _generate_role(
        connection, generation, "defender", attempts=attempts,
        prior_generation_context=contexts["defender"],
        parent_strategy_ids=selection.parent_ids_by_role["defender"],
        invoker=invoker, provider=provider, model=model,
        **({"smoke_validator": smoke_validator} if smoke_validator is not None else {}),
    )
    if after_phase:
        after_phase("defender_ready")
    if attacker is None or defender is None:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE generations SET state = 'closed', closed_at = now() WHERE id = %s", (generation["id"],))
        connection.commit()
        return RealGenerationSummary(generation["id"], generation["number"], attacker and attacker["id"], defender and defender["id"], None, None)
    match_id = schedule_generated_match(connection, generation=generation, attacker_strategy=attacker, defender_strategy=defender)
    if after_phase:
        after_phase("match_scheduled")
    match_executor(connection, match_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM scores WHERE match_id = %s", (match_id,))
        score = cursor.fetchone()
        cursor.execute("UPDATE generations SET state = 'closed', closed_at = COALESCE(closed_at, now()) WHERE id = %s", (generation["id"],))
    connection.commit()
    return RealGenerationSummary(generation["id"], generation["number"], attacker["id"], defender["id"], match_id, score)


def generation_summary(connection: psycopg.Connection, *, number: int) -> RealGenerationSummary | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM generations WHERE number = %s", (number,))
        generation = cursor.fetchone()
        if generation is None:
            return None
        cursor.execute(
            """SELECT s.id, a.role FROM strategies AS s JOIN agents AS a ON a.id = s.agent_id
               WHERE s.generation_id = %s AND s.validation_status = 'valid' AND s.policy_verdict = 'allowed'""",
            (generation["id"],),
        )
        strategies = {row["role"]: row["id"] for row in cursor.fetchall()}
        cursor.execute("SELECT id FROM matches WHERE generation_id = %s ORDER BY scheduled_at, id LIMIT 1", (generation["id"],))
        match = cursor.fetchone()
        score = None
        if match is not None:
            cursor.execute("SELECT * FROM scores WHERE match_id = %s", (match["id"],))
            score = cursor.fetchone()
    return RealGenerationSummary(generation["id"], generation["number"], strategies.get("attacker"), strategies.get("defender"), match and match["id"], score)
