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
    image_review_files = {
        "src/oms_hub/study_generation/quiz_images.py",
        "src/oms_hub/web/templates/studio_quiz_images.html",
        "src/oms_hub/web/static/studio_quiz_images.js",
    }
    assert image_review_files <= hotfix_names
    assert image_review_files <= source_names
    preview_files = {
        "src/oms_hub/web/templates/studio_quiz_preview.html",
        "src/oms_hub/web/static/studio_quiz_preview.js",
    }
    assert preview_files <= hotfix_names
    assert preview_files <= source_names
    for names in (hotfix_names, source_names):
        lowered = {name.casefold() for name in names}
        assert ".env" not in lowered
        assert not any(name.endswith(("hub.db", ".pyc")) for name in lowered)
        assert not any("__pycache__" in name for name in lowered)
        assert not any("gpt key" in name for name in lowered)
