import asyncio
from pathlib import Path
from typing import Any, Protocol

from oms_hub.study_generation.domain import (
    LectureSourceSet,
    NotebookAnswer,
    NotebookRef,
    PromptSnapshot,
    RemoteSource,
    RevisionSource,
    SourceIsolationError,
    SourceKind,
)


class NotebookGateway(Protocol):
    def ensure_notebook(self, subject: str, exam_number: int) -> NotebookRef: ...

    def ensure_sources(
        self,
        notebook: NotebookRef,
        lecture_id: int,
        pdf: RevisionSource,
        transcript: RevisionSource,
    ) -> LectureSourceSet: ...

    def ask(
        self,
        notebook: NotebookRef,
        sources: LectureSourceSet,
        prompt: PromptSnapshot,
    ) -> NotebookAnswer: ...


class NotebookLMGateway:
    """Strict adapter that never submits a NotebookLM prompt without source IDs."""

    def __init__(self, client: Any):
        self.client = client

    def ensure_notebook(self, subject: str, exam_number: int) -> NotebookRef:
        title = f"{subject} · Exam {exam_number}"
        notebooks = _run(self.client.notebooks.list())
        for notebook in notebooks:
            if notebook.title == title:
                return NotebookRef(str(notebook.id), title)
        created = _run(self.client.notebooks.create(title))
        return NotebookRef(str(created.id), title)

    def ensure_sources(
        self,
        notebook: NotebookRef,
        lecture_id: int,
        pdf: RevisionSource,
        transcript: RevisionSource,
    ) -> LectureSourceSet:
        _validate_revision_source(pdf, lecture_id, SourceKind.LECTURE_PDF)
        _validate_revision_source(
            transcript,
            lecture_id,
            SourceKind.CLEANED_TRANSCRIPT,
        )
        pdf_remote = self._upload(notebook, pdf)
        transcript_remote = self._upload(notebook, transcript)
        return LectureSourceSet(lecture_id, pdf_remote, transcript_remote)

    def ask(
        self,
        notebook: NotebookRef,
        sources: LectureSourceSet,
        prompt: PromptSnapshot,
    ) -> NotebookAnswer:
        if not prompt.content.strip():
            raise ValueError("Notebook prompt is empty")
        result = _run(
            self.client.chat.ask(
                notebook.id,
                prompt.content,
                source_ids=sources.remote_ids,
            )
        )
        text = getattr(result, "answer", None) or getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("NotebookLM returned an empty answer")
        return NotebookAnswer(text.strip())

    def _upload(
        self,
        notebook: NotebookRef,
        source: RevisionSource,
    ) -> RemoteSource:
        uploaded = _run(
            self.client.sources.add_file(
                notebook.id,
                source.path,
                wait=True,
                title=source.path.stem,
            )
        )
        remote_id = str(uploaded.id)
        ready = str(getattr(uploaded, "status", "ready")).casefold() not in {
            "error",
            "failed",
        }
        return RemoteSource(
            remote_id,
            source.lecture_id,
            source.revision_id,
            source.sha256,
            source.kind,
            ready,
        )


class StoredNotebookLMGateway:
    """Open a fresh authenticated NotebookLM session for each durable worker step."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path

    def _with_client(self) -> Any:
        from notebooklm import NotebookLMClient

        return NotebookLMClient.from_storage(str(self.storage_path))


def _validate_revision_source(
    source: RevisionSource,
    lecture_id: int,
    expected_kind: SourceKind,
) -> None:
    if source.lecture_id != lecture_id or source.kind is not expected_kind:
        raise SourceIsolationError("revision source does not match the selected lecture")
    if not source.path.is_file():
        raise SourceIsolationError("current lecture source file is missing")
    if len(source.sha256) != 64:
        raise SourceIsolationError("current lecture source fingerprint is invalid")


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
