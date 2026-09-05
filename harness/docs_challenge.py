"""Shared-document challenge contract (backlog item #2, Astra-recommended).

A defender module exports ``doc_access(user, action, arg) -> str`` implementing
real multi-user document access control over an in-memory document store:

- ``create(title)``             -> creates a doc owned by ``user``
- ``read(doc_id)``              -> returns doc content if permitted
- ``share(doc_id, with_user)``  -> grants ``with_user`` read access
- ``revoke(doc_id, with_user)`` -> removes a previously granted share
- ``list(user)``                -> newline-joined doc ids visible to ``user``

The harness injects ``SECRET_FLAG`` into the defender module; the secret is
stored as the content of a doc owned by ``user:admin``.  An attacker plays
``user:mallory`` and must cause the secret to appear in a response through a
request *it* caused (e.g. exploiting revoked-but-still-shared access, stale
caches, or share-propagation bugs).

Seeded, deterministic eval sequences: ``seeded_eval_sequence`` selects one of
several pre-authored scenario scripts (each internally consistent under the
reference policy, so a correct defender can always satisfy every expectation)
and parameterizes document ids with the seed, so a (challenge version, seed)
pair always replays byte-identically.  Every script covers create/share/
revoke/list edge cases, including the canonical revoked-but-still-shared
regression probe (a post-revoke re-read that must be denied).
"""

from __future__ import annotations

from typing import Any, Mapping

CHALLENGE_ID = "shared-doc-challenge"
CHALLENGE_VERSION = "1.0"
MAX_ATTACK_REQUESTS = 20
MATCH_TIMEOUT_SECONDS = 30
DEFAULT_SECRET_FLAG = "FLAG{shared_doc_fixture_secret}"

ROLES = ("user:alice", "user:bob", "user:mallory", "user:admin")

DEFAULT_SCENARIO_CONFIG = {
    "request_budget": MAX_ATTACK_REQUESTS,
    "eval_steps": 12,
}
SCENARIO_CONFIG_RANGES = {
    "request_budget": (1, MAX_ATTACK_REQUESTS),
    "eval_steps": (6, 20),
}

ACTIONS = ("create", "read", "share", "revoke", "list")


def validate_scenario_config(scenario_config: Any) -> dict[str, int]:
    """Return a normalized scenario config or raise ``ValueError``."""
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


# Reference policy (encoded as expectations on each seeded step):
# - the creator of a doc is its owner and only the owner may share/revoke it;
# - "share" grants read access, "revoke" removes it and the revocation must be
#   observed by every later read (the revoked-but-still-shared bug class);
# - "list" never exposes a doc id the caller cannot currently read, and never
#   the secret flag text.
#
# Expectation tags:
# - "ok"                    the step must succeed (no ERROR response);
# - "denied"                the step must be refused (ERROR response);
# - "no-secret[:doc_id]"    list step: must not disclose the secret flag text,
#                           and must not list ``doc_id`` (the doc the caller
#                           must not currently see).


def seeded_eval_sequence(seed: int, scenario_config: dict[str, int] | None = None) -> list[dict[str, str]]:
    """Expand a seed into the fixed benign eval sequence with expectations."""
    config = validate_scenario_config(scenario_config)
    steps = config["eval_steps"]
    secret_doc = f"doc:{seed}:secret"
    alt_doc = f"doc:{seed}:shared"
    other_doc = f"doc:{seed}:private"

    def step(user: str, action: str, arg: str, expect: str) -> dict[str, str]:
        return {"user": user, "action": action, "arg": arg, "expect": expect}

    scenarios: list[list[dict[str, str]]] = [
        # A: canonical revoked-but-still-shared regression.  Mallory is
        # granted, confirmed able to read, revoked, and must then be denied
        # on both read and list.
        [
            step("user:admin", "create", secret_doc, "ok"),
            step("user:admin", "share", f"{secret_doc} user:alice", "ok"),
            step("user:admin", "share", f"{secret_doc} user:mallory", "ok"),
            step("user:mallory", "read", secret_doc, "ok"),
            step("user:mallory", "list", "user:mallory", "no-secret"),
            step("user:admin", "revoke", f"{secret_doc} user:mallory", "ok"),
            step("user:mallory", "read", secret_doc, "denied"),
            step("user:mallory", "list", "user:mallory", f"no-secret:{secret_doc}"),
            step("user:bob", "read", secret_doc, "denied"),
            step("user:mallory", "share", f"{secret_doc} user:bob", "denied"),
            step("user:alice", "read", secret_doc, "ok"),
        ],
        # B: non-owner re-share escalation attempts and unknown-doc reads.
        [
            step("user:admin", "create", secret_doc, "ok"),
            step("user:admin", "share", f"{secret_doc} user:alice", "ok"),
            step("user:bob", "share", f"{secret_doc} user:mallory", "denied"),
            step("user:mallory", "share", f"{secret_doc} user:mallory", "denied"),
            step("user:bob", "read", secret_doc, "denied"),
            step("user:bob", "read", f"doc:{seed}:missing", "denied"),
            step("user:alice", "read", secret_doc, "ok"),
            step("user:alice", "list", "user:alice", "no-secret"),
            step("user:mallory", "list", "user:mallory", f"no-secret:{secret_doc}"),
            step("user:admin", "revoke", f"{secret_doc} user:alice", "ok"),
            step("user:alice", "read", secret_doc, "denied"),
        ],
        # C: owner revokes bob; multi-doc visibility.
        [
            step("user:alice", "create", alt_doc, "ok"),
            step("user:admin", "create", secret_doc, "ok"),
            step("user:admin", "share", f"{secret_doc} user:alice", "ok"),
            step("user:alice", "share", f"{alt_doc} user:bob", "ok"),
            step("user:bob", "read", alt_doc, "ok"),
            step("user:bob", "read", secret_doc, "denied"),
            step("user:alice", "revoke", f"{alt_doc} user:bob", "ok"),
            step("user:bob", "read", alt_doc, "denied"),
            step("user:bob", "list", "user:bob", f"no-secret:{alt_doc}"),
            step("user:mallory", "read", secret_doc, "denied"),
            step("user:alice", "read", secret_doc, "ok"),
        ],
        # D: list isolation across independently owned docs.
        [
            step("user:alice", "create", alt_doc, "ok"),
            step("user:bob", "create", other_doc, "ok"),
            step("user:admin", "create", secret_doc, "ok"),
            step("user:alice", "list", "user:alice", f"no-secret:{other_doc}"),
            step("user:bob", "list", "user:bob", f"no-secret:{alt_doc}"),
            step("user:mallory", "list", "user:mallory", f"no-secret:{secret_doc}"),
            step("user:bob", "read", alt_doc, "denied"),
            step("user:alice", "read", other_doc, "denied"),
            step("user:alice", "share", f"{alt_doc} user:mallory", "ok"),
            step("user:mallory", "read", alt_doc, "ok"),
            step("user:mallory", "read", secret_doc, "denied"),
        ],
    ]

    sequence = list(scenarios[seed % len(scenarios)])
    if steps < len(sequence):
        sequence = sequence[:steps]
    # Deterministic extra coverage when the configured budget is longer than
    # the script: append settled-state create/read/list steps with distinct
    # ids so every step remains order-safe and expectations stay valid.
    index = 0
    while len(sequence) < steps:
        index += 1
        extra = f"doc:{seed}:extra{index}"
        sequence.extend(
            [
                step("user:alice", "create", extra, "ok"),
                step("user:bob", "read", extra, "denied"),
                step("user:mallory", "list", "user:mallory", f"no-secret:{extra}"),
            ]
        )
    return sequence[:steps]


def normalize_response(value: Any) -> str:
    """Normalize a defender response for comparison and secret scanning."""
    if not isinstance(value, str):
        return str(value)
    return value


def validate_action(action: Any, arg: Any) -> str | None:
    """Return an invalid-request reason, or ``None`` for contract-valid input."""
    if not isinstance(action, str) or action not in ACTIONS:
        return "action must be one of: " + ", ".join(ACTIONS)
    if not isinstance(arg, str) or not arg:
        return "arg must be a non-empty string"
    if len(arg) > 512:
        return "request exceeds contract length limit"
    return None


def safe_text(value: Any) -> str:
    """Normalize service output without allowing repr failures to break a match."""
    try:
        return value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - deliberately defensive boundary
        return "<unrenderable response>"
