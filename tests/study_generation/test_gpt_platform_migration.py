import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from oms_hub.db import Database


def test_upgrade_preserves_legacy_run_and_enforces_bank_attempt_identity(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        for table in ("bank_topic_reviews", "bank_attempts", "bank_import_rows",
                      "bank_questions", "bank_imports"):
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("ALTER TABLE studio_runs DROP COLUMN backend"))
        connection.execute(text("UPDATE schema_version SET version=31 WHERE id=1"))
        connection.execute(text("INSERT INTO studio_runs (id,subject,subject_key,exam_number,"
            "destination_subject,destination_subject_key,destination_exam_number,label,label_key,"
            "prompt,workflow_kind,content_kind,state,stage,attempts,created_at,updated_at) VALUES "
            "('old','Heme','heme',3,'Heme','heme',3,'Old','old','','notebook_generation',"
            "'exam_review','complete','complete',1,'2026-09-11','2026-09-11')"))
    database.migrate()
    assert {'bank_imports', 'bank_questions', 'bank_import_rows', 'bank_attempts',
            'bank_topic_reviews'} <= set(inspect(database.engine).get_table_names())
    with database.engine.begin() as connection:
        assert connection.execute(text("SELECT backend FROM studio_runs WHERE id='old'"))\
            .scalar_one() == 'notebooklm'
        connection.execute(text("INSERT INTO bank_imports (id,learner_id,source,product,export_id,"
            "digest,provenance_json,imported_at,receipt_json) VALUES "
            "('i','owner','user','test','e','hash','{}','2026-09-11','{}')"))
        connection.execute(text(
            "INSERT INTO bank_questions (id,source,product,external_question_id,"
            "body_ready,reviewed_topics_json) VALUES (1,'user','test','00123',0,'[]')"))
        connection.execute(text("INSERT INTO bank_import_rows (id,import_id,row_number,question_id,"
            "canonical_row_hash,row_json,user_note,tags_json,topics_json,issues_json,body_ready) "
            "VALUES (1,'i',1,1,'hash','{}','','[]','[]','[]',0)"))
        connection.execute(text("INSERT INTO bank_attempts (learner_id,question_id,"
            "external_attempt_id,result,imported_at,import_row_id) VALUES "
            "('owner',1,'attempt','unknown','2026-09-11',1)"))
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(text("INSERT INTO bank_attempts (learner_id,question_id,"
            "external_attempt_id,result,imported_at,import_row_id) VALUES "
            "('owner',1,'attempt','correct','2026-09-11',1)"))
    database.close()
