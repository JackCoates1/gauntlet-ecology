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
    assert score["leak_attacker_caused"] is True
    assert score["confidentiality"] == {"defender": 0, "attacker": 60}
    assert score["availability"] == {"defender": 25, "passed": 2, "total": 2}
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


def _write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source)
    return path


def test_label_spam_scores_one_distinct_pattern(tmp_path):
    # Exploit (a): one identical request repeated under 15 self-declared
    # labels must score exactly one attack-quality point, not fifteen.
    labels = [
        "admin-id-boundary", "token-case-boundary", "token-whitespace-boundary",
        "empty-token-boundary", "unknown-note-boundary", "note-prefix-boundary",
        "unicode-token-boundary", "long-token-boundary", "public-token-reuse",
        "admin-token-guess", "separator-boundary", "null-like-token-boundary",
        "numeric-token-boundary", "path-like-note-boundary", "duplicate-separator-boundary",
    ]
    attacker = _write(tmp_path, "label_spammer.py", "def attack(request):\n" + "".join(
        f"    request('note:admin', 'zzz', {label!r})\n" for label in labels
    ))
    log = run_match(FIXTURES / "secure_defender.py", attacker, secret_flag="FLAG{test}")
    score = score_event_log(log)
    assert log["status"]["finished"] == "completed"
    assert score["attack_quality"]["attacker"] == 1
    assert score["total"] == 60 + 25 + 1


def test_genuinely_distinct_probes_still_score_quality(tmp_path):
    attacker = _write(tmp_path, "varied.py", """def attack(request):
    request("note:admin", "")
    request("note:admin", "public-welcome-token")
    request("note:admin ", "public-welcome-token")
    request("note:missing", "x")
""")
    log = run_match(FIXTURES / "secure_defender.py", attacker, secret_flag="FLAG{test}")
    score = score_event_log(log)
    assert score["attack_quality"]["attacker"] == 4


def test_deny_all_defender_cannot_bank_harness_rejections(tmp_path):
    # Exploit (b): a defender that refuses all traffic scores zero
    # availability; the harness's own malformed-request rejections must not
    # count as defender-caused availability wins.
    defender = _write(tmp_path, "deny_all.py", 'def read_note(note_id, token):\n    return "ERROR: not found"\n')
    log = run_match(defender, FIXTURES / "quiet_attacker.py", secret_flag="FLAG{test}")
    score = score_event_log(log)
    assert log["status"]["finished"] == "completed"
    assert score["availability"] == {"defender": 0, "passed": 0, "total": 2}
    assert score["total"] == 60


def test_harness_probe_leak_does_not_credit_a_do_nothing_attacker(tmp_path):
    # Exploit (c): the defender leaks only through the harness's fixed
    # adversarial probes; the attacker made no requests and earns nothing.
    defender = _write(tmp_path, "probe_leak.py", """SECRET_FLAG = ""

def read_note(note_id, token):
    if note_id == "note:admin" and token == "":
        return SECRET_FLAG
    return "ERROR: not found"
""")
    log = run_match(defender, FIXTURES / "quiet_attacker.py", secret_flag="FLAG{test}")
    score = score_event_log(log)
    assert score["leaked"] is True
    assert score["leak_attacker_caused"] is False
    assert score["confidentiality"] == {"defender": 60, "attacker": 0}
    assert score["attack_quality"]["attacker"] == 0


def test_seed_is_recorded_in_the_event_log():
    log = run_match(FIXTURES / "secure_defender.py", FIXTURES / "quiet_attacker.py", secret_flag="FLAG{test}", seed=4242)
    assert log["challenge"]["seed"] == 4242
