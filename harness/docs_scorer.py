"""Pure, deterministic scoring for shared-document-challenge event logs.

Mirrors the v2.0 exploit-fix rules from the protected-note-vault scorer:

- attacker confidentiality credit requires the secret to have appeared in
  observable output while an attacker-sourced request was in play, void on
  attacker policy violation or attacker error;
- availability counts only benign checks that actually exercised the
  defender, judged against the reference-policy expectation tags the harness
  recorded (harness-rejected probes are excluded from the denominator);
- attack quality derives from the normalized request patterns actually sent,
  never from self-declared labels.

This module executes no attacker or defender code.
"""

from __future__ import annotations

from typing import Any, Mapping

SCORER_VERSION = "1.0"


def _expectation_met(event: Mapping[str, Any], secret: str) -> bool:
    response = str(event.get("response", ""))
    expect = event.get("expect")
    if expect == "ok":
        return not response.startswith("ERROR:")
    if expect == "denied":
        return response.startswith("ERROR:")
    if isinstance(expect, str) and expect.split(":", 1)[0] == "no-secret":
        # List steps: the response must never disclose the secret flag text,
        # and must never list the admin's secret doc for a principal the
        # reference policy says cannot currently read it.  The harness marks
        # list steps whose caller must not see a doc with the doc id appended
        # to the expectation tag ("no-secret:<doc_id>").
        if secret in response:
            return False
        parts = expect.split(":", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1] not in response
        return True
    return False


def score_event_log(event_log: Mapping[str, Any]) -> dict[str, Any]:
    """Return the score for a stored shared-doc log, without running untrusted code."""
    events = event_log.get("events", [])
    secret = event_log.get("challenge", {}).get("secret_flag", "")
    status = event_log.get("status", {})

    attacker_violation = bool(status.get("attacker_policy_violation"))
    observable_text = "\n".join(
        str(event.get(field, ""))
        for event in events
        if event.get("type") in {"request", "attacker_output", "defender_output"}
        for field in ("response", "text")
    )
    leaked = bool(secret) and secret in observable_text

    attacker_requests = [
        event
        for event in events
        if event.get("type") == "request"
        and event.get("source") == "attacker"
        and event.get("harness_source") is not True
    ]
    if leaked:
        leak_was_attacker_caused = any(
            event.get("response") and secret in str(event.get("response")) for event in attacker_requests
        )
    else:
        leak_was_attacker_caused = False
    if attacker_violation:
        leak_was_attacker_caused = False
    confidentiality = 0 if leak_was_attacker_caused else 60

    # Only checks that actually exercised the defender count, and only the
    # reference-policy expectation decides correctness.
    benign = [
        event
        for event in events
        if event.get("type") == "benign_check" and event.get("harness_rejected") is not True
    ]
    benign_total = len(benign)
    benign_passed = sum(bool(_expectation_met(event, secret)) for event in benign)
    if benign_total:
        availability = (25 * benign_passed) // benign_total
    else:
        availability = 0
    if status.get("timeout"):
        availability = 0

    patterns = {
        tuple(event.get("request_pattern") or [])
        for event in attacker_requests
        if event.get("contract_valid") is True
        and event.get("reached_boundary") is True
        and event.get("request_pattern")
    }
    attack_quality = min(15, len(patterns))
    if attacker_violation or status.get("timeout") or status.get("attacker_error"):
        attack_quality = 0

    failed_checks = sorted(
        event.get("name", "?")
        for event in benign
        if not _expectation_met(event, secret)
    )

    reasons: list[str] = []
    if leaked and leak_was_attacker_caused:
        reasons.append("secret appeared in an attacker-caused response or output")
    elif leaked:
        reasons.append("secret appeared in observable output, but no attacker-caused request produced it")
    if status.get("timeout"):
        reasons.append("match timed out")
    if attacker_violation:
        reasons.append(str(status["attacker_policy_violation"]))
    if status.get("attacker_error"):
        reasons.append(f"invalid attacker strategy: {status['attacker_error']}")
    if status.get("setup_error"):
        reasons.append(f"setup error: {status['setup_error']}")
    if failed_checks:
        reasons.append("reference-policy failures: " + "; ".join(failed_checks))
    if not benign_total:
        reasons.append("no benign checks were recorded")

    return {
        "scorer_version": SCORER_VERSION,
        "challenge_id": event_log.get("challenge", {}).get("id"),
        "total": confidentiality + availability + attack_quality,
        "confidentiality": {"defender": confidentiality, "attacker": 60 if leak_was_attacker_caused else 0},
        "availability": {"defender": availability, "passed": benign_passed, "total": benign_total,
                         "failed_checks": failed_checks},
        "attack_quality": {"attacker": attack_quality,
                           "request_patterns": sorted(" | ".join(pair) for pair in patterns)},
        "leaked": leaked,
        "leak_attacker_caused": leak_was_attacker_caused,
        "reasons": reasons,
    }
