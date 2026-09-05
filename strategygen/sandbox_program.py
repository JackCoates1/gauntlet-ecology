"""Package strategy source and the existing harness for the proven sandbox."""

from __future__ import annotations

import base64
import gzip
import io
import json
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

_RUNNER = r'''from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import resource
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/scratch/strategy")

# Match payloads arrive on stdin as a gzip+base64 JSON envelope; argv never
# carries the secret, so process listings inside the sandbox cannot leak it.
def read_match_envelope() -> dict:
    return json.loads(sys.stdin.read())


from harness.challenge import CHALLENGE_ID, CHALLENGE_VERSION
from harness.harness import run_match


def load(path: str, role: str):
    spec = importlib.util.spec_from_file_location(f"smoke_{role}_{uuid.uuid4().hex}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to create module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event_log_for_smoke(role: str):
    module = load(f"/scratch/strategy/{role}.py", role)
    if role == "attacker":
        module.attack(lambda note_id, token, technique=None: "SMOKE")
    else:
        setattr(module, "SECRET_FLAG", "FLAG{smoke}")
        response = module.read_note("note:smoke", "smoke-token")
        if not isinstance(response, str):
            raise TypeError("defender smoke response was not a string")
    return {"event_log_version": "1.0", "challenge": {"id": CHALLENGE_ID, "version": CHALLENGE_VERSION, "secret_flag": "FLAG{smoke}"}, "status": {"finished": "smoke"}, "events": []}


mode = sys.argv[1]
if mode == "match":
    envelope = read_match_envelope()
    if envelope.get("mode") != "match":
        raise ValueError("stdin envelope must be a match envelope")
    event_log = run_match(
        "/scratch/strategy/defender.py",
        "/scratch/strategy/attacker.py",
        secret_flag=envelope["secret_flag"],
        scenario_config=envelope.get("scenario_config"),
        seed=envelope.get("seed"),
    )
elif mode == "smoke":
    event_log = event_log_for_smoke(sys.argv[2])
else:
    raise ValueError("unknown runner mode")

print("SCHEDULER_INFRA_LIMITS cpu_seconds=%s processes=%s file_blocks=%s virtual_kib=%s" % (resource.getrlimit(resource.RLIMIT_CPU)[0], resource.getrlimit(resource.RLIMIT_NPROC)[0], resource.getrlimit(resource.RLIMIT_FSIZE)[0], resource.getrlimit(resource.RLIMIT_AS)[0] // 1024))
payload = base64.b64encode(gzip.compress(json.dumps(event_log, sort_keys=True, separators=(",", ":")).encode(), mtime=0)).decode()
print("SCHEDULER_EVENT_LOG_GZIP_BASE64 " + payload)
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


def sandbox_program(*, attacker_source: str, defender_source: str, mode: str, secret: str | None = None, smoke_role: str | None = None, scenario_config: dict[str, int] | None = None, seed: int | None = None) -> str:
    """Return a BusyBox launcher; Python sources execute only after the sandbox chroot.

    In match mode the secret travels inside the serialized stdin envelope, not
    on the runner command line: process listings inside the sandbox would
    otherwise expose it to every process and to any code reading /proc.
    """
    if mode not in {"match", "smoke"}:
        raise ValueError("mode must be match or smoke")
    if mode == "match" and secret is None:
        raise ValueError("match mode requires a secret")
    if mode == "smoke" and smoke_role not in {"attacker", "defender"}:
        raise ValueError("smoke mode requires a role")
    payload = _bundle(
        {
            "harness/__init__.py": (ROOT / "harness" / "__init__.py").read_bytes(),
            "harness/challenge.py": (ROOT / "harness" / "challenge.py").read_bytes(),
            "harness/harness.py": (ROOT / "harness" / "harness.py").read_bytes(),
            "attacker.py": attacker_source.encode(),
            "defender.py": defender_source.encode(),
            "runner.py": _RUNNER.encode(),
        }
    )
    if mode == "match":
        envelope = {
            "mode": "match",
            "secret_flag": secret,
            "scenario_config": scenario_config,
            "seed": seed,
        }
        envelope_b64 = base64.b64encode(gzip.compress(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(), mtime=0)).decode()
        final_line = "/bin/busybox base64 -d <<'ECOLOGY_MATCH_ENVELOPE' | /bin/busybox gunzip | /usr/bin/python3 -I /scratch/strategy/runner.py match\n" + envelope_b64 + "\nECOLOGY_MATCH_ENVELOPE"
    else:
        final_line = "exec /usr/bin/python3 -I /scratch/strategy/runner.py smoke " + _shell_quote(smoke_role or "")
    return "\n".join(
        (
            "#!/bin/sh",
            "set -eu",
            "mkdir -p /scratch/strategy",
            "/bin/busybox base64 -d <<'ECOLOGY_STRATEGY_ARCHIVE' | /bin/busybox gunzip | /bin/busybox tar -x -C /scratch/strategy",
            payload,
            "ECOLOGY_STRATEGY_ARCHIVE",
            final_line,
        )
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
