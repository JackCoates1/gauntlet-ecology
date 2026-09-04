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

`build_prompt(role, prior_generation_context=None)` also accepts optional
untrusted, scored lineage context for the continuous evolution loop. Omitting
it preserves the original prompt exactly.

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
