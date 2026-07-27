from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION


def test_schema_v4_adds_generation_tables(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    names = set(inspect(database.engine).get_table_names())
    assert {
        "google_connection",
        "study_prompt_settings",
        "notebook_mappings",
        "notebook_source_mappings",
        "course_quiz_documents",
        "exam_quiz_tabs",
        "generation_jobs",
        "outline_outputs",
        "quiz_outputs",
    } <= names
    with database.session() as session:
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()
    assert version == LATEST_SCHEMA_VERSION == 4
