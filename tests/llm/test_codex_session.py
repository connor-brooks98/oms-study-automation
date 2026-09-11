import os
import runpy
import subprocess
from pathlib import Path

import pytest

from oms_hub.llm.codex_session import model_ready


def test_missing_image_capability_cannot_enable_image_generation():
    assert model_ready({"model": "chosen", "inputModalities": ["text"]}, images=True) is False
    assert model_ready({"model": "chosen"}, images=True) is False
    assert model_ready({"model": "chosen", "inputModalities": ["text", "image"]}, images=True)


@pytest.mark.parametrize("modalities", [None, "text,image", {}, [], ["image"], ["text", 1]])
def test_malformed_or_missing_text_metadata_is_not_ready(modalities):
    assert not model_ready({"model": "chosen", "inputModalities": modalities}, images=False)


def test_text_only_model_is_available_only_for_text():
    assert model_ready({"model": "chosen", "inputModalities": ["text"]}, images=False)
    assert not model_ready({"model": "", "inputModalities": ["text"]}, images=False)


def load_probe():
    return runpy.run_path(str(Path(__file__).parents[2] / "scripts/probe-codex-session.py"))


@pytest.mark.parametrize("argv", [[], ["--login"], ["--smoke"], ["--login", "--smoke"]])
def test_probe_import_and_modes_never_start_processes(monkeypatch, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("offline probe launched a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    probe = load_probe()
    with pytest.raises(SystemExit) as error:
        probe["main"](argv)
    assert error.value.code == 2


def test_probe_rejects_changed_schema_or_version(tmp_path):
    probe = load_probe()
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    with pytest.raises(ValueError, match="incompatible Codex schema"):
        probe["offline_probe"](schema, probe["PINNED_VERSION"])
    with pytest.raises(ValueError, match="incompatible Codex version"):
        probe["offline_probe"](schema, "codex-cli 0.0.0")


@pytest.mark.parametrize("payload", [{}, {"login_id": "x"}, {"loginId": "x", "token": "x"}])
def test_probe_rejects_drifted_fixture_members(payload):
    check = load_probe()["check_fixture_members"]
    with pytest.raises(ValueError):
        check({"required": ["loginId"], "properties": {"loginId": {"type": "string"}}}, payload)


@pytest.mark.skipif(
    not os.environ.get("CODEX_SESSION_SCHEMA"), reason="local schema export optional"
)
def test_exported_schema_and_fixtures_pass_without_live_readiness(capsys):
    probe = load_probe()
    schema = Path(os.environ["CODEX_SESSION_SCHEMA"])
    report = probe["offline_probe"](schema, probe["PINNED_VERSION"])
    assert report["fixture_member_checks"] == len(probe["FIXTURES"])
    assert report["live_ready"] is False
    assert report["provider_verified"] is False
    assert report["windows_verified"] is False
    assert report["restrictions_verified"] is False
    assert probe["main"](["--schema", str(schema)]) == 0
    assert '"offline_contract": "passed"' in capsys.readouterr().out
