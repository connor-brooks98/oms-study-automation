from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXECUTABLES = (
    "scripts/task28/private-shadow-controller.ps1",
    "scripts/task28/private-shadow-launcher.ps1",
    "scripts/task28/private-shadow-composition.ps1",
)


def test_task28_composition_executables_are_tracked() -> None:
    for executable in EXECUTABLES:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", executable],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 0, f"composition executable is untracked: {executable}"


def test_entrypoint_has_no_untracked_reviewed_operator_dependency() -> None:
    entrypoint = ROOT / "scripts" / "private-shadow-operator-entry.py"

    assert "private-shadow-operator-reviewed.py" not in entrypoint.read_text(encoding="utf-8")


def test_task28_composition_never_references_transient_executables() -> None:
    sources = [
        ROOT / "scripts" / "private-shadow-operator-entry.py",
        *(ROOT / executable for executable in EXECUTABLES),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    assert "/private/tmp/task28-" not in text
    assert "task28-*-composition" not in text


def test_composition_stages_and_verifies_a_source_bound_fake_path() -> None:
    composition = (
        ROOT / "scripts" / "task28" / "private-shadow-composition.ps1"
    ).read_text(encoding="utf-8")
    controller = (
        ROOT / "scripts" / "task28" / "private-shadow-controller.ps1"
    ).read_text(encoding="utf-8")
    launcher = (
        ROOT / "scripts" / "task28" / "private-shadow-launcher.ps1"
    ).read_text(encoding="utf-8")
    text = "\n".join((composition, controller, launcher))

    assert 'ValidateSet("Stage", "Verify")' in composition
    assert "git -C $RepositoryRoot archive" in composition
    assert ".task28-source-commit" in composition
    assert "mutable_state_path" in composition
    assert "hub_health_url" in composition
    assert "Register-ScheduledTask" not in text
    assert "private-shadow-launcher.ps1" in controller
    assert "private-shadow-operator-entry.py" in launcher
    assert "run-private-shadow-evidence.ps1" in launcher


def test_stage_uses_a_prefixed_virtual_commit_marker(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--prefix=source/",
            f"--add-virtual-file=source/.task28-source-commit:{commit}",
            f"--output={archive}",
            commit,
        ],
        cwd=ROOT,
        check=True,
    )

    with tarfile.open(archive) as bundle:
        assert "source/.task28-source-commit" in bundle.getnames()
    composition = (ROOT / "scripts" / "task28" / "private-shadow-composition.ps1").read_text(
        encoding="utf-8"
    )
    assert '"--add-virtual-file=source/.task28-source-commit:$SourceCommit"' in composition


def test_verify_uses_only_the_explicit_offline_seam() -> None:
    composition = (ROOT / "scripts" / "task28" / "private-shadow-composition.ps1").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "scripts" / "task28" / "private-shadow-launcher.ps1").read_text(
        encoding="utf-8"
    )

    assert "OMS_TASK28_COMPOSITION_VERIFY" in composition
    assert "OMS_TASK28_COMPOSITION_VERIFY" in launcher
    assert "RUN_PRIVATE_GEMINI_SHADOW" not in launcher
    assert "OMS_TASK28_PRIVATE_PROJECT" not in launcher
    assert "OMS_TASK28_PRIVATE_SCRATCH" not in launcher


def test_stage_uses_an_atomic_sibling_and_rejects_equal_roots() -> None:
    composition = (ROOT / "scripts" / "task28" / "private-shadow-composition.ps1").read_text(
        encoding="utf-8"
    )

    assert "$FinalDestination -ceq $MutableStatePath" in composition
    assert "$StageRoot" in composition
    assert "Move-Item -LiteralPath $StageRoot -Destination $FinalDestination" in composition


def test_composition_uses_case_insensitive_path_equality_and_hashed_manifest() -> None:
    composition = (ROOT / "scripts" / "task28" / "private-shadow-composition.ps1").read_text(
        encoding="utf-8"
    )

    assert "[StringComparison]::OrdinalIgnoreCase" in composition
    assert "run-manifest.$RunManifestHash.json" in composition
    assert "[IO.Directory]::Move" in composition


def test_launcher_uses_a_sanitized_explicit_composition_verify_child() -> None:
    launcher = (ROOT / "scripts" / "task28" / "private-shadow-launcher.ps1").read_text(
        encoding="utf-8"
    )
    wrapper = (ROOT / "scripts" / "run-private-shadow-evidence.ps1").read_text(
        encoding="utf-8"
    )

    assert "-CompositionVerify" in launcher
    assert "[Parameter(Mandatory = $true)][switch]$CompositionVerify" in wrapper
    assert ".EnvironmentVariables.Clear()" in wrapper
    assert '"OMS_TASK28_PRIVATE_PROJECT"' in wrapper
    assert '"PYTHONPATH"' in wrapper
