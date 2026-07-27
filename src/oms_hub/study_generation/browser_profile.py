from pathlib import Path
from typing import Any


def launch_google_profile(
    chromium: Any,
    profile_path: Path,
    *,
    headless: bool,
) -> Any:
    """Launch the dedicated Study Hub profile in system Google Chrome."""

    return chromium.launch_persistent_context(
        user_data_dir=str(profile_path),
        channel="chrome",
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--password-store=basic",
        ],
        ignore_default_args=["--enable-automation"],
    )
