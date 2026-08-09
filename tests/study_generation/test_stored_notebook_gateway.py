import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

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
from oms_hub.study_generation.practice_domain import QuestionDraft, QuestionSourceRef


@dataclass
class FakeRemote:
    id: str
    title: str
    status: str = "ready"


class FakeNotebooks:
    def __init__(self, notebooks):
        self.items = notebooks

    async def list(self):
        return list(self.items)

    async def create(self, title):
        created = FakeRemote("nb-created", title)
        self.items.append(created)
        return created


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
