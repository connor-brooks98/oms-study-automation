import subprocess

import pytest

from oms_hub.study_generation.notebook_auth import (
    NotebookCLIAuth,
    _default_executable,
)


class RecordingRunner:
    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.result = subprocess.CompletedProcess(
            [],
            returncode,
            stdout,
            stderr,
        )
        self.calls = []
        self.options = []
        self.error = None

    def __call__(self, arguments, **options):
        self.calls.append(list(arguments))
        self.options.append(options)
        if self.error is not None:
            raise self.error
        return self.result


def _auth(tmp_path, runner):
    return NotebookCLIAuth(
        tmp_path / "notebooklm-storage.json",
        executable=tmp_path / "notebooklm.exe",
        python_executable=tmp_path / "python.exe",
        runner=runner,
    )


def test_login_uses_gemini_notebook_compatibility_runner(tmp_path):
    runner = RecordingRunner()
    auth = _auth(tmp_path, runner)

    auth.login()

    assert runner.calls == [
        [
            str(tmp_path / "python.exe"),
            "-m",
            "oms_hub.study_generation.notebook_login_compat",
            "login",
            "--storage",
            str(tmp_path / "notebooklm-storage.json"),
            "--browser",
            "chrome",
        ]
    ]
    assert runner.options[0]["shell"] is False
    assert runner.options[0]["timeout"] == 330


@pytest.mark.parametrize(
    ("payload", "returncode", "connected"),
    [
        ('{"status":"ok","checks":{"token_fetch":true}}', 0, True),
        ('{"status":"error","checks":{"token_fetch":false}}', 0, False),
        ('{"status":"ok","checks":{"token_fetch":false}}', 0, False),
        ('{"status":"ok","checks":{"token_fetch":true}}', 1, False),
        ("not-json", 0, False),
    ],
)
def test_live_check_requires_ok_and_token_fetch(
    tmp_path,
    payload,
    returncode,
    connected,
):
    runner = RecordingRunner(returncode=returncode, stdout=payload)
    auth = _auth(tmp_path, runner)

    result = auth.check()

    assert result.connected is connected
    assert runner.calls == [
        [
            str(tmp_path / "notebooklm.exe"),
            "--storage",
            str(tmp_path / "notebooklm-storage.json"),
            "auth",
            "check",
            "--test",
            "--json",
        ]
    ]
    assert runner.options[0]["shell"] is False
    assert runner.options[0]["timeout"] == 60


def test_check_sanitizes_process_output(tmp_path):
    secret = "SID=secret-cookie-value"
    runner = RecordingRunner(
        returncode=1,
        stdout=secret,
        stderr=secret,
    )

    result = _auth(tmp_path, runner).check()

    assert not result.connected
    assert secret not in (result.message or "")
    assert result.message == "NotebookLM login is required."


def test_login_timeout_raises_safe_actionable_error(tmp_path):
    runner = RecordingRunner()
    runner.error = subprocess.TimeoutExpired(
        cmd=["notebooklm", "login"],
        timeout=330,
        output="SID=secret",
    )

    with pytest.raises(RuntimeError, match="timed out") as error:
        _auth(tmp_path, runner).login()

    assert "SID=secret" not in str(error.value)


def test_default_executable_is_resolved_next_to_python(tmp_path):
    executable = _default_executable(
        platform_name="nt",
        python_executable=str(tmp_path / "python.exe"),
    )

    assert executable == tmp_path / "notebooklm.exe"
