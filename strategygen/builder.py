"""Generate, validate, and persist builder-produced challenge scenarios."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import psycopg

from harness.challenge import validate_scenario_config

from .contracts import build_prompt, role_contract
from .generator import (
    CODEX_MODEL,
    DEFAULT_ARTIFACT_DIR,
    GENERATION_TIMEOUT_SECONDS,
    REASONING_EFFORT,
    ValidationResult,
    invoke_codex,
)


SEMVER = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:[+-][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class BuilderAttempt:
    role: str
    attempt: int
    scenario_config: dict[str, int] | None
    raw_output: str
    artifact_path: Path
    started_at: str
    wall_time_ms: int
    validation: ValidationResult
    command_error: str | None = None
    provider: str = "codex exec"
    model: str = CODEX_MODEL

    @property
    def provenance(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": REASONING_EFFORT,
            "timestamp": self.started_at,
            "wall_time_ms": self.wall_time_ms,
            "raw_output_bytes": len(self.raw_output.encode()),
            "command_error": self.command_error,
        }


def extract_json(raw_output: str) -> str:
    """Accept a single JSON fence for recovery, otherwise require JSON-only output."""
    text = raw_output.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(?P<body>.*?)\n?```", text, flags=re.IGNORECASE | re.DOTALL)
    return (fenced.group("body") if fenced else text).strip()


def validate_builder_output(raw_output: str) -> tuple[dict[str, int] | None, ValidationResult]:
    """Parse and range-check untrusted builder JSON before any database access."""
    candidate = extract_json(raw_output)
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError as error:
        return None, ValidationResult(False, f"invalid JSON: {error.msg}")
    try:
        return validate_scenario_config(decoded), ValidationResult(True)
    except ValueError as error:
        return None, ValidationResult(False, str(error))


def generate_builder_attempt(
    attempt: int,
    *,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    invoker: Callable[[str], tuple[str, int, str | None]] = invoke_codex,
    provider: str = "codex exec",
    model: str = CODEX_MODEL,
) -> BuilderAttempt:
    """Generate one JSON scenario, save its raw candidate, then validate it."""
    role_contract("builder")
    started_at = datetime.now(UTC).isoformat()
    raw_output, wall_time_ms, command_error = invoker(build_prompt("builder"))
    scenario_config, validation = validate_builder_output(raw_output)
    if command_error and validation.valid:
        validation = ValidationResult(False, command_error)
        scenario_config = None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"builder-{started_at.replace(':', '').replace('+', '_')}-attempt-{attempt}.json"
    artifact_path.write_text(extract_json(raw_output) + "\n")
    os.chmod(artifact_path, 0o600)
    return BuilderAttempt("builder", attempt, scenario_config, raw_output, artifact_path, started_at, wall_time_ms, validation, command_error, provider, model)


def _next_patch_semver(semver: str) -> str:
    match = SEMVER.fullmatch(semver)
    if match is None:
        raise ValueError(f"cannot bump invalid challenge semver {semver!r}")
    return f"{match['major']}.{match['minor']}.{int(match['patch']) + 1}"


def insert_challenge_version(connection: psycopg.Connection, attempt: BuilderAttempt) -> dict[str, Any]:
    """Append a version row for a valid builder scenario without changing prior rows."""
    if not attempt.validation.valid or attempt.scenario_config is None:
        raise ValueError(f"refusing to persist invalid builder output: {attempt.validation.reason or 'missing scenario_config'}")

    with connection.cursor() as cursor:
        # Serializes patch-number allocation while leaving all historical rows immutable.
        cursor.execute("LOCK TABLE challenge_versions IN EXCLUSIVE MODE")
        cursor.execute("SELECT * FROM challenge_versions ORDER BY created_at DESC, id DESC LIMIT 1")
        latest = cursor.fetchone()
        if latest is None:
            raise ValueError("cannot create a builder version without a base challenge_versions row")
        semver = _next_patch_semver(latest["semver"])
        description = (
            "Protected Note Vault builder scenario: "
            f"{attempt.scenario_config['request_budget']} attacker requests, "
            f"{attempt.scenario_config['decoy_note_count']} public decoys, "
            f"{attempt.scenario_config['token_length']}-character decoy tokens."
        )
        cursor.execute(
            """
            INSERT INTO challenge_versions
                (semver, source_ref, image_digest_ref, seed_policy, scenario_config,
                 scoring_policy_version, public_description)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            RETURNING *
            """,
            (
                semver,
                latest["source_ref"],
                latest["image_digest_ref"],
                json.dumps(latest["seed_policy"]),
                json.dumps(attempt.scenario_config),
                latest["scoring_policy_version"],
                description,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    assert row is not None
    return row
