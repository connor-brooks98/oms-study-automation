from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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
    assert settings.anki_worker_lease_seconds == 120
    assert settings.anki_worker_max_stage_attempts == 3
    assert settings.anki_semantic_model == "voyage-4-large"
    assert settings.anki_semantic_dimensions == 1024
    assert settings.anki_semantic_min_coverage == 0.995
    assert settings.anki_connect_url == "http://127.0.0.1:8765"
    assert settings.dashboard_port == 8787
    assert settings.transcript_prompt_path is None


def test_voyage_key_reads_standard_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "local-voyage-secret")

    settings = Settings(_env_file=None)

    assert isinstance(settings.voyage_api_key, SecretStr)
    assert settings.voyage_api_key.get_secret_value() == "local-voyage-secret"


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
        ("anki_worker_max_stage_attempts", 0),
    ],
)
def test_anki_settings_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_anki_connect_url_must_be_loopback(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            anki_connect_url="http://192.168.1.20:8765",
        )


def test_anki_connect_url_accepts_custom_loopback_port(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        anki_connect_url="http://127.0.0.1:8766/",
    )

    assert settings.anki_connect_url == "http://127.0.0.1:8766"


def test_enabled_anki_requires_a_distinct_dashboard_port(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="cannot share port"):
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            anki_enabled=True,
            dashboard_port=8766,
            anki_connect_url="http://127.0.0.1:8766",
        )

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        anki_enabled=True,
        dashboard_port=8787,
        anki_connect_url="http://127.0.0.1:8766",
    )

    assert settings.dashboard_port == 8787


def test_rehearsal_settings_require_every_mutable_path_inside_overlay(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "overlay"
    with pytest.raises(ValidationError, match="database.*inside the overlay"):
        Settings(
            _env_file=None,
            data_dir=overlay / "data",
            anki_data_dir=overlay / "anki",
            database_url=f"sqlite:///{tmp_path / 'outside.db'}",
            anki_enabled=True,
            anki_rehearsal_mode="deterministic",
            anki_rehearsal_overlay_dir=overlay,
            anki_rehearsal_replay_dir=overlay / "replay",
            study_root=overlay / "study",
            icloud_staging_root=overlay / "icloud-staging",
        )


def test_rehearsal_settings_are_loopback_nonpublic_and_git_static(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "overlay"
    base = {
        "data_dir": overlay / "data",
        "anki_data_dir": overlay / "anki",
        "database_url": f"sqlite:///{overlay / 'hub.db'}",
        "anki_enabled": True,
        "anki_rehearsal_mode": "deterministic",
        "anki_rehearsal_overlay_dir": overlay,
        "anki_rehearsal_replay_dir": overlay / "replay",
        "study_root": overlay / "study",
        "icloud_staging_root": overlay / "icloud-staging",
    }
    settings = Settings(_env_file=None, **base)
    assert settings.anki_rehearsal_mode == "deterministic"
    for update in (
        {"dashboard_host": "0.0.0.0"},
        {"public_hostname": "study.example.com"},
        {"anki_prompt_git_sync": True},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **base, **update)


def test_rehearsal_rejects_the_default_windows_study_root(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    with pytest.raises(ValidationError, match="study root.*inside the overlay"):
        Settings(
            _env_file=None,
            data_dir=overlay / "data",
            anki_data_dir=overlay / "anki",
            database_url=f"sqlite:///{overlay / 'hub.db'}",
            anki_enabled=True,
            anki_rehearsal_mode="deterministic",
            anki_rehearsal_overlay_dir=overlay,
            anki_rehearsal_replay_dir=overlay / "replay",
            icloud_staging_root=overlay / "icloud-staging",
        )


def test_rehearsal_rejects_unfenced_or_missing_input_roots(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    base = {
        "data_dir": overlay / "data",
        "anki_data_dir": overlay / "anki",
        "database_url": f"sqlite:///{overlay / 'hub.db'}",
        "anki_enabled": True,
        "anki_rehearsal_mode": "deterministic",
        "anki_rehearsal_overlay_dir": overlay,
        "anki_rehearsal_replay_dir": overlay / "replay",
        "study_root": overlay / "study",
        "icloud_staging_root": overlay / "icloud-staging",
    }
    with pytest.raises(ValidationError, match="requires an iCloud staging root"):
        Settings(_env_file=None, **{**base, "icloud_staging_root": None})
    for field in (
        "anki_prompt_directory",
        "anki_fixture_artifact_path",
        "transcript_prompt_path",
    ):
        with pytest.raises(ValidationError, match="inside the materialized overlay"):
            Settings(_env_file=None, **base, **{field: tmp_path / "outside"})

    materialized_inputs = overlay / "sources" / "a0data"
    settings = Settings(
        _env_file=None,
        **base,
        anki_prompt_directory=materialized_inputs / "prompts",
        anki_fixture_artifact_path=materialized_inputs / "fixture.json",
        transcript_prompt_path=materialized_inputs / "transcript.md",
    )
    assert settings.anki_prompt_directory == materialized_inputs / "prompts"
    assert settings.anki_fixture_artifact_path == materialized_inputs / "fixture.json"
    assert settings.transcript_prompt_path == materialized_inputs / "transcript.md"
