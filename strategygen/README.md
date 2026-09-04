# Codex strategy generation

`python -m strategygen.generate_and_run` opens a real non-fixture generation,
asks Codex for one attacker and one defender, records every attempt, and sends
the valid pair through `scheduler.worker.execute_match` and the existing
`scheduler.sandbox` wrapper. Generation 1 remains the scheduler's fixture
bootstrap; this command defaults to generation 2.

## Prompt templates

The prompt is built by `strategygen.contracts.build_prompt`, which imports the
live docstrings and constants from `harness.challenge` and the `run_match`
docstring from `harness.harness`. It consequently includes the challenge ID,
version, public notes, request limit, timeout, legal technique labels and the
current harness wording rather than a copied contract.

Both role templates require **only one Python file and no prose**. They forbid
filesystem, network, subprocess and non-standard-library use. The attacker
template asks for `def attack(request):` and describes the optional
`request(note_id, token, technique=None)` metadata argument. The defender
template asks for `def read_note(note_id, token):`, requires a string response,
and explains that `SECRET_FLAG` appears only after module import.

Codex is invoked ephemerally with model `gpt-5.6-terra`, reasoning effort
`medium`, and `--output-last-message`. The last-message file is used rather
than terminal output, and a Markdown fence is removed if the model ignored the
code-only instruction.

## Validation and budgets

- There are at most three attempts per role, each with a 120-second wall-clock timeout.
- Every response is saved under `strategygen/generated/` (gitignored) and gets a `strategies` row. Provenance records model, reasoning effort, UTC timestamp, wall time, output bytes and the unavailable exact-token count.
- Before any sandbox invocation, AST parsing checks syntax, exactly one top-level synchronous entrypoint, and its exact parameter names.
- A statically valid candidate is imported and called once only inside the existing isolated sandbox, using a trivial `note:smoke` request. It never runs on the scheduler host. Smoke failure changes that row to `validation_status='invalid'`; the batch continues with another attempt.
- Only two strategies with `validation_status='valid'` are scheduled. The actual match reuses the existing worker, scorer and sandbox; strategy source plus the live harness are packaged as a read-only bundle executed inside the runner's fresh namespace.

The opt-in integration test is `strategygen/tests/test_live_integration.py`.
Set `GAUNTLET_LIVE_CODEX=1` to run it; ordinary `pytest` never spends Codex
quota.

## Builder scenarios

The builder uses the same pluggable invoker pattern, but its artifact is JSON
rather than Python. `strategygen.contracts.build_prompt("builder")` asks for
only this complete `scenario_config` object:

```json
{
  "request_budget": 20,
  "decoy_note_count": 2,
  "token_length": 16
}
```

The prompt imports the live ranges from `harness.challenge`: `request_budget`
is an integer from 1–20, `decoy_note_count` is an integer from 0–10, and
`token_length` is an integer from 8–64. All three keys are required and no
additional keys are accepted. The baseline two public notes are never changed;
decoys are deterministically named public notes, and `token_length` affects
only their tokens. This is deliberately the whole current harness surface.

`strategygen.builder.validate_builder_output()` parses JSON and delegates the
range/shape check to the harness before any database work. A valid
`BuilderAttempt` can then be passed to `insert_challenge_version()`: it locks
version allocation, bumps the patch from the most recently created
`challenge_versions` row, copies its source/seed/scoring references, and
inserts a new row with the validated scenario. Historical rows are never
updated. `test_live_codex_builder_creates_a_new_challenge_version` is opt-in
under the existing `GAUNTLET_LIVE_CODEX=1` flag and spends real Codex quota.
