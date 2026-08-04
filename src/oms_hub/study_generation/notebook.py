import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol, cast

from oms_hub.study_generation.domain import (
    LectureSourceSet,
    NotebookAnswer,
    NotebookGeneration,
    NotebookRef,
    PromptSnapshot,
    RemoteSource,
    RevisionSource,
    SourceIsolationError,
    SourceKind,
)
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    translate_notebook_error,
)
from oms_hub.study_generation.notebook_storage import PlaintextNotebookStorage
from oms_hub.study_generation.repository import GenerationRepository

logger = logging.getLogger(__name__)
__all__ = ["NotebookAuthenticationError"]
_active_client: ContextVar[Any | None] = ContextVar(
    "notebooklm_active_client",
    default=None,
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
    """Use one authenticated NotebookLM session per durable generation operation."""

    def __init__(
        self,
        storage_path: Path | Any,
        repository: GenerationRepository,
        *,
        client_factory: Callable[[], Any] | None = None,
    ):
        self.storage = (
            PlaintextNotebookStorage(storage_path)
            if isinstance(storage_path, Path)
            else storage_path
        )
        self.repository = repository
        self.client_factory = client_factory

    def ensure_notebook(self, subject: str, exam_number: int) -> NotebookRef:
        return cast(
            NotebookRef,
            _run(
                self._ensure_notebook(subject, exam_number),
            ),
        )

    def ensure_sources(
        self,
        notebook: NotebookRef,
        lecture_id: int,
        pdf: RevisionSource,
        transcript: RevisionSource,
    ) -> LectureSourceSet:
        return cast(
            LectureSourceSet,
            _run(
                self._ensure_sources(
                    notebook,
                    lecture_id,
                    pdf,
                    transcript,
                )
            ),
        )

    def ask(
        self,
        notebook: NotebookRef,
        sources: LectureSourceSet,
        prompt: PromptSnapshot,
    ) -> NotebookAnswer:
        return cast(
            NotebookAnswer,
            _run(self._ask(notebook, sources, prompt)),
        )

    def generate(
        self,
        subject: str,
        exam_number: int,
        lecture_id: int,
        pdf: RevisionSource,
        transcript: RevisionSource,
        prompt: PromptSnapshot,
    ) -> NotebookGeneration:
        return cast(
            NotebookGeneration,
            _run(
                self._generate(
                    subject,
                    exam_number,
                    lecture_id,
                    pdf,
                    transcript,
                    prompt,
                )
            ),
        )

    def attach_studio_source(
        self,
        subject: str,
        exam_number: int,
        source_type: str,
        title: str,
        *,
        path: Path | None = None,
        text: str | None = None,
        url: str | None = None,
    ) -> tuple[str, str]:
        return cast(
            tuple[str, str],
            _run(
                self._attach_studio_source(
                    subject,
                    exam_number,
                    source_type,
                    title,
                    path=path,
                    text=text,
                    url=url,
                )
            ),
        )

    def ask_studio(
        self,
        subject: str,
        exam_number: int,
        prompt: str,
        source_ids: list[str],
    ) -> tuple[str, str]:
        return cast(
            tuple[str, str],
            _run(self._ask_studio(subject, exam_number, prompt, source_ids)),
        )

    def delete_studio_source(self, notebook_id: str, source_id: str) -> None:
        _run(self._delete_studio_source(notebook_id, source_id))

    async def _delete_studio_source(self, notebook_id: str, source_id: str) -> None:
        async with self._with_client() as client:
            await client.sources.delete(notebook_id, source_id)

    async def _ask_studio(
        self,
        subject: str,
        exam_number: int,
        prompt: str,
        source_ids: list[str],
    ) -> tuple[str, str]:
        if not prompt.strip():
            raise ValueError("Studio prompt is empty")
        if len(source_ids) != len(set(source_ids)):
            raise SourceIsolationError("Studio source selection contains duplicates")
        async with self._with_client() as client:
            token = _active_client.set(client)
            try:
                notebook = await self._ensure_notebook(subject, exam_number)
                remote_by_id = {
                    str(remote.id): remote for remote in await client.sources.list(notebook.id)
                }
                for source_id in source_ids:
                    remote = remote_by_id.get(source_id)
                    if remote is None or not _remote_ready(remote):
                        raise SourceIsolationError(
                            "A selected Studio source is missing or not ready"
                        )
                result = await client.chat.ask(
                    notebook.id,
                    prompt,
                    source_ids=list(source_ids),
                )
                text = getattr(result, "answer", None) or getattr(result, "text", None)
                if not isinstance(text, str) or not text.strip():
                    raise RuntimeError("NotebookLM returned an empty answer")
                return notebook.id, text.strip()
            finally:
                _active_client.reset(token)

    async def _attach_studio_source(
        self,
        subject: str,
        exam_number: int,
        source_type: str,
        title: str,
        *,
        path: Path | None,
        text: str | None,
        url: str | None,
    ) -> tuple[str, str]:
        async with self._with_client() as client:
            token = _active_client.set(client)
            try:
                notebook = await self._ensure_notebook(subject, exam_number)
                if source_type == "file" and path is not None:
                    remote = await client.sources.add_file(
                        notebook.id,
                        path,
                        wait=True,
                        title=title,
                    )
                elif source_type == "text" and text is not None:
                    remote = await client.sources.add_text(
                        notebook.id,
                        title,
                        text,
                        wait=True,
                    )
                elif source_type == "url" and url is not None:
                    remote = await client.sources.add_url(notebook.id, url, wait=True)
                else:
                    raise ValueError("Studio source payload is incomplete")
                if not _remote_ready(remote):
                    raise SourceIsolationError("NotebookLM source did not become ready")
                return notebook.id, str(remote.id)
            finally:
                _active_client.reset(token)

    async def _generate(
        self,
        subject: str,
        exam_number: int,
        lecture_id: int,
        pdf: RevisionSource,
        transcript: RevisionSource,
        prompt: PromptSnapshot,
    ) -> NotebookGeneration:
        async with self._with_client() as client:
            token = _active_client.set(client)
            try:
                notebook = await self._ensure_notebook(subject, exam_number)
                sources = await self._ensure_sources(
                    notebook,
                    lecture_id,
                    pdf,
                    transcript,
                )
                answer = await self._ask(notebook, sources, prompt)
                return NotebookGeneration(notebook, sources, answer)
            finally:
                _active_client.reset(token)

    async def _ensure_notebook(
        self,
        subject: str,
        exam_number: int,
    ) -> NotebookRef:
        title = f"{subject} · Exam {exam_number}"
        subject_key = " ".join(subject.casefold().split())
        async with self._with_client() as client:
            notebooks = await client.notebooks.list()
            by_id = {str(notebook.id): notebook for notebook in notebooks}
            stored = self.repository.notebook_mapping(subject_key, exam_number)
            if stored is not None and stored.remote_notebook_id in by_id:
                remote = by_id[stored.remote_notebook_id]
                return NotebookRef(str(remote.id), title)
            for notebook in notebooks:
                if notebook.title == title:
                    self.repository.save_notebook_mapping(
                        subject,
                        subject_key,
                        exam_number,
                        str(notebook.id),
                        title,
                    )
                    return NotebookRef(str(notebook.id), title)
            created = await client.notebooks.create(title)
            self.repository.save_notebook_mapping(
                subject,
                subject_key,
                exam_number,
                str(created.id),
                title,
            )
            return NotebookRef(str(created.id), title)

    async def _ensure_sources(
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
        notebook_mapping = self.repository.notebook_mapping_by_remote_id(notebook.id)
        if notebook_mapping is None:
            raise SourceIsolationError("NotebookLM notebook is not bound to this exam")
        async with self._with_client() as client:
            existing = await client.sources.list(notebook.id)
            by_id = {str(source.id): source for source in existing}

            async def ensure(source: RevisionSource) -> RemoteSource:
                display_title = source.path.stem
                binding = self.repository.source_binding(
                    notebook_mapping.id,
                    lecture_id,
                    source.kind,
                )
                remote = by_id.get(binding.remote_source_id) if binding is not None else None
                if (
                    binding is not None
                    and binding.revision_id == source.revision_id
                    and binding.source_sha256 == source.sha256
                    and remote is not None
                    and _remote_ready(remote)
                ):
                    if str(remote.title) != display_title:
                        remote = await client.sources.rename(
                            notebook.id,
                            str(remote.id),
                            display_title,
                        )
                        if remote is None:
                            remote = by_id[binding.remote_source_id]
                        self.repository.bind_source(
                            notebook_mapping.id,
                            lecture_id,
                            source.revision_id,
                            source.kind,
                            source.sha256,
                            str(remote.id),
                            display_title,
                        )
                    return RemoteSource(
                        str(remote.id),
                        lecture_id,
                        source.revision_id,
                        source.sha256,
                        source.kind,
                        True,
                    )

                uploaded = await client.sources.add_file(
                    notebook.id,
                    source.path,
                    wait=True,
                    title=display_title,
                )
                if not _remote_ready(uploaded):
                    raise SourceIsolationError("NotebookLM source did not become ready")
                remote_id = str(uploaded.id)
                self.repository.bind_source(
                    notebook_mapping.id,
                    lecture_id,
                    source.revision_id,
                    source.kind,
                    source.sha256,
                    remote_id,
                    display_title,
                )
                legacy = re.compile(
                    rf"^OMS-{lecture_id}-{re.escape(source.kind.value)}-"
                    r"[0-9a-f]{16}$"
                )
                stale_ids = {
                    str(item.id)
                    for item in existing
                    if str(item.id) != remote_id
                    and (str(item.title) == display_title or legacy.fullmatch(str(item.title)))
                }
                if binding is not None and binding.remote_source_id != remote_id:
                    stale_ids.add(binding.remote_source_id)
                for stale_id in stale_ids:
                    try:
                        await client.sources.delete(notebook.id, stale_id)
                    except Exception as error:  # noqa: BLE001 - cleanup is best effort
                        logger.warning(
                            "NotebookLM superseded source cleanup failed: %s",
                            type(error).__name__,
                        )
                by_id[remote_id] = uploaded
                return RemoteSource(
                    remote_id,
                    lecture_id,
                    source.revision_id,
                    source.sha256,
                    source.kind,
                    True,
                )

            pdf_remote = await ensure(pdf)
            transcript_remote = await ensure(transcript)
            return LectureSourceSet(
                lecture_id,
                pdf_remote,
                transcript_remote,
            )

    async def _ask(
        self,
        notebook: NotebookRef,
        sources: LectureSourceSet,
        prompt: PromptSnapshot,
    ) -> NotebookAnswer:
        selected_ids = sources.remote_ids
        if len(selected_ids) != 2 or len(set(selected_ids)) != 2:
            raise SourceIsolationError("exactly two distinct lecture sources are required")
        notebook_mapping = self.repository.notebook_mapping_by_remote_id(notebook.id)
        if notebook_mapping is None:
            raise SourceIsolationError("NotebookLM notebook is not bound to this exam")
        async with self._with_client() as client:
            remote_by_id = {
                str(remote.id): remote for remote in await client.sources.list(notebook.id)
            }
            for selected in (sources.pdf, sources.transcript):
                binding = self.repository.source_binding(
                    notebook_mapping.id,
                    sources.lecture_id,
                    selected.kind,
                )
                if (
                    binding is None
                    or binding.revision_id != selected.revision_id
                    or binding.source_sha256 != selected.sha256
                    or binding.remote_source_id != selected.remote_id
                ):
                    raise SourceIsolationError("NotebookLM source binding is stale")
                remote = remote_by_id.get(selected.remote_id)
                if remote is None:
                    raise SourceIsolationError("NotebookLM selected source is missing")
                if not _remote_ready(remote):
                    raise SourceIsolationError("NotebookLM selected source is not ready")
            result = await client.chat.ask(
                notebook.id,
                prompt.content,
                source_ids=selected_ids,
            )
        text = getattr(result, "answer", None) or getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("NotebookLM returned an empty answer")
        return NotebookAnswer(text.strip())

    @asynccontextmanager
    async def _with_client(self) -> Any:
        active = _active_client.get()
        if active is not None:
            yield active
            return
        with self.storage.plaintext() as storage_path:
            context = (
                self.client_factory()
                if self.client_factory is not None
                else _stored_client_context(storage_path)
            )
            async with context as client:
                yield client


def _stored_client_context(storage_path: Path) -> Any:
    from notebooklm import NotebookLMClient

    return NotebookLMClient.from_storage(str(storage_path))


def _remote_ready(remote: Any) -> bool:
    status = str(getattr(remote, "status", "")).casefold()
    return status == "ready" or status.endswith(".ready")


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
    try:
        return asyncio.run(awaitable)
    except Exception as error:
        translated = translate_notebook_error(error)
        if translated is None or translated is error:
            raise
        raise translated from error
