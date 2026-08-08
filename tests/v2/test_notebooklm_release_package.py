import importlib.util
import zipfile
from pathlib import Path


def _builder():
    path = Path(__file__).parents[2] / "scripts" / "build-v2-release.py"
    spec = importlib.util.spec_from_file_location("release_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_release_includes_generation_runtime_and_excludes_google_state(
    tmp_path,
):
    root = Path(__file__).parents[2]
    _, source = _builder().build_releases(root, tmp_path, "test")
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())

    assert "src/oms_hub/study_generation/notebook.py" in names
    assert "src/oms_hub/study_generation/native_quiz.py" in names
    assert "src/oms_hub/study_generation/notebook_connection.py" in names
    assert "src/oms_hub/study_generation/google_docs.py" not in names
    assert "src/oms_hub/study_generation/notebook_auth.py" in names
    assert "src/oms_hub/study_generation/outline_markup.py" in names
    forbidden = (
        "storage_state",
        "notebooklm-storage",
        "browser-profile",
        "oauth-client",
        "token.json",
    )
    assert not any(any(value in name.casefold() for value in forbidden) for name in names)


def test_hotfix_contains_dependencies_and_lecture_controls(tmp_path):
    root = Path(__file__).parents[2]
    hotfix, _ = _builder().build_releases(root, tmp_path, "test")
    with zipfile.ZipFile(hotfix) as archive:
        names = set(archive.namelist())

    assert "pyproject.toml" in names
    assert "src/oms_hub/study_generation/native_quiz.py" in names
    assert "src/oms_hub/study_generation/notebook_auth.py" in names
    assert "src/oms_hub/study_generation/outline_markup.py" in names
    assert "src/oms_hub/web/public_quiz_routes.py" in names
    assert "src/oms_hub/web/static/public_quiz.js" in names
    assert "src/oms_hub/web/static/public_quiz.css" in names
    assert "src/oms_hub/web/templates/public_quiz.html" in names
    assert "src/oms_hub/web/static/public_quiz_library.js" in names
    assert "src/oms_hub/web/static/public_quiz_library.css" in names
    assert "src/oms_hub/web/templates/public_quiz_library.html" in names
    assert "src/oms_hub/web/static/lecture.js" in names
    assert "src/oms_hub/web/templates/lecture.html" in names
