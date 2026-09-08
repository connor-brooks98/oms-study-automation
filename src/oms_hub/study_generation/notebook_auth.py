import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_AUTH_OUTPUT_BYTES = 64 * 1024
NOTEBOOK_CHECK_UNVERIFIED = (
    "Gemini Notebook connection could not be verified. "
    "Use Test connection in Settings to try again."
)
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class NotebookAuthCheck:
    connected: bool
    message: str | None = None
    requires_login: bool = False


class NotebookCLIAuth:
    def __init__(
        self,
        storage_path: Path,
        *,
        executable: Path | None = None,
        python_executable: Path | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.storage_path = storage_path.resolve()
        self.executable = executable or _default_executable()
        self.python_executable = Path(
            python_executable or sys.executable
        ).resolve()
        self.runner = runner

    def login(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = self._run(
                "-m",
                "oms_hub.study_generation.notebook_login_compat",
                "login",
                "--storage",
                str(self.storage_path),
                "--browser",
                "chrome",
                executable=self.python_executable,
                include_root_storage=False,
                timeout=330,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "Gemini Notebook login timed out. Connect Notebook again."
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
                "Gemini Notebook connection check timed out. Try again.",
            )
        except FileNotFoundError:
            return NotebookAuthCheck(
                False,
                "NotebookLM authentication is unavailable. Reinstall Study Hub.",
            )
        except OSError:
            return NotebookAuthCheck(False, NOTEBOOK_CHECK_UNVERIFIED)
        if len(result.stdout.encode("utf-8")) > MAX_AUTH_OUTPUT_BYTES:
            return NotebookAuthCheck(False, NOTEBOOK_CHECK_UNVERIFIED)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return NotebookAuthCheck(False, NOTEBOOK_CHECK_UNVERIFIED)
        checks = payload.get("checks") if isinstance(payload, dict) else None
        if isinstance(checks, dict):
            local_checks = [
                checks.get(name)
                for name in ("storage_exists", "json_valid", "cookies_present", "sid_cookie")
            ]
            if (
                result.returncode == 0
                and payload.get("status") == "ok"
                and all(value is True for value in local_checks)
                and checks.get("token_fetch") is True
            ):
                return NotebookAuthCheck(True)
            # The CLI gives expired cookies and network errors the same token_fetch=False.
            # Only its complete missing-storage result establishes that sign-in is needed.
            if (
                result.returncode == 1
                and payload.get("status") == "error"
                and all(value is False for value in local_checks)
                and "token_fetch" in checks
                and checks["token_fetch"] is None
            ):
                return NotebookAuthCheck(
                    False,
                    "Gemini Notebook login is required. Connect Notebook in Settings.",
                    requires_login=True,
                )
        return NotebookAuthCheck(False, NOTEBOOK_CHECK_UNVERIFIED)

    def _run(
        self,
        *arguments: str,
        executable: Path | None = None,
        include_root_storage: bool = True,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(executable or self.executable)]
        if include_root_storage:
            command.extend(
                ["--storage", str(self.storage_path)]
            )
        command.extend(arguments)
        return self.runner(
            command,
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
