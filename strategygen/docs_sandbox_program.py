"""Package the shared-document challenge and the existing infra sandbox.

Mirrors strategygen/sandbox_program.py but bundles the docs modules and a
standalone match runner, so a docs match can be validated end-to-end inside
the real VM namespace sandbox (infra/run_sandboxed.sh) without touching the
scheduler's protected-note-vault path.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_DOCS_RUNNER = r'''from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import resource
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/scratch/strategy")


def load(path: str, role: str):
    spec = importlib.util.spec_from_file_location(f"docs_{role}_{uuid.uuid4().hex}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to create module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


envelope = json.loads(sys.stdin.read())
from harness.docs_harness import run_match
from harness.docs_scorer import score_event_log

event_log = run_match(
    "/scratch/strategy/defender.py",
    "/scratch/strategy/attacker.py",
    secret_flag=envelope["secret_flag"],
    scenario_config=envelope.get("scenario_config"),
    seed=envelope.get("seed"),
)
score = score_event_log(event_log)
print("DOCS_SANDBOX_LIMITS cpu_seconds=%s processes=%s virtual_kib=%s" % (
    resource.getrlimit(resource.RLIMIT_CPU)[0],
    resource.getrlimit(resource.RLIMIT_NPROC)[0],
    resource.getrlimit(resource.RLIMIT_AS)[0] // 1024))
payload = base64.b64encode(gzip.compress(json.dumps(
    {"event_log": event_log, "score": score}, sort_keys=True, separators=(",", ":")).encode(), mtime=0)).decode()
print("DOCS_EVENT_LOG_GZIP_BASE64 " + payload)
'''


def _bundle(files: dict[str, bytes]) -> str:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    compressed = gzip.compress(raw.getvalue(), mtime=0)
    return base64.b64encode(compressed).decode()


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def docs_sandbox_program(*, defender_source: str, attacker_source: str, secret: str, seed: int, scenario_config: dict[str, int] | None = None) -> str:
    """Return a BusyBox launcher that runs one docs match inside the sandbox."""
    payload = _bundle(
        {
            "harness/__init__.py": (ROOT / "harness" / "__init__.py").read_bytes(),
            "harness/docs_challenge.py": (ROOT / "harness" / "docs_challenge.py").read_bytes(),
            "harness/docs_harness.py": (ROOT / "harness" / "docs_harness.py").read_bytes(),
            "harness/docs_scorer.py": (ROOT / "harness" / "docs_scorer.py").read_bytes(),
            "defender.py": defender_source.encode(),
            "attacker.py": attacker_source.encode(),
            "runner.py": _DOCS_RUNNER.encode(),
        }
    )
    envelope = {
        "secret_flag": secret,
        "scenario_config": scenario_config,
        "seed": seed,
    }
    envelope_b64 = base64.b64encode(gzip.compress(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(), mtime=0)).decode()
    return "\n".join(
        (
            "#!/bin/sh",
            "set -eu",
            "mkdir -p /scratch/strategy",
            "/bin/busybox base64 -d <<'DOCS_STRATEGY_ARCHIVE' | /bin/busybox gunzip | /bin/busybox tar -x -C /scratch/strategy",
            payload,
            "DOCS_STRATEGY_ARCHIVE",
            "/bin/busybox base64 -d <<'DOCS_MATCH_ENVELOPE' | /bin/busybox gunzip | /usr/bin/python3 -I /scratch/strategy/runner.py",
            envelope_b64,
            "DOCS_MATCH_ENVELOPE",
        )
    )
