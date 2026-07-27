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
    assert settings.anki_agent_token_key == "anki-agent-token"
    assert settings.anki_worker_poll_seconds == 5.0


def test_explicit_anki_data_directory_and_tailnet_hostname_are_normalized(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        anki_data_dir=tmp_path / "curator",
        anki_agent_hostname="STUDY-HUB.TAILNET-NAME.TS.NET.",
    )

    assert settings.resolved_anki_data_dir == tmp_path / "curator"
    assert settings.anki_agent_hostname == "study-hub.tailnet-name.ts.net"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("anki_agent_hostname", "https://study-hub.tailnet-name.ts.net"),
        ("anki_agent_token_key", "token with spaces"),
        ("anki_focused_retrieval_limit", 0),
        ("anki_global_retrieval_limit", 0),
        ("anki_image_medium_estimate_usd", -0.01),
    ],
)
def test_anki_settings_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
