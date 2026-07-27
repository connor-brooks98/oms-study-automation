import os
import subprocess
from pathlib import Path
from typing import Protocol


class PromptPathPicker(Protocol):
    def select(self) -> Path | None: ...


class SystemPromptPathPicker:
    """Open a file browser on the Study Hub device."""

    _SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Choose an Obsidian prompt file'
$dialog.Filter = 'Prompt files (*.md;*.txt)|*.md;*.txt|All files (*.*)|*.*'
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $dialog.FileName
}
"""

    def select(self) -> Path | None:
        if os.name != "nt":
            raise RuntimeError(
                "Select Path is available from the Study Hub running on the NUC"
            )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-STA",
                    "-Command",
                    self._SCRIPT,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                "The NUC could not open its file browser"
            ) from error
        if result.returncode != 0:
            raise RuntimeError("The NUC could not open its file browser")
        selected = result.stdout.strip()
        if not selected:
            return None
        path = Path(selected).resolve()
        if not path.is_file():
            raise RuntimeError("The selected prompt file is no longer available")
        if path.suffix.casefold() not in {".md", ".txt"}:
            raise RuntimeError("Choose a Markdown or text prompt file")
        return path
