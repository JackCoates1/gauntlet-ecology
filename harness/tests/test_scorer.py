import json

from harness.scorer import SCORER_VERSION, score_event_log


def test_leak_overrides_confidentiality_and_counts_distinct_techniques():
    event_log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"timeout": False, "attacker_policy_violation": None},
        "events": [
            {"type": "request", "source": "attacker", "contract_valid": True, "reached_boundary": True,
             "technique": "admin-id-boundary", "response": "no"},
            {"type": "request", "source": "attacker", "contract_valid": True, "reached_boundary": True,
             "technique": "admin-id-boundary", "response": "no"},
            {"type": "request", "source": "attacker", "contract_valid": True, "reached_boundary": True,
             "technique": "token-case-boundary", "response": "FLAG{x}"},
            {"type": "benign_check", "passed": True},
            {"type": "benign_check", "passed": True},
            {"type": "benign_check", "passed": False},
            {"type": "benign_check", "passed": True},
        ],
    }

    score = score_event_log(event_log)

    assert score["scorer_version"] == SCORER_VERSION
    assert score["confidentiality"] == {"defender": 0, "attacker": 60}
    assert score["availability"] == {"defender": 18, "passed": 3, "total": 4}
    assert score["attack_quality"] == {"attacker": 2, "techniques": ["admin-id-boundary", "token-case-boundary"]}
    assert score["total"] == 80


def test_policy_violation_zeroes_attack_quality():
    log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"timeout": False, "attacker_policy_violation": "attacker exceeded 20 requests"},
        "events": [
            {"type": "request", "source": "attacker", "contract_valid": True, "reached_boundary": True,
             "technique": "admin-id-boundary", "response": "no"},
            {"type": "benign_check", "passed": True},
        ],
    }
    assert score_event_log(log)["attack_quality"]["attacker"] == 0


def test_score_is_byte_identical_for_same_event_log():
    log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {},
        "events": [{"type": "benign_check", "passed": True}],
    }
    first = json.dumps(score_event_log(log), sort_keys=True, separators=(",", ":"))
    second = json.dumps(score_event_log(log), sort_keys=True, separators=(",", ":"))
    assert first == second
