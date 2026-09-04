from harness.scorer import score_event_log
from scheduler.fixtures import fixture_event_log, fixture_pairs
from scheduler.worker import _redact


def test_fixture_adapter_preserves_checked_in_fixture_score_contracts():
    leaky_pair, secure_pair = fixture_pairs()
    leaky_score = score_event_log(fixture_event_log(*leaky_pair, secret="FLAG{fixture}"))
    secure_score = score_event_log(fixture_event_log(*secure_pair, secret="FLAG{fixture}"))

    assert leaky_score["confidentiality"] == {"defender": 0, "attacker": 60}
    assert leaky_score["availability"]["defender"] == 25
    assert leaky_score["attack_quality"]["attacker"] == 3
    assert secure_score["confidentiality"] == {"defender": 60, "attacker": 0}
    assert secure_score["availability"]["defender"] == 25


def test_event_redaction_makes_adversarial_nul_input_safe_for_jsonb():
    redacted = _redact({"note_id": "note:admin\x00", "token": "FLAG{private}"}, "FLAG{private}")
    assert redacted == {"note_id": "note:admin[NUL]", "token": "[REDACTED]"}
