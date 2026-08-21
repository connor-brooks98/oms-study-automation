from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "frozen_paths.py"


def _reader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: object):
    map_path = tmp_path / "repo-map.json"
    map_path.write_text(json.dumps({"paths": paths}), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("frozen_paths", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "MAP", map_path)
    return module


def test_known_key_prints_each_frozen_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = _reader(monkeypatch, tmp_path, {"quiz_page_files": ["one", "two"]})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "quiz_page_files"])

    assert reader.main() == 0
    assert capsys.readouterr().out == "one\ntwo\n"


def test_unknown_key_fails_with_a_specific_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _reader(monkeypatch, tmp_path, {"quiz_page_files": ["one"]})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "unknown_key"])

    with pytest.raises(SystemExit, match="unknown frozen path key: unknown_key"):
        reader.main()


def test_empty_list_fails_with_a_specific_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _reader(monkeypatch, tmp_path, {"quiz_page_files": []})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "quiz_page_files"])

    with pytest.raises(SystemExit, match="frozen path key has no paths: quiz_page_files"):
        reader.main()


def test_path_with_spaces_is_preserved_as_one_output_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = _reader(monkeypatch, tmp_path, {"quiz_page_files": ["docs/with spaces/file.md"]})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "quiz_page_files"])

    assert reader.main() == 0
    assert capsys.readouterr().out == "docs/with spaces/file.md\n"


def test_absolute_script_reads_the_frozen_map_outside_the_repository_root(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "quiz_page_files"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "src/oms_hub/web/public_quiz_routes.py",
        "src/oms_hub/web/templates/public_quiz.html",
        "src/oms_hub/web/static/public_quiz.js",
        "src/oms_hub/web/static/public_quiz.css",
    ]


@pytest.mark.parametrize("contents", [None, "{"])
def test_unreadable_or_invalid_map_fails_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str | None
) -> None:
    reader = _reader(monkeypatch, tmp_path, {"quiz_page_files": ["one"]})
    map_path = tmp_path / "unreadable-map.json"
    if contents is not None:
        map_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(reader, "MAP", map_path)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "quiz_page_files"])

    with pytest.raises(SystemExit, match="unable to read frozen repository map"):
        reader.main()
