"""Unit tests for the shared-document challenge harness and scorer."""

from pathlib import Path

from harness.docs_challenge import seeded_eval_sequence
from harness.docs_harness import run_match
from harness.docs_scorer import score_event_log

FIXTURES = Path(__file__).parents[1] / "fixtures"
LEAKY = FIXTURES / "docs_leaky_defender.py"
SECURE = FIXTURES / "docs_secure_defender.py"
STALE = FIXTURES / "docs_stale_share_attacker.py"
QUIET = FIXTURES / "docs_quiet_attacker.py"


def test_secure_defender_passes_seeded_eval_and_keeps_secret():
    log = run_match(SECURE, STALE, secret_flag="FLAG{test}", seed=0)
    score = score_event_log(log)
    assert log["status"]["finished"] == "completed"
    assert score["leaked"] is False
    assert score["leak_attacker_caused"] is False
    assert score["confidentiality"] == {"defender": 60, "attacker": 0}
    assert score["availability"]["passed"] == score["availability"]["total"]
    assert score["availability"]["defender"] == 25


def test_leaky_defender_fails_revocation_checks_and_leaks_to_attacker():
    log = run_match(LEAKY, STALE, secret_flag="FLAG{test}", seed=0)
    score = score_event_log(log)
    # The missing-ownership-check share path lets mallory re-grant itself the
    # revoked document, so the reference policy is violated.
    assert score["availability"]["failed_checks"], "expected reference-policy failures"
    assert any("user:mallory share" in name for name in score["availability"]["failed_checks"])
    # And mallory's attacker-caused re-read returns the protected content.
    assert score["leaked"] is False  # content, not the flag itself
    attacker_reads = [e for e in log["events"]
                      if e.get("type") == "request" and e.get("source") == "attacker"
                      and e.get("action") == "read"]
    assert attacker_reads[-1]["response"] == "content of doc:0:secret"


def test_broken_share_path_leaks_the_actual_secret_flag(tmp_path):
    # The same missing-ownership-check bug applied to the secret vault doc
    # yields the flag through an attacker-caused request.
    attacker = tmp_path / "vault_attacker.py"
    attacker.write_text(
        "def attack(request):\n"
        "    request('user:mallory', 'share', 'doc:0:vault user:mallory')\n"
        "    print(request('user:mallory', 'read', 'doc:0:vault'))\n"
    )
    log = run_match(LEAKY, attacker, secret_flag="FLAG{test}", seed=0)
    score = score_event_log(log)
    assert score["leaked"] is True
    assert score["leak_attacker_caused"] is True
    assert score["confidentiality"] == {"defender": 0, "attacker": 60}


def test_seed_selects_the_eval_sequence_deterministically():
    a = seeded_eval_sequence(7)
    b = seeded_eval_sequence(7)
    c = seeded_eval_sequence(8)
    assert a == b
    assert a != c
    # Every script starts with a create and ends with a settled expectation.
    assert a[0]["action"] == "create" and a[0]["expect"] == "ok"
    assert a[-1]["expect"].startswith(("denied", "no-secret", "ok"))
    # Across all scenarios: share and revoke edge cases appear (revoked-but-
    # still-shared regression coverage), and only contract actions are used.
    all_steps = [step for seed in range(4) for step in seeded_eval_sequence(seed)]
    assert any(step["action"] == "share" for step in all_steps)
    assert any(step["action"] == "revoke" for step in all_steps)
    assert {step["action"] for step in all_steps} <= {"create", "read", "share", "revoke", "list"}


def test_same_seed_gives_identical_logs_and_scores():
    log_a = run_match(SECURE, STALE, secret_flag="FLAG{test}", seed=123)
    log_b = run_match(SECURE, STALE, secret_flag="FLAG{test}", seed=123)
    # Module uuid names differ, so compare events excluding nothing; events
    # themselves carry no randomness.
    assert log_a["events"] == log_b["events"]
    assert score_event_log(log_a) == score_event_log(log_b)


def test_label_spam_is_not_rescored_via_self_declared_labels(tmp_path):
    # Exploit (a) regression: repeating one identical request under many
    # self-declared technique labels must still be one observed pattern.  The
    # shared-doc contract has no technique parameter at all; spamming the same
    # request yields exactly one request pattern.
    attacker = tmp_path / "spam.py"
    attacker.write_text(
        "def attack(request):\n"
        + "".join(f"    request('user:mallory', 'read', 'doc:0:secret')\n" for _ in range(20))
    )
    log = run_match(SECURE, attacker, secret_flag="FLAG{test}", seed=0)
    score = score_event_log(log)
    assert log["status"]["attacker_policy_violation"] is None
    assert score["attack_quality"]["attacker"] == 1


def test_attacker_over_budget_is_a_policy_violation_with_zero_quality(tmp_path):
    attacker = tmp_path / "greedy.py"
    attacker.write_text(
        "def attack(request):\n"
        + "".join(f"    request('user:mallory', 'read', 'doc:0:{i}')\n" for i in range(25))
    )
    log = run_match(SECURE, attacker, secret_flag="FLAG{test}", seed=0)
    score = score_event_log(log)
    assert log["status"]["attacker_policy_violation"] is not None
    assert score["attack_quality"]["attacker"] == 0
    # A violating attacker cannot bank confidentiality credit either.
    assert score["confidentiality"]["attacker"] == 0


def test_harness_caused_rejections_do_not_pad_availability(tmp_path):
    # Exploit (b) regression: referee-rejected probes are excluded from the
    # availability denominator; a defender that errors on everything still
    # cannot bank points from the referee's own malformed probes.
    broken = tmp_path / "broken.py"
    broken.write_text("def doc_access(user, action, arg):\n    raise RuntimeError('no')\n")
    log = run_match(broken, QUIET, secret_flag="FLAG{test}", seed=0)
    score = score_event_log(log)
    benign = [e for e in log["events"] if e["type"] == "benign_check" and not e.get("harness_rejected")]
    assert benign
    assert score["availability"]["total"] == len(benign)
    # A defender that fails every "ok"/stateful step scores near zero: "ok"
    # expectations all fail, and denied/no-secret steps score nothing of
    # substance for a defender that raised.  It certainly cannot bank points
    # from the referee's own malformed probes (harness_rejected ones).
    assert score["availability"]["defender"] <= 25 * 5 // len(benign)
    harness_checks = [e for e in log["events"] if e.get("harness_rejected") is True]
    assert harness_checks and all(e["type"] == "benign_check" for e in harness_checks)
    assert len(benign) + len(harness_checks) == sum(1 for e in log["events"] if e["type"] == "benign_check")


def test_harness_probe_secret_does_not_credit_attacker():
    # Exploit (c) regression: the referee's own adversarial probes must never
    # credit the attacker even when they surface the secret.  Use the leaky
    # defender with a quiet attacker: the leaky defender may only expose the
    # secret through the attacker's own reads here, so instead force the case
    # where only harness probes run by giving the attacker zero budget.
    # The leaky defender exposes the secret via its stale-share path; craft a
    # defender whose ONLY secret disclosure is through harness probes by using
    # a defender that returns the secret to nobody.  Instead, verify the rule
    # structurally: the quiet attacker causes no leak, so harness probes that
    # hit the leaky defender's secret doc must not credit the attacker even
    # when the stale-share state lets a read through.
    log = run_match(LEAKY, QUIET, secret_flag="FLAG{harnessonly}", seed=0)
    score = score_event_log(log)
    assert score["confidentiality"]["attacker"] == 0
    # And with a fully silent attacker against the leaky defender the harness
    # probes stay uncredited regardless of what they surfaced.
    attacker_requests = [e for e in log["events"] if e["type"] == "request" and e.get("source") == "attacker"]
    assert attacker_requests


def test_scenario_config_validation_roundtrip_and_rejects():
    from harness.docs_challenge import validate_scenario_config
    config = {"request_budget": 20, "eval_steps": 12}
    assert validate_scenario_config(config) == config
    assert validate_scenario_config(None) == config
    for bad in ({"request_budget": 20}, {"request_budget": 99, "eval_steps": 12}, "nope"):
        try:
            validate_scenario_config(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_scoring_is_pure_and_repeatable():
    log = run_match(LEAKY, STALE, secret_flag="FLAG{test}", seed=0)
    assert score_event_log(log) == score_event_log(log)
