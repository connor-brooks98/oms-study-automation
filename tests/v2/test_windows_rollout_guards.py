from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_windows_launcher_exports_runtime_provenance() -> None:
    script = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")

    assert "OMS_HUB_DEPLOYMENT_ROOT" in script
    assert "OMS_HUB_BUILD_REVISION" in script
    assert "OMS_HUB_BUILD_TREE" in script
    assert "rev-parse HEAD" in script
    assert 'rev-parse "HEAD^{tree}"' in script
    assert "status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml" in script
    assert "refusing to start an editable deployment" in script


def test_windows_revision_capture_preserves_the_complete_git_hash() -> None:
    for relative_path in (
        "scripts/start-hub.ps1",
        "scripts/install-windows.ps1",
    ):
        script = (ROOT / relative_path).read_text(encoding="utf-8")

        # PowerShell unwraps one line of native-command output to a scalar
        # string. Indexing that scalar returns its first character, so the
        # command result must be forced to an array before selecting line 0.
        assert "$Revision = @(& $Git.Source" in script
        assert "([string]$Revision[0]).Trim()" in script

    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert '"^[0-9a-fA-F]{40}$"' in installer
    assert "Git is required to establish exact build provenance." in installer
    assert "The editable Study Hub runtime differs from HEAD" in installer
    assert "Get-ProjectBuildTree" in installer


def test_windows_installer_replaces_old_root_task_action_and_verifies_it() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert '"C:\\Services\\oms-study-automation-v2"' in script
    assert "-WorkingDirectory $ProjectRoot" in script
    assert "Assert-TaskActionTargetsProjectRoot" in script
    assert "$Task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop" in script
    assert "$Actions = @($Task.Actions)" in script
    assert "@(Get-ScheduledTask -TaskName $Name -ErrorAction Stop).Actions" not in script
    assert "does not target $ExpectedProjectRoot" in script


def test_windows_installer_stops_and_verifies_only_same_root_hub_processes() -> None:
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
    assert "same-root Study Hub process" in script


def test_windows_installer_guard_covers_same_root_tree_but_not_unrelated_python() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    # A same-root oms-hub.exe seeds descendant traversal. An orphaned same-root
    # Python process additionally needs an OMS Hub command-line match. A Python
    # process outside this deployment cannot be selected merely for being Python.
    assert "if ($IsHubLauncher -and $IsExpectedRoot)" in script
    assert "if ($IsPython -and $IsExpectedRoot -and $HasHubCommandLine" in script
    assert "positively identifies the process tree" in script
    assert "generic Python processes from another deployment" in script


def test_windows_installer_selects_every_descendant_of_verified_launcher() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    # Behavioral topology: the verified launcher owns a Python child which
    # owns a non-Python helper. An unrelated process is not descended from the
    # launcher and therefore remains outside the stop set.
    parents = {101: 1, 102: 101, 103: 102, 999: 1}
    pending = [101]
    selected: set[int] = set()
    while pending:
        process_id = pending.pop(0)
        if process_id in selected:
            continue
        selected.add(process_id)
        pending.extend(
            child_id
            for child_id, parent_id in parents.items()
            if parent_id == process_id
        )

    assert selected == {101, 102, 103}
    assert "descendant belongs to that tree" in script
    assert "$Selected.Add($Process)" in script
    selection_start = script.index("while ($Pending.Count -gt 0)")
    selection_end = script.index("foreach ($Process in $Processes)", selection_start)
    selection_block = script[selection_start:selection_end]
    assert "-or [string]$Process.Name -ieq" not in selection_block


def test_windows_installer_resolves_config_stops_tree_and_backs_up_before_pip() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    config = script.index("$EffectiveDataRootValue = Get-EffectiveSetting")
    stop = script.index("Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot")
    backup = script.index('"--source", $DatabasePath')
    complete = script.index("$BackupComplete = $true")
    pip = script.index('-m pip install --upgrade pip')

    assert config < stop < backup < complete < pip
    assert 'Get-EffectiveSetting `\n  -Name "OMS_HUB_DATABASE_URL"' in script
    assert "Resolve-SqliteDatabasePath" in script
    assert "Verified rollback backup was not completed; installation is blocked." in script


def test_windows_installer_fails_closed_after_every_install_native_command() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    expected_checks = (
        (
            '& $PythonCommand @PythonVenvArgs',
            'Assert-NativeCommandSucceeded -Operation "Python virtual environment creation"',
        ),
        (
            '-m pip install --upgrade pip',
            'Assert-NativeCommandSucceeded -Operation "pip upgrade"',
        ),
        (
            '-m pip install -e $EditableInstallTarget',
            'Assert-NativeCommandSucceeded -Operation "editable Study Hub installation"',
        ),
        (
            '-c "import anydoc"',
            'Assert-NativeCommandSucceeded -Operation "Anydoc document parser import check"',
        ),
        (
            'oms-hub.exe" validate-config',
            'Assert-NativeCommandSucceeded -Operation "Study Hub configuration validation"',
        ),
    )
    for invocation, check in expected_checks:
        assert script.index(invocation) < script.index(check)
    assert "failed with native exit code $LASTEXITCODE" in script
    assert '$EditableInstallTarget = "${ProjectRoot}[document-processing]"' in script


def test_windows_installer_backup_is_integrity_checked_and_atomically_complete() -> None:
    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    helper = (ROOT / "scripts" / "backup-sqlite.py").read_text(encoding="utf-8")

    assert "source_connection.backup(destination_connection)" in helper
    assert 'PRAGMA integrity_check' in helper
    assert '"--destination", $BackupDatabasePath' in installer
    assert "Artifact backup checksum mismatch" in installer
    assert "backup-manifest.json" in installer
    assert "Get-FileHash" in installer
    assert "backup-complete.json" in installer
    assert installer.index("backup-manifest.json.sha256") < installer.index(
        "$BackupComplete = $true"
    )


def test_windows_installer_polls_local_health_for_expected_root_and_revision() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert "Assert-StartedHubProvenance" in script
    assert 'http://127.0.0.1:$Port/health/ready' in script
    assert "deployment_root" in script
    assert "build_revision" in script
    assert "build_tree" in script
    assert "$TreeMatches" in script
    assert '@("generation_worker", "ingestion_worker", "studio_worker")' in script
    assert "[int]$Worker.start_count -ne 1" in script
    assert "Study Hub did not start from the expected root/build" in script
    assert "port ${Port}: $LastFailure" in script
    assert "port $Port: $LastFailure" not in script
