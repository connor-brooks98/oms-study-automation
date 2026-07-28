import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_AUTH_OUTPUT_BYTES = 64 * 1024
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class NotebookAuthCheck:
    connected: bool
    message: str | None = None


class NotebookCLIAuth:
    def __init__(
        self,
        storage_path: Path,
        *,
        executable: Path | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.storage_path = storage_path.resolve()
        self.executable = executable or _default_executable()
        self.runner = runner

    def login(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = self._run(
                "login",
                "--browser",
                "chrome",
                timeout=330,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "NotebookLM login timed out. Connect Google again."
            ) from error
        except FileNotFoundError as error:
            raise RuntimeError(
                "NotebookLM login is unavailable. Reinstall Study Hub."
            ) from error
        if result.returncode != 0:
            raise RuntimeError("NotebookLM login did not complete.")

    def check(self) -> NotebookAuthCheck:
        try:
            result = self._run(
                "auth",
                "check",
                "--test",
                "--json",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return NotebookAuthCheck(
                False,
                "NotebookLM authentication check timed out.",
            )
        except FileNotFoundError:
            return NotebookAuthCheck(
                False,
                "NotebookLM authentication is unavailable. Reinstall Study Hub.",
            )
        if result.returncode != 0:
            return NotebookAuthCheck(False, "NotebookLM login is required.")
        if len(result.stdout.encode("utf-8")) > MAX_AUTH_OUTPUT_BYTES:
            return NotebookAuthCheck(
                False,
                "NotebookLM authentication could not be verified.",
            )
        try:
            payload = json.loads(result.stdout)
            connected = (
                payload.get("status") == "ok"
                and payload.get("checks", {}).get("token_fetch") is True
            )
        except (AttributeError, json.JSONDecodeError):
            connected = False
        if not connected:
            return NotebookAuthCheck(
                False,
                "NotebookLM authentication could not be verified.",
            )
        return NotebookAuthCheck(True)

    def _run(
        self,
        *arguments: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return self.runner(
            [
                str(self.executable),
                "--storage",
                str(self.storage_path),
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=timeout,
        )


def _default_executable(
    *,
    platform_name: str | None = None,
    python_executable: str | None = None,
) -> Path:
    name = "notebooklm.exe" if (platform_name or os.name) == "nt" else "notebooklm"
    return Path(python_executable or sys.executable).resolve().with_name(name)
