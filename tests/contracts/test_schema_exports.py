"""Snapshot tests for generated grounded-learning wire schemas."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_NAMES = (
    "knowledge-v1.json",
    "ask-v1.json",
    "question-v1.json",
    "mastery-v1.json",
    "practice-v1.json",
    "journal-v1.json",
)
RESERVED_NAMES = SCHEMA_NAMES[2:]


def _export(output_dir: Path) -> None:
    environment = os.environ | {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}
    subprocess.run(
        [
            sys.executable,
            "scripts/export_grounded_contract_schemas.py",
            "--output-dir",
            str(output_dir),
        ],
        check=True,
        cwd=ROOT,
        env=environment,
    )


def test_exported_schemas_are_reproducible_and_match_snapshots(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _export(first)
    _export(second)

    for name in SCHEMA_NAMES:
        generated = (first / name).read_bytes()
        assert generated == (ROOT / "schemas" / name).read_bytes()
        assert generated == (second / name).read_bytes()
        assert str(ROOT).encode() not in generated
        json.loads(generated)


def test_active_schemas_export_current_wire_contracts_and_reserved_are_fail_closed(
    tmp_path: Path,
) -> None:
    _export(tmp_path)
    knowledge = json.loads((tmp_path / "knowledge-v1.json").read_text(encoding="utf-8"))
    ask = json.loads((tmp_path / "ask-v1.json").read_text(encoding="utf-8"))

    assert knowledge["$defs"]["AuthorityClass"]["enum"] == [
        "course_material",
        "published_journal",
        "generated_artifact",
        "question_style_reference",
    ]
    assert knowledge["$defs"]["TruthMode"]["enum"] == [
        "course_only",
        "course_and_literature",
        "literature_only",
    ]
    assert set(knowledge["$defs"]["RetrievalScope"]["properties"]) == {
        "course_id",
        "exam_id",
        "lecture_ids",
        "truth_mode",
        "source_revision_ids",
    }
    assert set(ask["$defs"]["AnswerEventType"]["enum"]) == {
        "status",
        "delta",
        "citations",
        "done",
        "error",
    }
    assert set(ask["properties"]) == {"event_type", "payload"}

    for name in RESERVED_NAMES:
        reserved = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert reserved["not"] == {}
