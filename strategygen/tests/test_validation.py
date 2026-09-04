from strategygen.generator import validate_source


def test_missing_required_entrypoint_is_rejected_before_any_sandbox_smoke():
    result = validate_source("def something_else(request):\n    return None\n", "attacker")
    assert result.valid is False
    assert "attack" in (result.reason or "")


def test_syntax_error_is_rejected_before_any_sandbox_smoke():
    result = validate_source("def read_note(note_id, token)\n    return 'x'\n", "defender")
    assert result.valid is False
    assert "syntax error" in (result.reason or "")
