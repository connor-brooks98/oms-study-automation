import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from oms_hub.db import Database


def test_chat_schema_serializes_conversation_and_preserves_existing_bank(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    assert {'study_chat_conversations', 'study_chat_requests', 'study_sessions',
            'study_session_questions'} <= set(inspect(database.engine).get_table_names())
    with database.engine.begin() as c:
        c.execute(text("INSERT INTO study_chat_conversations "
            "(id,owner_id,mode,source_snapshot_json,created_at,updated_at) "
            "VALUES ('c','owner','general','[]','now','now')"))
        values = {'id': 'r1'}
        statement = text("INSERT INTO study_chat_requests "
            "(request_id,conversation_id,request_sha256,question,model,evidence_json,"
            "history_request_ids_json,state,lifecycle_json,citation_ids_json,"
            "created_at,updated_at) "
            "VALUES (:id,'c','hash','question','chosen','[]','[]','pending','[]','[]','now','now')")
        c.execute(statement, values)
    with pytest.raises(IntegrityError), database.engine.begin() as c:
        c.execute(statement, {'id': 'r2'})
    with database.engine.begin() as c:
        c.execute(text("ALTER TABLE study_chat_requests DROP COLUMN raw_response_text"))
        c.execute(text("ALTER TABLE study_ai_settings DROP COLUMN codex_model"))
        c.execute(text("UPDATE schema_version SET version=36"))
    database.migrate()
    with database.engine.connect() as c:
        assert c.execute(text("SELECT question,raw_response_text FROM study_chat_requests "
            "WHERE request_id='r1'")).one() == ("question", None)
    database.close()
