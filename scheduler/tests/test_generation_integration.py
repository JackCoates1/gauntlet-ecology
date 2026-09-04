import hashlib
import json
import subprocess

from scheduler.worker import connect, enqueue_job, process_one


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
