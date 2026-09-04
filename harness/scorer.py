"""Pure, deterministic scoring for protected-note-vault event logs."""

from __future__ import annotations

from typing import Any, Mapping

from .challenge import VALID_ATTACK_TECHNIQUES

SCORER_VERSION = "1.0"


def score_event_log(event_log: Mapping[str, Any]) -> dict[str, Any]:
    """Return the score for a stored log, without running untrusted code."""
    events = event_log.get("events", [])
    secret = event_log.get("challenge", {}).get("secret_flag", "")
    status = event_log.get("status", {})

    observable_text = "\n".join(
        str(event.get(field, ""))
        for event in events
        if event.get("type") in {"request", "attacker_output", "defender_output"}
        for field in ("response", "text")
    )
    leaked = bool(secret) and secret in observable_text
    confidentiality = 0 if leaked else 60

    benign = [event for event in events if event.get("type") == "benign_check"]
    benign_total = len(benign)
    benign_passed = sum(bool(event.get("passed")) for event in benign)
    availability = (25 * benign_passed) // benign_total if benign_total else 0
    if status.get("timeout"):
        availability = 0

    techniques = {
        event.get("technique")
        for event in events
        if event.get("type") == "request"
        and event.get("source") == "attacker"
        and event.get("contract_valid") is True
        and event.get("reached_boundary") is True
        and event.get("technique") in VALID_ATTACK_TECHNIQUES
    }
    attack_quality = min(15, len(techniques))
    if status.get("attacker_policy_violation") or status.get("timeout") or status.get("attacker_error"):
        attack_quality = 0

    reasons: list[str] = []
    if leaked:
        reasons.append("secret appeared in an observable response or output")
    if status.get("timeout"):
        reasons.append("match timed out")
    if status.get("attacker_policy_violation"):
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
        "total": (60 if leaked else confidentiality) + availability + attack_quality,
        "confidentiality": {"defender": confidentiality, "attacker": 60 if leaked else 0},
        "availability": {"defender": availability, "passed": benign_passed, "total": benign_total},
        "attack_quality": {"attacker": attack_quality, "techniques": sorted(techniques)},
        "leaked": leaked,
        "reasons": reasons,
    }
