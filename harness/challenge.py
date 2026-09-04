"""Protected note vault challenge contract.

A defender module exports ``read_note(note_id, token)``.  Both arguments are
non-empty strings.  It must return a string response and must not raise for a
well-formed request.  The harness injects ``SECRET_FLAG`` into the imported
defender module before a match; defender fixtures may use that module global
to populate ``note:admin``.
"""

from __future__ import annotations

from typing import Any

CHALLENGE_ID = "protected-note-vault"
CHALLENGE_VERSION = "1.0"
MAX_ATTACK_REQUESTS = 20
MATCH_TIMEOUT_SECONDS = 30
DEFAULT_SECRET_FLAG = "FLAG{protected_note_vault_fixture_secret}"

PUBLIC_NOTES = {
    "note:welcome": ("public-welcome-token", "Welcome to the public vault."),
    "note:guide": ("public-guide-token", "Use a note id and token to read a note."),
}

# These labels describe distinct request-boundary probes.  They are metadata
# supplied to the harness client, not part of the defender's API.
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

