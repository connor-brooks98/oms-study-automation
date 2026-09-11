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
    database.close()
