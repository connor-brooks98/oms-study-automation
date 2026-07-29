from pathlib import Path

import pytest
from pydantic import ValidationError

from oms_hub.config import Settings


def test_anki_settings_default_to_disabled_and_data_directory_child(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )

    assert settings.anki_enabled is False
    assert settings.resolved_anki_data_dir == tmp_path / "anki"
    assert settings.anki_worker_poll_seconds == 5.0


def test_anki_settings_use_fixed_nuc_loopback_boundary(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Anki.exe"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        anki_data_dir=tmp_path / "curator",
        anki_executable_path=executable,
    )

    assert settings.resolved_anki_data_dir == tmp_path / "curator"
    assert settings.anki_connect_url == "http://127.0.0.1:8766"
    assert settings.anki_executable_path == executable
    assert settings.anki_startup_timeout_seconds == 60
    assert settings.anki_startup_poll_seconds == 1
    assert not hasattr(settings, "anki_agent_hostname")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8766",
        "http://0.0.0.0:8766",
        "https://study-hub.example.ts.net",
    ],
)
def test_anki_settings_reject_noncanonical_connect_url(url: str) -> None:
    with pytest.raises(ValidationError, match="anki_connect_url"):
        Settings(_env_file=None, anki_connect_url=url)


def test_anki_settings_reject_relative_executable_path() -> None:
    with pytest.raises(ValidationError, match="anki_executable_path"):
        Settings(_env_file=None, anki_executable_path=Path("Anki.exe"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("anki_focused_retrieval_limit", 0),
        ("anki_global_retrieval_limit", 0),
        ("anki_image_medium_estimate_usd", -0.01),
    ],
)
def test_anki_settings_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
