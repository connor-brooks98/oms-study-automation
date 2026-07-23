from pathlib import Path

from oms_hub.config import Settings


def test_canvas_defaults_are_local_and_discovery_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.canvas_base_url == "https://lmunet.instructure.com"
    assert settings.canvas_scan_minutes == 30
    assert settings.canvas_auto_process is False
    assert settings.study_root == Path(r"%USERPROFILE%\Documents\OMS II")
    assert settings.max_ingest_bytes == 250 * 1024 * 1024


def test_panopto_defaults_are_bounded_and_secret_free() -> None:
    settings = Settings(_env_file=None)

    assert settings.panopto_tenant_url == "https://lmunet.hosted.panopto.com"
    assert not hasattr(settings, "panopto_client_id")
    assert not hasattr(settings, "panopto_oauth_redirect_uri")
    assert settings.panopto_poll_minutes == 15
    assert settings.panopto_poll_start == "09:20"
    assert settings.panopto_poll_end == "19:00"
    assert settings.openai_model == "gpt-5.6-terra"
    assert settings.transcript_prompt_path == Path(
        r"C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md"
    )
