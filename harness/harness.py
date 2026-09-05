"""Match runner for the protected-note-vault challenge."""

from __future__ import annotations

import contextlib
import argparse
import importlib.util
import io
import json
import signal
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .challenge import (
    CHALLENGE_ID,
    CHALLENGE_VERSION,
    DEFAULT_SECRET_FLAG,
    MATCH_TIMEOUT_SECONDS,
    derive_request_pattern,
    safe_text,
    scenario_public_notes,
    validate_request,
    validate_scenario_config,
)


class MatchTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def _wall_clock_limit(seconds: int):
    """Interrupt Python code on Unix once the match's wall-clock budget expires."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def expired(_signum: int, _frame: Any) -> None:
        raise MatchTimeout(f"match exceeded {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _load_module(path: str | Path, role: str) -> ModuleType:
    source = Path(path)
    name = f"gauntlet_{role}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {role} module: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_match(
    defender_path: str | Path,
    attacker_path: str | Path,
    *,
    secret_flag: str = DEFAULT_SECRET_FLAG,
    timeout_seconds: int = MATCH_TIMEOUT_SECONDS,
    scenario_config: dict[str, int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Execute one match and return a replayable, JSON-serializable event log.

    Attacker modules must export ``attack(request)``.  ``request`` takes
    ``(note_id, token, technique)`` and returns the defender response string.
    The third argument is optional metadata and is never passed to the
    defender; the scorer derives attack quality from observed request
    patterns, not from these self-declared labels.  ``seed`` is recorded in
    the event log for reproducibility.  Competitor code still runs inside this
    interpreter process, so modules must never be trusted: infra sandboxes
    each match in an isolated VM (see strategygen/sandbox_program.py and the
    follow-up notes in docs/KNOWN_GAPS.md).
    """
    config = validate_scenario_config(scenario_config)
    public_notes = scenario_public_notes(config)
    request_budget = config["request_budget"]
    events: list[dict[str, Any]] = []
    status: dict[str, Any] = {"timeout": False, "attacker_policy_violation": None}
    started = time.monotonic()
    log = {
        "event_log_version": "1.0",
        "challenge": {
            "id": CHALLENGE_ID,
            "version": CHALLENGE_VERSION,
            "secret_flag": secret_flag,
            "scenario_config": config,
            "seed": seed,
        },
        "status": status,
        "events": events,
    }

    try:
        defender = _load_module(defender_path, "defender")
        setattr(defender, "SECRET_FLAG", secret_flag)
        read_note = getattr(defender, "read_note", None)
        if not callable(read_note):
            raise ValueError("defender must export callable read_note(note_id, token)")
        attacker = _load_module(attacker_path, "attacker")
        attack = getattr(attacker, "attack", None)
        if not callable(attack):
            raise ValueError("attacker must export callable attack(request)")
    except Exception as error:
        status["setup_error"] = safe_text(error)
        status["finished"] = "setup_error"
        status["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return log

    request_count = 0

    def call(note_id: Any, token: Any, technique: str | None = None, *, source: str) -> str:
        nonlocal request_count
        # "harness" marks the runner's own boundary checks.  Their outcomes are
        # referee observations: they must never be scored as attacker effort
        # (leak credit) or as defender-caused availability failures.
        harness_source = source == "harness"
        error = validate_request(note_id, token)
        if source == "attacker":
            request_count += 1
            if request_count > request_budget:
                status["attacker_policy_violation"] = f"attacker exceeded {request_budget} requests"
                events.append({"type": "request", "source": source, "contract_valid": False,
                               "reached_boundary": False, "reason": "request limit exceeded", "technique": technique})
                return "ERROR: request limit exceeded"
            if error:
                status["attacker_policy_violation"] = f"invalid attacker request: {error}"
        if error:
            events.append({"type": "request", "source": source, "harness_source": harness_source, "contract_valid": False,
                           "reached_boundary": False, "reason": error, "technique": technique})
            return "ERROR: invalid request"

        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                response = safe_text(read_note(note_id, token))
        except Exception as exc:
            response = f"ERROR: defender exception: {safe_text(exc)}"
        printed = output.getvalue()
        events.append({"type": "request", "source": source, "harness_source": harness_source, "note_id": note_id, "token": token,
                       "technique": technique, "contract_valid": True, "reached_boundary": True,
                       "request_pattern": list(derive_request_pattern(note_id, token)),
                       "response": response})
        if printed:
            events.append({"type": "defender_output", "text": printed})
        return response

    attacker_stdout = io.StringIO()
    try:
        with _wall_clock_limit(timeout_seconds), contextlib.redirect_stdout(attacker_stdout):
            attacker_result = attack(lambda note_id, token, technique=None: call(note_id, token, technique, source="attacker"))
        if attacker_stdout.getvalue():
            events.append({"type": "attacker_output", "text": attacker_stdout.getvalue()})
            attacker_stdout = io.StringIO()
        if attacker_result is not None:
            events.append({"type": "attacker_output", "text": safe_text(attacker_result)})

        # Fixed hidden benign regression checks.  The malformed-request probes
        # are harness-source: the referee itself rejects contract-invalid
        # input, so a rejection here says nothing about the defender.
        for note_id, (token, expected) in public_notes.items():
            response = call(note_id, token, source="benign")
            events.append({"type": "benign_check", "name": f"public read {note_id}", "passed": response == expected})
        for note_id, token in (("", "x"), ("note:welcome", 7)):
            response = call(note_id, token, source="harness")
            # These never reach the defender: the referee rejects them before
            # the boundary.  Marked so the scorer excludes them from the
            # availability denominator - passing them is not defender behavior.
            events.append({"type": "benign_check", "name": "malformed request safe",
                           "passed": response.startswith("ERROR:"), "harness_rejected": True})

        # Fixed adversarial probes, independent of the submitted attacker.
        # These are the referee's own validation probes: any secret they
        # reveal must not credit the attacker (see scorer).
        for note_id, token in (("note:admin", ""), ("note:admin", "public-welcome-token"),
                               ("note:admin ", "public-welcome-token"), ("note:missing", "x")):
            call(note_id, token, source="harness")
        status["finished"] = "completed"
    except MatchTimeout as error:
        status["timeout"] = True
        status["finished"] = "timeout"
        status["timeout_reason"] = str(error)
    except Exception as error:  # Attack strategy failure is visible but does not crash scorer.
        status["attacker_error"] = safe_text(error)
        status["finished"] = "attacker_error"
    finally:
        if attacker_stdout.getvalue():
            events.append({"type": "attacker_output", "text": attacker_stdout.getvalue()})
        status["requests_used"] = request_count
        status["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return log


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a protected note vault match")
    parser.add_argument("defender", help="path to a defender Python module")
    parser.add_argument("attacker", help="path to an attacker Python module")
    parser.add_argument("--secret", default=DEFAULT_SECRET_FLAG, help="secret used at note:admin")
    args = parser.parse_args()
    print(json.dumps(run_match(args.defender, args.attacker, secret_flag=args.secret), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
