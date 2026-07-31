import tomllib
from pathlib import Path


def test_windows_installs_include_timezone_database() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "tzdata>=2025.2; sys_platform == 'win32'" in project["project"]["dependencies"]


def test_runtime_installs_include_safe_image_decoder() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "Pillow>=11,<13" in project["project"]["dependencies"]
