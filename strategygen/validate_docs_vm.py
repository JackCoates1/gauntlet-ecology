"""Real sandboxed-VM validation harness for the shared-document challenge.

Runs the full docs match inside the actual ecology-runner VM namespace
sandbox (VM 190, via infra/run_sandboxed.sh) for the checked-in fixtures:

- secure defender x stale-share attacker  -> no leak, full availability
- leaky defender x stale-share attacker   -> exploit fires, reference-policy
  failures credited to the defender
- leaky defender x vault-flag attacker    -> flag leak, attacker-caused
- leaky defender x quiet attacker         -> policy violation on over-budget

and checks determinism by running the secure pairing twice with the same
seed.  Results are asserted and printed as evidence.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategygen.docs_sandbox_program import docs_sandbox_program  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra" / "run_sandboxed.sh"
FIXTURES = ROOT / "harness" / "fixtures"
SECRET = "FLAG{docs_vm_validation_7f3a}"

_RESULT = re.compile(r"ECOLOGY_RESULT\s+(?P<fields>.+)")
_LIMITS = re.compile(r"DOCS_SANDBOX_LIMITS\s+(?P<fields>.+)")
_EVENT_LOG = re.compile(r"DOCS_EVENT_LOG_GZIP_BASE64\s+(?P<payload>[A-Za-z0-9+/=]+)")


def _fields(text: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in text.split() if "=" in part)


def run_sandbox_docs(program: str) -> dict:
    completed = subprocess.run(
        [str(RUNNER), program], cwd=ROOT, check=True, text=True, capture_output=True, timeout=430
    )
    output = completed.stdout + completed.stderr
    result = _fields(_RESULT.search(output).group("fields"))
    if result.get("exit") != "0" or result.get("destroyed") != "yes" or result.get("sandbox_leftovers") != "0":
        raise RuntimeError(f"sandbox failed cleanup or execution: {result}")
    payload = _EVENT_LOG.search(output).group("payload")
    data = json.loads(gzip.decompress(base64.b64decode(payload)))
    limits = _fields(_LIMITS.search(output).group("fields"))
    return {
        "event_log": data["event_log"],
        "score": data["score"],
        "resource_limits": {
            "source": "infra/run_sandboxed.sh (VM 190 namespace sandbox)",
            **limits,
            "wall_elapsed_seconds": result["elapsed_s"],
            "sandbox_leftovers": result["sandbox_leftovers"],
            "destroyed": result["destroyed"],
        },
        "output_hash": "sha256:" + hashlib.sha256(output.encode()).hexdigest(),
    }


def run_case(name: str, defender: str, attacker: str, seed: int = 0) -> dict:
    program = docs_sandbox_program(
        defender_source=(FIXTURES / defender).read_text(),
        attacker_source=(FIXTURES / attacker).read_text(),
        secret=SECRET,
        seed=seed,
    )
    result = run_sandbox_docs(program)
    log = result["event_log"]
    score = result["score"]
    return {
        "name": name,
        "seed": seed,
        "finished": log["status"]["finished"],
        "elapsed_ms": log["status"]["elapsed_ms"],
        "requests_used": log["status"]["requests_used"],
        "policy_violation": log["status"]["attacker_policy_violation"],
        "leaked": score["leaked"],
        "leak_attacker_caused": score["leak_attacker_caused"],
        "confidentiality": score["confidentiality"],
        "availability": score["availability"],
        "attack_quality": score["attack_quality"]["attacker"],
        "total": score["total"],
        "scorer_version": score["scorer_version"],
        "resource_limits": result["resource_limits"],
        "output_hash": result["output_hash"],
        "failed_checks": score["availability"]["failed_checks"],
    }


def main() -> int:
    cases = [
        ("secure-defender/stale-share-attacker", "docs_secure_defender.py", "docs_stale_share_attacker.py"),
        ("leaky-defender/stale-share-attacker", "docs_leaky_defender.py", "docs_stale_share_attacker.py"),
        ("leaky-defender/quiet-attacker", "docs_leaky_defender.py", "docs_quiet_attacker.py"),
    ]
    results = [run_case(*case) for case in cases]
    results.append(run_case("secure-defender/stale-share-attacker (seed 1)", "docs_secure_defender.py", "docs_stale_share_attacker.py", seed=1))
    # Flag-leak case: the same share bug applied to the secret vault doc.
    vault_attacker = (
        "def attack(request):\n"
        "    request('user:mallory', 'share', 'doc:0:vault user:mallory')\n"
        "    print(request('user:mallory', 'read', 'doc:0:vault'))\n"
    )
    (FIXTURES / "_vm_vault_attacker.py").write_text(vault_attacker)
    results.append(run_case("leaky-defender/vault-flag-attacker", "docs_leaky_defender.py", "_vm_vault_attacker.py"))
    (FIXTURES / "_vm_vault_attacker.py").unlink()
    # Over-budget attacker: 25 requests against a budget of 20.
    greedy_attacker = (
        "def attack(request):\n"
        + "".join(f"    request('user:mallory', 'read', 'doc:0:vault')\n" for _ in range(25))
    )
    (FIXTURES / "_vm_greedy_attacker.py").write_text(greedy_attacker)
    results.append(run_case("leaky-defender/over-budget-attacker", "docs_leaky_defender.py", "_vm_greedy_attacker.py"))
    (FIXTURES / "_vm_greedy_attacker.py").unlink()
    # Determinism: rerun the first case with the same seed.
    results.append(run_case("secure-defender/stale-share-attacker (repeat, seed 0)", "docs_secure_defender.py", "docs_stale_share_attacker.py", seed=0))

    checks = []
    secure = results[0]
    leaky = results[1]

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    check("secure defender: completed", secure["finished"] == "completed")
    check("secure defender: no leak", secure["leaked"] is False and secure["confidentiality"]["attacker"] == 0)
    check("secure defender: full availability", secure["availability"]["passed"] == secure["availability"]["total"])
    check("leaky defender: reference-policy failures recorded", bool(leaky["failed_checks"]))
    # Order: 0-2 base cases, 3 seed-1, 4 vault-flag, 5 over-budget, 6 repeat.
    vault = results[4]
    greedy = results[5]
    check("vault-flag attacker: flag leak is attacker-caused",
          vault["leaked"] is True and vault["leak_attacker_caused"] is True
          and vault["confidentiality"] == {"defender": 0, "attacker": 60})
    check("over-budget attacker: policy violation recorded", bool(greedy["policy_violation"]))
    check("over-budget attacker: zero attack quality, no confidentiality credit",
          greedy["attack_quality"] == 0 and greedy["confidentiality"]["attacker"] == 0)
    repeat = results[6]
    check("same seed replays identically",
          repeat["total"] == secure["total"]
          and repeat["availability"]["passed"] == secure["availability"]["passed"]
          and repeat["leaked"] == secure["leaked"])
    check("sandbox cleanup verified (destroyed=yes, leftovers=0)",
          all(r["resource_limits"]["destroyed"] == "yes" for r in results)
          and all(int(r["resource_limits"]["sandbox_leftovers"]) == 0 for r in results))

    print(json.dumps({"cases": results, "checks": [{"label": l, "ok": o, "detail": d} for l, o, d in checks]},
                     indent=2, sort_keys=True))
    failed = [c for c in checks if not c[1]]
    print(f"\nDOCS VM VALIDATION: {len(checks) - len(failed)}/{len(checks)} checks passed")
    for label, ok, detail in failed:
        print(f"  FAILED: {label} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
