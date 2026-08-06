from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_windows_launcher_exports_runtime_provenance() -> None:
    script = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")

    assert "OMS_HUB_DEPLOYMENT_ROOT" in script
    assert "OMS_HUB_BUILD_REVISION" in script
    assert "rev-parse HEAD" in script


def test_windows_installer_replaces_old_root_task_action_and_verifies_it() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert '"C:\\Services\\oms-study-automation-v2"' in script
    assert "-WorkingDirectory $ProjectRoot" in script
    assert "Assert-TaskActionTargetsProjectRoot" in script
    assert "$Task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop" in script
    assert "$Actions = @($Task.Actions)" in script
    assert "@(Get-ScheduledTask -TaskName $Name -ErrorAction Stop).Actions" not in script
    assert "does not target $ExpectedProjectRoot" in script


def test_windows_installer_stops_only_stale_old_root_hub_processes() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert "Get-CimInstance Win32_Process" in script
    assert "ChildrenByParent" in script
    assert "$ChildrenByParent.ContainsKey($ChildKey)" in script
    assert "oms-hub.exe" in script
    assert "python.exe" in script
    assert "(?i)oms[-_]hub" in script
    assert "Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot" in script
    assert "Get-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue" in script
    assert "Stop-Process -Id $Process.ProcessId -Force" in script
    assert "Stop-Process -Name python" not in script
    assert "old-root oms-hub.exe remains" in script


def test_windows_installer_guard_covers_old_hub_tree_but_not_unrelated_python() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    # A stale oms-hub.exe seeds its descendant traversal. An orphaned old-venv
    # Python process needs an explicit OMS Hub command-line match. Therefore an
    # unrelated system Python process is not selected merely for being Python.
    assert "if ($IsHubLauncher -and -not $IsExpectedRoot)" in script
    assert "if ($IsPython -and -not $IsExpectedRoot -and $HasHubCommandLine" in script
    assert "select only Hub/Python nodes" in script
    assert "unrelated tools" in script


def test_windows_installer_polls_local_health_for_expected_root_and_revision() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert "Assert-StartedHubProvenance" in script
    assert "deployment_root" in script
    assert "build_revision" in script
    assert "Study Hub did not start from the expected root/build" in script
    assert "port ${Port}: $LastFailure" in script
    assert "port $Port: $LastFailure" not in script
