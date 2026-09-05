import json

from harness.scorer import SCORER_VERSION, score_event_log


def _pattern(note_id, token):
    from harness.challenge import derive_request_pattern

    return list(derive_request_pattern(note_id, token))


def _attacker_request(note_id, token, response="no", harness_source=False, technique=None):
    return {
        "type": "request", "source": "attacker", "harness_source": harness_source,
        "note_id": note_id, "token": token, "technique": technique,
        "contract_valid": True, "reached_boundary": True,
        "request_pattern": _pattern(note_id, token), "response": response,
    }


def test_leak_overrides_confidentiality_and_counts_distinct_patterns():
    event_log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"timeout": False, "attacker_policy_violation": None},
        "events": [
            _attacker_request("note:admin", "aaa"),
            _attacker_request("note:admin", "aaa"),
            _attacker_request("note:admin", "bbb", response="FLAG{x}"),
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
    # One actually-distinct request pattern; the original test counted
    # self-declared labels here, which is exactly the exploit this fix removes.
    assert score["attack_quality"]["attacker"] == 1
    assert score["total"] == 19


def test_repeated_request_under_different_labels_scores_one_pattern():
    # Exploit (a): one identical request repeated under 15 self-declared
    # technique labels must yield exactly one observed request pattern.
    labels = [
        "admin-id-boundary", "token-case-boundary", "token-whitespace-boundary",
        "empty-token-boundary", "unknown-note-boundary", "note-prefix-boundary",
        "unicode-token-boundary", "long-token-boundary", "public-token-reuse",
        "admin-token-guess", "separator-boundary", "null-like-token-boundary",
        "numeric-token-boundary", "path-like-note-boundary", "duplicate-separator-boundary",
    ]
    events = [_attacker_request("note:admin", "zzz", technique=label) for label in labels]
    score = score_event_log({
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {},
        "events": events,
    })
    assert score["attack_quality"]["attacker"] == 1
    assert len(score["attack_quality"]["request_patterns"]) == 1


def test_distinct_request_patterns_score_separately():
    events = [
        _attacker_request("note:admin", ""),
        _attacker_request("note:admin", "public-welcome-token"),
        _attacker_request("note:admin ", "public-welcome-token"),
        _attacker_request("note:missing", "x"),
    ]
    score = score_event_log({
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {},
        "events": events,
    })
    assert score["attack_quality"]["attacker"] == 4


def test_harness_probe_leak_does_not_credit_the_attacker():
    # Exploit (c): the harness's own adversarial probe extracted the secret;
    # the submitted attacker made no requests, so it earns nothing.
    events = [
        {"type": "request", "source": "harness", "harness_source": True, "note_id": "note:admin", "token": "",
         "technique": None, "contract_valid": True, "reached_boundary": True,
         "request_pattern": _pattern("note:admin", ""), "response": "FLAG{x}"},
    ]
    score = score_event_log({
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"finished": "completed"},
        "events": events,
    })
    assert score["leaked"] is True
    assert score["leak_attacker_caused"] is False
    assert score["confidentiality"] == {"defender": 60, "attacker": 0}
    assert score["total"] == 60
    assert "no attacker-caused request produced it" in score["reasons"][0]


def test_attacker_caused_leak_still_credits_the_attacker():
    events = [_attacker_request("note:admin", "guess", response="FLAG{x}")]
    score = score_event_log({
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"finished": "completed"},
        "events": events,
    })
    assert score["leaked"] is True
    assert score["leak_attacker_caused"] is True
    assert score["confidentiality"] == {"defender": 0, "attacker": 60}


def test_harness_rejected_checks_do_not_pad_defender_availability():
    # Exploit (b): a defender that refuses all traffic must score zero
    # availability; the harness-rejected malformed probes are not defender
    # behavior and must not enter the availability denominator.
    events = [
        {"type": "benign_check", "name": "public read note:welcome", "passed": False},
        {"type": "benign_check", "name": "public read note:guide", "passed": False},
        {"type": "benign_check", "name": "malformed request safe", "passed": True, "harness_rejected": True},
        {"type": "benign_check", "name": "malformed request safe", "passed": True, "harness_rejected": True},
    ]
    score = score_event_log({
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {},
        "events": events,
    })
    assert score["availability"] == {"defender": 0, "passed": 0, "total": 2}
    assert score["total"] == 60


def test_policy_violation_zeroes_attack_quality():
    log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"timeout": False, "attacker_policy_violation": "attacker exceeded 20 requests"},
        "events": [_attacker_request("note:admin", "a")],
    }
    assert score_event_log(log)["attack_quality"]["attacker"] == 0


def test_attacker_exception_zeroes_attack_quality_with_a_visible_reason():
    log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {"attacker_error": "broken strategy"},
        "events": [_attacker_request("note:admin", "a")],
    }
    score = score_event_log(log)
    assert score["attack_quality"]["attacker"] == 0
    assert "invalid attacker strategy: broken strategy" in score["reasons"]


def test_score_is_byte_identical_for_same_event_log():
    log = {
        "challenge": {"id": "protected-note-vault", "secret_flag": "FLAG{x}"},
        "status": {},
        "events": [{"type": "benign_check", "passed": True}],
    }
    first = json.dumps(score_event_log(log), sort_keys=True, separators=(",", ":"))
    second = json.dumps(score_event_log(log), sort_keys=True, separators=(",", ":"))
    assert first == second
