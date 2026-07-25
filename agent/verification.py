from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path

from config.config import Config

MAX_OUTPUT_CHARS = 8_000


@dataclass
class VerificationResult:
    command: str
    passed: bool
    output: str
    timed_out: bool = False

    def to_feedback(self) -> str:
        header = (
            f"Verification command `{self.command}` timed out."
            if self.timed_out
            else f"Verification command `{self.command}` failed."
        )

        return (
            f"{header}\n\n"
            f"{self.output}\n\n"
            "The work is not complete while this is failing. Diagnose the failure and "
            "fix it. If the failure is unrelated to your changes, say so explicitly "
            "instead of continuing to edit."
        )


def _truncate(output: str) -> str:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output

    half = MAX_OUTPUT_CHARS // 2
    omitted = len(output) - (half * 2)

    return f"{output[:half]}\n... [{omitted} characters omitted] ...\n{output[-half:]}"


class Verifier:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.attempts = 0
        self._detected: str | None | bool = False

    @property
    def enabled(self) -> bool:
        return self.config.verification.enabled

    def attempts_remaining(self) -> bool:
        return self.attempts < self.config.verification.max_attempts

    def detect_command(self) -> str | None:
        if self.config.verification.command:
            return self.config.verification.command

        if self._detected is not False:
            return self._detected

        self._detected = self._detect(self.config.cwd)
        return self._detected

    def _detect(self, cwd: Path) -> str | None:
        package_json = cwd / "package.json"
        if package_json.is_file():
            try:
                scripts = json.loads(package_json.read_text("utf-8")).get("scripts", {})
            except (OSError, ValueError):
                scripts = {}

            if "test" in scripts:
                return "npm test"

        if (cwd / "Cargo.toml").is_file():
            return "cargo test"

        if (cwd / "go.mod").is_file():
            return "go test ./..."

        if (cwd / "pyproject.toml").is_file() or (cwd / "setup.py").is_file():
            if self._has_pytest(cwd):
                return "python -m pytest -q"
            if (cwd / "tests").is_dir():
                return "python -m unittest discover -s tests"

        makefile = cwd / "Makefile"
        if makefile.is_file():
            try:
                if "\ntest:" in makefile.read_text("utf-8"):
                    return "make test"
            except OSError:
                pass

        return None

    def _has_pytest(self, cwd: Path) -> bool:
        pyproject = cwd / "pyproject.toml"
        if not pyproject.is_file():
            return False

        try:
            content = pyproject.read_text("utf-8")
        except OSError:
            return False

        return "pytest" in content

    async def run(self) -> VerificationResult | None:
        command = self.detect_command()
        if not command:
            return None

        self.attempts += 1

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.config.cwd),
            start_new_session=True,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.verification.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return VerificationResult(
                command=command,
                passed=False,
                output="",
                timed_out=True,
            )

        output = _truncate(stdout.decode("utf-8", errors="replace").strip())

        return VerificationResult(
            command=command,
            passed=process.returncode == 0,
            output=output,
        )
