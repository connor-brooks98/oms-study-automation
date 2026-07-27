import os
import plistlib
import subprocess
from pathlib import Path


def test_launch_agent_template_runs_read_only_agent_without_a_token() -> None:
    root = Path(__file__).parents[2]
    template = root / "scripts" / "macos" / "com.omsstudy.anki-agent.plist"
    payload = template.read_text(encoding="utf-8")

    assert "__EXECUTABLE__" in payload
    assert "__HUB_URL__" in payload
    assert "run" in payload
    assert "token" not in payload.casefold()
    assert "bearer" not in payload.casefold()


def test_installer_writes_valid_plist_and_prints_status_command(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    installer = root / "scripts" / "macos" / "install-anki-agent.sh"
    executable = tmp_path / "bin" / "oms-anki-agent"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    home = tmp_path / "home"

    result = subprocess.run(
        [
            str(installer),
            "--home",
            str(home),
            "--executable",
            str(executable),
            "--hub-url",
            "https://study-hub.example.ts.net",
            "--no-load",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )

    installed = home / "Library" / "LaunchAgents" / "com.omsstudy.anki-agent.plist"
    with installed.open("rb") as stream:
        payload = plistlib.load(stream)
    assert payload["ProgramArguments"] == [str(executable), "run"]
    assert payload["EnvironmentVariables"]["OMS_ANKI_AGENT_HUB_URL"] == (
        "https://study-hub.example.ts.net"
    )
    serialized = installed.read_text(encoding="utf-8").casefold()
    assert "token" not in serialized
    assert f"launchctl print gui/{os.getuid()}/com.omsstudy.anki-agent" in result.stdout
