# Known gaps: referee isolation (Gauntlet: Ecology)

Status: deliberately deferred during the backlog-referee-fixes pass
(branch `backlog-referee-fixes`, 2026-09-05). This file records what is still
open so a follow-up pass can pick it up without re-deriving the analysis.

## What this pass fixed (scoring layer)

All three demonstrated scoring exploits are closed and regression-tested:

- (a) Self-declared technique labels no longer award attack-quality points.
  Quality is derived from normalized request patterns actually observed
  (`harness.challenge.derive_request_pattern`).
- (b) Harness-caused rejections (contract-invalid malformed probes) no longer
  count as defender-caused availability wins; deny-all defenders score 60, not
  72.
- (c) Attacker confidentiality credit requires the leak to come from an
  attacker-caused request; the harness's own adversarial probes extracting the
  secret credit nobody.
- Secret delivery: the generated-strategy runner now receives the secret via a
  gzip+base64 stdin envelope instead of `sys.argv`, closing the
  process-listing leak (verified with an argv-reading attacker in a real VM
  match: argv is now `['/scratch/strategy/runner.py', 'match']`, launcher file
  contains no flag).

## What is still open (architecture layer)

1. **One interpreter for attacker, defender, and referee.**
   `harness.run_match` still imports the attacker and defender modules into
   the same Python process that runs the harness. A hostile module can still:
   - reach harness/evaluator state through `sys.modules`, garbage-collection
     hooks, import machinery, or any reference it can obtain at import time;
   - tamper with module globals (e.g. rewrite its own `read_note` after the
     contract check);
   - observe the wall-clock `SIGALRM` handler or fake responses.

2. **Closures still cross the trust boundary.**
   The `request` callable handed to `attack()` closes over harness state
   (`events`, `status`, `request_count`). A malicious attacker module can
   introspect `request.__closure__` to reach the live event log and status
   dict. Closing this properly means handing the attacker a narrow
   proxy/interface object without cell references (e.g. a C-level or
   `os.pipe`-based boundary), not a Python closure.

3. **The event log is assembled inside the trusted process.**
   Until scoring inputs are produced by a process the competitors cannot
   address, "tamper with the referee" remains in scope for a hostile module.

## Why it was deferred

The sandbox VM (infra/) already provides OS-level containment: each match runs
in a fresh namespace sandbox inside the network-less `ecology-runner` VM, so
shared-interpreter state cannot reach the *host* or the database. The scoring
exploits were the actually-exploitable surface (they produced wrong scores
even with honest modules) and are fixed and verified. In-process isolation
(separate subprocesses per role with pipe-based request/response, or per-role
sandboxes) is a structural change that touches the runner protocol, the
fixture adapter, and the smoke path; it deserves its own reviewed change
rather than a half-done rewrite in this pass.

## Suggested follow-up design sketch

- Runner spawns two child processes (attacker, defender) communicating with
  the referee over pipes using a strict request/response schema.
- Secret is injected into the defender child only; attacker child never sees
  it in its address space.
- Scoring stays pure (`harness.scorer.score_event_log`) and unchanged; only
  event production moves.
- The fixture adapter (`scheduler/fixtures.py`) keeps producing the same event
  log shape, so the DB contract is unaffected.
