from __future__ import annotations

import subprocess
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
