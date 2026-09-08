import json
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
        (
            json.dumps({"status": "ok", "checks": {
                "storage_exists": True, "json_valid": True,
                "cookies_present": True, "sid_cookie": True, "token_fetch": True,
            }}),
            0,
            True,
        ),
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
    assert "verified" in result.message
    assert not result.requires_login


@pytest.mark.parametrize(
    ("checks", "returncode", "requires_login"),
    [
        ({"storage_exists": False, "json_valid": False, "cookies_present": False,
          "sid_cookie": False, "token_fetch": None}, 1, True),
        ({"storage_exists": True, "json_valid": True, "cookies_present": True,
          "sid_cookie": True, "token_fetch": False}, 1, False),
        ({"storage_exists": True, "json_valid": False, "cookies_present": False,
          "sid_cookie": False, "token_fetch": None}, 1, False),
        ({"storage_exists": True, "json_valid": True, "cookies_present": False,
          "sid_cookie": False, "token_fetch": False}, 1, False),
        ({"storage_exists": False, "json_valid": False, "cookies_present": False,
          "sid_cookie": False, "token_fetch": None}, 2, False),
        ({"storage_exists": 0, "json_valid": False, "cookies_present": False,
          "sid_cookie": False, "token_fetch": None}, 1, False),
        ({"token_fetch": True}, 0, False),
        ({"storage_exists": True, "json_valid": True, "cookies_present": True,
          "sid_cookie": True, "token_fetch": 1}, 0, False),
        (None, 0, False),
        ([], 0, False),
    ],
)
def test_check_only_requests_login_for_confirmed_missing_session(
    tmp_path, checks, returncode, requires_login,
):
    runner = RecordingRunner(returncode=returncode, stdout=json.dumps({
        "status": "ok" if returncode == 0 else "error",
        "checks": checks,
        "details": {"error": "SID=secret-cookie-value"},
    }))

    result = _auth(tmp_path, runner).check()

    assert not result.connected
    assert result.requires_login is requires_login
    assert "SID=" not in result.message
    if not requires_login:
        assert "login is required" not in result.message


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("notebooklm", 60, output="SID=secret"),
    FileNotFoundError("SID=secret"),
    PermissionError("SID=secret"),
])
def test_check_execution_failure_does_not_request_login(tmp_path, error):
    runner = RecordingRunner()
    runner.error = error

    result = _auth(tmp_path, runner).check()

    assert not result.connected
    assert not result.requires_login
    assert "SID=" not in result.message


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
