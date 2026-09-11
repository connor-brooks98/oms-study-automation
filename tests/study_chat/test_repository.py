import json
from dataclasses import replace
from uuid import uuid4

import pytest

from oms_hub.llm.codex_session import SessionLifecycle
from oms_hub.models import (
    ChatRequestModel,
    StudyRevisionModel,
)
from oms_hub.study_chat.contracts import ChatAnswer, ChatRequest
from oms_hub.study_chat.repository import ChatRepository


def request(repo, mode="lecture"):
    ids = (1,) if mode == "lecture" else ()
    cid = repo.create("owner", mode, ids)
    return ChatRequest(str(uuid4()), "owner", cid, mode, "Explain iron", ids)


def complete_provider(repo, req):
    for phase, thread, turn in (
        ("dispatching", None, None),
        ("thread_created", "thread", None),
        ("turn_started", "thread", "turn"),
        ("completed", "thread", "turn"),
    ):
        repo.record_lifecycle(
            req.request_id,
            SessionLifecycle(req.request_id, phase, thread, turn),
            owner_id=req.owner_id,
        )


def test_scope_evidence_and_idempotency_are_persisted(setup):
    repo, database, _ = setup
    req = request(repo)
    started = repo.begin(req, model="chosen")
    assert started.created and started.record.evidence
    assert not repo.begin(req, model="chosen").created
    with pytest.raises(ValueError, match="conflict"):
        repo.begin(replace(req, question="different"), model="chosen")
    with pytest.raises(ValueError, match="active"):
        repo.begin(replace(req, request_id=str(uuid4())), model="chosen")
    with database.session() as session:
        row = session.get(ChatRequestModel, req.request_id)
        assert json.loads(row.evidence_json)[0]["text"].startswith("Iron")
        assert row.model == "chosen"
    with pytest.raises(PermissionError):
        repo.load(req.conversation_id, owner_id="other")
    with pytest.raises(PermissionError):
        repo.begin(replace(req, owner_id="other"), model="chosen")
    with pytest.raises(PermissionError):
        repo.create("other", "lecture", (1,))


def test_provider_completion_does_not_accept_answer(setup):
    repo, _, _ = setup
    req = request(repo)
    record = repo.begin(req, model="chosen").record
    complete_provider(repo, req)
    stored = repo.load_request(req.request_id, owner_id="owner")
    assert stored.state == "running" and stored.answer is None
    answer = ChatAnswer("answered", "Iron stores are low.", (record.evidence[0].passage_id,))
    repo.append(req, answer)
    assert repo.load_request(req.request_id, owner_id="owner").answer == answer
    repo.append(req, answer)
    with pytest.raises(ValueError):
        repo.append(req, replace(answer, text="different"))


def test_wrong_citation_and_changed_hash_cannot_be_accepted(setup):
    repo, _, source = setup
    req = request(repo)
    record = repo.begin(req, model="chosen").record
    complete_provider(repo, req)
    with pytest.raises(ValueError, match="citation"):
        repo.append(req, ChatAnswer("answered", "Wrong", ("invented",)))
    source.write_text("Changed while provider was running")
    with pytest.raises(ValueError, match="source"):
        repo.append(req, ChatAnswer("answered", "Iron", (record.evidence[0].passage_id,)))
    assert repo.load_request(req.request_id, owner_id="owner").answer is None


def test_current_revision_and_source_scope_cannot_change(setup):
    repo, database, _ = setup
    req = request(repo)
    with pytest.raises(ValueError, match="scope"):
        repo.begin(replace(req, mode="general", revision_ids=()), model="chosen")
    with database.session() as session:
        session.get(StudyRevisionModel, 1).current = False
    with pytest.raises(ValueError, match="source"):
        repo.begin(req, model="chosen")


def test_lifecycle_is_owner_bound_ordered_and_synchronous(setup):
    repo, database, _ = setup
    req = request(repo, "general")
    repo.begin(req, model="chosen")
    event = SessionLifecycle(req.request_id, "dispatching")
    with pytest.raises(PermissionError):
        repo.record_lifecycle(req.request_id, event, owner_id="other")
    with pytest.raises(ValueError):
        repo.record_lifecycle(
            req.request_id, replace(event, request_id=str(uuid4())), owner_id="owner"
        )
    with pytest.raises(ValueError):
        repo.record_lifecycle(req.request_id, replace(event, phase="completed"), owner_id="owner")
    repo.record_lifecycle(req.request_id, event, owner_id="owner")
    with pytest.raises(ValueError, match="dispatch"):
        repo.record_lifecycle(req.request_id, event, owner_id="owner")
    with database.session() as session:
        row = session.get(ChatRequestModel, req.request_id)
        assert row.provider_phase == "dispatching"
        assert len(json.loads(row.lifecycle_json)) == 1
    repo.record_lifecycle(
        req.request_id, replace(event, phase="thread_created", thread_id="t"), owner_id="owner"
    )
    with pytest.raises(ValueError):
        repo.record_lifecycle(
            req.request_id,
            replace(event, phase="turn_started", thread_id="other-thread", turn_id="turn"),
            owner_id="owner",
        )


def test_clear_and_recovery_do_not_redispatch_or_accept_late_results(setup):
    repo, _, _ = setup
    req = request(repo, "general")
    repo.begin(req, model="chosen")
    complete_provider(repo, req)
    assert repo.interrupt_pending(owner_id="other") == 0
    assert repo.interrupt_pending(owner_id="owner") == 1
    assert repo.load_request(req.request_id, owner_id="owner").state == "interrupted"
    assert not repo.begin(req, model="chosen").created
    with pytest.raises(ValueError):
        repo.append(req, ChatAnswer("answered", "Late", ()))
    repo.clear(req.conversation_id, owner_id="owner")
    with pytest.raises(ValueError, match="cleared"):
        repo.begin(replace(req, request_id=str(uuid4())), model="chosen")


def test_history_is_scoped_and_no_support_needs_no_provider(setup):
    repo, _, _ = setup
    req = replace(request(repo), question="unmatched potassium")
    assert repo.begin(req, model="chosen").record.evidence == ()
    repo.append(req, ChatAnswer("no_support", "No matching source.", ()))
    second = replace(req, request_id=str(uuid4()), question="iron")
    record = repo.begin(second, model="chosen").record
    assert record.history_request_ids == (req.request_id,)
    other = request(repo, "general")
    assert repo.begin(other, model="chosen").record.history_request_ids == ()


def test_changed_source_is_blocked_before_dispatch(setup):
    repo, _, source = setup
    req = request(repo)
    repo.begin(req, model="chosen")
    source.write_text("changed")
    with pytest.raises(ValueError, match="source"):
        repo.record_lifecycle(
            req.request_id, SessionLifecycle(req.request_id, "dispatching"), owner_id="owner"
        )
    assert repo.load_request(req.request_id, owner_id="owner").provider_phase is None


def test_provider_errors_remain_terminal_and_owner_scoped(setup):
    repo, _, _ = setup
    req = request(repo, "general")
    repo.begin(req, model="chosen")
    with pytest.raises(PermissionError):
        repo.fail(req.request_id, owner_id="other", error_code="rate_limited")
    with pytest.raises(ValueError):
        repo.fail(req.request_id, owner_id="owner", error_code="Bearer secret token")
    repo.record_lifecycle(
        req.request_id, SessionLifecycle(req.request_id, "failed"), owner_id="owner"
    )
    repo.fail(req.request_id, owner_id="owner", error_code="rate_limited")
    stored = repo.load_request(req.request_id, owner_id="owner")
    assert (stored.state, stored.error_code) == ("failed", "rate_limited")
    assert not repo.begin(req, model="chosen").created
    with pytest.raises(ValueError):
        repo.append(req, ChatAnswer("answered", "not complete", ()))


def test_concurrent_repository_instances_only_create_one_request(setup):
    from concurrent.futures import ThreadPoolExecutor

    repo, database, _ = setup
    req = request(repo, "general")
    other_repo = ChatRepository(database.session, sources=repo.sources)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda r: r.begin(req, model="chosen"), (repo, other_repo)))
    assert sorted(r.created for r in results) == [False, True]


def test_history_character_bound_and_clear_exclude_previous_answers(setup):
    repo, _, _ = setup
    req = request(repo, "general")
    repo.begin(req, model="chosen")
    complete_provider(repo, req)
    repo.append(req, ChatAnswer("answered", "a" * 12001, ()))
    second = replace(req, request_id=str(uuid4()))
    assert repo.begin(second, model="chosen").record.history_request_ids == ()
    with pytest.raises(PermissionError):
        repo.clear(req.conversation_id, owner_id="other")
    repo.clear(req.conversation_id, owner_id="owner")
    assert repo.load_request(second.request_id, owner_id="owner").state == "interrupted"
    with pytest.raises(ValueError, match="cleared"):
        repo.record_lifecycle(
            second.request_id, SessionLifecycle(second.request_id, "dispatching"), owner_id="owner"
        )


def test_source_loader_rejects_wrong_lecture_and_file_changes_during_extraction(setup, monkeypatch):
    repo, _, source = setup
    snapshots = repo.sources.snapshot("owner", (1,))
    original = repo.sources.extractor.extract

    def expanded(ids):
        return [replace(p, lecture_id=999) for p in original(ids)]

    monkeypatch.setattr(repo.sources.extractor, "extract", expanded)
    with pytest.raises(ValueError, match="scope"):
        repo.sources.passages("owner", snapshots)

    def modified(ids):
        result = original(ids)
        source.write_text("different bytes")
        return result

    monkeypatch.setattr(repo.sources.extractor, "extract", modified)
    with pytest.raises(ValueError, match="source"):
        repo.sources.passages("owner", snapshots)


@pytest.mark.parametrize("terminal", ["interrupted", "failed"])
def test_dispatch_and_later_notifications_cannot_replay_after_terminal_state(setup, terminal):
    repo, _, _ = setup
    for phase in ("dispatching", "thread_created"):
        req = request(repo, "general")
        repo.begin(req, model="chosen")
        dispatch = SessionLifecycle(req.request_id, "dispatching")
        repo.record_lifecycle(req.request_id, dispatch, owner_id="owner")
        last = dispatch
        if phase == "thread_created":
            last = SessionLifecycle(req.request_id, "thread_created", "thread")
            repo.record_lifecycle(req.request_id, last, owner_id="owner")
            # Harmless notification replay is allowed only while this request is active.
            repo.record_lifecycle(req.request_id, last, owner_id="owner")
        if terminal == "interrupted":
            repo.interrupt_pending(owner_id="owner")
        else:
            repo.fail(req.request_id, owner_id="owner", error_code="protocol_error")
        with pytest.raises(ValueError):
            repo.record_lifecycle(req.request_id, last, owner_id="owner")
        with pytest.raises(ValueError):
            repo.record_lifecycle(req.request_id, dispatch, owner_id="owner")
