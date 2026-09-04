from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from psycopg import Connection

from api.db import get_connection, pool


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    pool.open(wait=True)
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="Gauntlet: Ecology API",
    version="0.1.0",
    description="Read-only access to generations, matches, and scores.",
    lifespan=lifespan,
)


@app.get("/generations")
def list_generations(connection: Connection = Depends(get_connection)) -> list[dict]:
    """List generations with their challenge version and match count."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT g.id, g.number, g.state, g.random_seed, g.opened_at, g.closed_at,
                   g.challenge_version_id, cv.semver AS challenge_semver,
                   count(m.id)::integer AS match_count
            FROM generations AS g
            JOIN challenge_versions AS cv ON cv.id = g.challenge_version_id
            LEFT JOIN matches AS m ON m.generation_id = g.id
            GROUP BY g.id, cv.semver
            ORDER BY g.number DESC
            """
        )
        return cursor.fetchall()


@app.get("/generations/{generation_id}")
def get_generation(
    generation_id: UUID, connection: Connection = Depends(get_connection)
) -> dict:
    """Get one generation and its strategy population."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT g.*, cv.semver AS challenge_semver, cv.public_description
            FROM generations AS g
            JOIN challenge_versions AS cv ON cv.id = g.challenge_version_id
            WHERE g.id = %s
            """,
            (generation_id,),
        )
        generation = cursor.fetchone()
        if generation is None:
            raise HTTPException(status_code=404, detail="generation not found")
        cursor.execute(
            """
            SELECT s.id, s.agent_id, a.role, a.display_name, s.parent_strategy_ids,
                   s.manifest_version, s.source_bundle_hash, s.validation_status,
                   s.policy_verdict, s.created_at
            FROM strategies AS s
            JOIN agents AS a ON a.id = s.agent_id
            WHERE s.generation_id = %s
            ORDER BY s.created_at, s.id
            """,
            (generation_id,),
        )
        generation["strategies"] = cursor.fetchall()
        return generation


@app.get("/generations/{generation_id}/lineage")
def generation_lineage(
    generation_id: UUID, connection: Connection = Depends(get_connection)
) -> dict:
    """Show selected parents and the attacker/defender score trend to this generation."""
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, number, challenge_version_id FROM generations WHERE id = %s""",
            (generation_id,),
        )
        generation = cursor.fetchone()
        if generation is None:
            raise HTTPException(status_code=404, detail="generation not found")
        cursor.execute(
            """
            SELECT sd.eligible_match_ids, sd.aggregate_metrics,
                   sd.diversity_score::double precision AS diversity_score,
                   sd.ranking_seed, sd.selected_parent_strategy_ids, sd.created_at
            FROM selection_decisions AS sd
            WHERE sd.generation_id = %s
            ORDER BY sd.created_at, sd.id LIMIT 1
            """,
            (generation_id,),
        )
        decision = cursor.fetchone()
        cursor.execute(
            """
            SELECT g.id AS generation_id, g.number,
                   count(s.id)::integer AS scored_matches,
                   avg(s.attacker_points)::double precision AS average_attacker_points,
                   avg(s.defender_points + s.availability_points)::double precision AS average_defender_points,
                   count(*) FILTER (WHERE s.attacker_points > s.defender_points + s.availability_points)::integer AS attacker_wins,
                   count(*) FILTER (WHERE s.attacker_points < s.defender_points + s.availability_points)::integer AS defender_wins
            FROM generations AS g
            LEFT JOIN matches AS m ON m.generation_id = g.id AND m.status = 'completed'
            LEFT JOIN scores AS s ON s.match_id = m.id
            WHERE g.challenge_version_id = %s AND g.number <= %s
            GROUP BY g.id
            ORDER BY g.number
            """,
            (generation["challenge_version_id"], generation["number"]),
        )
        trend = cursor.fetchall()
    return {
        "generation_id": generation["id"],
        "generation_number": generation["number"],
        "selection_decision": decision,
        "trend": trend,
    }


@app.get("/matches/{match_id}")
def get_match(match_id: UUID, connection: Connection = Depends(get_connection)) -> dict:
    """Get a match, both competitors, score, and execution summaries."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.*, attacker_agent.display_name AS attacker_name,
                   defender_agent.display_name AS defender_name,
                   s.attacker_points::double precision AS attacker_points,
                   s.defender_points::double precision AS defender_points,
                   s.availability_points::double precision AS availability_points,
                   s.policy_penalties, s.exploit_classification, s.scorer_version,
                   s.evidence_root_hash
            FROM matches AS m
            JOIN strategies AS attacker_strategy ON attacker_strategy.id = m.attacker_strategy_id
            JOIN agents AS attacker_agent ON attacker_agent.id = attacker_strategy.agent_id
            JOIN strategies AS defender_strategy ON defender_strategy.id = m.defender_strategy_id
            JOIN agents AS defender_agent ON defender_agent.id = defender_strategy.agent_id
            LEFT JOIN scores AS s ON s.match_id = m.id
            WHERE m.id = %s
            """,
            (match_id,),
        )
        match = cursor.fetchone()
        if match is None:
            raise HTTPException(status_code=404, detail="match not found")
        cursor.execute(
            """
            SELECT id, stage, attempt, runner_image_digest, resource_limits,
                   started_at, ended_at, exit_reason, output_hash, attestation_signature
            FROM executions
            WHERE match_id = %s
            ORDER BY stage, attempt
            """,
            (match_id,),
        )
        match["executions"] = cursor.fetchall()
        return match


@app.get("/leaderboard")
def leaderboard(connection: Connection = Depends(get_connection)) -> list[dict]:
    """Aggregate completed-score points by strategy and competitor."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH entries AS (
                SELECT m.attacker_strategy_id AS strategy_id, s.attacker_points AS points
                FROM matches AS m JOIN scores AS s ON s.match_id = m.id
                UNION ALL
                SELECT m.defender_strategy_id, s.defender_points + s.availability_points
                FROM matches AS m JOIN scores AS s ON s.match_id = m.id
            )
            SELECT st.id AS strategy_id, a.id AS agent_id, a.role, a.display_name,
                   count(*)::integer AS scored_matches,
                   sum(entries.points)::double precision AS total_points
            FROM entries
            JOIN strategies AS st ON st.id = entries.strategy_id
            JOIN agents AS a ON a.id = st.agent_id
            GROUP BY st.id, a.id, a.role, a.display_name
            ORDER BY total_points DESC, scored_matches DESC, st.id
            """
        )
        return cursor.fetchall()
