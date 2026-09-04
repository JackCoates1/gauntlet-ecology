"""CLI entry point for the first real ecology generation."""

from __future__ import annotations

import argparse
import json
import socket

from scheduler.worker import connect, enqueue_job, generation_summary, process_one


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Gauntlet: Ecology fixture generation")
    parser.add_argument("--challenge-semver", default="1.0.0")
    parser.add_argument("--number", type=int, default=1)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-scheduler")
    args = parser.parse_args()
    payload = {"challenge_semver": args.challenge_semver, "number": args.number}
    key = f"generation:{args.challenge_semver}:{args.number}"
    with connect() as connection:
        enqueue_job(connection, job_type="run_generation", idempotency_key=key, payload_ref=json.dumps(payload, sort_keys=True))
        summary = process_one(connection, worker_id=args.worker_id)
        if summary is None:
            summary = generation_summary(connection, number=args.number)
            if summary is None:
                raise RuntimeError("idempotent job exists but its generation is missing")
    print(f"generation={summary.generation_number} matches={summary.match_count} scores={summary.score_count} pass={str(summary.passed).lower()}")
    if not summary.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
