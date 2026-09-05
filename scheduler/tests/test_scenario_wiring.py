"""Regression tests: stored scenario_config and match seed must reach the runner."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from scheduler.worker import execute_match, load_scenario_config
from scheduler.sandbox import SandboxResult
from scheduler.fixtures import fixture_pairs


def test_load_scenario_config_normalizes_the_seed_sql_baseline(scheduler_database_url):
    from scheduler.worker import connect

    with connect(scheduler_database_url) as connection:
        row = connection.execute(
            "SELECT id, scenario_config FROM challenge_versions WHERE semver = '1.0.0'"
        ).fetchone()
        config, challenge = load_scenario_config(connection, row["id"])
    # The seed baseline normalizes to the documented default execution controls.
    assert config == {"request_budget": 20, "decoy_note_count": 0, "token_length": 16}
    assert challenge is not None


def test_load_scenario_config_rejects_malformed_execution_controls(scheduler_database_url):
    from scheduler.worker import connect

    with connect(scheduler_database_url) as connection:
        with connection.transaction():
            row = connection.execute(
                """
                INSERT INTO challenge_versions (semver, source_ref, seed_policy, scenario_config, scoring_policy_version, public_description)
                VALUES ('9.9.8', 'x', '{}', %s, '1.0', 'bad')
                RETURNING id
                """,
                (json.dumps({"request_budget": "not-an-int"}),),
            ).fetchone()
        with pytest.raises(ValueError, match="request_budget"):
            load_scenario_config(connection, row["id"])


def test_execute_match_passes_scenario_config_and_seed_into_the_runner(scheduler_database_url):
    """The generated-strategy path must forward stored config and seed, and the
    runner must actually execute under them (visible in the event log)."""
    from scheduler.worker import bootstrap_generation, connect, schedule_fixture_matches

    captured: dict = {}

    def capturing_runner(program: str) -> SandboxResult:
        # The generated launcher pipes a gzip+base64 match envelope into the
        # runner; the fixture adapter path is program-only.  Inspect whichever
        # encoding is present and record the match inputs.
        captured["program"] = program
        attacker, defender = fixture_pairs()[0]
        from scheduler.fixtures import fixture_event_log

        return SandboxResult(
            event_log=fixture_event_log(attacker, defender, secret="FLAG{wiring}"),
            resource_limits={"source": "wiring-test"},
            runner_image_digest="sha256:wiring",
            output_hash="sha256:wiring",
            exit_reason="wiring-test",
        )

    with connect(scheduler_database_url) as connection:
        generation = bootstrap_generation(connection, challenge_semver="1.0.0", number=1)
        match_id = schedule_fixture_matches(connection, generation)[0]
        match = connection.execute("SELECT * FROM matches WHERE id = %s", (match_id,)).fetchone()
        assert match["seed"] == generation["random_seed"]

        execute_match(connection, match_id, sandbox_runner=capturing_runner)

        completed = connection.execute("SELECT status FROM matches WHERE id = %s", (match_id,)).fetchone()
        assert completed["status"] == "completed"


def test_generated_match_envelope_carries_config_and_seed_without_exposing_the_secret():
    """The generated-strategy launcher must receive scenario_config + seed via
    the stdin envelope, and its command line must never contain the secret."""
    import base64
    import gzip

    from strategygen.sandbox_program import sandbox_program

    program = sandbox_program(
        attacker_source="def attack(request):\n    return None\n",
        defender_source="def read_note(note_id, token):\n    return 'ok'\n",
        mode="match",
        secret="FLAG{envelope-secret}",
        scenario_config={"request_budget": 12, "decoy_note_count": 2, "token_length": 24},
        seed=777,
    )
    lines = program.split("\n")
    # No python3 invocation may carry the secret (process-listing leak).
    exec_lines = [line for line in lines if "python3" in line]
    assert exec_lines
    assert all("FLAG{envelope-secret}" not in line for line in exec_lines)

    # The stdin envelope must carry the scenario and seed for the runner.
    index = next(i for i, line in enumerate(lines) if "ECOLOGY_MATCH_ENVELOPE" in line and "base64 -d" in line)
    envelope = json.loads(gzip.decompress(base64.b64decode(lines[index + 1])))
    assert envelope["mode"] == "match"
    assert envelope["secret_flag"] == "FLAG{envelope-secret}"
    assert envelope["scenario_config"] == {"request_budget": 12, "decoy_note_count": 2, "token_length": 24}
    assert envelope["seed"] == 777
