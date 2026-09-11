import json

import pytest

from oms_hub.db import Database
from oms_hub.llm.codex_session import SessionLifecycle
from oms_hub.models import StudioRunModel
from oms_hub.study_generation.studio_repository import StudioRepository


def test_lifecycle_is_scoped_ordered_and_durable(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(StudioRunModel(id="run", subject="Heme", subject_key="heme", exam_number=3,
            destination_subject="Heme", destination_subject_key="heme", destination_exam_number=3,
            label="Lecture", prompt="", backend="codex_subscription"))
    repo = StudioRepository(database)
    request = "run:batch1"
    with pytest.raises(ValueError, match="ownership"):
        repo.record_gpt_lifecycle("run", SessionLifecycle("other:batch1", "dispatching"))
    with pytest.raises(ValueError, match="transition"):
        repo.record_gpt_lifecycle("run", SessionLifecycle(request, "turn_started", "t", "u"))
    for event in (SessionLifecycle(request, "dispatching"),
                  SessionLifecycle(request, "thread_created", "t"),
                  SessionLifecycle(request, "turn_started", "t", "u"),
                  SessionLifecycle(request, "completed", "t", "u")):
        repo.record_gpt_lifecycle("run", event)
    artifact = repo.run_artifact("run", "gpt:attempt:" + request)
    assert artifact is not None
    result = json.loads(artifact.payload_json)
    assert result['phase'] == 'completed'
    assert result['turn_id'] == 'u'
    assert len(result['events']) == 4
    repo.record_gpt_lifecycle("run", event)  # exact terminal replay is harmless
    with pytest.raises(ValueError):
        repo.record_gpt_lifecycle("run", SessionLifecycle(request, "completed", "t", "other"))
    database.close()


def test_restart_never_requeues_ambiguous_gpt_run(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        for run_id, backend in (("gpt", "codex_subscription"), ("old", "notebooklm")):
            session.add(StudioRunModel(id=run_id, subject="Heme", subject_key="heme", exam_number=3,
                destination_subject="Heme", destination_subject_key="heme",
                destination_exam_number=3,
                label=run_id, label_key=run_id, prompt="", state="running", backend=backend))
    repo = StudioRepository(database)
    assert repo.recover_interrupted_jobs() == 2
    assert repo.get_run("gpt").state.value == "interrupted"
    assert repo.get_run("old").state.value == "queued"
    assert repo.claim_next_run().id == "old"
    assert repo.claim_next_run() is None
    database.close()
