import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
    NotebookScopeBusyError,
    NotebookScopeLostError,
    translate_notebook_error,
)
from oms_hub.study_generation.notebook_storage import PlaintextNotebookStorage
from oms_hub.study_generation.practice_domain import QuestionDraft
from oms_hub.study_generation.repository import GenerationRepository

logger = logging.getLogger(__name__)
__all__ = ["NotebookAuthenticationError"]
_active_client: ContextVar[Any | None] = ContextVar(
    "notebooklm_active_client",
    default=None,
)


@dataclass(slots=True)
class _MutationScopeState:
    gateway_id: int
    subject_key: str
    exam_number: int
    lost: Event


_active_scope: ContextVar[_MutationScopeState | None] = ContextVar(
    "notebooklm_active_scope",
    default=None,
)


class NotebookQuestionStatus(StrEnum):
    ANSWERED = "answered"
    NO_SUPPORT = "no_support"


@dataclass(frozen=True, slots=True)
class NotebookQuestionResult:
    status: NotebookQuestionStatus
    correct_index: int | None
    rationale: str | None
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, NotebookQuestionStatus):
            raise NotebookQuestionContractError("NotebookLM answer status is invalid")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise NotebookQuestionContractError("NotebookLM rationale is empty")
        if self.status is NotebookQuestionStatus.ANSWERED and _contains_hedge(self.rationale):
            raise NotebookQuestionContractError("NotebookLM rationale is hedged")
        if any(not isinstance(item, str) or not item.strip() for item in self.evidence):
            raise NotebookQuestionContractError("NotebookLM evidence is invalid")
        if self.status is NotebookQuestionStatus.ANSWERED:
            if self.correct_index is None or self.correct_index < 0 or not self.evidence:
                raise NotebookQuestionContractError("NotebookLM answered result is incomplete")
        elif self.correct_index is not None:
            raise NotebookQuestionContractError("NotebookLM no_support result is inconsistent")


class NotebookQuestionContractError(ValueError):
    """NotebookLM did not provide a complete, decisive question result."""


class _NotebookQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["answered", "no_support"]
    correct_index: int | None
    rationale: str = Field(min_length=1, max_length=20_000)
    evidence: tuple[str, ...] = Field(max_length=50)

    @field_validator("evidence", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("evidence")
    @classmethod
    def evidence_is_nonempty_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("evidence entries must not be blank")
        return values

    @model_validator(mode="after")
    def result_is_decisive(self) -> "_NotebookQuestionResponse":
        if self.status == "answered" and _contains_hedge(self.rationale):
            raise ValueError("rationale must not hedge")
        if self.status == "answered":
            if self.correct_index is None:
                raise ValueError("answered result requires correct_index")
            if not self.evidence:
                raise ValueError("answered result requires evidence")
        elif self.correct_index is not None:
            raise ValueError("no_support result must not include correct_index")
        return self


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
        scope_lease_duration: timedelta = timedelta(minutes=30),
        scope_renew_interval_seconds: float = 60.0,
    ):
        self.storage = (
            PlaintextNotebookStorage(storage_path)
            if isinstance(storage_path, Path)
            else storage_path
        )
        self.repository = repository
        self.client_factory = client_factory
        if scope_lease_duration.total_seconds() <= scope_renew_interval_seconds:
            raise ValueError("notebook scope lease must exceed its renewal interval")
        if scope_renew_interval_seconds <= 0:
            raise ValueError("notebook scope renewal interval must be positive")
        self._scope_lease_duration = scope_lease_duration
        self._scope_renew_interval_seconds = scope_renew_interval_seconds
        self._scope_locks: dict[tuple[str, int], Lock] = {}
        self._scope_locks_guard = Lock()

    @contextmanager
    def mutation_scope(
        self,
        subject: str,
        exam_number: int,
        owner_kind: str,
        owner_id: str,
    ) -> Iterator[None]:
        """Serialize every local and cross-process mutation of one notebook."""
        subject_key = " ".join(subject.casefold().split())
        scope = (subject_key, exam_number)
        active = _active_scope.get()
        if self._scope_matches(active, subject_key, exam_number):
            self._raise_if_scope_lost(active)
            yield
            self._raise_if_scope_lost(active)
            return
        with self._scope_locks_guard:
            local_lock = self._scope_locks.setdefault(scope, Lock())
        if not local_lock.acquire(blocking=False):
            raise NotebookScopeBusyError()
        durable_acquired = False
        acquire = getattr(self.repository, "acquire_notebook_scope", None)
        renew = getattr(self.repository, "renew_notebook_scope", None)
        release = getattr(self.repository, "release_notebook_scope", None)
        stop_renewal = Event()
        state = _MutationScopeState(id(self), subject_key, exam_number, Event())
        renewal_thread: Thread | None = None
        try:
            if callable(acquire):
                durable_acquired = bool(
                    acquire(
                        subject_key,
                        exam_number,
                        owner_kind,
                        owner_id,
                        lease_duration=self._scope_lease_duration,
                    )
                )
                if not durable_acquired:
                    raise NotebookScopeBusyError()
                if not callable(renew):
                    raise RuntimeError("durable notebook scope does not support renewal")
                renewal_thread = Thread(
                    target=self._renew_scope_lease,
                    args=(
                        subject_key,
                        exam_number,
                        owner_kind,
                        owner_id,
                        renew,
                        stop_renewal,
                        state.lost,
                    ),
                    name=f"oms-notebook-scope-{exam_number}",
                    daemon=True,
                )
                renewal_thread.start()
            token = _active_scope.set(state)
            try:
                yield
                stop_renewal.set()
                if renewal_thread is not None:
                    renewal_thread.join(timeout=5)
                self._raise_if_scope_lost(state)
            finally:
                _active_scope.reset(token)
                stop_renewal.set()
                if renewal_thread is not None and renewal_thread.is_alive():
                    renewal_thread.join(timeout=5)
                if durable_acquired and callable(release):
                    try:
                        release(subject_key, exam_number, owner_kind, owner_id)
                    except Exception:  # noqa: BLE001 - expiry remains a safe fallback
                        logger.exception("NotebookLM scope lease release failed")
        finally:
            local_lock.release()

    def _scope_matches(
        self,
        active: _MutationScopeState | None,
        subject_key: str,
        exam_number: int,
    ) -> bool:
        return bool(
            active is not None
            and active.gateway_id == id(self)
            and active.subject_key == subject_key
            and active.exam_number == exam_number
        )

    @staticmethod
    def _raise_if_scope_lost(active: _MutationScopeState | None) -> None:
        if active is not None and active.lost.is_set():
            raise NotebookScopeLostError()

    def _renew_scope_lease(
        self,
        subject_key: str,
        exam_number: int,
        owner_kind: str,
        owner_id: str,
        renew: Callable[..., object],
        stop: Event,
        lost: Event,
    ) -> None:
        deadline = monotonic() + self._scope_lease_duration.total_seconds()
        while not stop.wait(self._scope_renew_interval_seconds):
            try:
                renewed = bool(
                    renew(
                        subject_key,
                        exam_number,
                        owner_kind,
                        owner_id,
                        lease_duration=self._scope_lease_duration,
                    )
                )
            except Exception:  # noqa: BLE001 - retry until the existing lease expires
                logger.exception("NotebookLM scope lease renewal failed")
                if monotonic() >= deadline:
                    lost.set()
                    return
                continue
            if not renewed:
                lost.set()
                return
            deadline = monotonic() + self._scope_lease_duration.total_seconds()

    @contextmanager
    def _remote_notebook_scope(
        self,
        notebook_id: str,
        owner_kind: str,
    ) -> Iterator[None]:
        mapping = self.repository.notebook_mapping_by_remote_id(notebook_id)
        if mapping is None:
            raise SourceIsolationError("NotebookLM notebook is not bound to an exam")
        subject_key = " ".join(mapping.subject_key.casefold().split())
        active = _active_scope.get()
        if self._scope_matches(active, subject_key, mapping.exam_number):
            self._raise_if_scope_lost(active)
            yield
            self._raise_if_scope_lost(active)
            return
        with self.mutation_scope(
            subject_key,
            mapping.exam_number,
            owner_kind,
            str(uuid4()),
        ):
            yield

    def ensure_notebook(self, subject: str, exam_number: int) -> NotebookRef:
        with self.mutation_scope(
            subject, exam_number, "notebook", str(uuid4())
        ):
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
        with self._remote_notebook_scope(notebook.id, "generation"):
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
        with self.mutation_scope(
            subject, exam_number, "generation", str(uuid4())
        ):
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
        with self.mutation_scope(subject, exam_number, "studio", str(uuid4())):
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

    def prepare_studio_source_add(
        self,
        subject: str,
        exam_number: int,
    ) -> tuple[str, frozenset[str]]:
        """Resolve the notebook and snapshot its sources before a durable add."""
        with self.mutation_scope(subject, exam_number, "studio", str(uuid4())):
            return cast(
                tuple[str, frozenset[str]],
                _run(self._prepare_studio_source_add(subject, exam_number)),
            )

    def add_studio_source_to_notebook(
        self,
        notebook_id: str,
        source_type: str,
        title: str,
        *,
        path: Path | None = None,
        text: str | None = None,
        url: str | None = None,
    ) -> str:
        """Perform the effect only after the caller commits its operation intent."""
        with self._remote_notebook_scope(notebook_id, "studio"):
            return cast(
                str,
                _run(
                    self._add_studio_source_to_notebook(
                        notebook_id,
                        source_type,
                        title,
                        path=path,
                        text=text,
                        url=url,
                    )
                ),
            )

    def list_studio_source_ids(self, notebook_id: str) -> frozenset[str]:
        with self._remote_notebook_scope(notebook_id, "studio"):
            return cast(
                frozenset[str],
                _run(self._list_studio_source_ids(notebook_id)),
            )

    def ask_studio(
        self,
        subject: str,
        exam_number: int,
        prompt: str,
        source_ids: list[str],
    ) -> tuple[str, str]:
        with self.mutation_scope(subject, exam_number, "studio", str(uuid4())):
            return cast(
                tuple[str, str],
                _run(self._ask_studio(subject, exam_number, prompt, source_ids)),
            )

    def answer_studio_question(
        self,
        subject: str,
        exam_number: int,
        question: QuestionDraft,
        source_ids: tuple[str, ...],
    ) -> NotebookQuestionResult:
        with self.mutation_scope(subject, exam_number, "studio", str(uuid4())):
            return cast(
                NotebookQuestionResult,
                _run(
                    self._answer_studio_question(
                        subject,
                        exam_number,
                        question,
                        source_ids,
                    )
                ),
            )

    def delete_studio_source(self, notebook_id: str, source_id: str) -> bool:
        with self._remote_notebook_scope(notebook_id, "studio"):
            return cast(bool, _run(self._delete_studio_source(notebook_id, source_id)))

    async def _delete_studio_source(self, notebook_id: str, source_id: str) -> bool:
        async with self._with_client() as client:
            try:
                await client.sources.delete(notebook_id, source_id)
            except Exception as error:
                if _is_remote_source_not_found(error):
                    return False
                raise
        return True

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

    async def _answer_studio_question(
        self,
        subject: str,
        exam_number: int,
        question: QuestionDraft,
        source_ids: tuple[str, ...],
    ) -> NotebookQuestionResult:
        if not source_ids:
            raise SourceIsolationError("at least one supporting source must be selected")
        if len(source_ids) != len(set(source_ids)):
            raise SourceIsolationError("supporting source selection contains duplicates")
        async with self._with_client() as client:
            token = _active_client.set(client)
            try:
                notebook = await self._ensure_notebook(subject, exam_number)
                remote_by_id = {
                    str(remote.id): remote for remote in await client.sources.list(notebook.id)
                }
                for source_id in source_ids:
                    remote = remote_by_id.get(source_id)
                    if remote is None:
                        raise SourceIsolationError("selected supporting source is missing")
                    if not _remote_ready(remote):
                        raise SourceIsolationError("selected supporting source is not ready")
                response = await client.chat.ask(
                    notebook.id,
                    _studio_question_prompt(question),
                    source_ids=list(source_ids),
                )
            finally:
                _active_client.reset(token)
        text = getattr(response, "answer", None) or getattr(response, "text", None)
        return _parse_question_response(text, len(question.choices))

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
                remote_id = await self._add_studio_source_to_notebook(
                    notebook.id,
                    source_type,
                    title,
                    path=path,
                    text=text,
                    url=url,
                )
                return notebook.id, remote_id
            finally:
                _active_client.reset(token)

    async def _prepare_studio_source_add(
        self,
        subject: str,
        exam_number: int,
    ) -> tuple[str, frozenset[str]]:
        notebook = await self._ensure_notebook(subject, exam_number)
        return notebook.id, await self._list_studio_source_ids(notebook.id)

    async def _list_studio_source_ids(self, notebook_id: str) -> frozenset[str]:
        async with self._with_client() as client:
            return frozenset(str(source.id) for source in await client.sources.list(notebook_id))

    async def _add_studio_source_to_notebook(
        self,
        notebook_id: str,
        source_type: str,
        title: str,
        *,
        path: Path | None,
        text: str | None,
        url: str | None,
    ) -> str:
        async with self._with_client() as client:
            if source_type == "file" and path is not None:
                remote = await client.sources.add_file(
                    notebook_id,
                    path,
                    wait=True,
                    title=title,
                )
            elif source_type == "text" and text is not None:
                remote = await client.sources.add_text(
                    notebook_id,
                    title,
                    text,
                    wait=True,
                )
            elif source_type == "url" and url is not None:
                remote = await client.sources.add_url(notebook_id, url, wait=True)
            else:
                raise ValueError("Studio source payload is incomplete")
            if not _remote_ready(remote):
                raise SourceIsolationError("NotebookLM source did not become ready")
            return str(remote.id)

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


def _studio_question_prompt(question: QuestionDraft) -> str:
    choices = "\n".join(
        f"{index}. {choice}" for index, choice in enumerate(question.choices)
    )
    return (
        "Answer this one multiple-choice question using only the selected sources. "
        "Return JSON only with status, correct_index, rationale, and evidence. "
        "status must be answered or no_support. answered requires one decisive "
        "zero-based correct_index and nonempty evidence. no_support requires "
        "correct_index null and is allowed only when the selected sources do not "
        "support an answer.\n\n"
        f"Question: {question.stem}\nChoices:\n{choices}"
    )


def _parse_question_response(value: object, choice_count: int) -> NotebookQuestionResult:
    if not isinstance(value, str) or not value.strip():
        raise NotebookQuestionContractError("NotebookLM question response is empty")
    try:
        payload = json.loads(value)
        parsed = _NotebookQuestionResponse.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise NotebookQuestionContractError(
            "NotebookLM question response violates the required contract"
        ) from error
    if parsed.correct_index is not None and parsed.correct_index >= choice_count:
        raise NotebookQuestionContractError(
            "NotebookLM answer index is outside the available choices"
        )
    return NotebookQuestionResult(
        NotebookQuestionStatus(parsed.status),
        parsed.correct_index,
        parsed.rationale,
        parsed.evidence,
    )


def _contains_hedge(value: str) -> bool:
    normalized = value.casefold()
    return any(
        marker in normalized
        for marker in (
            "maybe",
            "might",
            "could",
            "not sure",
            "probably",
            "possibly",
            "appears to be",
        )
    )


def _is_remote_source_not_found(error: BaseException) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status == 404:
        return True
    message = str(error).casefold()
    return "not found" in message or "does not exist" in message


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
