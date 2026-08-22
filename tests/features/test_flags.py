from collections.abc import Mapping
from typing import cast

import pytest
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.features import FeatureFlag as ExportedFeatureFlag
from oms_hub.features import FeatureFlags as ExportedFeatureFlags
from oms_hub.features.flags import FeatureFlag, FeatureFlags
from oms_hub.runtime_settings import RuntimeSettingsRepository

NEW_FLAGS = tuple(
    flag
    for flag in FeatureFlag
    if flag is not FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION
)


def test_feature_flag_values_match_the_shared_contract() -> None:
    assert {flag.value for flag in FeatureFlag} == {
        "source_trust_v1",
        "gemini_file_search_v1",
        "ask_studyhub_v1",
        "ask_quiz_context_v1",
        "board_question_v1",
        "adaptive_practice_v1",
        "practice_modes_v1",
        "error_notebook_v1",
        "timed_blocks_v1",
        "anki_learning_loop_v1",
        "board_runway_v1",
        "journal_evidence_v1",
        "legacy_notebooklm_generation",
    }


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_new_flags_default_off(flag: FeatureFlag) -> None:
    assert not FeatureFlags.from_mapping({}).is_enabled(flag)


@pytest.mark.parametrize("enabled", tuple(FeatureFlag))
def test_each_known_flag_isolated_when_enabled(enabled: FeatureFlag) -> None:
    flags = FeatureFlags.from_mapping({enabled.value: True})

    assert flags.is_enabled(enabled)
    assert all(
        flags.is_enabled(flag) is (flag is enabled)
        for flag in FeatureFlag
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_notebooklm_mapping_is_preserved(enabled: bool) -> None:
    flags = FeatureFlags.from_mapping(
        {FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION.value: enabled}
    )

    assert flags.is_enabled(FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION) is enabled


def test_unknown_flag_is_rejected() -> None:
    with pytest.raises(ValueError, match="invented_flag"):
        FeatureFlags.from_mapping({"invented_flag": True})


def test_multiple_unknown_flags_are_named() -> None:
    with pytest.raises(ValueError) as error:
        FeatureFlags.from_mapping({"first_unknown": True, "second_unknown": False})

    assert "first_unknown" in str(error.value)
    assert "second_unknown" in str(error.value)


@pytest.mark.parametrize("invalid", ["false", 1, None])
def test_non_bool_values_are_rejected(invalid: object) -> None:
    values = cast(Mapping[str, bool], {FeatureFlag.SOURCE_TRUST_V1.value: invalid})

    with pytest.raises(ValueError, match="source_trust_v1"):
        FeatureFlags.from_mapping(values)


def test_input_mapping_is_copied() -> None:
    values = {FeatureFlag.SOURCE_TRUST_V1.value: True}
    flags = FeatureFlags.from_mapping(values)
    values[FeatureFlag.SOURCE_TRUST_V1.value] = False

    assert flags.is_enabled(FeatureFlag.SOURCE_TRUST_V1)


def test_exposed_state_is_immutable() -> None:
    flags = FeatureFlags.from_mapping({})

    with pytest.raises(TypeError):
        flags.values[FeatureFlag.SOURCE_TRUST_V1] = True


def test_direct_construction_copies_and_freezes_values() -> None:
    values = {FeatureFlag.SOURCE_TRUST_V1: True}
    flags = FeatureFlags(values)
    values[FeatureFlag.SOURCE_TRUST_V1] = False

    assert flags.is_enabled(FeatureFlag.SOURCE_TRUST_V1)
    with pytest.raises(TypeError):
        flags.values[FeatureFlag.SOURCE_TRUST_V1] = False


def test_direct_construction_rejects_non_bool_values() -> None:
    values = cast(Mapping[FeatureFlag, bool], {FeatureFlag.SOURCE_TRUST_V1: "false"})

    with pytest.raises(ValueError, match="source_trust_v1"):
        FeatureFlags(values)


def test_direct_construction_rejects_unknown_keys() -> None:
    values = cast(Mapping[FeatureFlag, bool], {"invented_flag": True})

    with pytest.raises(ValueError, match="invented_flag"):
        FeatureFlags(values)


def test_settings_rejects_preconstructed_invalid_flags() -> None:
    values = cast(Mapping[FeatureFlag, bool], {FeatureFlag.SOURCE_TRUST_V1: "false"})

    with pytest.raises(ValueError, match="source_trust_v1"):
        Settings(_env_file=None, feature_flags=FeatureFlags(values))


def test_features_package_exports_the_public_contract() -> None:
    assert ExportedFeatureFlag is FeatureFlag
    assert ExportedFeatureFlags is FeatureFlags


def test_settings_defaults_new_flags_off_and_preserves_legacy_generation() -> None:
    settings = Settings(_env_file=None)

    assert all(not settings.feature_flags.is_enabled(flag) for flag in NEW_FLAGS)
    assert settings.feature_flags.is_enabled(FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION)


def test_settings_parses_feature_flags_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMS_HUB_FEATURE_FLAGS", '{"source_trust_v1": true}')

    settings = Settings(_env_file=None)

    assert settings.feature_flags.is_enabled(FeatureFlag.SOURCE_TRUST_V1)
    assert all(
        not settings.feature_flags.is_enabled(flag)
        for flag in NEW_FLAGS
        if flag is not FeatureFlag.SOURCE_TRUST_V1
    )
    assert settings.feature_flags.is_enabled(FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION)


def test_settings_preserves_explicit_legacy_notebooklm_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMS_HUB_FEATURE_FLAGS",
        '{"legacy_notebooklm_generation": false}',
    )

    settings = Settings(_env_file=None)

    assert not settings.feature_flags.is_enabled(
        FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION
    )


def test_runtime_settings_copy_preserves_flags_and_unrelated_configuration(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'hub.db'}"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=database_url,
        dashboard_port=8787,
        feature_flags={FeatureFlag.SOURCE_TRUST_V1.value: True},
    )
    database = Database(database_url)
    database.migrate()
    runtime_settings = RuntimeSettingsRepository(database, settings)
    runtime_settings.stage_anki_connect_port(8766, actor="test")

    try:
        effective = runtime_settings.effective_settings()

        assert effective.dashboard_port == 8787
        assert effective.anki_connect_url == "http://127.0.0.1:8766"
        assert effective.feature_flags.is_enabled(FeatureFlag.SOURCE_TRUST_V1)
        assert effective.feature_flags.is_enabled(
            FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION
        )
    finally:
        database.close()


def test_existing_app_routes_stay_available(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    app = create_app(settings)

    try:
        client = TestClient(app)
        assert client.get("/health/live").status_code == 200
        assert client.get("/settings").status_code == 200
    finally:
        app.state.database.close()
