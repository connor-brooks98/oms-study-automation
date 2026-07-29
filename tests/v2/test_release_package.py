import importlib.util
import zipfile
from pathlib import Path


def load_builder():
    path = Path(__file__).parents[2] / "scripts" / "build-v2-release.py"
    spec = importlib.util.spec_from_file_location("build_v2_release", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_builder_creates_secret_safe_hotfix_and_source_archives(tmp_path):
    builder = load_builder()
    root = Path(__file__).parents[2]

    hotfix, source = builder.build_releases(root, tmp_path, "20260726")

    with zipfile.ZipFile(hotfix) as archive:
        hotfix_names = set(archive.namelist())
    with zipfile.ZipFile(source) as archive:
        source_names = set(archive.namelist())

    assert "src/oms_hub/llm/service.py" in hotfix_names
    assert "src/oms_hub/web/static/settings.js" in hotfix_names
    assert "src/oms_hub/migrations.py" in hotfix_names
    assert "src/oms_hub/ingestion/domain.py" in hotfix_names
    assert "src/oms_hub/ingestion/service.py" in hotfix_names
    assert "src/oms_hub/ingestion/staging.py" in hotfix_names
    assert "src/oms_hub/web/upload_routes.py" in hotfix_names
    assert "src/oms_hub/web/artifact_routes.py" in hotfix_names
    assert "src/oms_hub/web/static/uploads.js" in hotfix_names
    assert "src/oms_hub/web/templates/uploads.html" in hotfix_names
    assert "src/oms_hub/web/templates/artifact_text.html" in hotfix_names
    assert "pyproject.toml" in source_names
    assert "src/oms_hub/app.py" in source_names
    assert "tests/v2/test_llm_settings_routes.py" in source_names
    for names in (hotfix_names, source_names):
        lowered = {name.casefold() for name in names}
        assert ".env" not in lowered
        assert not any(name.endswith(("hub.db", ".pyc")) for name in lowered)
        assert not any("__pycache__" in name for name in lowered)
        assert not any("gpt key" in name for name in lowered)


def test_release_contains_nuc_anki_runtime_and_no_mac_bridge(tmp_path):
    builder = load_builder()
    root = Path(__file__).parents[2]

    hotfix, source = builder.build_releases(root, tmp_path, "20260729")

    for artifact in (hotfix, source):
        with zipfile.ZipFile(artifact) as archive:
            names = set(archive.namelist())
        assert "src/oms_hub/anki/ankiconnect.py" in names
        assert "src/oms_hub/anki/runtime.py" in names
        assert "src/oms_hub/anki/apply.py" in names
        assert "src/oms_hub/anki/service.py" in names
        assert not any(name.startswith("src/oms_anki_agent/") for name in names)
        assert not any(name.startswith("scripts/macos/") for name in names)


def test_readme_and_installer_describe_nuc_local_anki() -> None:
    root = Path(__file__).parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    installer = (root / "scripts" / "install-windows.ps1").read_text(
        encoding="utf-8"
    )

    assert "127.0.0.1:8766" in readme
    assert "anki-doctor" in readme
    assert "auto_sync" in readme
    assert "tailscale serve" not in readme.casefold()
    assert "launchagent" not in readme.casefold()
    assert "OMS_HUB_ANKI_ENABLED" in installer
    assert "anki-doctor" in installer
