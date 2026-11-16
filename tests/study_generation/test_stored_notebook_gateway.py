import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.study_generation.domain import (
    NotebookMapping,
    NotebookSourceBinding,
    RevisionSource,
)
from oms_hub.study_generation.notebook import (
    NotebookQuestionContractError,
    NotebookQuestionResult,
    NotebookQuestionStatus,
    StoredNotebookLMGateway,
)
from oms_hub.study_generation.notebook_errors import (
    NotebookScopeBusyError,
    NotebookScopeLostError,
)
from oms_hub.study_generation.practice_domain import QuestionDraft, QuestionSourceRef
from oms_hub.study_generation.repository import GenerationRepository


@dataclass
class FakeRemote:
    id: str
    title: str
    status: str = "ready"


class FakeNotebooks:
    def __init__(self, notebooks):
        self.items = notebooks
        self.deleted = []

    async def list(self):
        return list(self.items)

    async def create(self, title):
        created = FakeRemote("nb-created", title)
        self.items.append(created)
        return created

    async def delete(self, notebook_id):
        self.deleted.append(notebook_id)
        self.items = [item for item in self.items if item.id != notebook_id]


class FakeSources:
    def __init__(self, sources, events):
        self.items = list(sources)
        self.events = events
        self.upload_titles = []

    async def list(self, notebook_id):
        assert notebook_id == "nb-1"
        return list(self.items)

    async def add_file(self, notebook_id, path, *, wait, title):
        assert notebook_id == "nb-1"
        assert Path(path).is_file()
        assert wait is True
        self.upload_titles.append(title)
        remote = FakeRemote(f"new-{len(self.upload_titles)}", title)
        self.items.append(remote)
        self.events.append(("upload", remote.id))
        return remote

    async def rename(self, notebook_id, source_id, new_title):
        assert notebook_id == "nb-1"
        remote = next(item for item in self.items if item.id == source_id)
        remote.title = new_title
        self.events.append(("rename", source_id))
        return remote

    async def delete(self, notebook_id, source_id):
        assert notebook_id == "nb-1"
        self.events.append(("delete", source_id))
        self.items = [item for item in self.items if item.id != source_id]


class FakeChat:
    def __init__(self):
        self.calls = []
        self.answer = "Selected lecture only"

    async def ask(self, notebook_id, question, *, source_ids):
        self.calls.append(
            {
                "notebook_id": notebook_id,
                "question": question,
                "source_ids": list(source_ids),
            }
        )
        return type("Answer", (), {"answer": self.answer})()


class FakeClient:
    def __init__(self, sources, events):
        self.notebooks = FakeNotebooks([FakeRemote("nb-1", "Neuro · Exam 1")])
        self.sources = FakeSources(sources, events)
        self.chat = FakeChat()


class FakeClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ExpiredClientContext:
    async def __aenter__(self):
        raise RuntimeError(
            "Authentication expired or invalid. Redirected to: "
            "https://accounts.google.com/ Run 'notebooklm login' "
            "to re-authenticate."
        )

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeRepository:
    def __init__(self, events):
        self.events = events
        self.notebook = NotebookMapping(
            1,
            "Neuro",
            "neuro",
            1,
            "nb-1",
            "Neuro · Exam 1",
        )
        self.bindings = {}

    def notebook_mapping(self, subject_key, exam_number):
        if (subject_key, exam_number) == ("neuro", 1):
            return self.notebook
        return None

    def notebook_mapping_by_remote_id(self, remote_notebook_id):
        return self.notebook if remote_notebook_id == "nb-1" else None

    def save_notebook_mapping(
        self,
        subject,
        subject_key,
        exam_number,
        remote_notebook_id,
        title,
    ):
        self.notebook = NotebookMapping(
            1,
            subject,
            subject_key,
            exam_number,
            remote_notebook_id,
            title,
        )
        return self.notebook

    def source_binding(self, notebook_mapping_id, lecture_id, source_kind):
        return self.bindings.get((notebook_mapping_id, lecture_id, source_kind))

    def bind_source(
        self,
        notebook_mapping_id,
        lecture_id,
        revision_id,
        source_kind,
        source_sha256,
        remote_source_id,
        display_title,
    ):
        binding = NotebookSourceBinding(
            len(self.bindings) + 1,
            notebook_mapping_id,
            lecture_id,
            revision_id,
            source_kind,
            source_sha256,
            remote_source_id,
            display_title,
            "ready",
        )
        self.bindings[(notebook_mapping_id, lecture_id, source_kind)] = binding
        self.events.append(("bind", remote_source_id))
        return binding


def _revision(path, lecture_id, revision_id, kind, payload):
    path.write_bytes(payload)
    return RevisionSource(
        lecture_id,
        revision_id,
        path,
        hashlib.sha256(payload).hexdigest(),
        kind,
    )


def _gateway(tmp_path, client, repository):
    return StoredNotebookLMGateway(
        tmp_path / "notebooklm-storage.json",
        repository,
        client_factory=lambda: FakeClientContext(client),
    )


def _question() -> QuestionDraft:
    return QuestionDraft(
        question_id="question-1",
        original_identifier="1",
        stem="Which muscle flexes the elbow?",
        choices=("Biceps", "Triceps"),
        correct_index=None,
        rationale=None,
        image_ref=None,
        source_refs=(QuestionSourceRef("questions", "page-1", "page 1"),),
        answer_provenance=None,
        extraction_confidence=0.9,
        diagnostics=(),
        verification_required=True,
        verified_at=None,
    )














def test_durable_scope_heartbeat_blocks_competitor_past_initial_expiry(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    initial_time = datetime(2026, 1, 1, tzinfo=UTC)
    clock_now = initial_time
    renewed_at = initial_time + timedelta(minutes=20)
    renewed = threading.Event()

    class ClockedRepository(GenerationRepository):
        def acquire_notebook_scope(self, *args, **kwargs):
            return super().acquire_notebook_scope(*args, now=clock_now, **kwargs)

        def renew_notebook_scope(self, *args, **kwargs):
            renewal_time = clock_now
            accepted = super().renew_notebook_scope(*args, now=renewal_time, **kwargs)
            if accepted and renewal_time >= renewed_at:
                renewed.set()
            return accepted

    first_repository = ClockedRepository(database)
    second_repository = ClockedRepository(database)
    first_repository.save_notebook_mapping(
        "Neuro", "neuro", 1, "nb-1", "Neuro · Exam 1"
    )
    upload_started = threading.Event()
    allow_upload = threading.Event()
    events = []

    class BlockingSources(FakeSources):
        async def add_file(self, notebook_id, path, *, wait, title):
            upload_started.set()
            allow_upload.wait()
            return await super().add_file(
                notebook_id, path, wait=wait, title=title
            )

    client = FakeClient([], events)
    client.sources = BlockingSources([], events)
    first_gateway = StoredNotebookLMGateway(
        tmp_path / "first-storage.json",
        first_repository,
        client_factory=lambda: FakeClientContext(client),
        scope_renew_interval_seconds=0.03,
    )
    second_gateway = StoredNotebookLMGateway(
        tmp_path / "second-storage.json",
        second_repository,
        client_factory=lambda: FakeClientContext(client),
        scope_renew_interval_seconds=0.03,
    )
    payload = tmp_path / "notes.pdf"
    payload.write_bytes(b"pdf")
    result = []
    failure = []

    def upload() -> None:
        try:
            with first_gateway.mutation_scope("Neuro", 1, "studio", "op-1"):
                upload_started.set()
                assert allow_upload.wait(timeout=5)
                result.append("held")
        except BaseException as error:  # pragma: no cover - surfaced below
            failure.append(error)

    thread = threading.Thread(target=upload)
    thread.start()
    try:
        assert upload_started.wait(timeout=5)
        clock_now = renewed_at
        assert renewed.wait(timeout=5), "heartbeat did not commit a renewal"
        # Past the initial 30-minute expiry, inside the renewed lease until minute 50.
        clock_now = initial_time + timedelta(minutes=31)
        with pytest.raises(NotebookScopeBusyError):
            with second_gateway.mutation_scope("Neuro", 1, "studio", "op-2"):
                pytest.fail("competing worker entered a renewed mutation scope")

        allow_upload.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert failure == []
        assert result == ["held"]

        with second_gateway.mutation_scope("Neuro", 1, "studio", "op-2"):
            pass
    finally:
        allow_upload.set()
        thread.join(timeout=5)
        database.close()


def test_durable_scope_reports_lost_owner_after_failed_renewal(tmp_path):
    upload_started = threading.Event()
    allow_upload = threading.Event()
    events = []

    class LosingRepository(FakeRepository):
        def acquire_notebook_scope(self, *args, **kwargs):
            return True

        def renew_notebook_scope(self, *args, **kwargs):
            return False

        def release_notebook_scope(self, *args, **kwargs):
            return True

    class BlockingSources(FakeSources):
        async def add_file(self, notebook_id, path, *, wait, title):
            upload_started.set()
            assert allow_upload.wait(timeout=5)
            return await super().add_file(
                notebook_id, path, wait=wait, title=title
            )

    client = FakeClient([], events)
    client.sources = BlockingSources([], events)
    gateway = StoredNotebookLMGateway(
        tmp_path / "storage.json",
        LosingRepository(events),
        client_factory=lambda: FakeClientContext(client),
        scope_lease_duration=timedelta(milliseconds=180),
        scope_renew_interval_seconds=0.03,
    )
    payload = tmp_path / "notes.pdf"
    payload.write_bytes(b"pdf")

    def release_upload() -> None:
        assert upload_started.wait(timeout=5)
        time.sleep(0.08)
        allow_upload.set()

    releaser = threading.Thread(target=release_upload)
    releaser.start()
    with pytest.raises(NotebookScopeLostError, match="ownership was lost"):
        with gateway.mutation_scope("Neuro", 1, "studio", "op-1"):
            upload_started.set()
            assert allow_upload.wait(timeout=5)
    releaser.join(timeout=5)














def test_notebook_question_result_rejects_internally_inconsistent_status() -> None:
    with pytest.raises(NotebookQuestionContractError):
        NotebookQuestionResult(NotebookQuestionStatus.ANSWERED, None, "Biceps", ("p4",))
