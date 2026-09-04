from pathlib import Path

import pytest

from strategygen.builder import generate_builder_attempt, insert_challenge_version, validate_builder_output


class _NoDatabaseAccess:
    def cursor(self):
        raise AssertionError("invalid builder output reached the database")


def test_malformed_or_out_of_range_builder_output_is_rejected_before_database_access(tmp_path: Path):
    malformed, malformed_validation = validate_builder_output("not JSON")
    assert malformed is None
    assert malformed_validation.valid is False

    attempt = generate_builder_attempt(
        1,
        artifact_dir=tmp_path,
        invoker=lambda _prompt: ('{"request_budget": 20, "decoy_note_count": 2, "token_length": -5}', 1, None),
    )

    assert attempt.validation.valid is False
    assert "token_length" in (attempt.validation.reason or "")
    with pytest.raises(ValueError, match="refusing to persist invalid builder output"):
        insert_challenge_version(_NoDatabaseAccess(), attempt)  # type: ignore[arg-type]
