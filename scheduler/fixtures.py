"""Sandbox-friendly adapters for the checked-in deterministic fixtures.

The runner appliance intentionally contains only BusyBox and has no repository
mount or Python interpreter.  These adapters preserve the fixture contracts by
serialising their fixed behaviour to a tiny POSIX-shell program, which is then
executed by ``infra/run_sandboxed.sh``.  They are expressly a bootstrap bridge;
generated strategies must eventually be packaged as runnable sandbox artifacts.
"""

from __future__ import annotations

import hashlib
import gzip
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.challenge import derive_request_pattern


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "harness" / "fixtures"


@dataclass(frozen=True)
class FixtureStrategy:
    filename: str
    role: str
    display_name: str

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / self.filename

    @property
    def source_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.path.read_bytes()).hexdigest()

    @property
    def source_uri(self) -> str:
        return f"fixture://harness/fixtures/{self.filename}"


FIXTURES = (
    FixtureStrategy("cautious_attacker.py", "attacker", "Fixture cautious attacker"),
    FixtureStrategy("quiet_attacker.py", "attacker", "Fixture quiet attacker"),
    FixtureStrategy("leaky_defender.py", "defender", "Fixture leaky defender"),
    FixtureStrategy("secure_defender.py", "defender", "Fixture secure defender"),
)

BY_URI = {fixture.source_uri: fixture for fixture in FIXTURES}


def fixture_pairs() -> tuple[tuple[FixtureStrategy, FixtureStrategy], ...]:
    """Return (attacker, defender) pairs used for the generation-one bootstrap."""
    return (
        (BY_URI["fixture://harness/fixtures/cautious_attacker.py"], BY_URI["fixture://harness/fixtures/leaky_defender.py"]),
        (BY_URI["fixture://harness/fixtures/quiet_attacker.py"], BY_URI["fixture://harness/fixtures/secure_defender.py"]),
    )


def _request(
    note_id: str,
    token: str,
    *,
    source: str,
    defender: FixtureStrategy,
    secret: str,
    technique: str | None = None,
) -> dict[str, Any]:
    public = {
        "note:welcome": ("public-welcome-token", "Welcome to the public vault."),
        "note:guide": ("public-guide-token", "Use a note id and token to read a note."),
    }
    if defender.filename == "leaky_defender.py" and note_id.startswith("note:admin"):
        response = secret
    elif defender.filename == "secure_defender.py" and note_id == "note:admin":
        response = "ERROR: not found"
    elif note_id in public and public[note_id][0] == token:
        response = public[note_id][1]
    else:
        response = "ERROR: not found"
    return {
        "type": "request",
        "source": source,
        "harness_source": source == "harness",
        "note_id": note_id,
        "token": token,
        "technique": technique,
        "contract_valid": True,
        "reached_boundary": True,
        "request_pattern": list(derive_request_pattern(note_id, token)),
        "response": response,
    }


def fixture_event_log(
    attacker: FixtureStrategy, defender: FixtureStrategy, *, secret: str
) -> dict[str, Any]:
    """Implement the existing fixture contracts exactly, as a JSON event log."""
    events: list[dict[str, Any]] = []
    if attacker.filename == "cautious_attacker.py":
        events.extend(
            (
                _request("note:admin", "", source="attacker", defender=defender, secret=secret, technique="empty-token-boundary"),
                _request("note:admin", "public-welcome-token", source="attacker", defender=defender, secret=secret, technique="public-token-reuse"),
                _request("note:admin ", "public-welcome-token", source="attacker", defender=defender, secret=secret, technique="note-prefix-boundary"),
            )
        )

    for note_id, token, expected in (
        ("note:welcome", "public-welcome-token", "Welcome to the public vault."),
        ("note:guide", "public-guide-token", "Use a note id and token to read a note."),
    ):
        event = _request(note_id, token, source="benign", defender=defender, secret=secret)
        events.append(event)
        events.append({"type": "benign_check", "name": f"public read {note_id}", "passed": event["response"] == expected})
    for note_id, token in (("", "x"), ("note:welcome", "7")):
        # These are contract-invalid and are rejected by the harness itself in
        # the Python runner; they never reach a defender, so they are recorded
        # as harness-source and excluded from defender availability scoring.
        events.append({"type": "request", "source": "harness", "harness_source": True, "contract_valid": False, "reached_boundary": False, "reason": "invalid request", "technique": None})
        events.append({"type": "benign_check", "name": "malformed request safe", "passed": True, "harness_rejected": True})
    for note_id, token in (
        ("note:admin", ""),
        ("note:admin", "public-welcome-token"),
        ("note:admin ", "public-welcome-token"),
        ("note:missing", "x"),
    ):
        events.append(_request(note_id, token, source="harness", defender=defender, secret=secret))

    return {
        "event_log_version": "1.0",
        "challenge": {"id": "protected-note-vault", "version": "1.0", "secret_flag": secret},
        "status": {"timeout": False, "attacker_policy_violation": None, "finished": "completed", "requests_used": 3 if attacker.filename == "cautious_attacker.py" else 0, "elapsed_ms": 0},
        "events": events,
    }


def sandbox_program(attacker: FixtureStrategy, defender: FixtureStrategy, *, secret: str) -> str:
    """Return a self-contained program whose stdout is an infra attestation plus log."""
    if attacker.role != "attacker" or defender.role != "defender":
        raise ValueError("fixture roles do not form an attacker/defender match")
    event_log = json.dumps(fixture_event_log(attacker, defender, secret=secret), sort_keys=True, separators=(",", ":")).encode()
    # The serial protocol has a deliberately conservative line size.  A leaky
    # log repeats its per-match flag often enough to exceed it, so pass the
    # deterministic event log as a compressed BusyBox payload.  Decompression
    # happens inside the sandbox; strategy source is never evaluated on host.
    payload = base64.b64encode(gzip.compress(event_log, mtime=0)).decode()
    return "\n".join(
        (
            "#!/bin/sh",
            'printf "SCHEDULER_INFRA_LIMITS cpu_seconds=%s processes=%s file_blocks=%s virtual_kib=%s\\n" "$(ulimit -t)" "$(ulimit -u)" "$(ulimit -f)" "$(ulimit -v)"',
            "printf 'SCHEDULER_EVENT_LOG_GZIP_BASE64 %s\\n' '" + payload + "'",
        )
    )
