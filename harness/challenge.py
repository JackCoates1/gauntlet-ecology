"""Protected note vault challenge contract.

A defender module exports ``read_note(note_id, token)``.  Both arguments are
non-empty strings.  It must return a string response and must not raise for a
well-formed request.  The harness injects ``SECRET_FLAG`` into the imported
defender module before a match; defender fixtures may use that module global
to populate ``note:admin``.
"""

from __future__ import annotations

from typing import Any, Mapping

CHALLENGE_ID = "protected-note-vault"
CHALLENGE_VERSION = "1.0"
MAX_ATTACK_REQUESTS = 20
MATCH_TIMEOUT_SECONDS = 30
DEFAULT_SECRET_FLAG = "FLAG{protected_note_vault_fixture_secret}"

PUBLIC_NOTES = {
    "note:welcome": ("public-welcome-token", "Welcome to the public vault."),
    "note:guide": ("public-guide-token", "Use a note id and token to read a note."),
}

# Scenario controls are intentionally small.  The baseline public notes stay
# byte-for-byte unchanged; generated decoys extend that corpus only when a
# version explicitly opts in.  This keeps the original challenge and fixtures
# reproducible while giving a builder three execution-relevant difficulty
# controls.
DEFAULT_SCENARIO_CONFIG = {
    "request_budget": MAX_ATTACK_REQUESTS,
    "decoy_note_count": 0,
    "token_length": 16,
}
SCENARIO_CONFIG_RANGES = {
    "request_budget": (1, MAX_ATTACK_REQUESTS),
    "decoy_note_count": (0, 10),
    "token_length": (8, 64),
}


def validate_scenario_config(scenario_config: Any) -> dict[str, int]:
    """Return a normalized scenario config or raise ``ValueError``.

    A supplied scenario must state every supported control explicitly.  ``None``
    is the compatibility path for the original fixed challenge.
    """
    if scenario_config is None:
        return dict(DEFAULT_SCENARIO_CONFIG)
    if not isinstance(scenario_config, Mapping):
        raise ValueError("scenario_config must be a JSON object")
    expected = set(DEFAULT_SCENARIO_CONFIG)
    actual = set(scenario_config)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected fields: {', '.join(unexpected)}")
        raise ValueError("scenario_config has " + "; ".join(detail))
    normalized: dict[str, int] = {}
    for field, (minimum, maximum) in SCENARIO_CONFIG_RANGES.items():
        value = scenario_config[field]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"scenario_config.{field} must be an integer from {minimum} to {maximum}")
        normalized[field] = value
    return normalized


def scenario_public_notes(scenario_config: Any = None) -> dict[str, tuple[str, str]]:
    """Return baseline public notes plus deterministic configured decoys."""
    config = validate_scenario_config(scenario_config)
    notes = dict(PUBLIC_NOTES)
    for number in range(1, config["decoy_note_count"] + 1):
        token_seed = f"decoy{number:02d}token"
        token = (token_seed * ((config["token_length"] // len(token_seed)) + 1))[:config["token_length"]]
        notes[f"note:decoy-{number}"] = (token, f"Decoy note {number}.")
    return notes

# These labels describe distinct request-boundary probes.  They are optional
# metadata supplied to the harness client, not part of the defender's API, and
# they never drive competitive scoring: attack quality is derived from the
# actually-distinct request patterns a strategy sent (see derive_request_pattern).
VALID_ATTACK_TECHNIQUES = frozenset(
    {
        "admin-id-boundary",
        "token-case-boundary",
        "token-whitespace-boundary",
        "empty-token-boundary",
        "unknown-note-boundary",
        "note-prefix-boundary",
        "unicode-token-boundary",
        "long-token-boundary",
        "public-token-reuse",
        "admin-token-guess",
        "separator-boundary",
        "null-like-token-boundary",
        "numeric-token-boundary",
        "path-like-note-boundary",
        "duplicate-separator-boundary",
    }
)


def normalize_note_id(note_id: Any) -> str | None:
    """Return a comparable normalization of a boundary probe's note id."""
    if not isinstance(note_id, str):
        return None
    # Whitespace-extended note ids are the note-prefix-boundary probe class;
    # treat all whitespace variants as one observed pattern.
    normalized = note_id.rstrip()
    if normalized != note_id:
        normalized += "<trailing-whitespace>"
    return normalized


def normalize_token(token: Any) -> str | None:
    """Return a comparable normalization of a boundary probe's token."""
    if not isinstance(token, str):
        return "<non-string>"
    if token == "":
        return "<empty>"
    left = token.lstrip()
    right = token.rstrip()
    if left != token or right != token:
        return "<whitespace-padded>"
    # Token value classes, not token values: reusing any documented public
    # token is one reused-public-token pattern, not one per token value.
    if token in {documented for documented, _ in PUBLIC_NOTES.values()}:
        return "<public-token>"
    if token.isdigit():
        return "<numeric>"
    if token.lower() != token:
        return "<case-variant>"
    if len(token) > 64:
        return "<long>"
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return "<unicode>"
    return "<opaque-value>"


def derive_request_pattern(note_id: Any, token: Any) -> tuple[str | None, str | None]:
    """Return one normalized (note_id, token) pattern for a request pair.

    Scoring derives attack quality from these observed patterns instead of
    attacker-supplied technique labels, so repeating one identical request
    under 15 different labels is still exactly one observed pattern.
    """
    return (normalize_note_id(note_id), normalize_token(token))


def validate_request(note_id: Any, token: Any) -> str | None:
    """Return an invalid-request reason, or ``None`` for contract-valid input."""
    if not isinstance(note_id, str) or not note_id:
        return "note_id must be a non-empty string"
    if not isinstance(token, str):
        return "token must be a string"
    if len(note_id) > 256 or len(token) > 1024:
        return "request exceeds contract length limit"
    return None


def safe_text(value: Any) -> str:
    """Normalize service output without allowing repr failures to break a match."""
    try:
        return value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - deliberately defensive boundary
        return "<unrenderable response>"
