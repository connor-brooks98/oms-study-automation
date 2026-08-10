from pathlib import Path

from oms_hub.config import Settings

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


def test_scheduled_launcher_propagates_the_exact_serve_exit_code() -> None:
    script = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")
    lines = [line.strip() for line in script.splitlines() if line.strip()]

    serve_index = lines.index("& $HubExecutable serve")
    assert lines[serve_index + 1] == "$ServeExitCode = $LASTEXITCODE"
    assert "if ($ServeExitCode -ne 0) {" in lines[serve_index + 2 :]
    assert (
        '"Study Hub server exited with native exit code $ServeExitCode"'
        in lines[serve_index + 2 :]
    )
    assert lines[-1] == "exit $ServeExitCode"
    assert not any(
        line.casefold().startswith("throw ")
        for line in lines[serve_index + 1 :]
    )


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
    assert 'Get-CimInstance Win32_Process `' in script
    assert '-Filter "ProcessId = $ProcessId" `' in script
    assert "Stop-Process -InputObject $LiveHandle -Force" in script
    assert "Stop-Process -Id $ProcessId -Force" not in script
    assert "Stop-Process -Name python" not in script
    assert "same-root Study Hub process" in script


def test_windows_installer_revalidates_process_identity_immediately_before_force_stop() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    stop_function = script[
        script.index("function Stop-ConflictingHubProcesses") : script.index(
            "# Establish clean, exact source provenance"
        )
    ]

    rediscovery = stop_function.index("Get-CimInstance Win32_Process")
    creation_identity = stop_function.index("$LiveProcess.CreationDate")
    executable_identity = stop_function.index("$LiveProcess.ExecutablePath")
    parent_identity = stop_function.index("$LiveProcess.ParentProcessId")
    refusal = stop_function.index("refusing to stop a potentially unrelated process")
    stable_handle = stop_function.index("$null = $LiveHandle.Handle")
    handle_identity = stop_function.index("$LiveHandle.StartTime.ToUniversalTime()")
    force_stop = stop_function.index("Stop-Process -InputObject $LiveHandle -Force")

    assert rediscovery < creation_identity < executable_identity < parent_identity < refusal
    assert refusal < stable_handle < handle_identity < force_stop
    assert "[string]$LiveProcess.CommandLine -eq [string]$Process.CommandLine" in stop_function
    assert "[string]::IsNullOrWhiteSpace([string]$Process.CreationDate)" in stop_function
    assert "Stop-Process -Id $ProcessId" not in stop_function


def test_windows_installer_whatif_never_stops_processes_or_reports_completion() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    first_stop = (
        'if ($PSCmdlet.ShouldProcess($ProjectRoot, '
        '"Stop same-root Study Hub processes")) {\n'
        '  Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot\n'
        "}"
    )
    assert first_stop in script
    stop_call = "Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot"
    assert script.count(stop_call) == 3
    install_guard = script.index(
        'if ($PSCmdlet.ShouldProcess($ProjectRoot, "Install Study Hub V2")) {'
    )
    install_stop = script.index(stop_call, install_guard)
    pip_upgrade = script.index("-m pip install --upgrade pip", install_guard)
    assert install_guard < install_stop < pip_upgrade
    scheduled_guard = script.index(
        'if ($PSCmdlet.ShouldProcess($TaskName, "Install scheduled startup")) {'
    )
    scheduled_stop = script.index(stop_call, scheduled_guard)
    scheduled_start = script.index(
        "Start-ScheduledTask -TaskName $TaskName", scheduled_guard
    )
    assert scheduled_guard < scheduled_stop < scheduled_start

    final_guard = script.rindex('if ($WhatIfPreference) {')
    preview_message = script.index("install preview complete", final_guard)
    normal_completion = script.index("Study Hub V2 install complete.", final_guard)
    else_branch = script.index("} else {", preview_message, normal_completion)
    assert final_guard < preview_message < else_branch < normal_completion
    assert "No processes or files were changed." in script


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
    selected_order: list[int] = []
    while pending:
        process_id = pending.pop(0)
        if process_id in selected:
            continue
        selected.add(process_id)
        selected_order.append(process_id)
        pending.extend(
            child_id
            for child_id, parent_id in parents.items()
            if parent_id == process_id
        )

    assert selected == {101, 102, 103}
    assert list(reversed(selected_order)) == [103, 102, 101]
    assert "descendant belongs to that tree" in script
    assert "$Selected.Add($Process)" in script
    selection_start = script.index("while ($Pending.Count -gt 0)")
    selection_end = script.index("foreach ($Process in $Processes)", selection_start)
    selection_block = script[selection_start:selection_end]
    assert "-or [string]$Process.Name -ieq" not in selection_block

    stop_function = script[
        script.index("function Stop-ConflictingHubProcesses") : script.index(
            "# Establish clean, exact source provenance"
        )
    ]
    assert "$StopOrder = @($Conflicts)" in stop_function
    assert "[array]::Reverse($StopOrder)" in stop_function
    assert "foreach ($Process in $StopOrder)" in stop_function
    assert "$StableClearObservations -ge 2" in stop_function
    assert "Start-Sleep -Milliseconds 500" in stop_function


def test_windows_installer_resolves_config_stops_tree_and_backs_up_before_pip() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    provenance = script.index(
        "$PreflightBuildRevision = Get-ProjectBuildRevision"
    )
    task_stop = script.index(
        'Stop-ScheduledTask -TaskName $TaskName',
        provenance,
    )
    config = script.index("$EffectiveDataRootValue = Get-EffectiveSetting")
    stop = script.index("Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot")
    backup = script.index('"--source", $DatabasePath')
    complete = script.index("$BackupComplete = $true")
    pip = script.index('-m pip install --upgrade pip')

    assert provenance < task_stop
    assert config < stop < backup < complete < pip
    assert 'Get-EffectiveSetting `\n  -Name "OMS_HUB_DATABASE_URL"' in script
    assert "Resolve-SqliteDatabasePath" in script
    assert "Verified rollback backup was not completed; installation is blocked." in script


def test_windows_installer_preflights_config_before_downtime_without_live_db_mutation() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    preflight_database = script.index(
        '[Environment]::SetEnvironmentVariable(\n'
        '      "OMS_HUB_DATABASE_URL",\n'
        '      "sqlite:///:memory:",\n'
        '      "Process"'
    )
    preflight_validate = script.index(
        '& $ExistingHubExecutable validate-config', preflight_database
    )
    restore_environment = script.index("} finally {", preflight_validate)
    task_stop = script.index(
        "Stop-ScheduledTask -TaskName $TaskName", restore_environment
    )

    assert preflight_database < preflight_validate < restore_environment < task_stop
    assert "Existing Study Hub configuration preflight" in script
    assert "$PreviousProcessDatabaseUrl" in script


def test_windows_installer_database_default_matches_runtime_under_partial_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMS_HUB_DATA_DIR", r"D:\StudyHubData")
    monkeypatch.delenv("OMS_HUB_DATABASE_URL", raising=False)

    settings = Settings(_env_file=None)
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert settings.database_url == "sqlite:///C:/ProgramData/OMSStudyHub/hub.db"
    assert (
        '$DefaultDatabaseUrl = "sqlite:///C:/ProgramData/OMSStudyHub/hub.db"'
        in script
    )
    assert 'Join-Path $EffectiveDataRoot "hub.db"' not in script


def test_windows_installer_dotenv_resolution_matches_runtime_precedence() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    parser = script[
        script.index("function Get-DotEnvValue") : script.index(
            "function Get-EffectiveSetting"
        )
    ]

    assert "Select-Object -Last 1" in parser
    assert "Select-Object -First 1" not in parser
    assert '"\\s+#.*$"' in parser
    assert 'StartsWith("#")' in parser
    assert "(?:export\\s+)?" in parser
    assert 'if ($null -ne $ProcessValue)' in script
    assert 'if ($null -ne $FileValue)' in script


def test_windows_installer_checks_identity_at_every_stop_boundary() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    provenance = script.index("$PreflightBuildRevision = Get-ProjectBuildRevision")
    task_stop = script.index("Stop-ScheduledTask -TaskName $TaskName", provenance)
    task_check = script.rindex("Assert-ProjectBuildIdentity `", provenance, task_stop)
    first_process_stop = script.index(
        "Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot",
        task_stop,
    )
    first_process_check = script.rindex(
        "Assert-ProjectBuildIdentity `", task_stop, first_process_stop
    )
    task_start = script.index("Start-ScheduledTask -TaskName $TaskName")
    final_process_stop = script.rindex(
        "Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot",
        first_process_stop,
        task_start,
    )
    final_pre_stop_check = script.rindex(
        "Assert-ProjectBuildIdentity `", first_process_stop, final_process_stop
    )
    final_pre_start_check = script.rindex(
        "Assert-ProjectBuildIdentity `", final_process_stop, task_start
    )

    assert provenance < task_check < task_stop
    assert task_stop < first_process_check < first_process_stop
    assert first_process_stop < final_pre_stop_check < final_process_stop
    assert final_process_stop < final_pre_start_check < task_start


def test_windows_installer_pins_preflight_identity_through_startup() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert "function Assert-ProjectBuildIdentity" in script
    assert script.count("Assert-ProjectBuildIdentity `") >= 5
    metadata = script[script.index("$ConfigMetadata = [ordered]@{") :]
    assert "build_revision = $PreflightBuildRevision" in metadata
    assert "build_tree = $PreflightBuildTree" in metadata
    start = script.index("Start-ScheduledTask -TaskName $TaskName")
    final_identity_check = script.rindex("Assert-ProjectBuildIdentity `", 0, start)
    assert final_identity_check < start
    health = script.index("Assert-StartedHubProvenance `", start)
    health_block = script[health : script.index("}", health)]
    assert "-ExpectedBuildRevision $PreflightBuildRevision" in health_block
    assert "-ExpectedBuildTree $PreflightBuildTree" in health_block
    assert "$ExpectedBuildRevision = Get-ProjectBuildRevision" not in script


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


def test_windows_installer_preserves_python_launcher_argument_boundaries() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    # PowerShell enumerates the output of an if expression. A one-item array
    # returned from the true branch therefore becomes a scalar string, and
    # adding another array concatenates all arguments into one string. Keep the
    # prefix explicitly typed and force array composition at every native call.
    assert "[string[]]$PythonPrefix = @()" in script
    assert 'if ($PyLauncher) {\n  $PythonPrefix = @("-3.12")\n}' in script
    assert "$PythonPrefix = if ($PyLauncher)" not in script
    assert '$PythonVersionArgs = @($PythonPrefix) + @("--version")' in script
    assert "& $PythonCommand @PythonVersionArgs" in script
    assert "$BackupArguments = @($PythonPrefix) + @(" in script
    assert "& $PythonCommand @BackupArguments" in script
    assert (
        '$PythonVenvArgs = @($PythonPrefix) + @("-m", "venv", '
        '"$ProjectRoot\\.venv")'
        in script
    )
    assert "& $PythonCommand @PythonVenvArgs" in script

    version_build = script.index("$PythonVersionArgs =")
    version_call = script.index("& $PythonCommand @PythonVersionArgs")
    backup_build = script.index("$BackupArguments =")
    backup_call = script.index("& $PythonCommand @BackupArguments")
    venv_build = script.index("$PythonVenvArgs =")
    venv_call = script.index("& $PythonCommand @PythonVenvArgs")

    assert version_build < version_call < backup_build < backup_call
    assert backup_call < venv_build < venv_call


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
    assert "database_backed_up = $DatabaseBackedUp" in installer
    assert "database_path = $DatabasePath" in installer
    assert "source_url = $EffectiveDatabaseUrl" in installer
    assert "$ExistingTask -and -not $DatabasePresentAtPreflight" in installer
    assert "effective runtime database does not exist" in installer
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
    assert "database_reachable" in script
    assert "schema_version" in script
    assert "$TreeMatches" in script
    assert '@("generation_worker", "ingestion_worker", "studio_worker")' in script
    assert "[int]$Worker.start_count -ne 1" in script
    assert "Study Hub did not start from the expected root/build" in script
    assert "port ${Port}: $LastFailure" in script
    assert "port $Port: $LastFailure" not in script
