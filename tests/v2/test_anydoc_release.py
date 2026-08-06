"""Release contracts for the Anydoc-backed Quiz Builder."""

from __future__ import annotations

import importlib.util
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]


def _release_builder() -> ModuleType:
    path = ROOT / "scripts" / "build-v2-release.py"
    spec = importlib.util.spec_from_file_location("anydoc_release_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_metadata_pins_document_processors_and_keeps_python_312_range() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert project["requires-python"] == ">=3.12,<3.14"
    extras = project["optional-dependencies"]
    assert extras["document-processing"] == ["firecrawl-anydoc==0.1.3"]
    assert extras["pdf-inspection"] == [
        "pdf-inspector @ git+https://github.com/firecrawl/pdf-inspector.git@ae6246ba0c39008931b67f9cee1a898ee405d023"
    ]
    assert project["scripts"]["oms-anki-agent"] == "oms_anki_agent.cli:main"


def test_source_release_contains_quiz_builder_runtime_and_web_assets(tmp_path: Path) -> None:
    _, source = _release_builder().build_releases(ROOT, tmp_path, "acceptance")
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())

    assert {
        "src/oms_hub/document_processing/__init__.py",
        "src/oms_hub/document_processing/anydoc_adapter.py",
        "src/oms_hub/document_processing/shadow.py",
        "src/oms_hub/study_generation/practice_answers.py",
        "src/oms_hub/study_generation/practice_review.py",
        "src/oms_hub/study_generation/quiz_import_worker.py",
        "src/oms_hub/web/templates/studio_quiz_review.html",
        "src/oms_hub/web/templates/public_quiz_library.html",
        "src/oms_hub/web/static/studio_quiz_review.js",
        "src/oms_hub/web/static/public_quiz_library.js",
    } <= names
