"""Shared dependency contract for the approved Gemini SDK prerequisite."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_official_gemini_sdk_is_exactly_pinned_and_locked() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert "google-genai==2.14.0" in project["dependencies"]
    assert not any(
        dependency.startswith("google-generativeai")
        for dependency in project["dependencies"]
    )

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {
        (package["name"], package["version"])
        for package in lock["package"]
        if "version" in package
    }
    assert ("google-genai", "2.14.0") in packages

    root = next(
        package
        for package in lock["package"]
        if package["name"] == "oms-study-automation"
    )
    assert {"name": "google-genai", "specifier": "==2.14.0"} in root[
        "metadata"
    ]["requires-dist"]
