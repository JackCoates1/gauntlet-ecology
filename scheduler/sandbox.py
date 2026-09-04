"""The scheduler's narrow integration with the proven infra wrapper."""

from __future__ import annotations

import hashlib
import base64
import gzip
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra" / "run_sandboxed.sh"
_RESULT = re.compile(r"ECOLOGY_RESULT\s+(?P<fields>.+)")
_LIMITS = re.compile(r"SCHEDULER_INFRA_LIMITS\s+(?P<fields>.+)")
_EVENT_LOG = re.compile(r"SCHEDULER_EVENT_LOG_GZIP_BASE64\s+(?P<payload>[A-Za-z0-9+/=]+)")


@dataclass(frozen=True)
class SandboxResult:
    event_log: dict[str, Any]
    resource_limits: dict[str, Any]
    runner_image_digest: str
    output_hash: str
    exit_reason: str


def _fields(text: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in text.split() if "=" in part)


def _runner_image_digest() -> str:
    """Fingerprint the live runner appliance configuration, not a placeholder."""
    try:
        config = subprocess.run(
            ["qm", "config", "190"], cwd=ROOT, check=True, text=True, capture_output=True
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError("unable to inspect ecology-runner VM configuration") from error
    return "sha256:" + hashlib.sha256(config.encode()).hexdigest()


def _extract_event_log(output: str) -> dict[str, Any]:
    match = _EVENT_LOG.search(output)
    if match is None:
        raise RuntimeError("sandbox output did not contain an encoded event log")
    try:
        return json.loads(gzip.decompress(base64.b64decode(match.group("payload"))))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("sandbox event-log payload was not valid gzip JSON") from error


def run(program: str) -> SandboxResult:
    """Execute one match through infra, returning only sandbox-produced evidence."""
    try:
        completed = subprocess.run(
            [str(RUNNER), program], cwd=ROOT, check=True, text=True, capture_output=True, timeout=430
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("infra sandbox invocation timed out") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stdout or "") + (error.stderr or "")
        raise RuntimeError(f"infra sandbox invocation failed: {detail[-1000:]}") from error
    output = completed.stdout + completed.stderr
    result_match = _RESULT.search(output)
    limits_match = _LIMITS.search(output)
    if result_match is None or limits_match is None:
        raise RuntimeError("infra sandbox omitted result or live limit attestation")
    result = _fields(result_match.group("fields"))
    if result.get("exit") != "0" or result.get("destroyed") != "yes" or result.get("sandbox_leftovers") != "0":
        raise RuntimeError(f"infra sandbox failed cleanup or command execution: {result}")
    live_limits = _fields(limits_match.group("fields"))
    resource_limits = {
        "source": "infra/run_sandboxed.sh",
        "cpu_seconds": int(live_limits["cpu_seconds"]),
        "processes": int(live_limits["processes"]),
        "file_blocks": int(live_limits["file_blocks"]),
        "virtual_kib": int(live_limits["virtual_kib"]),
        "wall_elapsed_seconds": int(result["elapsed_s"]),
        "stdout_bytes": int(result["stdout_bytes"]),
        "stderr_bytes": int(result["stderr_bytes"]),
        "sandbox_leftovers": int(result["sandbox_leftovers"]),
        "destroyed": result["destroyed"] == "yes",
    }
    return SandboxResult(
        event_log=_extract_event_log(output),
        resource_limits=resource_limits,
        runner_image_digest=_runner_image_digest(),
        output_hash="sha256:" + hashlib.sha256(output.encode()).hexdigest(),
        exit_reason="sandbox_exit_0",
    )
