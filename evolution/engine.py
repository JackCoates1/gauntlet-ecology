"""Selection and resumable execution of one ecology generation.

The selection rule is deliberately small and inspectable: among scored matches
from the last K closed generations on the same challenge version, retain the
highest average-fitness attacker and defender strategy (average, not sum, so a
strategy is not favoured merely for having played more matches).  Defender
fitness includes availability points.  The selected source is attached to the
next prompt as untrusted reference material, and all choices are recorded
before model calls.

A single one-off match between a new attacker and a new defender cannot tell a
genuinely stronger strategy apart from one that merely got a weak opponent
this generation.  So after that head-to-head match, each new candidate is also
benchmarked against a small fixed panel of the last few role-appropriate
parents (see BENCHMARK_PANEL_SIZE) drawn from the same eligible-match window
used for parent selection.  Those benchmark matches are ordinary matches
(flagged ``is_benchmark``) and their scores feed back into the average-fitness
ranking above once their generation closes, so future selection reflects
performance across several opponents rather than a single draw.
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


SELECTION_POLICY = "top-per-role-avg-fitness-last-k-v1"

# Small, fixed number of recent role-appropriate parents each new candidate is
# additionally played against, so selection fitness reflects more than one
# opponent draw. Bounded deliberately: this is a benchmark panel, not a
# round-robin tournament.
BENCHMARK_PANEL_SIZE = 3


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
    connection: psycopg.Connection,
    *,
    eligible_match_ids: tuple[UUID, ...],
    panel_size: int = 1,
    require_generated_source: bool = False,
) -> list[dict[str, Any]]:
    """Rank strategies per role by average (not summed) fitness over eligible matches.

    Averaging means a strategy is not favoured merely for having played more
    matches than another (for example one that has already been through a few
    benchmark pairings). ``panel_size`` controls how many top-ranked
    strategies per role are returned: 1 for parent selection, or more to draw
    a benchmark opponent panel from the same eligible window.

    ``require_generated_source`` excludes the checked-in bootstrap fixtures
    (``fixture://`` source URIs). Fixture "matches" are a canned, precomputed
    event log rather than executed source (see scheduler/fixtures.py); the
    sandbox has no interpreter to actually run a generated candidate against
    one, so the benchmark panel is restricted to real generated opponents.
    """
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
                JOIN strategies AS st ON st.id = m.attacker_strategy_id
                WHERE m.id = ANY(%s) AND (NOT %s OR st.source_bundle_uri LIKE 'file://%%')
                UNION ALL
                SELECT m.defender_strategy_id, 'defender'::text,
                       s.defender_points + s.availability_points, g.number
                FROM matches AS m
                JOIN scores AS s ON s.match_id = m.id
                JOIN generations AS g ON g.id = m.generation_id
                JOIN strategies AS st ON st.id = m.defender_strategy_id
                WHERE m.id = ANY(%s) AND (NOT %s OR st.source_bundle_uri LIKE 'file://%%')
            ), aggregated AS (
                SELECT strategy_id, role, avg(points)::double precision AS fitness,
                       count(*)::integer AS sample_size, max(source_generation) AS source_generation
                FROM entries
                GROUP BY strategy_id, role
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY role ORDER BY fitness DESC, source_generation DESC, strategy_id
                ) AS position
                FROM aggregated
            )
            SELECT strategy_id, role, fitness, sample_size, source_generation
            FROM ranked WHERE position <= %s ORDER BY role, position
            """,
            (
                list(eligible_match_ids), require_generated_source,
                list(eligible_match_ids), require_generated_source,
                panel_size,
            ),
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


def _benchmark_opponents(
    connection: psycopg.Connection, *, eligible_match_ids: tuple[UUID, ...], panel_size: int = BENCHMARK_PANEL_SIZE
) -> dict[str, tuple[UUID, ...]]:
    """The last ``panel_size`` role-appropriate parents to benchmark a new candidate against.

    Drawn from the same eligible-match window as parent selection, so "recent"
    means the same thing in both places.
    """
    ranked = _ranked_parents(
        connection, eligible_match_ids=eligible_match_ids, panel_size=panel_size, require_generated_source=True
    )
    panel: dict[str, list[UUID]] = {"attacker": [], "defender": []}
    for row in ranked:
        panel[row["role"]].append(row["strategy_id"])
    return {role: tuple(ids) for role, ids in panel.items()}


def _run_benchmark_matches(
    connection: psycopg.Connection,
    *,
    generation: dict[str, Any],
    candidate: dict[str, Any],
    candidate_role: str,
    opponent_ids: tuple[UUID, ...],
    match_executor: Callable[[psycopg.Connection, UUID], None],
) -> tuple[UUID, ...]:
    """Play one new candidate against each opponent in its benchmark panel."""
    match_ids = []
    for opponent_id in opponent_ids:
        opponent = {"id": opponent_id}
        attacker_strategy, defender_strategy = (candidate, opponent) if candidate_role == "attacker" else (opponent, candidate)
        match_id = schedule_generated_match(
            connection, generation=generation, attacker_strategy=attacker_strategy, defender_strategy=defender_strategy, is_benchmark=True
        )
        match_executor(connection, match_id)
        match_ids.append(match_id)
    return tuple(match_ids)


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
    connection.commit()

    # One head-to-head match cannot tell a genuinely stronger candidate apart
    # from one that merely drew a weak partner this generation, so each
    # candidate also plays a small fixed panel of recent opposite-role parents.
    panel = _benchmark_opponents(connection, eligible_match_ids=selection.eligible_match_ids)
    _run_benchmark_matches(
        connection, generation=generation, candidate=attacker, candidate_role="attacker",
        opponent_ids=panel["defender"], match_executor=match_executor,
    )
    _run_benchmark_matches(
        connection, generation=generation, candidate=defender, candidate_role="defender",
        opponent_ids=panel["attacker"], match_executor=match_executor,
    )
    if after_phase:
        after_phase("benchmarks_recorded")

    with connection.cursor() as cursor:
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
