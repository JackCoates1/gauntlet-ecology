import hashlib
import json
import subprocess

import pytest

from scheduler.sandbox import SandboxResult
from scheduler.worker import bootstrap_generation, connect, enqueue_job, execute_match, process_one, schedule_fixture_matches
from evolution.run_loop import run_loop
from scheduler.worker import run_generation


def _run_once(database_url: str):
    with connect(database_url) as connection:
        enqueue_job(
            connection,
            job_type="run_generation",
            idempotency_key="generation:1.0.0:1",
            payload_ref=json.dumps({"challenge_semver": "1.0.0", "number": 1}),
        )
        return process_one(connection, worker_id="scheduler-integration-test")


def test_full_generation_uses_postgres_and_real_sandbox(scheduler_database_url):
    summary = _run_once(scheduler_database_url)
    assert summary is not None
    assert summary.match_count == summary.score_count == 2
    assert summary.passed is True

    with connect(scheduler_database_url) as connection:
        generation = connection.execute("SELECT * FROM generations WHERE number = 1").fetchone()
        assert generation["state"] == "closed"
        assert connection.execute("SELECT count(*) FROM agents").fetchone()["count"] == 4
        assert connection.execute("SELECT count(*) FROM strategies").fetchone()["count"] == 4
        assert connection.execute("SELECT count(*) FROM matches").fetchone()["count"] == 2
        assert connection.execute("SELECT count(*) FROM executions").fetchone()["count"] == 2
        assert connection.execute("SELECT count(*) FROM events").fetchone()["count"] > 0
        assert connection.execute("SELECT count(*) FROM scores").fetchone()["count"] == 2
        decision = connection.execute("SELECT * FROM selection_decisions WHERE generation_id = %s", (generation["id"],)).fetchone()
        assert decision["eligible_match_ids"] == []
        assert decision["selected_parent_strategy_ids"] == []
        assert decision["aggregate_metrics"]["bootstrap"] is True

        scores = connection.execute(
            "SELECT attacker_points, defender_points, availability_points, exploit_classification FROM scores ORDER BY attacker_points DESC"
        ).fetchall()
        # harness nested score -> attacker confidentiality + attack-quality,
        # defender confidentiality, defender availability respectively.
        assert scores[0] == {"attacker_points": 63, "defender_points": 0, "availability_points": 25, "exploit_classification": "secret-leak"}
        assert scores[1] == {"attacker_points": 0, "defender_points": 60, "availability_points": 25, "exploit_classification": "no-secret-leak"}

        executions = connection.execute("SELECT * FROM executions ORDER BY match_id").fetchall()
        vm_config = subprocess.run(["qm", "config", "190"], check=True, text=True, capture_output=True).stdout
        expected_digest = "sha256:" + hashlib.sha256(vm_config.encode()).hexdigest()
        for execution in executions:
            assert execution["runner_image_digest"] == expected_digest
            limits = execution["resource_limits"]
            assert limits["source"] == "infra/run_sandboxed.sh"
            assert limits["destroyed"] is True
            assert limits["sandbox_leftovers"] == 0
            assert limits["cpu_seconds"] == 30
            assert limits["processes"] == 100
            assert limits["stdout_bytes"] > 0
            assert execution["output_hash"].startswith("sha256:")


def test_generation_job_and_rows_are_idempotent(scheduler_database_url):
    # The preceding full generation run used this exact key.  A second run
    # returns the existing succeeded job, so no second sandbox run and no
    # duplicate match, score, or decision is possible.
    assert _run_once(scheduler_database_url) is None
    with connect(scheduler_database_url) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()["count"] == 1
        assert connection.execute("SELECT count(*) FROM matches").fetchone()["count"] == 2
        assert connection.execute("SELECT count(*) FROM scores").fetchone()["count"] == 2
        assert connection.execute("SELECT count(*) FROM selection_decisions").fetchone()["count"] == 1


def test_claim_query_uses_skip_locked():
    # Keep the concurrency guarantee visible as part of the tested worker contract.
    import inspect
    from scheduler.worker import claim_job

    assert "FOR UPDATE SKIP LOCKED" in inspect.getsource(claim_job)


def _fixture_evolution_invoker(prompt: str):
    """Deterministic local strategy output; never calls Codex in CI."""
    from scheduler.fixtures import BY_URI

    if "Your role is 'attacker'" in prompt:
        source = BY_URI["fixture://harness/fixtures/cautious_attacker.py"].path.read_text()
    else:
        source = BY_URI["fixture://harness/fixtures/secure_defender.py"].path.read_text()
    return source, 0, None


def _fixture_match_executor(connection, match_id):
    """Fast scheduler-level fixture executor; no VM or model is needed here."""
    from scheduler.fixtures import fixture_event_log, fixture_pairs

    _, secure_pair = fixture_pairs()
    execute_match(
        connection,
        match_id,
        sandbox_runner=lambda _program: SandboxResult(
            event_log=fixture_event_log(*secure_pair, secret=f"FLAG{{fixture-{match_id}}}"),
            resource_limits={"source": "fixture-test"},
            runner_image_digest="sha256:fixture",
            output_hash=f"sha256:{match_id}",
            exit_reason="fixture",
        ),
    )


def _seed_with_fixture_scores(connection):
    generation = bootstrap_generation(connection, challenge_semver="1.0.0", number=1)
    for match_id in schedule_fixture_matches(connection, generation):
        _fixture_match_executor(connection, match_id)
    connection.execute("UPDATE generations SET state = 'closed', closed_at = now() WHERE id = %s", (generation["id"],))
    connection.commit()


def test_evolution_loop_records_real_lineage_across_two_generations(scheduler_database_url):
    # Generation one is the scored fixture population.  The two loop turns
    # create generated child populations, but their source comes from a local
    # deterministic invoker rather than an external model.
    with connect(scheduler_database_url) as connection:
        _seed_with_fixture_scores(connection)
        result = run_loop(
            connection,
            generations=2,
            max_model_calls=4,
            max_wall_seconds=300,
            lookback=2,
            worker_id="fixture-evolution-test",
            invoker=_fixture_evolution_invoker,
            provider="fixture",
            model="fixture-v1",
            smoke_validator=lambda _attempt: None,
            match_executor=_fixture_match_executor,
        )
        assert [summary.generation_number for summary in result.summaries] == [2, 3]
        assert all(summary.passed for summary in result.summaries)
        assert result.model_calls == 4

        decisions = connection.execute(
            """SELECT g.number, sd.* FROM selection_decisions AS sd
               JOIN generations AS g ON g.id = sd.generation_id
               WHERE g.number IN (2, 3) ORDER BY g.number"""
        ).fetchall()
        assert len(decisions) == 2
        assert all(len(row["eligible_match_ids"]) >= 1 for row in decisions)
        assert all(len(row["selected_parent_strategy_ids"]) == 2 for row in decisions)
        assert all(row["aggregate_metrics"]["policy"] == "top-per-role-avg-fitness-last-k-v1" for row in decisions)
        for decision in decisions:
            parent_count = connection.execute(
                "SELECT count(*) AS count FROM strategies WHERE id = ANY(%s)",
                (decision["selected_parent_strategy_ids"],),
            ).fetchone()["count"]
            assert parent_count == 2

        children = connection.execute(
            """SELECT g.number, a.role, s.parent_strategy_ids
               FROM strategies AS s
               JOIN agents AS a ON a.id = s.agent_id
               JOIN generations AS g ON g.id = s.generation_id
               WHERE g.number IN (2, 3) AND s.validation_status = 'valid'
               ORDER BY g.number, a.role"""
        ).fetchall()
        assert len(children) == 4
        assert all(len(child["parent_strategy_ids"]) == 1 for child in children)


def test_evolution_retry_after_mid_generation_crash_does_not_duplicate_rows(scheduler_database_url):
    def crash_after_attacker(phase: str) -> None:
        if phase == "attacker_ready":
            raise RuntimeError("simulated worker death")

    with connect(scheduler_database_url) as connection:
        with pytest.raises(RuntimeError, match="simulated worker death"):
            run_loop(
                connection,
                generations=1,
                max_model_calls=4,
                max_wall_seconds=300,
                worker_id="fixture-evolution-crash",
                invoker=_fixture_evolution_invoker,
                provider="fixture",
                model="fixture-v1",
                after_phase=crash_after_attacker,
                smoke_validator=lambda _attempt: None,
                match_executor=_fixture_match_executor,
            )
        crashed_generation = connection.execute(
            "SELECT * FROM generations WHERE parent_selection_policy = 'top-per-role-avg-fitness-last-k-v1' AND state = 'open'"
        ).fetchone()
        assert crashed_generation is not None
        before = connection.execute(
            """SELECT count(*) AS agents, (SELECT count(*) FROM strategies WHERE generation_id = %s) AS strategies,
                      (SELECT count(*) FROM selection_decisions WHERE generation_id = %s) AS decisions
               FROM agents WHERE creation_generation_id = %s""",
            (crashed_generation["id"], crashed_generation["id"], crashed_generation["id"]),
        ).fetchone()
        assert before == {"agents": 1, "strategies": 1, "decisions": 1}
        # Model the harsher failure mode where the process vanished before it
        # could mark its job failed. The same stable CLI worker ID may reclaim
        # only its own lease on rerun.
        connection.execute(
            """UPDATE jobs SET status = 'running', lease_owner = 'fixture-evolution-crash',
               lease_until = now() + interval '15 minutes'
               WHERE idempotency_key LIKE 'evolution-generation:%'"""
        )
        connection.commit()

        result = run_loop(
            connection,
            generations=1,
            max_model_calls=4,
            max_wall_seconds=300,
            worker_id="fixture-evolution-crash",
            invoker=_fixture_evolution_invoker,
            provider="fixture",
            model="fixture-v1",
            smoke_validator=lambda _attempt: None,
            match_executor=_fixture_match_executor,
        )
        assert len(result.summaries) == 1 and result.summaries[0].passed
        after = connection.execute(
            """SELECT count(*) AS agents, (SELECT count(*) FROM strategies WHERE generation_id = %s) AS strategies,
                      (SELECT count(*) FROM selection_decisions WHERE generation_id = %s) AS decisions,
                      (SELECT count(*) FROM matches WHERE generation_id = %s) AS matches
               FROM agents WHERE creation_generation_id = %s""",
            (crashed_generation["id"], crashed_generation["id"], crashed_generation["id"], crashed_generation["id"]),
        ).fetchone()
        # The crash-and-resume path must not duplicate rows. The retried
        # generation (number 4) also benchmarks each new candidate against its
        # panel of recent generated (non-fixture) opposite-role parents: two
        # generated attackers (generations 2 and 3) for the new defender, and
        # two generated defenders (generations 2 and 3) for the new attacker.
        # That is 1 head-to-head match + 2 + 2 benchmark matches = 5, still
        # created exactly once despite the simulated crash and retry.
        assert after == {"agents": 2, "strategies": 2, "decisions": 1, "matches": 5}
