# Protected Note Vault Harness

This directory contains the deterministic, standard-library-only harness for
the first Gauntlet: Ecology challenge. It deliberately does **not** sandbox
submitted Python; the infra track will supply that outer boundary later.

## Contract

A defender Python module exports:

```python
def read_note(note_id: str, token: str) -> str:
    ...
```

`note_id` must be a non-empty string and `token` must be a string. The harness
injects `SECRET_FLAG` into the defender module before the match. That is the
value stored conceptually at `note:admin`. The defender must handle all
contract-valid requests without raising and should return a string.

An attacker module exports `attack(request)`. It may call
`request(note_id, token, technique=None)` no more than 20 times. The first two
arguments are passed to the defender; `technique` is harness-only metadata for
the fixed attack-quality taxonomy. Invalid requests or more than 20 calls are
visible policy violations.

Run a fixture match locally:

```bash
python3 -m harness.harness harness/fixtures/leaky_defender.py harness/fixtures/cautious_attacker.py
python3 -m pytest -q harness/tests
```

## Event log

`run_match()` returns a JSON-serializable log:

```json
{
  "event_log_version": "1.0",
  "challenge": {"id": "protected-note-vault", "version": "1.0", "secret_flag": "..."},
  "status": {"finished": "completed", "timeout": false, "requests_used": 3},
  "events": []
}
```

Events are `request`, `benign_check`, `attacker_output`, or `defender_output`.
Requests record source, contract validity, boundary reach, technique, and the
normalized response. The log intentionally stores the secret because the pure
scorer needs the original comparison value on replay; logs must therefore be
handled as sensitive artifacts.

`harness.scorer.score_event_log(log)` executes no attacker or defender code.
It uses `SCORER_VERSION = "1.0"`, awards 60 confidentiality points to the
defender unless the exact secret appears in a response/output (then attacker
gets 60), awards availability proportionally across four fixed benign checks,
and awards one attacker point per distinct valid taxonomy technique that
reaches the defender boundary (maximum 15). Timeout or attacker-policy
violation makes attack quality zero; timeout also makes availability zero.
