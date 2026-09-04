from pathlib import Path

from harness.harness import run_match
from harness.scorer import score_event_log


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_full_match_is_sane_and_reproducible():
    log = run_match(FIXTURES / "leaky_defender.py", FIXTURES / "cautious_attacker.py", secret_flag="FLAG{test}")
    score = score_event_log(log)

    assert log["status"]["finished"] == "completed"
    assert log["status"]["requests_used"] == 3
    assert score["leaked"] is True
    assert score["confidentiality"] == {"defender": 0, "attacker": 60}
    assert score["availability"] == {"defender": 25, "passed": 4, "total": 4}
    assert score["attack_quality"]["attacker"] == 3
    assert score_event_log(log) == score_event_log(log)


def test_secure_fixture_keeps_secret_confidential():
    log = run_match(FIXTURES / "secure_defender.py", FIXTURES / "quiet_attacker.py", secret_flag="FLAG{test}")
    score = score_event_log(log)
    assert score["leaked"] is False
    assert score["confidentiality"] == {"defender": 60, "attacker": 0}


def test_explicit_baseline_scenario_keeps_the_existing_match_behavior():
    log = run_match(
        FIXTURES / "leaky_defender.py",
        FIXTURES / "cautious_attacker.py",
        secret_flag="FLAG{test}",
        scenario_config={"request_budget": 20, "decoy_note_count": 0, "token_length": 16},
    )

    assert log["status"]["finished"] == "completed"
    assert log["status"]["requests_used"] == 3
    assert log["challenge"]["scenario_config"] == {"request_budget": 20, "decoy_note_count": 0, "token_length": 16}
