"""CLI and orchestration for continuous Gauntlet: Ecology evolution."""

from __future__ import annotations

import argparse
import json
import socket
from collections.abc import Callable
from typing import Any

import psycopg

from scheduler.worker import connect, enqueue_job, process_one, reclaim_own_running_job, run_generation
from strategygen.generator import CODEX_MODEL, invoke_codex

from .engine import (
    EvolutionBudget,
    EvolutionCapReached,
    LoopResult,
    SELECTION_POLICY,
    generation_summary,
    run_evolution_generation,
)


def _next_target_number(connection: psycopg.Connection) -> int:
    """Resume an unfinished evolution generation before allocating another one."""
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT number FROM generations
               WHERE parent_selection_policy = %s AND state <> 'closed'
               ORDER BY number LIMIT 1""",
            (SELECTION_POLICY,),
        )
        unfinished = cursor.fetchone()
        if unfinished is not None:
            return int(unfinished["number"])
        cursor.execute("SELECT COALESCE(max(number), 0) + 1 AS number FROM generations")
        return int(cursor.fetchone()["number"])


def _ensure_scored_seed(connection: psycopg.Connection, *, challenge_semver: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM scores")
        has_scores = int(cursor.fetchone()["count"]) > 0
    if not has_scores:
        # This is the existing deterministic bootstrap.  Evolution itself is
        # always job-backed below; the bootstrap remains independently useful.
        run_generation(connection, challenge_semver=challenge_semver, number=1)


def run_loop(
    connection: psycopg.Connection,
    *,
    generations: int,
    max_model_calls: int,
    max_wall_seconds: float,
    challenge_semver: str = "1.0.0",
    lookback: int = 3,
    attempts: int = 3,
    worker_id: str = "evolution-loop",
    invoker: Callable[[str], tuple[str, int, str | None]] | None = None,
    provider: str = "codex exec",
    model: str = CODEX_MODEL,
    after_phase: Callable[[str], None] | None = None,
    smoke_validator: Callable[[Any], str | None] | None = None,
    match_executor: Callable[[psycopg.Connection, int], None] | None = None,
) -> LoopResult:
    """Create and score up to ``generations`` new durable generations.

    Both caps are required at the public boundary.  A failed job is revived by
    its same idempotency key on the next call; persisted agents, strategies,
    selection decisions, matches and scores are reused rather than inserted
    again.
    """
    if generations < 1:
        raise ValueError("generations must be at least 1")
    if lookback < 1:
        raise ValueError("lookback must be at least 1")
    budget = EvolutionBudget(max_model_calls=max_model_calls, max_wall_seconds=max_wall_seconds)
    _ensure_scored_seed(connection, challenge_semver=challenge_semver)
    summaries = []
    stopped_by_cap = False

    # The default command honours the remaining loop wall time for each Codex
    # subprocess, so one invocation cannot silently run beyond the cap.
    if invoker is None:
        def timed_codex(prompt: str) -> tuple[str, int, str | None]:
            remaining = budget.remaining_seconds
            if remaining < 1:
                raise EvolutionCapReached("maximum wall-clock budget reached")
            return invoke_codex(prompt, timeout_seconds=remaining)

        base_invoker = timed_codex
    else:
        base_invoker = invoker
    bounded_invoker = budget.wrap(base_invoker)

    for _ in range(generations):
        number = _next_target_number(connection)
        payload = {
            "challenge_semver": challenge_semver,
            "number": number,
            "lookback": lookback,
            "attempts": attempts,
            "selection_policy": SELECTION_POLICY,
        }
        job = enqueue_job(
            connection,
            job_type="run_evolution_generation",
            idempotency_key=f"evolution-generation:{challenge_semver}:{number}",
            payload_ref=json.dumps(payload, sort_keys=True),
        )
        if job["status"] in {"leased", "running"} and job["lease_owner"] == worker_id:
            # The previous process bearing this stable CLI worker ID died. Do
            # not touch a lease owned by any other worker.
            reclaim_own_running_job(connection, idempotency_key=job["idempotency_key"], worker_id=worker_id)

        def handler(job_connection: psycopg.Connection, job_payload: dict[str, Any]):
            return run_evolution_generation(
                job_connection,
                challenge_semver=str(job_payload["challenge_semver"]),
                number=int(job_payload["number"]),
                lookback=int(job_payload["lookback"]),
                attempts=int(job_payload["attempts"]),
                invoker=bounded_invoker,
                provider=provider,
                model=model,
                after_phase=after_phase,
                smoke_validator=smoke_validator,
                **({"match_executor": match_executor} if match_executor is not None else {}),
            )

        try:
            summary = process_one(connection, worker_id=worker_id, handlers={"run_evolution_generation": handler})
        except EvolutionCapReached:
            stopped_by_cap = True
            break
        if summary is None:
            summary = generation_summary(connection, number=number)
            if summary is None:
                raise RuntimeError("idempotent evolution job exists but its generation is missing")
        summaries.append(summary)
        if not summary.passed:
            break
    return LoopResult(tuple(summaries), budget.model_calls, budget.elapsed_seconds, stopped_by_cap)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run continuous, job-backed Gauntlet ecology evolution")
    parser.add_argument("--generations", type=int, required=True, help="number of new generations to attempt")
    parser.add_argument("--max-model-calls", type=int, required=True, help="hard cap across this loop invocation")
    parser.add_argument("--max-wall-seconds", type=float, required=True, help="hard wall-clock cap across this loop invocation")
    parser.add_argument("--challenge-semver", default="1.0.0")
    parser.add_argument("--lookback", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-evolution")
    args = parser.parse_args()
    with connect() as connection:
        result = run_loop(
            connection,
            generations=args.generations,
            max_model_calls=args.max_model_calls,
            max_wall_seconds=args.max_wall_seconds,
            challenge_semver=args.challenge_semver,
            lookback=args.lookback,
            attempts=args.attempts,
            worker_id=args.worker_id,
        )
    print(json.dumps({
        "generations": [
            {"number": summary.generation_number, "match_id": str(summary.match_id) if summary.match_id else None,
             "score": summary.score, "passed": summary.passed}
            for summary in result.summaries
        ],
        "model_calls": result.model_calls,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "stopped_by_cap": result.stopped_by_cap,
    }, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
