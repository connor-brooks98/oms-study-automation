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
            label="Lecture", prompt="", state="running", backend="codex_subscription"))
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


def test_concurrent_terminal_events_cannot_overwrite_acknowledged_completion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError

    from sqlalchemy import event as sql_event

    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(StudioRunModel(id="run", subject="Heme", subject_key="heme", exam_number=3,
            destination_subject="Heme", destination_subject_key="heme", destination_exam_number=3,
            label="Lecture", prompt="", state="running", backend="codex_subscription"))
    repo = StudioRepository(database)
    for lifecycle in (SessionLifecycle("run:b", "dispatching"),
                      SessionLifecycle("run:b", "thread_created", "t"),
                      SessionLifecycle("run:b", "turn_started", "t", "u")):
        repo.record_gpt_lifecycle("run", lifecycle)
    reads = Barrier(2)

    def align_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT studio_run_artifacts."):
            try:
                reads.wait(timeout=0.15)
            except BrokenBarrierError:
                pass  # Serialized reads cannot meet; an unguarded implementation can.

    sql_event.listen(database.engine, "after_cursor_execute", align_reads)

    def finish(phase):
        try:
            repo.record_gpt_lifecycle("run", SessionLifecycle("run:b", phase, "t", "u"))
            return phase
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(finish, ("completed", "failed")))
    sql_event.remove(database.engine, "after_cursor_execute", align_reads)
    assert sum(value is not None for value in accepted) == 1
    saved = json.loads(repo.run_artifact("run", "gpt:attempt:run:b").payload_json)
    assert saved['phase'] == next(value for value in accepted if value is not None)
    assert len(saved['events']) == 4
    database.close()


def test_explicit_cancel_resume_and_stopped_review_are_owner_bound(tmp_path):
    from oms_hub.llm.codex_session import SessionError

    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(StudioRunModel(id="run", subject="Heme", subject_key="heme", exam_number=3,
            destination_subject="Heme", destination_subject_key="heme", destination_exam_number=3,
            label="Lecture", prompt="", state="running", backend="codex_subscription"))
    repo = StudioRepository(database)
    repo.save_run_artifact("run", "gpt:settings", "hash", '{"owner_id":"owner"}')
    with pytest.raises(ValueError, match="ownership"):
        repo.control_gpt_run("run", owner_id="other", action="cancel")
    repo.record_gpt_lifecycle("run", SessionLifecycle("run:b", "dispatching"))
    repo.control_gpt_run("run", owner_id="owner", action="cancel")
    assert repo.gpt_cancelled("run")
    with pytest.raises(ValueError, match="active run"):
        repo.record_gpt_lifecycle("run", SessionLifecycle("run:late", "dispatching"))
    with pytest.raises(ValueError, match="stopped before review"):
        repo.await_import_review("run", ())
    with pytest.raises(ValueError, match="unfinished"):
        repo.control_gpt_run("run", owner_id="owner", action="resume")
    repo.record_gpt_lifecycle("run", SessionLifecycle("run:b", "interrupted"))
    with pytest.raises(ValueError, match="still stopping"):
        repo.control_gpt_run("run", owner_id="owner", action="resume")
    repo.stop_gpt_run("run", SessionError("interrupted"))
    repo.control_gpt_run("run", owner_id="owner", action="resume")
    assert repo.gpt_resume_requested("run") and not repo.gpt_cancelled("run")
    run = repo.claim_next_run()
    assert run is not None and run.id == "run"
    repo.stop_gpt_run("run", SessionError("rate_limited", reset_at="2026-09-12T00:00:00+00:00"))
    assert repo.get_run("run").state.value == "paused"
    assert repo.claim_next_run() is None
    database.close()


def test_cancel_preflight_waits_for_worker_stop_and_restart_clears_wait(tmp_path):
    from oms_hub.llm.codex_session import SessionError

    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(StudioRunModel(id="run", subject="Heme", subject_key="heme", exam_number=3,
            destination_subject="Heme", destination_subject_key="heme", destination_exam_number=3,
            label="Lecture", prompt="", state="running", backend="codex_subscription"))
    repo = StudioRepository(database)
    repo.save_run_artifact("run", "gpt:settings", "hash", '{"owner_id":"owner"}')
    repo.control_gpt_run("run", owner_id="owner", action="cancel")
    with pytest.raises(ValueError, match="still stopping"):
        repo.control_gpt_run("run", owner_id="owner", action="resume")
    repo.stop_gpt_run("run", SessionError("interrupted"))
    repo.control_gpt_run("run", owner_id="owner", action="resume")
    assert repo.claim_next_run().id == "run"
    repo.control_gpt_run("run", owner_id="owner", action="cancel")
    repo.recover_interrupted_jobs()
    repo.control_gpt_run("run", owner_id="owner", action="resume")
    assert repo.claim_next_run().id == "run"
    database.close()
