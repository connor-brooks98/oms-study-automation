from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_startup_starts_chrome_only_when_missing_then_starts_hub() -> None:
    script = (ROOT / "scripts/start-hub.ps1").read_text(encoding="utf-8")
    process_check = "Get-Process -Name chrome -ErrorAction SilentlyContinue"
    assert process_check in script
    assert script.index(process_check) < script.index("Start-Process -FilePath $ChromePath")
    assert script.index("Start-Process -FilePath $ChromePath") < script.index("oms-hub.exe")
    assert "https://lmunet.instructure.com/" in script
    assert "Stop-Process" not in script
    assert "taskkill" not in script.casefold()


def test_installer_preserves_env_and_creates_managed_roots() -> None:
    script = (ROOT / "scripts/install-windows.ps1").read_text(encoding="utf-8")
    assert 'if (-not (Test-Path "$ProjectRoot\\.env"))' in script
    assert "CanvasInbox" in script
    assert "artifacts\\revisions" in script
    assert "artifacts\\panopto\\revisions" in script
    assert "Documents\\OMS II" in script
    assert "Transcript Cleaning.md" not in script
    assert "client_secret" not in script.casefold()
    assert "api_key" not in script.casefold()


def test_installer_grants_scheduled_task_user_modify_access_to_data_root() -> None:
    script = (ROOT / "scripts/install-windows.ps1").read_text(encoding="utf-8")

    assert "WindowsIdentity]::GetCurrent().Name" in script
    assert "icacls.exe" in script
    assert '"${TaskIdentity}:(OI)(CI)M"' in script
    assert script.index("icacls.exe") < script.index("Register-ScheduledTask")


def test_installer_splats_complete_native_python_argument_lists() -> None:
    script = (ROOT / "scripts/install-windows.ps1").read_text(encoding="utf-8")

    assert '$PythonVersionArgs = $PythonPrefix + @("--version")' in script
    assert "& $PythonCommand @PythonVersionArgs" in script
    assert (
        '$PythonVenvArgs = $PythonPrefix + @("-m", "venv", "$ProjectRoot\\.venv")'
        in script
    )
    assert "& $PythonCommand @PythonVenvArgs" in script
    assert "@PythonPrefix --version" not in script
    assert "@PythonPrefix -m venv" not in script
