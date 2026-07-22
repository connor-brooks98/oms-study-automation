from pathlib import Path

from oms_hub.config import Settings


def test_canvas_defaults_are_local_and_discovery_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.canvas_base_url == "https://lmunet.instructure.com"
    assert settings.canvas_scan_minutes == 30
    assert settings.canvas_auto_process is False
    assert settings.study_root == Path(r"%USERPROFILE%\Documents\OMS II")
    assert settings.max_ingest_bytes == 250 * 1024 * 1024
