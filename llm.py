"""Optional external renderer.

The bot never calls an LLM API on its own. If you *want* model-written copy,
point `scheduler.llm_command` at a local CLI (ollama, llama-cli, your own
script). The command receives the rendered prompt on stdin and must print the
final message on stdout.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from logging_setup import get_logger

LOG = get_logger("llm")

MIN_OUTPUT_CHARS = 40


class LLMError(Exception):
    """Raised when the configured renderer is unusable or fails."""


class SubprocessLLM:
    """Callable renderer backed by a local command."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout: int = 120,
        min_chars: int = MIN_OUTPUT_CHARS,
    ) -> None:
        self.command: List[str] = [str(part) for part in command if str(part).strip()]
        self.timeout = max(int(timeout), 5)
        self.min_chars = max(int(min_chars), 1)
        if not self.command:
            raise LLMError("llm_command is empty; set scheduler.llm_command to enable --llm")

    # -- introspection ----------------------------------------------------- #

    @property
    def executable(self) -> str:
        return self.command[0]

    def exists(self) -> bool:
        return bool(self.command) and shutil.which(self.command[0]) is not None

    def describe(self) -> str:
        return " ".join(self.command)

    # -- rendering --------------------------------------------------------- #

    def __call__(self, prompt: str) -> str:
        if not self.command:
            raise LLMError("no renderer command configured")
        if not self.exists():
            raise LLMError(
                f"renderer executable {self.command[0]!r} not found on PATH "
                f"(command: {self.describe()})"
            )
        LOG.info("Calling renderer: %s", self.describe())
        try:
            completed = subprocess.run(
                self.command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"renderer timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise LLMError(f"renderer could not be started: {exc}") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:400]
            raise LLMError(f"renderer exited {completed.returncode}: {stderr}")

        output = (completed.stdout or "").strip()
        if len(output) < self.min_chars:
            raise LLMError(f"renderer output too short ({len(output)} chars) - treating as failure")
        return output


def build_renderer(command: Sequence[str], timeout: int = 120) -> Optional[SubprocessLLM]:
    """Return a renderer, or None when no command is configured."""
    if not command:
        return None
    return SubprocessLLM(command, timeout=timeout)


def executable_from_path(candidate: str) -> bool:
    return Path(candidate).exists() or shutil.which(candidate) is not None