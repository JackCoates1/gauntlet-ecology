"""CLI for one live Codex-generated attacker/defender match."""

from __future__ import annotations

import argparse
import json

from scheduler.worker import connect

from .generator import MAX_ATTEMPTS_PER_ROLE
from .run import generate_and_run


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one real Gauntlet attacker and defender with Codex, then sandbox one match")
    parser.add_argument("--challenge-semver", default="1.0.0")
    parser.add_argument("--number", type=int, default=2, help="generation number; 1 is reserved for fixture bootstrap")
    parser.add_argument("--attempts", type=int, default=MAX_ATTEMPTS_PER_ROLE)
    args = parser.parse_args()
    with connect() as connection:
        summary = generate_and_run(connection, challenge_semver=args.challenge_semver, number=args.number, attempts=args.attempts)
    print(json.dumps({"generation": summary.generation_number, "match_id": str(summary.match_id) if summary.match_id else None, "score": summary.score, "passed": summary.passed}, default=str, sort_keys=True))
    if not summary.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
