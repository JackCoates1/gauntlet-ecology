"""Match runner for the shared-document challenge.

Mirrors the isolation and scoring rules established by the referee-fixes
pass (see docs/KNOWN_GAPS.md):

- harness-source probes are referee observations: never leak-credited and
  never counted as defender-caused availability failures;
- contract-invalid attacker probes are rejected by the referee before the
  boundary and are policy violations;
- the secret travels to the defender via module injection, not argv;
- technique-like metadata is recorded but never drives scoring.

Competitor code still runs inside this interpreter process, so modules must
never be trusted: infra sandboxes each match in an isolated VM (see
strategygen/sandbox_program.py and docs/KNOWN_GAPS.md for the known residual
same-interpreter gaps, which this challenge inherits unchanged).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import signal
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .docs_challenge import (
    CHALLENGE_ID,
    CHALLENGE_VERSION,
    DEFAULT_SECRET_FLAG,
    MATCH_TIMEOUT_SECONDS,
    ROLES,
    safe_text,
    seeded_eval_sequence,
    validate_action,
    validate_scenario_config,
)


class MatchTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def _wall_clock_limit(seconds: int):
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
    name = f"gauntlet_docs_{role}_{uuid.uuid4().hex}"
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
    """Execute one shared-doc match and return a replayable event log.

    Defender exports ``doc_access(user, action, arg) -> str``.  Attacker
    exports ``attack(request)`` where ``request(user, action, arg)`` plays
    mallory against the defender; no more than ``request_budget`` calls.
    ``seed`` selects the deterministic benign eval sequence and is recorded
    in the log.  The log intentionally stores the secret because the pure
    scorer needs the original comparison value on replay; logs must be
    handled as sensitive artifacts.
    """
    config = validate_scenario_config(scenario_config)
    request_budget = config["request_budget"]
    seed = int(seed) if seed is not None else 0
    eval_sequence = seeded_eval_sequence(seed, config)
    events: list[dict[str, Any]] = []
    status: dict[str, Any] = {"timeout": False, "attacker_policy_violation": None, "seed": seed}
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
        # The seeded scenario's secret document id, so reference defenders
        # can place the secret in the doc the eval sequence protects.
        setattr(defender, "SECRET_DOC_ID", f"doc:{seed}:vault")
        doc_access = getattr(defender, "doc_access", None)
        if not callable(doc_access):
            raise ValueError("defender must export callable doc_access(user, action, arg)")
        attacker = _load_module(attacker_path, "attacker")
        attack = getattr(attacker, "attack", None)
        if not callable(attack):
            raise ValueError("attacker must export callable attack(request)")
        # Same values for the attacker, so target-directed strategies can
        # find the sensitive/vault documents without probing for their ids.
        setattr(attacker, "SECRET_DOC_ID", f"doc:{seed}:vault")
        # The eval sequence's sensitive (non-secret-content) document.
        setattr(attacker, "SENSITIVE_DOC_ID", f"doc:{seed}:secret")
    except Exception as error:
        status["setup_error"] = safe_text(error)
        status["finished"] = "setup_error"
        status["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return log

    request_count = 0

    def call(user: Any, action: Any, arg: Any, *, source: str) -> str:
        nonlocal request_count
        # "harness" marks the referee's own boundary probes.  Their outcomes
        # are observations only: they must never be scored as attacker effort
        # or as defender-caused availability failures.
        harness_source = source == "harness"
        error = validate_action(action, arg)
        if source == "attacker":
            request_count += 1
            if request_count > request_budget:
                status["attacker_policy_violation"] = f"attacker exceeded {request_budget} requests"
                events.append({"type": "request", "source": source, "contract_valid": False,
                               "reached_boundary": False, "reason": "request limit exceeded"})
                return "ERROR: request limit exceeded"
            if error:
                status["attacker_policy_violation"] = f"invalid attacker request: {error}"
        elif source != "attacker" and user not in ROLES:
            # The referee rejects requests for unknown principals before the
            # boundary; such probes carry no defender-availability signal.
            events.append({"type": "request", "source": source, "harness_source": harness_source,
                           "contract_valid": False, "reached_boundary": False,
                           "reason": "unknown principal", "action": action})
            return "ERROR: invalid request"
        if error:
            events.append({"type": "request", "source": source, "harness_source": harness_source,
                           "contract_valid": False, "reached_boundary": False, "reason": error,
                           "action": action})
            return "ERROR: invalid request"

        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                response = safe_text(doc_access(user, action, arg))
        except Exception as exc:
            response = f"ERROR: defender exception: {safe_text(exc)}"
        printed = output.getvalue()
        events.append({"type": "request", "source": source, "harness_source": harness_source,
                       "user": user, "action": action, "arg": arg,
                       "contract_valid": True, "reached_boundary": True,
                       "request_pattern": [f"action={action} arg={arg}"],
                       "response": response})
        if printed:
            events.append({"type": "defender_output", "text": printed})
        return response

    attacker_stdout = io.StringIO()
    try:
        with _wall_clock_limit(timeout_seconds), contextlib.redirect_stdout(attacker_stdout):
            # Seeded deterministic benign eval sequence FIRST: it settles the
            # shared document state (create/share/revoke/list edge cases) that
            # the attacker then exploits, so attacker strategies probe real
            # state (grants, revocations, listings) rather than an empty store.
            # Each step is scored as a benign check judged against the
            # reference-policy expectation tag the harness recorded.
            for step in eval_sequence:
                response = call(step["user"], step["action"], step["arg"], source="benign")
                events.append({"type": "benign_check", "name": f"{step['user']} {step['action']} {step['arg']}",
                               "response": response, "expect": step["expect"], "harness_rejected": False})

            # Pre-create the admin's secret vault document holding the flag,
            # so attacker strategies can target it via SECRET_DOC_ID without
            # needing create rights on it.  The defender returns whatever a
            # correct implementation would; the scorer only checks whether
            # the flag text surfaced through attacker-caused requests.
            call("user:admin", "create", f"doc:{seed}:vault", source="harness")

            attacker_result = attack(lambda user, action, arg: call(user, action, arg, source="attacker"))
        if attacker_stdout.getvalue():
            events.append({"type": "attacker_output", "text": attacker_stdout.getvalue()})
            attacker_stdout = io.StringIO()
        if attacker_result is not None:
            events.append({"type": "attacker_output", "text": safe_text(attacker_result)})

        # Referee-only malformed probes: never reach the defender, excluded
        # from the availability denominator by the scorer.
        for user, action, arg in (("", "read", "doc:x"), ("user:mallory", "delete", "doc:x")):
            response = call(user, action, arg, source="harness")
            events.append({"type": "benign_check", "name": "malformed request safe",
                           "passed": response.startswith("ERROR:"), "harness_rejected": True})

        # Fixed adversarial referee probes, independent of the attacker.  Any
        # secret they reveal credits nobody (see scorer).
        for user, action, arg in (("user:mallory", "read", "doc:admin-secret"),
                                  ("user:mallory", "list", "user:mallory")):
            call(user, action, arg, source="harness")

        status["finished"] = "completed"
    except MatchTimeout as error:
        status["timeout"] = True
        status["finished"] = "timeout"
        status["timeout_reason"] = str(error)
    except Exception as error:
        status["attacker_error"] = safe_text(error)
        status["finished"] = "attacker_error"
    finally:
        if attacker_stdout.getvalue():
            events.append({"type": "attacker_output", "text": attacker_stdout.getvalue()})
        status["requests_used"] = request_count
        status["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return log


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a shared-document match")
    parser.add_argument("defender", help="path to a defender Python module")
    parser.add_argument("attacker", help="path to an attacker Python module")
    parser.add_argument("--secret", default=DEFAULT_SECRET_FLAG, help="secret stored in the admin doc")
    parser.add_argument("--seed", type=int, default=0, help="deterministic eval-sequence seed")
    args = parser.parse_args()
    print(json.dumps(run_match(args.defender, args.attacker, secret_flag=args.secret, seed=args.seed),
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
