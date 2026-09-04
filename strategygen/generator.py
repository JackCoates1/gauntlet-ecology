"""Codex invocation, extraction, and static validation for strategies."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .contracts import RoleContract, build_prompt, role_contract


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = ROOT / "strategygen" / "generated"
CODEX_MODEL = "gpt-5.6-terra"
REASONING_EFFORT = "medium"
GENERATION_TIMEOUT_SECONDS = 120
MAX_ATTEMPTS_PER_ROLE = 3


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class GenerationAttempt:
    role: str
    attempt: int
    source: str
    raw_output: str
    artifact_path: Path
    started_at: str
    wall_time_ms: int
    validation: ValidationResult
    command_error: str | None = None
    provider: str = "codex exec"
    model: str = CODEX_MODEL

    @property
    def provenance(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": REASONING_EFFORT,
            "timestamp": self.started_at,
            "wall_time_ms": self.wall_time_ms,
            "exact_token_count": None,
            "raw_output_bytes": len(self.raw_output.encode()),
            "command_error": self.command_error,
        }


def extract_python(raw_output: str, contract: RoleContract) -> str:
    """Remove a final-message Markdown fence while preserving source exactly otherwise."""
    text = raw_output.strip()
    fenced = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        preferred = [block for block in fenced if f"def {contract.callable_name}(" in block]
        text = (preferred or fenced)[0].strip()
    return text + "\n" if text else ""


def validate_source(source: str, role: str) -> ValidationResult:
    """Reject malformed or contract-incompatible source without importing it."""
    contract = role_contract(role)
    try:
        tree = ast.parse(source, filename=f"generated_{role}.py")
    except SyntaxError as error:
        return ValidationResult(False, f"syntax error: {error.msg} at line {error.lineno}")

    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == contract.callable_name]
    if len(functions) != 1:
        return ValidationResult(False, f"must define exactly one top-level {contract.callable_name} function")
    function = functions[0]
    if isinstance(function, ast.AsyncFunctionDef):
        return ValidationResult(False, f"{contract.callable_name} must be synchronous")
    positional = [*function.args.posonlyargs, *function.args.args]
    if function.args.vararg is not None or function.args.kwarg is not None or len(positional) != len(contract.parameters):
        return ValidationResult(False, f"{contract.callable_name} must take exactly ({', '.join(contract.parameters)})")
    names = tuple(argument.arg for argument in positional)
    if names != contract.parameters:
        return ValidationResult(False, f"{contract.callable_name} parameters must be named ({', '.join(contract.parameters)})")
    return ValidationResult(True)


def _codex_command(prompt: str, output_path: Path) -> list[str]:
    return [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "--skip-git-repo-check",
        "-m",
        CODEX_MODEL,
        "-c",
        f"model_reasoning_effort={REASONING_EFFORT}",
        "--output-last-message",
        str(output_path),
        prompt,
    ]


def invoke_codex(prompt: str, *, timeout_seconds: int = GENERATION_TIMEOUT_SECONDS) -> tuple[str, int, str | None]:
    """Ask Codex for its final message; stdout is intentionally not treated as source."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gauntlet-codex-") as temporary:
        output_path = Path(temporary) / "last-message.txt"
        try:
            completed = subprocess.run(
                _codex_command(prompt, output_path),
                cwd=temporary,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            error = None if completed.returncode == 0 else f"codex exited {completed.returncode}: {completed.stderr[-500:]}"
        except subprocess.TimeoutExpired:
            error = f"codex timed out after {timeout_seconds}s"
        raw_output = output_path.read_text() if output_path.exists() else ""
    return raw_output, int((time.monotonic() - started) * 1000), error


def generate_attempt(
    role: str,
    attempt: int,
    *,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    invoker: Callable[[str], tuple[str, int, str | None]] = invoke_codex,
    provider: str = "codex exec",
    model: str = CODEX_MODEL,
) -> GenerationAttempt:
    """Generate and statically validate one strategy attempt, never importing it on the host."""
    contract = role_contract(role)
    if contract.output_kind != "python":
        raise ValueError(f"{role!r} emits {contract.output_kind}, use its dedicated generator")
    started_at = datetime.now(UTC).isoformat()
    raw_output, wall_time_ms, command_error = invoker(build_prompt(role))
    source = extract_python(raw_output, contract)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{role}-{started_at.replace(':', '').replace('+', '_')}-attempt-{attempt}.py"
    artifact_path.write_text(source)
    os.chmod(artifact_path, 0o600)
    validation = validate_source(source, role)
    if command_error and validation.valid:
        validation = ValidationResult(False, command_error)
    return GenerationAttempt(role, attempt, source, raw_output, artifact_path, started_at, wall_time_ms, validation, command_error, provider, model)
