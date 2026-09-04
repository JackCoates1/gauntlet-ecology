"""Opt-in only: this test consumes real Codex quota and runs the VM sandbox."""

from __future__ import annotations

import os

import pytest

from scheduler.worker import connect
from strategygen.builder import generate_builder_attempt, insert_challenge_version
from strategygen.run import generate_and_run


pytestmark = pytest.mark.skipif(os.environ.get("GAUNTLET_LIVE_CODEX") != "1", reason="set GAUNTLET_LIVE_CODEX=1 to spend Codex quota and use the live sandbox")


def test_live_codex_generation_and_sandbox_match(scheduler_database_url):
    with connect(scheduler_database_url) as connection:
        summary = generate_and_run(connection, number=77, attempts=1)
        assert summary.passed
        assert summary.score is not None


def test_live_codex_builder_creates_a_new_challenge_version(scheduler_database_url, tmp_path):
    attempt = generate_builder_attempt(1, artifact_dir=tmp_path)
    assert attempt.validation.valid, attempt.validation.reason
    with connect(scheduler_database_url) as connection:
        row = insert_challenge_version(connection, attempt)
    assert row["semver"] == "1.0.1"
    assert row["scenario_config"] == attempt.scenario_config
