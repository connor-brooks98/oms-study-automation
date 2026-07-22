from sqlalchemy import inspect

from oms_hub.canvas.domain import ArtifactRole, ConnectionState, SourceKind
from oms_hub.db import Database


def test_canvas_domain_values_are_stable() -> None:
    assert ConnectionState.LOGIN_REQUIRED.value == "canvas_login_required"
    assert SourceKind.LECTURE.value == "lecture"
    assert ArtifactRole.LOCAL_PDF.value == "local_pdf"


def test_create_schema_adds_phase_2_tables_without_removing_lectures(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.create_schema()
    tables = set(inspect(database.engine).get_table_names())
    assert "lectures" in tables
    assert {
        "canvas_connections",
        "canvas_course_mappings",
        "canvas_source_items",
        "source_revisions",
        "artifacts",
        "processing_jobs",
    } <= tables
