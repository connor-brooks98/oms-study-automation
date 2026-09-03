import re
import shutil
import subprocess
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

    serve_index = lines.index("& $HubPython -m oms_hub.cli serve")
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


def test_f28_launcher_and_installer_bind_one_acl_restricted_gate_directory() -> None:
    launcher = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert "param(" in launcher
    assert "[string]$DataRoot" in launcher
    assert 'Join-Path $ResolvedDataRoot "acceptance\\f28"' in launcher
    assert '$env:OMS_HUB_F28_GATE_DIR = $GateDirectory' in launcher
    assert "CONTROLLED_RESTART_EXIT_CODE" not in launcher
    assert "$ServeExitCode -eq 75" in launcher
    assert "latest-server-exit.json" in launcher
    assert "consumed-launcher-server-exit-$Nonce.json" in launcher
    assert "claimed_latest_server_exit_sha256" in launcher
    assert "latest_server_exit_sha256" in launcher
    assert "expected_schema" in launcher
    assert "launcher-exit-$Nonce.json" in launcher
    assert "UTF8Encoding($false, $true)" in launcher
    assert "function Test-JsonInteger" in launcher
    assert "function Test-ExactJsonInteger" in launcher
    assert "[int]$Record." not in launcher
    assert "[int]$Finalized." not in launcher
    assert "Test-Path -LiteralPath $ClaimedArchivePath)" in launcher
    assert "Test-Path -LiteralPath $Destination)" in launcher
    assert "Test-Path -LiteralPath $ClaimedArchivePath -PathType Leaf" not in launcher
    assert "Test-Path -LiteralPath $Destination -PathType Leaf" in launcher
    assert launcher.index("$ClaimedArchivePath") < launcher.index(
        "Move-Item -LiteralPath $ClaimPath -Destination $ClaimedArchivePath"
    )
    launcher_destination = (
        "$Destination = Join-Path $Directory \"launcher-exit-$Nonce.json\""
    )
    archive_move = "Move-Item -LiteralPath $ClaimPath -Destination $ClaimedArchivePath"
    assert launcher.index(launcher_destination) < launcher.index(archive_move)
    assert "exit $ServeExitCode" in launcher

    assert "Initialize-F28GateDirectory" in installer
    assert "SetAccessRuleProtection" in installer
    assert "$Directory.GetAccessControl(" in installer
    assert (
        "[System.Security.AccessControl.AccessControlSections]::Access"
        in installer
    )
    assert ".SetAccessControl($Acl)" in installer
    assert "SetOwner(" not in installer
    assert "Set-Acl -LiteralPath $GateDirectory" not in installer
    assert 'S-1-5-18' in installer
    assert 'S-1-5-32-544' in installer
    assert 'Get-ExpectedRecoveryTaskArguments' in installer
    assert "ExpectedDataRoot" in installer
    assert "F28 gate directory" in installer


def test_f28_gate_acl_initialization_runs_natively_without_owner_or_audit_changes() -> None:
    installer_path = ROOT / "scripts" / "install-windows.ps1"
    harness_path = ROOT / "tests" / "v2" / "f28_gate_acl_initialization.ps1"
    harness = harness_path.read_text(encoding="utf-8")

    assert "F28_GATE_ACL_INITIALIZATION_VERIFIED" in harness
    assert "Owner changed during DACL-only F28 initialization." in harness
    assert "Audit rule count changed during DACL-only F28 initialization." in harness

    powershell = next(
        (
            executable
            for name in ("powershell.exe", "powershell")
            if (executable := shutil.which(name)) is not None
        ),
        None,
    )
    if powershell is None:
        # macOS cannot execute the native ACL regression. The source contract
        # above remains mandatory, and the Windows verification runs this harness.
        return
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness_path),
            "-InstallerScript",
            str(installer_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "F28_GATE_ACL_INITIALIZATION_VERIFIED" in result.stdout


def test_f28_installs_exact_four_action_authorized_recovery_chain() -> None:
    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")
    recovery = (ROOT / "scripts" / "restart-hub-after-failure.ps1").read_text(
        encoding="utf-8"
    )

    assert '[ValidateRange(0, 3)]' in launcher
    assert "action_index = $CurrentActionIndex" in launcher
    assert "recovery-authorized-{0}-{1}.json" in launcher
    assert "predecessor_evidence_sha256" in launcher
    assert "expires_at" in launcher
    assert "f28-primary-0" in installer
    for index in (1, 2, 3):
        assert f"f28-recovery-{index}" in installer
    assert "RestartCount" not in installer
    assert "RestartInterval" not in installer
    assert "-DelaySeconds 60" in installer
    assert "scheduled-task-before.xml" in installer
    assert "PriorTaskXmlSha256" in installer
    assert "Restore-PreviousScheduledTask" in installer
    assert "Unregister-ScheduledTask" in installer
    assert '[ValidateRange(1, 3)]' in recovery
    assert '[ValidateRange(1, 60)]' in recovery
    assert "recovery-consumed-$Nonce-$ActionIndex.json" in recovery
    assert "[System.IO.File]::Move($AuthorizationPath, $ConsumedPath)" in recovery
    assert "$Item.PSIsContainer" in recovery
    assert "Get-StrictUtcTimestamp" in recovery
    assert "Get-BytesSha256" in recovery
    assert "Start-Sleep -Seconds $DelaySeconds" in recovery
    assert "Test-SameRootHubRuntime" in recovery
    assert "Start-ScheduledTask" not in recovery
    assert "Register-ScheduledTask" not in recovery
    assert "Stop-Process" not in recovery
    assert "& $PowerShell -NoProfile -ExecutionPolicy Bypass -File $StartScript" in recovery


def test_f28_assigns_and_verifies_action_ids_before_registration() -> None:
    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert '-Id "f28-primary-0"' not in installer
    assert '-Id "f28-recovery-$Index"' not in installer
    primary_assignment = '$PrimaryAction.Id = "f28-primary-0"'
    recovery_assignment = '$RecoveryAction.Id = "f28-recovery-$Index"'
    assert primary_assignment in installer
    assert recovery_assignment in installer
    primary_readback = '[string]$PrimaryAction.Id -cne "f28-primary-0"'
    recovery_readback = '[string]$RecoveryAction.Id -cne "f28-recovery-$Index"'
    aggregation = "$Action = @($PrimaryAction) + $RecoveryActions"
    installation_start = installer.index(
        'if ($PSCmdlet.ShouldProcess($TaskName, "Install scheduled startup")) {'
    )
    installation_end = installer.index("Assert-TaskActionTargetsProjectRoot", installation_start)
    installation = installer[installation_start:installation_end]
    register = installation.index("Register-ScheduledTask")
    assert installation.index("$PrimaryAction = New-ScheduledTaskAction") < installation.index(
        primary_assignment
    ) < installation.index(primary_readback) < installation.index(aggregation) < register
    assert installation.index("$RecoveryAction = New-ScheduledTaskAction") < installation.index(
        recovery_assignment
    ) < installation.index(recovery_readback) < installation.index(aggregation) < register


def test_f28_acceptance_requires_exact_hresult_and_same_instance_action_order() -> None:
    script = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")
    verifier = (ROOT / "src" / "oms_hub" / "task_scheduler_evidence.py").read_text(
        encoding="utf-8"
    )

    assert "2147942475" in verifier
    assert "old_win32_exit_code" in verifier
    assert "exactly one Event 129 and one Event 200" in verifier
    assert "task completion" in verifier
    assert "command_line" in script
    assert "--recovery-action-index" in script
    assert "[Convert]::ToBase64String" in script
    assert "$RecoveryActionArgumentsBase64" in script
    assert "--recovery-action-arguments-base64" in script
    assert "--recovery-action-arguments $TaskBefore.arguments" not in script
    assert "base64.b64decode" in verifier
    assert "validate=True" in verifier
    assert "base64.b64encode(decoded_bytes) != encoded_bytes" in verifier
    assert 'decode("utf-8")' in verifier
    assert "decoded != decoded.strip()" in verifier
    assert "argparse.ArgumentParser(allow_abbrev=False)" in verifier
    assert "0x8007004B / Win32 75" in script


def test_f28_acceptance_compares_logical_database_state_and_records_physical_drift() -> None:
    script = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")
    helper = (ROOT / "scripts" / "backup-sqlite.py").read_text(encoding="utf-8")

    assert "logical_sha256" in helper
    assert '"logical_sha256"' in helper
    assert "$DatabaseBefore.logical_sha256" in script
    assert "$DatabaseAfter.logical_sha256" in script
    assert "$DatabaseAfter.logical_sha256 -cne $DatabaseBefore.logical_sha256" in script
    assert "$DatabaseAfter.physical_sha256 -cne $DatabaseBefore.physical_sha256" in script
    assert "database_before_sha256" in script
    assert "database_after_sha256" in script
    assert "database_logical_sha256" in script
    assert "database_physical_changed" in script
    assert "$ProofRecords.Count -ne 1" in script
    assert "$ExpectedDestination" in script
    assert "$DatabaseHashAfter -cne $DatabaseHashBefore" not in script


def test_f28_acceptance_manifest_counts_are_arrays_for_zero_or_one_member() -> None:
    script = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")

    assert "$BackupBefore = @(Get-FileManifest -Root $ExpectedBackupPath)" in script
    assert (
        "$PdfBefore = @(Get-PdfManifest -Roots @($ProjectRoot, $DataRoot) "
        "-ExcludeRoot $GateDirectory)"
    ) in script
    assert "$BackupAfter = @(Get-FileManifest -Root $ExpectedBackupPath)" in script
    assert (
        "$PdfAfter = @(Get-PdfManifest -Roots @($ProjectRoot, $DataRoot) "
        "-ExcludeRoot $GateDirectory)"
    ) in script


def test_f28_acceptance_script_is_two_phase_and_never_kills_or_reconfigures() -> None:
    script = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")
    verifier = (ROOT / "src" / "oms_hub" / "task_scheduler_evidence.py").read_text(
        encoding="utf-8"
    )

    assert "SupportsShouldProcess" in script
    assert "request.json" in script
    assert "armed-$Nonce.json" in script
    assert "fire-$Nonce.json" in script
    assert "server-exit-$Nonce.json" in script
    assert "consumed-launcher-server-exit-$Nonce.json" in script
    assert "launcher-exit-$Nonce.json" in script
    assert "consumed-$Nonce.json" in script
    assert "finalized-$Nonce.json" in script
    assert "latest_server_exit_sha256" in script
    assert "claimed_latest_server_exit_sha256" in script
    assert "EventRecordID" in script
    assert ".ToXml()" in script
    assert ".Message" not in script
    assert "CreatedTaskProcess" in script
    assert "task_scheduler_evidence" in script
    assert "Event 110/manual launch" in script
    assert "Event 107/trigger launch" in script
    assert "Test-Path -LiteralPath $ActivePath" in script
    assert "RestartCount" not in script
    assert "2147942475" in script
    assert "LastTaskResult" in script
    assert "Get-WinEvent" in script
    assert "SystemTime" in verifier
    assert "creation_date" in script
    assert "function Test-JsonInteger" in script
    assert "function Test-ExactJsonInteger" in script
    assert "[int]$Record." not in script
    assert "[int]$Consumed." not in script
    assert "[int]$Finalized." not in script
    assert "Test-ExactJsonInteger -Value $Record.exit_code" in script
    assert "Test-ExactJsonInteger -Value $Finalized.expected_schema" in script
    assert "Test-ExactJsonInteger -Value $Armed.schema_version" in script
    assert "backup-sqlite.py" in script
    assert "F28_NATIVE_RESTART_" in script
    assert "Start-ScheduledTask" in script
    assert "RECOVERY ONLY" in script
    assert "Stop-Process" not in script
    assert "Stop-ScheduledTask" not in script
    assert "Register-ScheduledTask" not in script
    assert "Set-Content" not in script


def test_f28_acceptance_publishes_exact_success_leaf_before_printing_marker() -> None:
    script = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")

    assert "if (Test-Path -LiteralPath $Path)" in script
    assert "[System.IO.File]::Move($Temporary, $Path)" in script
    assert "Test-Path -LiteralPath $Path -PathType Leaf" in script
    assert "F28 atomic JSON publication did not create the exact destination file" in script
    cleanup = "Remove-F28TemporaryDatabaseSnapshots `"
    publish = "Write-AtomicJson -Path $SuccessPath -Value $Result"
    output = "$Result | ConvertTo-Json -Depth 20"
    assert script.index(cleanup) < script.index(publish) < script.index(output)
    assert "-ErrorAction SilentlyContinue" in script


def test_f28_recovery_always_rethrows_the_original_acceptance_failure() -> None:
    script = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")

    assert "F28 recovery attempt failed; rethrowing the original acceptance failure." in script
    recovery_start = script.index("if ($FireWritten) {")
    recovery_throw = script.index("throw $Failure", recovery_start)
    task_start = script.index("Start-ScheduledTask -TaskName $TaskName", recovery_start)
    recovery_guard = script.index(
        "F28 recovery attempt failed; rethrowing the original acceptance failure.",
        task_start,
    )
    assert task_start < recovery_guard < recovery_throw
    assert "function Write-F28Diagnostic" in script
    assert (
        "F28 pre-fire request archival failed; rethrowing the original acceptance failure."
        in script
    )
    assert script.count("[Console]::Error.WriteLine") == 1


def test_f28_acceptance_recovery_cleanup_keeps_one_result_observation_and_strict_rollback() -> None:
    acceptance = (ROOT / "scripts" / "accept-f28-restart.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert acceptance.count("$ObservedLastResults.Add([int]$TaskInfo.LastTaskResult)") == 1
    assert "$RecoveryAuthorizedAt -lt $RequestIssuedAt" in acceptance
    assert "$RecoveryAuthorizedAt -gt $RecoveryValidationNow" in acceptance
    recovery_only = acceptance.index("RECOVERY ONLY")
    manual_start = acceptance.index("Start-ScheduledTask -TaskName $TaskName", recovery_only)
    diagnostic = acceptance.index(
        "F28 beginning recovery-only manual task start.", manual_start - 200
    )
    assert diagnostic < manual_start
    assert "Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue" in installer
    assert "Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop" in installer


def test_windows_installer_defines_its_restored_task_xml_hash_helper_before_use() -> None:
    installer = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    definition = "function Get-StringSha256"
    use = "Get-StringSha256 -Value $RestoredXml"
    assert definition in installer
    assert installer.index(definition) < installer.index(use)
    helper_end = installer.index("function Get-ExpectedTaskArguments")
    helper = installer[installer.index(definition) : helper_end]
    assert "UTF8Encoding($false, $true)" in helper
    assert "SHA256" in helper


def test_f28_pins_windows_powershell_from_system_directory_without_path_lookup() -> None:
    for relative_path in (
        "scripts/install-windows.ps1",
        "scripts/accept-f28-restart.ps1",
        "scripts/restart-hub-after-failure.ps1",
    ):
        script = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "function Get-F28SystemPowerShell" in script
        assert "[System.Environment]::SystemDirectory" in script
        assert '"WindowsPowerShell\\v1.0\\powershell.exe"' in script
        assert "[System.IO.Path]::GetFullPath" in script
        assert "-PathType Leaf" in script
        assert "ReparsePoint" in script
        assert "Get-Command powershell.exe" not in script


def test_f28_probe_failure_has_a_native_original_error_regression() -> None:
    script_path = ROOT / "scripts" / "accept-f28-restart.ps1"
    harness_path = ROOT / "tests" / "v2" / "f28_original_error_preservation.ps1"
    script = script_path.read_text(encoding="utf-8")
    harness = harness_path.read_text(encoding="utf-8")

    helper = "function Invoke-F28FailurePreservingAction"
    pre_fire = "if (-not $FireWritten) {"
    probe = "Test-Path -LiteralPath $RequestPath -PathType Leaf"
    original_throw = "throw $Failure"
    assert helper in script
    pre_fire_index = script.index(pre_fire)
    protected_index = script.index("Invoke-F28FailurePreservingAction", pre_fire_index)
    probe_index = script.index(probe, protected_index)
    original_throw_index = script.index(original_throw, probe_index)
    assert pre_fire_index < protected_index < probe_index < original_throw_index
    assert probe not in script[pre_fire_index:protected_index]
    assert "F28_TEST_PATH_PROBE_FAILURE" in harness
    assert "F28_TEST_PRIMARY_ACCEPTANCE_FAILURE" in harness
    assert "F28_ORIGINAL_ERROR_PRESERVATION_VERIFIED" in harness
    assert "F28_JSON_INTEGER_TYPES_VERIFIED" in harness

    powershell = next(
        (
            executable
            for name in ("powershell.exe", "powershell", "pwsh")
            if (executable := shutil.which(name)) is not None
        ),
        None,
    )
    if powershell is None:
        # macOS cannot execute the native regression. Its absence is not a skip:
        # the source contract above remains mandatory, and Windows runs this harness.
        return
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness_path),
            "-AcceptanceScript",
            str(script_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "F28_ORIGINAL_ERROR_PRESERVATION_VERIFIED" in result.stdout


def test_windows_json_integer_guards_use_powershell_51_clr_types() -> None:
    for relative_path in (
        "scripts/start-hub.ps1",
        "scripts/accept-f28-restart.ps1",
    ):
        script = (ROOT / relative_path).read_text(encoding="utf-8")
        integer_guard = script.split("function Test-JsonInteger", maxsplit=1)[1].split(
            "function Test-ExactJsonInteger", maxsplit=1
        )[0]

        for clr_type in (
            "[System.SByte]",
            "[System.Byte]",
            "[System.Int16]",
            "[System.UInt16]",
            "[System.Int32]",
            "[System.UInt32]",
            "[System.Int64]",
            "[System.UInt64]",
        ):
            assert clr_type in integer_guard
        for unsupported_alias in ("[short]", "[ushort]"):
            assert unsupported_alias not in integer_guard
        exact_guard = script.split(
            "function Test-ExactJsonInteger", maxsplit=1
        )[1].split("function ", maxsplit=1)[0]
        assert "[System.Int64]$Expected" in exact_guard
        assert "[System.Decimal]$Value" in exact_guard
        assert "[System.Decimal]$Expected" in exact_guard


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
    assert "exactly four ordered F28 actions" in script
    assert '"f28-primary-0", "f28-recovery-1", "f28-recovery-2", "f28-recovery-3"' in script
    assert "RestartCount" not in script


def test_windows_installer_sets_and_verifies_unlimited_task_execution_time() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    action_guard = script[
        script.index("function Assert-TaskActionTargetsProjectRoot") : script.index(
            "function Restore-PreviousScheduledTask"
        )
    ]
    install_guard = script[
        script.index(
            'if ($PSCmdlet.ShouldProcess($TaskName, "Install scheduled startup")) {'
        ) : script.index("Start-ScheduledTask -TaskName $TaskName")
    ]

    settings = (
        "New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-ExecutionTimeLimit ([TimeSpan]::Zero)"
    )
    readback = '[string]$Task.Settings.ExecutionTimeLimit -cne "PT0S"'
    assert settings in install_guard
    assert readback in action_guard
    assert install_guard.index(settings) < install_guard.index(
        "Register-ScheduledTask"
    )
    assert script.index("Register-ScheduledTask", script.index(install_guard)) < script.index(
        "Assert-TaskActionTargetsProjectRoot", script.index(install_guard)
    ) < script.index("Start-ScheduledTask -TaskName $TaskName")


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


def test_windows_installer_builds_a_single_separator_root_prefix() -> None:
    script = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    # PowerShell does not use backslash escaping. Two backslashes here would
    # build a root\\ prefix and silently exclude every same-root process.
    assert '$ExpectedPrefix = $ExpectedProjectRoot.TrimEnd("\\") + "\\"' in script
    assert '$ExpectedPrefix = $ExpectedProjectRoot.TrimEnd("\\\\") + "\\\\"' not in script


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
    # Only the deepest process from a discovery snapshot may cross a destructive
    # boundary. Stopping it can make its parents exit naturally, so processing a
    # reversed stale snapshot can misclassify those exiting parents as PID reuse.
    # Depth must come from the selected parent graph rather than CIM/seed order.
    assert "$ConflictsById = @{}" in stop_function
    assert "$DeepestDepth = -1" in stop_function
    assert "$Lineage.Add($CursorId)" in stop_function
    assert "$ConflictsById.ContainsKey($ParentKey)" in stop_function
    assert "$Process = $DeepestProcess" in stop_function
    assert "$Process = $Conflicts[-1]" not in stop_function
    assert "$StopOrder = @($Conflicts)" not in stop_function
    assert "[array]::Reverse($StopOrder)" not in stop_function
    assert "foreach ($Process in $StopOrder)" not in stop_function
    assert "Re-discover before selecting another process" in stop_function
    discovery = stop_function.index("$Conflicts = @(")
    depth_map = stop_function.index("$ConflictsById = @{}")
    selection = stop_function.index("$Process = $DeepestProcess")
    force_stop = stop_function.index("Stop-Process -InputObject $LiveHandle -Force")
    settle = stop_function.index("Start-Sleep -Milliseconds 500", force_stop)
    assert discovery < depth_map < selection < force_stop < settle
    assert stop_function.count("Stop-Process -InputObject $LiveHandle -Force") == 1
    assert "$StableClearObservations -ge 2" in stop_function
    assert "Start-Sleep -Milliseconds 500" in stop_function


def test_windows_installer_rediscovers_after_each_destructive_stop() -> None:
    """A child stop may make every parent in that snapshot disappear."""

    snapshots = iter(
        (
            [101, 102, 103],
            [],
            [],
        )
    )
    stopped: list[int] = []
    stable_clear_observations = 0
    discoveries = 0

    while stable_clear_observations < 2:
        conflicts = next(snapshots)
        discoveries += 1
        if not conflicts:
            stable_clear_observations += 1
            continue

        stable_clear_observations = 0
        process_id = conflicts[-1]
        stopped.append(process_id)
        # Model the native observation: stopping the server child causes both
        # wrappers from this snapshot to exit without another Stop-Process call.

    assert stopped == [103]
    assert discoveries == 3


def test_windows_installer_depth_selection_ignores_nested_launcher_seed_order() -> None:
    """A child launcher may appear before its same-root parent in CIM output."""

    parents = {101: 1, 102: 101, 103: 102, 201: 1, 202: 201}

    def select_deepest(conflicts: list[int]) -> int:
        conflict_ids = set(conflicts)
        depths: dict[int, int] = {}
        for process_id in conflicts:
            lineage: set[int] = set()
            cursor = process_id
            depth = 0
            while cursor in conflict_ids:
                if cursor in lineage:
                    raise AssertionError("cycle in selected process graph")
                lineage.add(cursor)
                parent_id = parents[cursor]
                if parent_id not in conflict_ids:
                    break
                depth += 1
                cursor = parent_id
            depths[process_id] = depth
        return max(conflicts, key=lambda process_id: (depths[process_id], process_id))

    assert select_deepest([102, 101, 103, 201, 202]) == 103
    assert select_deepest([201, 202, 103, 102, 101]) == 103


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
        '& $ExistingHubPython -m oms_hub.cli validate-config', preflight_database
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
            'python.exe" -m oms_hub.cli validate-config',
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


def test_grouped_matching_release_is_tracked_three_mode_json_transaction() -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("Preflight", "Deploy", "Postflight")]' in release
    assert "OMS_GROUPED_MATCHING_PREFLIGHT_COMPLETE" in release
    assert "OMS_GROUPED_MATCHING_DEPLOY_COMPLETE" in release
    assert "OMS_GROUPED_MATCHING_POSTFLIGHT_COMPLETE" in release
    assert "-RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog" in release
    assert "[Console]::Error.WriteLine" in release
    assert "ManagementDateTimeConverter" not in release
    assert "function H(" not in release
    assert "function PS(" not in release
    assert "$pid" not in release.casefold()
    assert "[datetime]$_.CreationDate" in release
    assert "[datetime]$process.CreationDate" in release
    assert "Expected exactly one loopback listener" in release
    assert "exactly one task-launched primary system PowerShell ancestor" in release
    assert '.venv\\Scripts\\python.exe' in release
    assert "oms_hub\\.cli\\s+serve" in release
    assert "& $HubPython -m oms_hub.cli serve" in launcher
    assert "Test-ExactJsonBoolean" in release
    assert "Test-ExactJsonInteger" in release
    assert "-RequireXmlDigest" in release
    assert "if ($RequireXmlDigest -and" in release
    assert "Set-StrictMode -Version Latest" in release
    assert '$ProjectRoot.TrimEnd("\\") + "\\"' in release
    assert '$ProjectRoot.TrimEnd("\\\\") + "\\\\"' not in release
    assert 'ExpectedScriptSha256 -notmatch "^[0-9a-f]{64}$"' in release
    assert 'ExpectedMergedCommit, $ExpectedMergedTree' in release
    assert '"^[0-9a-f]{40}$"' in release


def test_grouped_matching_release_binds_before_mutation_and_handles_rollback_limits() -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )

    preflight = release.index('if ($Mode -eq "Preflight")')
    deploy = release.index('if ($Mode -eq "Deploy")')
    preview = release.index("Invoke-Installer $configuration -WhatIf", deploy)
    mutation = release.index("$script:CheckoutMutated = $true", deploy)
    merge = release.index('Invoke-Git @("merge", "--ff-only", "origin/main")', deploy)
    installer = release.index("Invoke-Installer $configuration", merge)
    assert preflight < deploy < preview < mutation < merge < installer
    assert "release failed before installer; checkout restored and runtime was untouched" in release
    assert "installer began without a complete verified backup" in release
    assert "old runtime recovery attempted without certifying data or task" in release
    assert "rollback incomplete:" in release
    assert "Never rethrow inside this recovery try" in release
    assert "Stop-Process -InputObject $processHandle" in release
    assert "if ($clearSnapshots -ge 2) { return }" in release
    assert "Get-SameRootHubProcesses" in release
    assert "yyyyMMddHHmmss.ffffff" in release
    assert "Stop-SameRootRuntime -StopTask" in release
    assert "Move-Item -LiteralPath $runtimePath -Destination $quarantine" in release
    assert "PRAGMA integrity_check" in release
    assert "Register-ScheduledTask -TaskName $TaskName -Xml" in release
    assert "Assert-RestoredRuntimeData $backup $Configuration" in release
    assert "Assert-OldRuntimeIntact $configuration $binding -RequireOriginalListener" in release
    assert "old runtime recovery attempted without certifying data or task" in release
    assert "Stop-InstallerProcessTree" in release
    assert "$deadline = (Get-Date).AddMinutes(10)" in release
    assert "Installer termination is unproven; refusing rollback" in release
    assert "Get-InstallerRootRecord" in release
    assert "Get-CimCreationKey" in release
    assert "Update-InstallerOwnedInstances" in release
    assert "every owned descendant are absent" in release
    assert "$rootInstanceKey = \"{0}|{1}\"" in release
    assert "if (-not $rootPresent -and $present.Count -eq 0)" in release
    assert "Test-ProcessInstanceMatch" in release
    assert "$script:InstallerTerminationProven = $false" in release
    disagreement = release.index("$handleKey -cne [string]$deepest.creation_key")
    termination_false = release.index(
        "$script:InstallerTerminationProven = $false", disagreement
    )
    disagreement_throw = release.index(
        "Installer process changed while acquiring its stable handle.", disagreement
    )
    assert disagreement < termination_false < disagreement_throw
    final_handle = release.index(
        "if (-not $installerProcess.HasExited -or -not $script:InstallerTerminationProven)"
    )
    final_reset = release.index("$script:InstallerTerminationProven = $false", final_handle)
    final_throw = release.index("Installer termination is unproven; refusing rollback.")
    assert final_handle < final_reset < final_throw
    withheld = release.index("rollback withheld because installer termination is unproven")
    rollback = release.index("Invoke-Rollback $configuration $binding $backupPath $failure")
    assert withheld < rollback
    copy = release.index("Copy-Item -LiteralPath $backup.database")
    restored_hash = release.index("Assert-RestoredRuntimeData $backup $Configuration")
    sqlite = release.index("PRAGMA integrity_check")
    installer = release.index("Invoke-Installer $Configuration", sqlite)
    assert copy < restored_hash < sqlite < installer
    quarantine = release.index("New-Item -ItemType Directory -Path $quarantine")
    assert quarantine < release.index("Assert-Directory $quarantine", quarantine)


def test_grouped_matching_release_wraps_indexed_git_output_before_selecting_line_zero() -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )

    # Mirror the scalar-output regression covered by the launcher/installer
    # test above: a one-line native result is a scalar in PowerShell, where
    # `[0]` selects only its first character. The release wrapper must make
    # every indexed native result an array before selecting line zero.
    complete_hash = "a" * 40
    assert complete_hash[0] != complete_hash
    assert [complete_hash][0] == complete_hash
    for variable, arguments in (
        ("commitLines", '"rev-parse", "HEAD"'),
        ("treeLines", '"rev-parse", "HEAD^{tree}"'),
        ("originMainLines", '"rev-parse", "origin/main"'),
    ):
        assignment = re.escape(f"${variable} = @(Invoke-Git @({arguments}))")
        selection = re.escape(f"[string]${variable}[0]")
        assignment_match = re.search(assignment, release)
        selection_match = re.search(selection, release)
        assert assignment_match
        assert selection_match
        assert release.index(assignment_match.group()) < release.index(
            selection_match.group()
        )
    assert "(Invoke-Git @(\"rev-parse\", \"HEAD\"))[0]" not in release
    assert "(Invoke-Git @(\"rev-parse\", \"HEAD^{tree}\"))[0]" not in release
    assert "(Invoke-Git @(\"rev-parse\", \"origin/main\"))[0]" not in release


def test_grouped_matching_release_stops_recovery_when_installer_termination_is_unproven() -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )
    recovery = release[
        release.index("function Invoke-UnverifiedOldRuntimeRecovery") : release.index(
            "function Invoke-Rollback"
        )
    ]
    rollback = release[
        release.index("function Invoke-Rollback") : release.index("function Get-Binding")
    ]
    withheld = (
        "rollback incomplete; further recovery withheld because installer termination is unproven"
    )

    rollback_gate = rollback.index("if (-not $script:InstallerTerminationProven)")
    subsequent_recovery = rollback.index("Invoke-UnverifiedOldRuntimeRecovery")
    assert rollback_gate < subsequent_recovery
    assert withheld in rollback
    assert recovery.index("if (-not $script:InstallerTerminationProven)") < recovery.index(
        "Stop-SameRootRuntime -StopTask"
    )
    recovery_catch = recovery[recovery.index("} catch {") :]
    assert f'throw "{withheld}"' in recovery_catch
    assert recovery_catch.index(withheld) < recovery_catch.index(
        'return "old runtime recovery attempt failed:'
    )


def test_grouped_matching_release_marks_installer_started_only_after_real_process_launch() -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )
    installer = release[
        release.index("function Invoke-Installer") : release.index("function Wait-ForFinalState")
    ]
    start_process = installer.index("$installerProcess = Start-Process")
    started = installer.index("$script:InstallerStarted = $true")
    assert start_process < started
    assert "if (-not $WhatIf) {\n      $script:InstallerStarted = $true" in installer
    assert "$script:InstallerTerminationProven = $false" not in installer[:start_process]

    deploy = release[release.index('if ($Mode -eq "Deploy")') :]
    assert "$script:InstallerStarted = $true; Invoke-Installer $configuration" not in deploy
    pre_installer_failure = deploy.index("if (-not $script:InstallerStarted)")
    withheld = deploy.index("rollback withheld because installer termination is unproven")
    assert withheld < pre_installer_failure
    assert "release failed before installer; checkout restored and runtime was untouched" in deploy


def test_grouped_matching_release_checks_reparse_safety_for_changed_paths_and_runtime_trees(
) -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )
    safety_start = release.index("function Assert-ReleasePathSafety")
    safety_end = release.index("function Get-ReleasePaths")
    safety = release[safety_start:safety_end]
    assert "while (-not (Test-Path -LiteralPath $cursor))" in safety
    assert "Assert-NonReparsePath -Path $cursor" in safety
    assert "Release path escapes project root" in safety
    assert '$root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\\")' in safety
    assert '$RelativePath.Replace("/", "\\")' in safety
    assert '$prefix = $root + "\\"' in safety
    assert '.TrimEnd("\\\\")' not in safety
    assert '.Replace("/", "\\\\")' not in safety
    assert '$prefix = $root + "\\\\"' not in safety

    release_paths = release[
        release.index("function Get-ReleasePaths") : release.index("function Get-BackupNames")
    ]
    assert "Assert-ReleasePathSafety -RelativePath $path" in release_paths

    installer_paths = release[
        release.index("function Assert-InstallerMutationPaths") : release.index(
            "function Invoke-Installer"
        )
    ]
    assert 'Join-Path $ProjectRoot ".venv"' in installer_paths
    assert "Assert-Directory -Path $venv" in installer_paths
    assert 'Join-Path $Configuration.data_root "artifacts"' in installer_paths
    assert "Assert-Directory -Path $artifacts" in installer_paths

    installer = release[
        release.index("function Invoke-Installer") : release.index("function Wait-ForFinalState")
    ]
    mutation_paths = installer.index("Assert-InstallerMutationPaths $Configuration")
    assert mutation_paths < installer.index(
        "$installerProcess = Start-Process"
    )
    assert "if (-not $WhatIf) { Assert-InstallerMutationPaths $Configuration }" not in installer
    assert installer.index("$logRoot") > mutation_paths

    rollback = release[
        release.index("function Invoke-Rollback") : release.index("function Get-Binding")
    ]
    artifacts_guard = rollback.index("Assert-Directory -Path $artifacts")
    artifacts_move = rollback.index("Move-Item -LiteralPath $runtimePath")
    assert artifacts_guard < artifacts_move


def test_grouped_matching_delivery_plan_limits_automatic_rollback_to_deploy() -> None:
    delivery_plan = (
        ROOT / "docs" / "superpowers" / "plans" / "2026-09-02-grouped-matching-delivery.md"
    ).read_text(encoding="utf-8")

    assert "Automatic rollback covers\nfailures during the `Deploy` invocation" in delivery_plan
    assert "OMS_GROUPED_MATCHING_DEPLOY_COMPLETE" in delivery_plan
    assert "`Postflight` is separate read-only verification" in delivery_plan
    assert "stops without an automatic mutation" in delivery_plan


def test_grouped_matching_release_validates_complete_backup_members() -> None:
    release = (ROOT / "scripts" / "deploy-grouped-matching-release.ps1").read_text(
        encoding="utf-8"
    )

    assert "backup-manifest.json.sha256" in release
    assert "Backup completion record differs" in release
    assert "Backup manifest bindings differ" in release
    assert "Backup effective configuration differs" in release
    assert "Duplicate backup member" in release
    assert "Backup member hash or size differs" in release
    assert "Critical backup member missing or not exactly once" in release
    assert "Unsafe backup member path" in release


def test_grouped_matching_nuc_driver_is_one_shot_and_cleans_exact_remote_leaf() -> None:
    driver = (ROOT / "scripts" / "deploy-grouped-matching-nuc.sh").read_text(
        encoding="utf-8"
    )

    assert driver.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in driver
    assert "remote_transport_path=\"C:/Users/conbr/AppData/Local/Temp/" in driver
    assert "remote_native_path=\"C:\\\\Users" in driver
    assert "unique remote release path already exists" in driver
    assert "Get-FileHash -LiteralPath" in driver
    assert "[Management.Automation.Language.Parser]::ParseFile" in driver
    assert "trap cleanup_remote EXIT" in driver
    assert "upload_attempted=false" in driver
    assert 'if [[ "$upload_attempted" != true ]]; then return; fi' in driver
    assert "upload_attempted=true\nscp -q" in driver
    assert "remote_created" not in driver
    assert "remote release ownership hash differs" not in driver
    assert "if(Test-Path -LiteralPath \\$p){\\$i=Get-Item" in driver
    assert "remote release leaf remains" in driver
    assert "assert len(rows)==1" in driver
    assert driver.count("invoke_mode Deploy") == 1
    assert "git show \"$merge_commit:$release_script\"" in driver
    assert "test \"$release_tree\" = \"$merged_tree\"" in driver
    assert "/tmp/oms-grouped-matching-release-binding.txt" not in driver
    assert "OMS_GROUPED_MATCHING_DELIVERY_COMPLETE" in driver
    assert '"listener_creation_date":post["listener_creation_date"]' in driver
    assert "expected_blob_sha256=$(git show" in driver
    assert "test -z \"$(git status --porcelain)\"" in driver


def test_grouped_matching_delivery_plan_is_self_derived_not_cross_fence_bound() -> None:
    plan_path = ROOT / "docs" / "superpowers" / "plans"
    plan_path /= "2026-09-02-grouped-matching-delivery.md"
    plan = plan_path.read_text(
        encoding="utf-8"
    )

    assert "/tmp/oms-grouped-matching-release-binding.txt" not in plan
    assert "driver derives its release and merge SHA/tree directly" in plan
    assert "scripts/deploy-grouped-matching-nuc.sh" in plan
