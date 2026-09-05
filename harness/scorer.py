"""Pure, deterministic scoring for protected-note-vault event logs."""

from __future__ import annotations

from typing import Any, Mapping

SCORER_VERSION = "2.0"


def score_event_log(event_log: Mapping[str, Any]) -> dict[str, Any]:
    """Return the score for a stored log, without running untrusted code.

    Competitive scoring reads only what competitor code actually caused:

    - Attacker confidentiality credit requires the secret to have appeared in
      observable output while an attacker-sourced request was in play, and it
      is void if the attacker committed a policy violation.  Secret text the
      harness's own validation probes extracted never credits the attacker.
    - Availability counts only defender-observed check outcomes.  Requests the
      harness itself rejected (contract-invalid malformed probes) never count
      as defender-caused failures, so refusing all traffic cannot bank points.
    - Attack quality is derived from the normalized request patterns the
      attacker actually sent, never from self-declared technique labels.
    """
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
        leak_was_attacker_caused = any(event.get("response") and secret in str(event.get("response")) for event in attacker_requests)
    else:
        leak_was_attacker_caused = False
    if attacker_violation:
        leak_was_attacker_caused = False
    confidentiality = 0 if leak_was_attacker_caused else 60

    # Only checks that actually exercised the defender count.  The harness's
    # own malformed-request probes are rejected by the referee before the
    # boundary, so "passing" them is not defender behavior and must not pad
    # availability for a defender that refuses everything.
    benign = [
        event
        for event in events
        if event.get("type") == "benign_check" and event.get("harness_rejected") is not True
    ]
    benign_total = len(benign)
    benign_passed = sum(bool(event.get("passed")) for event in benign)
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
    if not benign_total:
        reasons.append("no benign checks were recorded")

    return {
        "scorer_version": SCORER_VERSION,
        "challenge_id": event_log.get("challenge", {}).get("id"),
        "total": confidentiality + availability + attack_quality,
        "confidentiality": {"defender": confidentiality, "attacker": 60 if leak_was_attacker_caused else 0},
        "availability": {"defender": availability, "passed": benign_passed, "total": benign_total},
        "attack_quality": {"attacker": attack_quality, "request_patterns": sorted("note_id=" + str(pair[0]) + " token=" + str(pair[1]) for pair in patterns)},
        "leaked": leaked,
        "leak_attacker_caused": leak_was_attacker_caused,
        "reasons": reasons,
    }
