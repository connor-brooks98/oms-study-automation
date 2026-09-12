import json

import pytest

from oms_hub.llm.codex_session import SessionLifecycle, SessionResult
from oms_hub.models import StudyTopicSuggestionModel
from oms_hub.study_progress.tags import TopicService
from oms_hub.study_progress.taxonomy import TAXONOMIES

from .test_blocks import blocks  # noqa: F401
from .test_sessions import sessions  # noqa: F401


class FakeClient:
    def __init__(self, text=None):
        self.text = text
        self.calls = []

    def generate(self, request, *, cancelled, on_lifecycle):
        self.calls.append(request)
        for phase, thread, turn in (
            ("dispatching", None, None),
            ("thread_created", "thread", None),
            ("turn_started", "thread", "turn"),
            ("completed", "thread", "turn"),
        ):
            on_lifecycle(SessionLifecycle(request.request_id, phase, thread, turn))
        return SessionResult(
            "thread",
            "turn",
            self.text
            if self.text is not None
            else json.dumps(
                {
                    "tags": [
                        {
                            "canonical_id": TAXONOMIES[0].categories[0].canonical_id,
                            "evidence_quote": "Which?",
                            "confidence": 0.8,
                        }
                    ]
                }
            ),
        )


def service_for(block_service, tmp_path, client):
    return TopicService(
        blocks=block_service, media_root=tmp_path, client=client, model=lambda: "chosen"
    )


def test_suggestions_are_frozen_pending_until_explicit_review(blocks, tmp_path):  # noqa: F811
    block_service, bank, database, _ = blocks
    key = block_service.catalog("owner").questions[0].key
    client = FakeClient()
    service = service_for(block_service, tmp_path, client)
    prepared = service.prepare("owner", key.question_id)
    completed = service.run("owner", prepared.id)
    assert completed.state == "completed" and completed.tags[0].topic.review_state == "pending"
    assert bank.get_question(key).topics == ()
    assert json.loads(client.calls[0].source_text)["taxonomy"] == list(prepared.taxonomy)
    assert service.run("owner", prepared.id).id == prepared.id and len(client.calls) == 1
    with pytest.raises(PermissionError):
        service.load("other", prepared.id)
    service.accept("owner", prepared.id, (completed.tags[0].topic.canonical_id,))
    assert bank.get_question(key).topics[0].review_state == "accepted"
    assert bank.iter_attempts(learner_id="owner") == ()
    with database.session() as session:
        row = session.get(StudyTopicSuggestionModel, prepared.id)
        assert row.raw_response_text and row.state == "completed"
        assert "raw_response_text" not in vars(service.load("owner", prepared.id))


@pytest.mark.parametrize(
    "output",
    [
        "broken json",
        json.dumps(
            {"tags": [{"canonical_id": "invented", "evidence_quote": "Which?", "confidence": 0.9}]}
        ),
        json.dumps(
            {
                "tags": [
                    {
                        "canonical_id": TAXONOMIES[0].categories[0].canonical_id,
                        "evidence_quote": "invented source",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
    ],
)
def test_invalid_raw_is_retained_but_never_becomes_analytics(blocks, tmp_path, output):  # noqa: F811
    block_service, bank, database, _ = blocks
    key = block_service.catalog("owner").questions[0].key
    service = service_for(block_service, tmp_path, FakeClient(output))
    prepared = service.prepare("owner", key.question_id)
    assert service.run("owner", prepared.id).state == "failed"
    with database.session() as session:
        assert session.get(StudyTopicSuggestionModel, prepared.id).raw_response_text == output
    assert bank.get_question(key).topics == ()
    with pytest.raises(ValueError):
        service.accept("owner", prepared.id, (TAXONOMIES[0].categories[0].canonical_id,))


def test_unavailable_stale_review_and_interrupted_requests_never_dispatch(blocks, tmp_path):  # noqa: F811
    from dataclasses import replace

    from oms_hub.llm.codex_session import SessionError

    block_service, bank, database, publications = blocks
    key = block_service.catalog("owner").questions[0].key
    client = FakeClient()
    service = service_for(block_service, tmp_path, client)
    unavailable = service_for(block_service, tmp_path, None)
    with pytest.raises(SessionError):
        unavailable.prepare("owner", key.question_id)
    assert bank.get_question(key) is None
    context = service.context("owner", key.question_id)
    with pytest.raises(ValueError):
        service.review("owner", key.question_id, (), expected_content_hash="0" * 64)
    with pytest.raises(ValueError):
        service.review(
            "owner", key.question_id, ("invented",), expected_content_hash=context.content_hash
        )
    pending = service.prepare("owner", key.question_id)
    running = service.prepare("owner", key.question_id)
    cancelled = service.prepare("owner", key.question_id)
    service.cancel("owner", cancelled.id)
    with database.session() as session:
        session.get(StudyTopicSuggestionModel, running.id).state = "running"
    assert service.interrupt_pending("other") == 0
    assert service.interrupt_pending("owner") == 2
    assert service.interrupt_pending("owner") == 0
    for view in (pending, running, cancelled):
        assert service.load("owner", view.id).state == "interrupted"
        with pytest.raises(ValueError):
            service.run("owner", view.id)
    stale = service.prepare("owner", key.question_id)
    publication = publications[context.candidate.reference.quiz_token]
    publications[publication.token] = replace(publication, version=2)
    with pytest.raises(PermissionError):
        service.run("owner", stale.id)
    assert client.calls == [] and bank.get_question(key).topics == ()


def test_rate_reset_and_lifecycle_mismatch_are_durable_without_replay(blocks, tmp_path):  # noqa: F811
    from oms_hub.llm.codex_session import SessionError

    block_service, bank, _, _ = blocks
    key = block_service.catalog("owner").questions[0].key

    class LimitedClient:
        calls = 0

        def generate(self, request, *, cancelled, on_lifecycle):
            self.calls += 1
            on_lifecycle(SessionLifecycle(request.request_id, "dispatching"))
            raise SessionError("rate_limited", reset_at="2026-09-12T12:30:00+00:00")

    client = LimitedClient()
    service = service_for(block_service, tmp_path, client)
    pending = service.prepare("owner", key.question_id)
    failed = service.run("owner", pending.id)
    assert failed.state == "failed" and failed.reset_at == "2026-09-12T12:30:00+00:00"
    with pytest.raises(ValueError):
        service.run("owner", pending.id)
    assert client.calls == 1

    class WrongLifecycle:
        def generate(self, request, *, cancelled, on_lifecycle):
            on_lifecycle(SessionLifecycle("wrong", "dispatching"))
            raise AssertionError("must stop before provider work")

    service.client = WrongLifecycle()
    pending = service.prepare("owner", key.question_id)
    failed = service.run("owner", pending.id)
    assert failed.state == "failed" and failed.reset_at is None
    assert bank.get_question(key).topics == ()


def test_source_change_during_generation_retains_raw_without_acceptance(blocks, tmp_path):  # noqa: F811
    from dataclasses import replace

    block_service, bank, database, publications = blocks
    key = block_service.catalog("owner").questions[0].key

    class ChangedSource(FakeClient):
        def generate(self, request, **kwargs):
            result = super().generate(request, **kwargs)
            publications["quiz"] = replace(publications["quiz"], version=2)
            return result

    client = ChangedSource()
    service = service_for(block_service, tmp_path, client)
    pending = service.prepare("owner", key.question_id)
    assert service.run("owner", pending.id).state == "failed"
    assert bank.get_question(key).topics == ()
    with database.session() as session:
        assert session.get(StudyTopicSuggestionModel, pending.id).raw_response_text
    with pytest.raises(ValueError):
        service.run("owner", pending.id)
    assert len(client.calls) == 1
