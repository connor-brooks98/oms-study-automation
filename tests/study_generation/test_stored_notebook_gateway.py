import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.study_generation.domain import (
    LectureSourceSet,
    NotebookMapping,
    NotebookRef,
    NotebookSourceBinding,
    PromptSnapshot,
    RemoteSource,
    RevisionSource,
    SourceIsolationError,
    SourceKind,
)
from oms_hub.study_generation.notebook import (
    NotebookAuthenticationError,
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


def test_source_upload_uses_canonical_path_stem(tmp_path):
    events = []
    repository = FakeRepository(events)
    client = FakeClient([], events)
    gateway = _gateway(tmp_path, client, repository)
    pdf = _revision(
        tmp_path / "Lecture 02 - Demyelinating Disease.pdf",
        2,
        10,
        SourceKind.LECTURE_PDF,
        b"pdf",
    )
    transcript = _revision(
        tmp_path / "Lecture 02 - Demyelinating Disease - Transcript.txt",
        2,
        11,
        SourceKind.CLEANED_TRANSCRIPT,
        b"transcript",
    )

    sources = gateway.ensure_sources(
        NotebookRef("nb-1", "Neuro · Exam 1"),
        2,
        pdf,
        transcript,
    )

    assert client.sources.upload_titles == [
        "Lecture 02 - Demyelinating Disease",
        "Lecture 02 - Demyelinating Disease - Transcript",
    ]
    assert sources.remote_ids == ["new-1", "new-2"]


def test_gateway_translates_expired_storage_into_safe_auth_error(tmp_path):
    gateway = StoredNotebookLMGateway(
        tmp_path / "notebooklm-storage.json",
        FakeRepository([]),
        client_factory=ExpiredClientContext,
    )

    with pytest.raises(
        NotebookAuthenticationError,
        match="reconnect Google in Settings",
    ) as error:
        gateway.ensure_notebook("Neuro", 1)

    assert "accounts.google.com" not in str(error.value)


def test_studio_saga_gateway_snapshots_then_adds_to_known_notebook(tmp_path):
    events = []
    repository = FakeRepository(events)
    client = FakeClient([FakeRemote("existing", "Existing")], events)
    gateway = _gateway(tmp_path, client, repository)
    payload = tmp_path / "notes.pdf"
    payload.write_bytes(b"pdf")

    notebook_id, baseline = gateway.prepare_studio_source_add("Neuro", 1)
    remote_id = gateway.add_studio_source_to_notebook(
        notebook_id,
        "file",
        "Notes",
        path=payload,
    )

    assert notebook_id == "nb-1"
    assert baseline == frozenset({"existing"})
    assert remote_id == "new-1"
    assert gateway.list_studio_source_ids("nb-1") == frozenset(
        {"existing", "new-1"}
    )


def test_generation_notebook_creation_blocks_competing_studio_preparation(tmp_path):
    create_started = threading.Event()
    allow_create = threading.Event()
    events = []

    class EmptyRepository(FakeRepository):
        def __init__(self):
            super().__init__(events)
            self.notebook = None

        def notebook_mapping(self, subject_key, exam_number):
            return self.notebook

        def notebook_mapping_by_remote_id(self, remote_notebook_id):
            return self.notebook

    class BlockingNotebooks(FakeNotebooks):
        async def create(self, title):
            create_started.set()
            assert allow_create.wait(timeout=5)
            created = FakeRemote("nb-1", title)
            self.items.append(created)
            return created

    repository = EmptyRepository()
    client = FakeClient([], events)
    client.notebooks = BlockingNotebooks([])
    gateway = _gateway(tmp_path, client, repository)
    generation_result = []

    thread = threading.Thread(
        target=lambda: generation_result.append(
            gateway.ensure_notebook("Neuro", 1)
        )
    )
    thread.start()
    assert create_started.wait(timeout=5)

    with pytest.raises(NotebookScopeBusyError):
        gateway.prepare_studio_source_add("Neuro", 1)

    allow_create.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert generation_result == [NotebookRef("nb-1", "Neuro · Exam 1")]
    assert len(client.notebooks.items) == 1

    notebook_id, baseline = gateway.prepare_studio_source_add("Neuro", 1)
    assert notebook_id == "nb-1"
    assert baseline == frozenset()
    assert len(client.notebooks.items) == 1


def test_durable_scope_heartbeat_blocks_competitor_past_initial_expiry(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    first_repository = GenerationRepository(database)
    second_repository = GenerationRepository(database)
    first_repository.save_notebook_mapping(
        "Neuro", "neuro", 1, "nb-1", "Neuro · Exam 1"
    )
    upload_started = threading.Event()
    allow_upload = threading.Event()
    events = []

    class BlockingSources(FakeSources):
        async def add_file(self, notebook_id, path, *, wait, title):
            upload_started.set()
            assert allow_upload.wait(timeout=5)
            return await super().add_file(
                notebook_id, path, wait=wait, title=title
            )

    client = FakeClient([], events)
    client.sources = BlockingSources([], events)
    first_gateway = StoredNotebookLMGateway(
        tmp_path / "first-storage.json",
        first_repository,
        client_factory=lambda: FakeClientContext(client),
        scope_lease_duration=timedelta(milliseconds=180),
        scope_renew_interval_seconds=0.03,
    )
    second_gateway = StoredNotebookLMGateway(
        tmp_path / "second-storage.json",
        second_repository,
        client_factory=lambda: FakeClientContext(client),
        scope_lease_duration=timedelta(milliseconds=180),
        scope_renew_interval_seconds=0.03,
    )
    payload = tmp_path / "notes.pdf"
    payload.write_bytes(b"pdf")
    result = []
    failure = []

    def upload() -> None:
        try:
            with first_gateway.mutation_scope("Neuro", 1, "studio", "op-1"):
                result.append(
                    first_gateway.add_studio_source_to_notebook(
                        "nb-1", "file", "Notes", path=payload
                    )
                )
        except BaseException as error:  # pragma: no cover - surfaced below
            failure.append(error)

    thread = threading.Thread(target=upload)
    thread.start()
    assert upload_started.wait(timeout=5)
    time.sleep(0.35)

    with pytest.raises(NotebookScopeBusyError):
        with second_gateway.mutation_scope("Neuro", 1, "studio", "op-2"):
            pytest.fail("competing worker entered a renewed mutation scope")

    allow_upload.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failure == []
    assert result == ["new-1"]

    with second_gateway.mutation_scope("Neuro", 1, "studio", "op-2"):
        pass
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
            gateway.add_studio_source_to_notebook(
                "nb-1", "file", "Notes", path=payload
            )
    releaser.join(timeout=5)


def test_studio_saga_gateway_treats_remote_not_found_delete_as_success(tmp_path):
    events = []
    repository = FakeRepository(events)
    client = FakeClient([], events)

    async def missing(notebook_id, source_id):
        raise RuntimeError("remote source not found")

    client.sources.delete = missing
    gateway = _gateway(tmp_path, client, repository)

    assert gateway.delete_studio_source("nb-1", "missing") is False


def test_changed_revision_binds_replacement_before_old_and_legacy_delete(tmp_path):
    events = []
    old = FakeRemote("remote-old", "Lecture 02 - Disease")
    legacy = FakeRemote(
        "legacy-old",
        "OMS-2-lecture_pdf-0123456789abcdef",
    )
    other_lecture = FakeRemote(
        "legacy-other",
        "OMS-3-lecture_pdf-fedcba9876543210",
    )
    transcript_remote = FakeRemote(
        "transcript-current",
        "Lecture 02 - Disease - Transcript",
    )
    repository = FakeRepository(events)
    repository.bindings[(1, 2, SourceKind.LECTURE_PDF)] = NotebookSourceBinding(
        1,
        1,
        2,
        9,
        SourceKind.LECTURE_PDF,
        "a" * 64,
        "remote-old",
        "Lecture 02 - Disease",
        "ready",
    )
    repository.bindings[(1, 2, SourceKind.CLEANED_TRANSCRIPT)] = (
        NotebookSourceBinding(
            2,
            1,
            2,
            11,
            SourceKind.CLEANED_TRANSCRIPT,
            hashlib.sha256(b"transcript").hexdigest(),
            "transcript-current",
            "Lecture 02 - Disease - Transcript",
            "ready",
        )
    )
    client = FakeClient(
        [old, legacy, other_lecture, transcript_remote],
        events,
    )
    gateway = _gateway(tmp_path, client, repository)
    pdf = _revision(
        tmp_path / "Lecture 02 - Disease.pdf",
        2,
        10,
        SourceKind.LECTURE_PDF,
        b"new pdf",
    )
    transcript = _revision(
        tmp_path / "Lecture 02 - Disease - Transcript.txt",
        2,
        11,
        SourceKind.CLEANED_TRANSCRIPT,
        b"transcript",
    )

    sources = gateway.ensure_sources(
        NotebookRef("nb-1", "Neuro · Exam 1"),
        2,
        pdf,
        transcript,
    )

    assert sources.remote_ids == ["new-1", "transcript-current"]
    assert events.index(("upload", "new-1")) < events.index(("bind", "new-1"))
    assert events.index(("bind", "new-1")) < events.index(
        ("delete", "remote-old")
    )
    assert ("delete", "legacy-old") in events
    assert ("delete", "legacy-other") not in events


def test_ask_ignores_other_lecture_sources(tmp_path):
    events = []
    repository = FakeRepository(events)
    selected = LectureSourceSet(
        2,
        RemoteSource(
            "pdf-2",
            2,
            10,
            "a" * 64,
            SourceKind.LECTURE_PDF,
            True,
        ),
        RemoteSource(
            "txt-2",
            2,
            11,
            "b" * 64,
            SourceKind.CLEANED_TRANSCRIPT,
            True,
        ),
    )
    for source in (selected.pdf, selected.transcript):
        repository.bindings[(1, 2, source.kind)] = NotebookSourceBinding(
            len(repository.bindings) + 1,
            1,
            2,
            source.revision_id,
            source.kind,
            source.sha256,
            source.remote_id,
            source.remote_id,
            "ready",
        )
    client = FakeClient(
        [
            FakeRemote("pdf-1", "Lecture 01"),
            FakeRemote("txt-1", "Lecture 01 - Transcript"),
            FakeRemote("pdf-2", "Lecture 02"),
            FakeRemote("txt-2", "Lecture 02 - Transcript"),
        ],
        events,
    )
    gateway = _gateway(tmp_path, client, repository)

    answer = gateway.ask(
        NotebookRef("nb-1", "Neuro · Exam 1"),
        selected,
        PromptSnapshot(
            tmp_path / "Outline.md",
            "Make the outline",
            "c" * 64,
            "now",
        ),
    )

    assert answer.text == "Selected lecture only"
    assert client.chat.calls[-1]["source_ids"] == ["pdf-2", "txt-2"]


def test_generate_uses_disposable_lecture_notebook_without_touching_exam_chat(tmp_path):
    events = []
    repository = FakeRepository(events)
    client = FakeClient([], events)
    sources_by_notebook = {"nb-1": []}

    class IsolatedSources:
        async def list(self, notebook_id):
            return list(sources_by_notebook.get(notebook_id, []))

        async def add_file(self, notebook_id, path, *, wait, title):
            assert Path(path).is_file()
            assert wait is True
            items = sources_by_notebook.setdefault(notebook_id, [])
            remote = FakeRemote(f"{notebook_id}-source-{len(items) + 1}", title)
            items.append(remote)
            return remote

        async def rename(self, notebook_id, source_id, new_title):
            remote = next(
                item for item in sources_by_notebook[notebook_id] if item.id == source_id
            )
            remote.title = new_title
            return remote

        async def delete(self, notebook_id, source_id):
            sources_by_notebook[notebook_id] = [
                item for item in sources_by_notebook[notebook_id] if item.id != source_id
            ]

    client.sources = IsolatedSources()
    gateway = _gateway(tmp_path, client, repository)
    pdf = _revision(
        tmp_path / "Lecture 02 - Disease.pdf",
        2,
        10,
        SourceKind.LECTURE_PDF,
        b"pdf",
    )
    transcript = _revision(
        tmp_path / "Lecture 02 - Disease - Transcript.txt",
        2,
        11,
        SourceKind.CLEANED_TRANSCRIPT,
        b"transcript",
    )

    generated = gateway.generate(
        "Neuro",
        1,
        2,
        pdf,
        transcript,
        PromptSnapshot(tmp_path / "Outline.md", "Make the outline", "c" * 64, "now"),
    )

    assert generated.notebook.id == "nb-1"
    assert generated.sources.remote_ids == ["nb-1-source-1", "nb-1-source-2"]
    assert client.chat.calls == [
        {
            "notebook_id": "nb-created",
            "question": "Make the outline",
            "source_ids": ["nb-created-source-1", "nb-created-source-2"],
        }
    ]
    assert client.notebooks.deleted == ["nb-created"]
    assert [notebook.id for notebook in client.notebooks.items] == ["nb-1"]
    assert [source.id for source in sources_by_notebook["nb-1"]] == [
        "nb-1-source-1",
        "nb-1-source-2",
    ]


def test_ask_fails_closed_when_selected_remote_is_not_ready(tmp_path):
    events = []
    repository = FakeRepository(events)
    selected = LectureSourceSet(
        2,
        RemoteSource(
            "pdf-2",
            2,
            10,
            "a" * 64,
            SourceKind.LECTURE_PDF,
            True,
        ),
        RemoteSource(
            "txt-2",
            2,
            11,
            "b" * 64,
            SourceKind.CLEANED_TRANSCRIPT,
            True,
        ),
    )
    for source in (selected.pdf, selected.transcript):
        repository.bindings[(1, 2, source.kind)] = NotebookSourceBinding(
            len(repository.bindings) + 1,
            1,
            2,
            source.revision_id,
            source.kind,
            source.sha256,
            source.remote_id,
            source.remote_id,
            "ready",
        )
    client = FakeClient(
        [
            FakeRemote("pdf-2", "Lecture 02", status="processing"),
            FakeRemote("txt-2", "Lecture 02 - Transcript"),
        ],
        events,
    )
    gateway = _gateway(tmp_path, client, repository)

    with pytest.raises(SourceIsolationError, match="not ready"):
        gateway.ask(
            NotebookRef("nb-1", "Neuro · Exam 1"),
            selected,
            PromptSnapshot(
                tmp_path / "Outline.md",
                "Make the outline",
                "c" * 64,
                "now",
            ),
        )

    assert client.chat.calls == []


def test_answer_studio_question_uses_only_selected_ready_supporting_sources(tmp_path) -> None:
    events = []
    repository = FakeRepository(events)
    client = FakeClient(
        [FakeRemote("support-1", "Course guide"), FakeRemote("other", "Other source")], events
    )
    client.chat.answer = (
        '{"status":"answered","correct_index":0,"rationale":"The guide says biceps flexes.",'
        '"evidence":["Course guide, page 4"]}'
    )

    result = _gateway(tmp_path, client, repository).answer_studio_question(
        "Neuro", 1, _question(), ("support-1",)
    )

    assert result.status is NotebookQuestionStatus.ANSWERED
    assert result.correct_index == 0
    assert client.chat.calls[-1]["source_ids"] == ["support-1"]
    assert "Biceps" in client.chat.calls[-1]["question"]


def test_notebook_question_result_rejects_internally_inconsistent_status() -> None:
    with pytest.raises(NotebookQuestionContractError):
        NotebookQuestionResult(NotebookQuestionStatus.ANSWERED, None, "Biceps", ("p4",))


def test_answer_studio_question_accepts_explicit_no_support_json(tmp_path) -> None:
    events = []
    client = FakeClient([FakeRemote("support-1", "Course guide")], events)
    client.chat.answer = (
        '{"status":"no_support","correct_index":null,'
        '"rationale":"The selected guide does not address elbow flexion.","evidence":[]}'
    )

    result = _gateway(tmp_path, client, FakeRepository(events)).answer_studio_question(
        "Neuro", 1, _question(), ("support-1",)
    )

    assert result == NotebookQuestionResult(
        NotebookQuestionStatus.NO_SUPPORT,
        None,
        "The selected guide does not address elbow flexion.",
        (),
    )


@pytest.mark.parametrize(
    ("source_ids", "sources"),
    [
        (("support-1", "support-1"), [FakeRemote("support-1", "Course guide")]),
        (("missing",), [FakeRemote("support-1", "Course guide")]),
        (("support-1",), [FakeRemote("support-1", "Course guide", status="processing")]),
    ],
)
def test_answer_studio_question_rejects_invalid_source_selection(
    tmp_path, source_ids, sources
) -> None:
    events = []
    gateway = _gateway(tmp_path, FakeClient(sources, events), FakeRepository(events))

    with pytest.raises(SourceIsolationError):
        gateway.answer_studio_question("Neuro", 1, _question(), source_ids)


@pytest.mark.parametrize(
    "response",
    [
        "",
        "maybe the answer is 0",
        '{"status":"answered","correct_index":null,"rationale":"Maybe Biceps",'
        '"evidence":["p4"]}',
        '{"status":"answered","correct_index":0,"rationale":"Probably Biceps",'
        '"evidence":["p4"]}',
        '{"status":"answered","correct_index":0,"rationale":"It appears to be Biceps",'
        '"evidence":["p4"]}',
        '{"status":"answered","correct_index":2,"rationale":"Biceps",' '"evidence":["p4"]}',
    ],
)
def test_answer_studio_question_rejects_invalid_model_contract(tmp_path, response) -> None:
    events = []
    client = FakeClient([FakeRemote("support-1", "Course guide")], events)
    client.chat.answer = response

    with pytest.raises(NotebookQuestionContractError):
        _gateway(tmp_path, client, FakeRepository(events)).answer_studio_question(
            "Neuro", 1, _question(), ("support-1",)
        )
