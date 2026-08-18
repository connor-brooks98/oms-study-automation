"""Durable, signature-driven orchestration for direct practice-question imports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from typing import Protocol, TypedDict, cast

from oms_hub.db import is_sqlite_busy
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.llm.domain import DiagnosticSource, LLMRequestError, LLMTask
from oms_hub.study_generation.notebook_errors import NotebookGatewayError
from oms_hub.study_generation.practice_answers import AnswerResolutionScope
from oms_hub.study_generation.practice_contracts import ExtractedAnswer, ExtractedQuestion
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    ImportSourceRole,
    QuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.practice_extraction import (
    ExtractionError,
    ExtractionProviderMetadata,
    ExtractionResult,
    SourceDocument,
)
from oms_hub.study_generation.practice_matching import pair_supplied_answers
from oms_hub.study_generation.studio_domain import (
    StudioRun,
    StudioRunStage,
    StudioSource,
    StudioSourceState,
)
from oms_hub.study_generation.studio_repository import NotebookMutationBusy, StudioRepository

_DOWNSTREAM_PREFIXES = {
    StudioRunStage.PARSE: ("extract", "pair", "answered", "normalized"),
    StudioRunStage.EXTRACT: ("pair", "answered", "normalized"),
    StudioRunStage.PAIR: ("answered", "normalized"),
    StudioRunStage.ANSWER_NOTEBOOK: ("normalized",),
    StudioRunStage.NORMALIZE: (),
}


class DocumentParser(Protocol):
    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument: ...


class QuestionExtractor(Protocol):
    def extract(
        self, documents: tuple[ParsedDocument | SourceDocument, ...]
    ) -> ExtractionResult: ...


class AnswerResolver(Protocol):
    def resolve(self, draft: QuestionDraft, scope: AnswerResolutionScope) -> QuestionDraft: ...


class NotebookAttacher(Protocol):
    def prepare_studio_source_add(
        self,
        subject: str,
        exam_number: int,
    ) -> tuple[str, frozenset[str]]: ...

    def add_studio_source_to_notebook(
        self,
        notebook_id: str,
        source_type: str,
        title: str,
        *,
        path: Path | None = None,
        text: str | None = None,
        url: str | None = None,
    ) -> str: ...

    def list_studio_source_ids(self, notebook_id: str) -> frozenset[str]: ...


class AttachmentArguments(TypedDict, total=False):
    path: Path
    text: str
    url: str


class _ImportAttachmentPending(RuntimeError):
    """A durable remote effect must be reconciled before import can continue."""


def stage_signature(
    stage: str,
    *,
    source_hashes: tuple[str, ...],
    parser_versions: tuple[str, ...],
    provider_model: str,
    prompt_version: str,
    artifact_hashes: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
    binding_identities: tuple[str, ...] = (),
) -> str:
    """Hash a canonical stage input, retaining source order and role assignments."""
    payload = {
        "stage": stage,
        "source_hashes": list(source_hashes),
        "parser_versions": list(parser_versions),
        "provider_model": provider_model,
        "prompt_version": prompt_version,
    }
    if artifact_hashes:
        payload["artifact_hashes"] = list(artifact_hashes)
    if roles:
        payload["roles"] = list(roles)
    if binding_identities:
        payload["binding_identities"] = list(binding_identities)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class QuizImportWorker:
    """Execute direct imports until human review, never publication.

    Artifacts are JSON so a retry after process restart reconstructs canonical parser
    output rather than reparsing immutable snapshots.  A signature mismatch removes
    only derived downstream artifacts and review rows.
    """

    def __init__(
        self,
        repository: StudioRepository,
        parser: DocumentParser,
        extractor: QuestionExtractor,
        answers: AnswerResolver,
        notebook: NotebookAttacher,
        asset_root: Path,
        *,
        extraction_model: str | None = None,
        extraction_prompt_version: str = "practice-extraction-v1",
    ) -> None:
        self.repository = repository
        self.parser = parser
        self.extractor = extractor
        self.answers = answers
        self.notebook = notebook
        self.asset_root = asset_root
        self.extraction_model = extraction_model
        self.extraction_prompt_version = extraction_prompt_version

    def run(self, run: StudioRun | None) -> None:
        if run is None:
            return
        try:
            sources, roles = self._sources(run)
            parsed = self._parse(run, sources, roles)
            extracted = self._extract(run, parsed, sources, roles)
            drafts = self._pair(run, extracted, sources, roles)
            if any(_requires_review_before_resolution(draft) for draft in drafts):
                self._review(run, drafts, sources, roles)
                return
            resolved = self._resolve_answers(run, drafts, sources, roles)
            self._review(run, resolved, sources, roles)
        except ExtractionError as error:
            metadata = error.provider_metadata
            provider = metadata[-1] if metadata else None
            self.repository.save_run_artifact(
                run.id,
                "failure:extract",
                _failure_signature(run.id, error.raw_responses, metadata),
                _extraction_failure_json(error),
                provider=provider.provider.value if provider else None,
                model=provider.model if provider else None,
                request_id=provider.request_id if provider else None,
            )
            self._record_failure(
                run,
                DiagnosticSource.CONTRACT.value,
                str(error),
                retry=False,
                raw_response="\n".join(error.raw_responses),
            )
        except _ImportAttachmentPending as error:
            self.repository.retry_run(
                run.id,
                DiagnosticSource.STUDY_HUB.value,
                str(error),
                timedelta(seconds=1),
            )
        except NotebookGatewayError as error:
            self._record_failure(run, error.source.value, str(error), retry=error.retryable)
        except LLMRequestError as error:
            self._record_failure(
                run,
                error.source.value,
                str(error),
                retry=error.source
                in {DiagnosticSource.NETWORK, DiagnosticSource.QUOTA, DiagnosticSource.SERVICE},
            )
        except Exception as error:  # noqa: BLE001 - durable worker boundary
            self._record_failure(
                run,
                DiagnosticSource.STUDY_HUB.value,
                str(error),
                retry=is_sqlite_busy(error),
            )

    def _sources(
        self, run: StudioRun
    ) -> tuple[tuple[StudioSource, ...], tuple[ImportSourceRole, ...]]:
        bindings = self.repository.import_sources(run.id)
        if not bindings:
            raise ValueError("direct import run has no source bindings")
        sources: list[StudioSource] = []
        for binding in bindings:
            source = self.repository.get(binding.source_id)
            if (
                source is None
                or source.state
                not in {
                    StudioSourceState.READY,
                    StudioSourceState.ATTACHING,
                    StudioSourceState.ATTACHED,
                }
                or source.payload_path is None
                or source.snapshot_sha256 is None
                or source.media_type is None
            ):
                raise ValueError("direct import source is no longer a verified local snapshot")
            sources.append(source)
        return tuple(sources), tuple(binding.role for binding in bindings)

    def _parse(
        self,
        run: StudioRun,
        sources: tuple[StudioSource, ...],
        roles: tuple[ImportSourceRole, ...],
    ) -> tuple[ParsedDocument, ...]:
        self.repository.set_run_stage(run.id, StudioRunStage.PARSE)
        self.asset_root.mkdir(parents=True, exist_ok=True)
        documents: list[ParsedDocument] = []
        for source, role in zip(sources, roles, strict=True):
            signature = stage_signature(
                "parse",
                source_hashes=(source.snapshot_sha256 or "",),
                parser_versions=self._parser_versions(),
                provider_model="local",
                prompt_version="canonical-document-v1",
                roles=(role.value,),
            )
            key = f"parse:{source.id}"
            cached = self.repository.run_artifact(run.id, key)
            if cached is not None and cached.signature_sha256 == signature:
                documents.append(_document_from_json(cached.payload_json))
                continue
            if cached is not None:
                self.repository.invalidate_import_artifacts_after(
                    run.id, _DOWNSTREAM_PREFIXES[StudioRunStage.PARSE]
                )
            assert source.payload_path is not None
            assert source.media_type is not None
            assert source.snapshot_sha256 is not None
            snapshot = SourceSnapshot(
                source.id,
                source.title,
                source.payload_path,
                source.media_type,
                source.snapshot_sha256,
                source.final_url,
            )
            document = self.parser.parse(snapshot, self.asset_root / source.id)
            if document.source_id != source.id or document.source_sha256 != source.snapshot_sha256:
                raise ValueError("parser returned a document for another immutable source")
            self.repository.save_run_artifact(run.id, key, signature, _document_json(document))
            documents.append(document)
        return tuple(documents)

    def _extract(
        self,
        run: StudioRun,
        documents: tuple[ParsedDocument, ...],
        sources: tuple[StudioSource, ...],
        roles: tuple[ImportSourceRole, ...],
    ) -> ExtractionResult:
        self.repository.set_run_stage(run.id, StudioRunStage.EXTRACT)
        parse_hashes = tuple(
            _artifact_hash(self.repository, run.id, f"parse:{source.id}") for source in sources
        )
        signature = stage_signature(
            "extract",
            source_hashes=tuple(source.snapshot_sha256 or "" for source in sources),
            parser_versions=tuple(
                f"{item.parser_name}:{item.parser_version}" for item in documents
            ),
            provider_model=self._extraction_model(),
            prompt_version=self.extraction_prompt_version,
            artifact_hashes=parse_hashes,
            roles=tuple(role.value for role in roles),
        )
        cached = self.repository.run_artifact(run.id, "extract")
        if cached is not None and cached.signature_sha256 == signature:
            return _extraction_from_json(cached.payload_json)
        if cached is not None:
            self.repository.invalidate_import_artifacts_after(
                run.id, _DOWNSTREAM_PREFIXES[StudioRunStage.EXTRACT]
            )
        result = self.extractor.extract(
            tuple(
                SourceDocument(document, source.title, role.value)
                for document, source, role in zip(documents, sources, roles, strict=True)
            )
        )
        parser_blockers = tuple(
            DraftDiagnostic(
                "parser-blocker",
                warning.removeprefix("BLOCKER: ").strip(),
                DiagnosticSeverity.BLOCKER,
            )
            for document in documents
            for warning in document.warnings
            if warning.startswith("BLOCKER:")
        )
        if parser_blockers:
            result = replace(result, diagnostics=(*result.diagnostics, *parser_blockers))
        provider = result.provider_metadata[-1] if result.provider_metadata else None
        self.repository.save_run_artifact(
            run.id,
            "extract",
            signature,
            _extraction_json(result),
            provider=provider.provider.value if provider else None,
            model=provider.model if provider else None,
            request_id=provider.request_id if provider else None,
        )
        return result

    def _pair(
        self,
        run: StudioRun,
        extracted: ExtractionResult,
        sources: tuple[StudioSource, ...],
        roles: tuple[ImportSourceRole, ...],
    ) -> tuple[QuestionDraft, ...]:
        self.repository.set_run_stage(run.id, StudioRunStage.PAIR)
        signature = stage_signature(
            "pair",
            source_hashes=tuple(source.snapshot_sha256 or "" for source in sources),
            parser_versions=(),
            provider_model="deterministic",
            prompt_version="supplied-answer-pairing-v2",
            artifact_hashes=(_artifact_hash(self.repository, run.id, "extract"),),
            roles=tuple(role.value for role in roles),
        )
        cached = self.repository.run_artifact(run.id, "pair")
        if cached is not None and cached.signature_sha256 == signature:
            return _drafts_from_json(cached.payload_json)
        if cached is not None:
            self.repository.invalidate_import_artifacts_after(
                run.id, _DOWNSTREAM_PREFIXES[StudioRunStage.PAIR]
            )
        drafts = pair_supplied_answers(
            extracted.questions,
            extracted.answers,
            question_source_refs=extracted.question_source_refs,
        )
        # Extraction-level ambiguity belongs to the run, not every question.  Copying
        # it made one missing count look like N separate question failures.
        self.repository.save_run_artifact(
            run.id,
            "review:run-diagnostics",
            _artifact_hash(self.repository, run.id, "extract"),
            json.dumps(
                [
                    {
                        "code": item.code,
                        "message": item.message,
                        "severity": item.severity.value,
                        "acknowledged": False,
                        "overridable": item.code
                        in {
                            "conflicting-duplicate-question",
                            "conflicting-question-identifier",
                            "conflicting-question-source-reference",
                            "incomplete-sequential-question-extraction",
                        },
                    }
                    for item in extracted.diagnostics
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.repository.save_run_artifact(run.id, "pair", signature, _drafts_json(drafts))
        return drafts

    def _resolve_answers(
        self,
        run: StudioRun,
        drafts: tuple[QuestionDraft, ...],
        sources: tuple[StudioSource, ...],
        roles: tuple[ImportSourceRole, ...],
    ) -> tuple[QuestionDraft, ...]:
        missing = tuple(draft for draft in drafts if draft.correct_index is None)
        if not missing:
            return drafts
        if not any(
            binding.attach_to_notebook
            and binding.role
            in {ImportSourceRole.SUPPORTING_REFERENCE, ImportSourceRole.COMBINED}
            for binding in self.repository.import_sources(run.id)
        ):
            return tuple(
                replace(
                    draft,
                    diagnostics=(
                        *draft.diagnostics,
                        DraftDiagnostic(
                            "notebook-support-not-selected",
                            (
                                "answer remains unresolved because no supporting reference "
                                "was selected for NotebookLM"
                            ),
                            DiagnosticSeverity.BLOCKER,
                        ),
                    ),
                )
                if draft.correct_index is None
                else draft
                for draft in drafts
            )
        self.repository.set_run_stage(run.id, StudioRunStage.ANSWER_NOTEBOOK)
        remote_ids = self._supporting_remote_ids(run, sources, roles)
        binding_identities = self._supporting_binding_identities(run.id)
        signature = stage_signature(
            "answer",
            source_hashes=tuple(source.snapshot_sha256 or "" for source in sources),
            parser_versions=(),
            provider_model=f"notebooklm+{self._answer_model()}",
            prompt_version="practice-answer-resolution-v1",
            artifact_hashes=(_artifact_hash(self.repository, run.id, "pair"),),
            roles=tuple(role.value for role in roles),
            binding_identities=binding_identities,
        )
        cached = self.repository.run_artifact(run.id, "answered")
        if cached is not None and cached.signature_sha256 == signature:
            return _drafts_from_json(cached.payload_json)
        if cached is not None:
            self.repository.invalidate_import_artifacts_after(
                run.id, _DOWNSTREAM_PREFIXES[StudioRunStage.ANSWER_NOTEBOOK]
            )
        scope = AnswerResolutionScope(run.subject, run.exam_number, remote_ids)
        resolved = tuple(
            _without_resolved_missing_answer_diagnostics(
                self.answers.resolve(draft, scope),
                was_missing=draft.correct_index is None,
            )
            for draft in drafts
        )
        if any(draft.answer_provenance is AnswerProvenance.GENERATED_BY_AI for draft in resolved):
            self.repository.set_run_stage(run.id, StudioRunStage.ANSWER_FALLBACK)
        self.repository.save_run_artifact(run.id, "answered", signature, _drafts_json(resolved))
        return resolved

    def _supporting_remote_ids(
        self,
        run: StudioRun,
        sources: tuple[StudioSource, ...],
        roles: tuple[ImportSourceRole, ...],
    ) -> tuple[str, ...]:
        bindings = self.repository.import_sources(run.id)
        remote_ids: list[str] = []
        for binding, source, role in zip(bindings, sources, roles, strict=True):
            if not binding.attach_to_notebook:
                continue
            if role not in {ImportSourceRole.SUPPORTING_REFERENCE, ImportSourceRole.COMBINED}:
                raise ValueError("question and answer-key sources must not attach to NotebookLM")
            if bool(binding.remote_notebook_id) != bool(binding.remote_source_id):
                raise ValueError("NotebookLM supporting source binding is incomplete")
            if binding.remote_notebook_id and binding.remote_source_id:
                remote_ids.append(binding.remote_source_id)
                continue
            remote_id = self._attach_supporting_source(run, source)
            remote_ids.append(remote_id)
        if not remote_ids:
            raise ValueError("missing answers require an attached supporting reference")
        if len(remote_ids) != len(set(remote_ids)):
            raise ValueError("NotebookLM supporting source bindings are not distinct")
        return tuple(remote_ids)

    def _attach_supporting_source(self, run: StudioRun, source: StudioSource) -> str:
        """Drive one durable add, reconciling an interrupted effect before retry."""
        for _attempt in range(2):
            try:
                operation = self.repository.ensure_import_source_attachment(run.id, source.id)
            except NotebookMutationBusy as error:
                raise _ImportAttachmentPending(str(error)) from error
            if operation is None:
                binding = next(
                    item
                    for item in self.repository.import_sources(run.id)
                    if item.source_id == source.id
                )
                if not binding.remote_source_id:
                    raise ValueError("durable NotebookLM import binding is incomplete")
                return binding.remote_source_id

            claimed = self.repository.claim_source_operation(operation.id)
            if claimed is None:
                raise _ImportAttachmentPending(
                    "NotebookLM source attachment is owned by another worker"
                )
            operation, claimed_source = claimed
            if operation.state == "reconciling":
                try:
                    with self._notebook_scope(run, operation.id):
                        if not operation.notebook_id:
                            raise ValueError("durable source operation is missing its notebook")
                        remote_ids = self.notebook.list_studio_source_ids(
                            operation.notebook_id
                        )
                    outcome = self.repository.reconcile_attach_operation(
                        operation.id,
                        set(remote_ids),
                        import_run_id=run.id,
                    )
                except NotebookGatewayError as error:
                    self.repository.mark_attach_reconciling(
                        operation.id,
                        error.source.value,
                        str(error),
                        during_reconciliation=True,
                    )
                    raise _ImportAttachmentPending(str(error)) from error
                except Exception as error:
                    self.repository.mark_attach_reconciling(
                        operation.id,
                        DiagnosticSource.STUDY_HUB.value,
                        str(error),
                        during_reconciliation=True,
                    )
                    raise _ImportAttachmentPending(str(error)) from error
                if outcome == "adopted":
                    binding = next(
                        item
                        for item in self.repository.import_sources(run.id)
                        if item.source_id == source.id
                    )
                    assert binding.remote_source_id is not None
                    return binding.remote_source_id
                if outcome == "retry":
                    continue
                raise ValueError(
                    "NotebookLM source attachment requires manual reconciliation"
                )

            remote_effect_started = False
            try:
                arguments = _attachment_arguments(claimed_source)
                with self._notebook_scope(run, operation.id):
                    notebook_id, baseline = self.notebook.prepare_studio_source_add(
                        run.subject,
                        run.exam_number,
                    )
                    self.repository.record_attach_baseline(
                        operation.id,
                        notebook_id,
                        set(baseline),
                    )
                    remote_effect_started = True
                    remote_id = self.notebook.add_studio_source_to_notebook(
                        notebook_id,
                        claimed_source.source_type.value,
                        claimed_source.title,
                        **arguments,
                    )
                self.repository.complete_attach_operation(
                    operation.id,
                    remote_id,
                    import_run_id=run.id,
                )
                return remote_id
            except NotebookMutationBusy as error:
                self.repository.defer_attach_for_notebook(operation.id)
                raise _ImportAttachmentPending(str(error)) from error
            except NotebookGatewayError as error:
                if remote_effect_started:
                    self.repository.mark_attach_reconciling(
                        operation.id,
                        error.source.value,
                        str(error),
                    )
                    raise _ImportAttachmentPending(str(error)) from error
                self.repository.fail_attach_preparation(
                    operation.id,
                    error.source.value,
                    str(error),
                    retry=error.retryable,
                )
                raise
            except Exception as error:
                if remote_effect_started:
                    self.repository.mark_attach_reconciling(
                        operation.id,
                        DiagnosticSource.STUDY_HUB.value,
                        str(error),
                    )
                    raise _ImportAttachmentPending(str(error)) from error
                self.repository.fail_attach_preparation(
                    operation.id,
                    DiagnosticSource.STUDY_HUB.value,
                    str(error),
                    retry=is_sqlite_busy(error),
                )
                raise
        raise _ImportAttachmentPending(
            "NotebookLM source attachment is queued after reconciliation"
        )

    def _notebook_scope(
        self,
        run: StudioRun,
        operation_id: str,
    ) -> AbstractContextManager[None]:
        scope = getattr(self.notebook, "mutation_scope", None)
        if not callable(scope):
            return nullcontext()
        return cast(
            Callable[[str, int, str, str], AbstractContextManager[None]],
            scope,
        )(run.subject, run.exam_number, "studio", operation_id)

    def _supporting_binding_identities(self, run_id: str) -> tuple[str, ...]:
        bindings = self.repository.import_sources(run_id)
        support_bindings = tuple(
            binding
            for binding in bindings
            if binding.attach_to_notebook
            and binding.role in {ImportSourceRole.SUPPORTING_REFERENCE, ImportSourceRole.COMBINED}
        )
        if any(
            not binding.remote_notebook_id or not binding.remote_source_id
            for binding in support_bindings
        ):
            raise ValueError("NotebookLM supporting source binding is incomplete")
        return tuple(
            f"{binding.position}:{binding.source_id}:{binding.remote_notebook_id}:{binding.remote_source_id}"
            for binding in support_bindings
        )

    def _review(
        self,
        run: StudioRun,
        drafts: tuple[QuestionDraft, ...],
        sources: tuple[StudioSource, ...],
        roles: tuple[ImportSourceRole, ...],
    ) -> None:
        self.repository.set_run_stage(run.id, StudioRunStage.NORMALIZE)
        pair_or_answer = "answered" if self.repository.run_artifact(run.id, "answered") else "pair"
        signature = stage_signature(
            "normalize",
            source_hashes=tuple(source.snapshot_sha256 or "" for source in sources),
            parser_versions=(),
            provider_model="local",
            prompt_version="question-draft-review-v1",
            artifact_hashes=(_artifact_hash(self.repository, run.id, pair_or_answer),),
            roles=tuple(role.value for role in roles),
        )
        cached = self.repository.run_artifact(run.id, "normalized")
        if cached is None or cached.signature_sha256 != signature:
            self.repository.save_run_artifact(run.id, "normalized", signature, _drafts_json(drafts))
        self.repository.await_import_review(run.id, drafts)

    def _record_failure(
        self,
        run: StudioRun,
        source: str,
        error: str,
        *,
        retry: bool,
        raw_response: str | None = None,
    ) -> None:
        self.repository.record_run_attempt(run.id, run.attempts, source, raw_response, error)
        if retry and run.attempts < 4:
            self.repository.retry_run(
                run.id,
                source,
                error,
                timedelta(seconds=min(30 * (2 ** (run.attempts - 1)), 300)),
            )
        else:
            self.repository.fail_run(run.id, source, error)

    def _parser_versions(self) -> tuple[str, ...]:
        processors = (getattr(self.parser, "primary", None), *getattr(self.parser, "fallbacks", ()))
        versions = tuple(
            f"{processor.name}:{processor.version}"
            for processor in processors
            if processor is not None
            and hasattr(processor, "name")
            and hasattr(processor, "version")
        )
        return versions or (f"{type(self.parser).__module__}.{type(self.parser).__qualname__}",)

    def _extraction_model(self) -> str:
        if self.extraction_model is not None:
            return self.extraction_model
        generator = getattr(self.extractor, "generator", None)
        settings = getattr(generator, "settings", None)
        if settings is not None:
            assignment = settings.assignment(LLMTask.QUIZ_EXTRACTION)
            return f"{assignment.provider.value}:{assignment.model}"
        return "configured-extractor"

    def _answer_model(self) -> str:
        fallback = getattr(self.answers, "fallback", None)
        settings = getattr(fallback, "settings", None)
        if settings is not None:
            assignment = settings.assignment(LLMTask.QUIZ_ANSWER_GENERATION)
            return f"{assignment.provider.value}:{assignment.model}"
        return "configured-answer-fallback"


def _artifact_hash(repository: StudioRepository, run_id: str, key: str) -> str:
    artifact = repository.run_artifact(run_id, key)
    if artifact is None:
        raise ValueError(f"required import artifact is missing: {key}")
    return hashlib.sha256(artifact.payload_json.encode("utf-8")).hexdigest()


def _failure_signature(
    run_id: str,
    raw_responses: tuple[str, ...],
    metadata: tuple[ExtractionProviderMetadata, ...],
) -> str:
    payload = {
        "run_id": run_id,
        "raw_responses": list(raw_responses),
        "provider_metadata": [
            {**asdict(item), "provider": item.provider.value} for item in metadata
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _extraction_failure_json(error: ExtractionError) -> str:
    return json.dumps(
        {
            "raw_responses": list(error.raw_responses),
            "provider_metadata": [
                {**asdict(item), "provider": item.provider.value}
                for item in error.provider_metadata
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _attachment_arguments(source: StudioSource) -> AttachmentArguments:
    """Build one and only one NotebookLM payload argument from an immutable snapshot."""
    assert source.payload_path is not None
    if source.source_type.value == "file":
        return {"path": source.payload_path}
    if source.source_type.value == "text":
        return {"text": source.payload_path.read_text(encoding="utf-8")}
    if source.source_type.value == "url":
        if not source.final_url:
            raise ValueError("URL import source is missing its final URL snapshot")
        return {"url": source.final_url}
    raise ValueError("unsupported Studio source type for NotebookLM attachment")


def _requires_review_before_resolution(draft: QuestionDraft) -> bool:
    """Only pairing's expected missing-answer markers are eligible for resolution."""
    return any(
        diagnostic.severity is DiagnosticSeverity.BLOCKER
        and diagnostic.code not in {"missing-supplied-answer", "unmatched-question"}
        for diagnostic in draft.diagnostics
    )


def _without_resolved_missing_answer_diagnostics(
    draft: QuestionDraft, *, was_missing: bool
) -> QuestionDraft:
    if not was_missing:
        return draft
    return replace(
        draft,
        diagnostics=tuple(
            diagnostic
            for diagnostic in draft.diagnostics
            if diagnostic.code not in {"missing-supplied-answer", "unmatched-question"}
        ),
    )


def _document_json(document: ParsedDocument) -> str:
    return json.dumps(
        {
            "source_id": document.source_id,
            "source_sha256": document.source_sha256,
            "source_format": document.source_format,
            "parser_name": document.parser_name,
            "parser_version": document.parser_version,
            "segments": [
                {
                    "key": item.key,
                    "kind": item.kind.value,
                    "text": item.text,
                    "locator": asdict(item.locator),
                    "asset_keys": list(item.asset_keys),
                    "parent_key": item.parent_key,
                    "previous_key": item.previous_key,
                    "next_key": item.next_key,
                    "style_metadata": list(item.style_metadata),
                }
                for item in document.segments
            ],
            "assets": [
                {
                    "key": item.key,
                    "path": str(item.path) if item.path else None,
                    "media_type": item.media_type,
                    "sha256": item.sha256,
                    "locator": asdict(item.locator),
                    "width": item.width,
                    "height": item.height,
                    "origin": item.origin,
                }
                for item in document.assets
            ],
            "warnings": list(document.warnings),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _document_from_json(payload_json: str) -> ParsedDocument:
    payload = json.loads(payload_json)
    return ParsedDocument(
        payload["source_id"],
        payload["source_sha256"],
        payload["source_format"],
        payload["parser_name"],
        payload["parser_version"],
        tuple(
            ParsedSegment(
                item["key"],
                SegmentKind(item["kind"]),
                item["text"],
                DocumentLocator(**item["locator"]),
                tuple(item["asset_keys"]),
                item["parent_key"],
                item["previous_key"],
                item["next_key"],
                tuple(item.get("style_metadata", ())),
            )
            for item in payload["segments"]
        ),
        tuple(
            ParsedAsset(
                item["key"],
                Path(item["path"]) if item["path"] else None,
                item["media_type"],
                item["sha256"],
                DocumentLocator(**item["locator"]),
                item["width"],
                item["height"],
                item["origin"],
            )
            for item in payload["assets"]
        ),
        tuple(payload["warnings"]),
    )


def _extraction_json(result: ExtractionResult) -> str:
    return json.dumps(
        {
            "questions": [item.model_dump(mode="json") for item in result.questions],
            "answers": [item.model_dump(mode="json") for item in result.answers],
            "question_source_refs": [
                [asdict(item) for item in refs] for refs in result.question_source_refs
            ],
            "provider_metadata": [
                {**asdict(item), "provider": item.provider.value}
                for item in result.provider_metadata
            ],
            "diagnostics": [
                {**asdict(item), "severity": item.severity.value} for item in result.diagnostics
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _extraction_from_json(payload_json: str) -> ExtractionResult:
    from oms_hub.llm.domain import ProviderName

    payload = json.loads(payload_json)
    return ExtractionResult(
        tuple(ExtractedQuestion.model_validate(item) for item in payload["questions"]),
        tuple(ExtractedAnswer.model_validate(item) for item in payload["answers"]),
        tuple(
            tuple(QuestionSourceRef(**item) for item in refs)
            for refs in payload["question_source_refs"]
        ),
        tuple(
            ExtractionProviderMetadata(
                ProviderName(item["provider"]),
                item["model"],
                item["request_id"],
                item["input_tokens"],
                item["output_tokens"],
                item["cost_microusd"],
                item["cache_creation_input_tokens"],
                item["cache_read_input_tokens"],
            )
            for item in payload["provider_metadata"]
        ),
        tuple(
            DraftDiagnostic(item["code"], item["message"], DiagnosticSeverity(item["severity"]))
            for item in payload["diagnostics"]
        ),
    )


def _drafts_json(drafts: tuple[QuestionDraft, ...]) -> str:
    return json.dumps(
        [
            {
                "question_id": draft.question_id,
                "original_identifier": draft.original_identifier,
                "stem": draft.stem,
                "choices": list(draft.choices),
                "correct_index": draft.correct_index,
                "rationale": draft.rationale,
                "image_ref": asdict(draft.image_ref) if draft.image_ref else None,
                "source_refs": [asdict(item) for item in draft.source_refs],
                "answer_provenance": (
                    draft.answer_provenance.value if draft.answer_provenance else None
                ),
                "extraction_confidence": draft.extraction_confidence,
                "diagnostics": [
                    {**asdict(item), "severity": item.severity.value} for item in draft.diagnostics
                ],
                "verification_required": draft.verification_required,
                "verified_at": draft.verified_at,
            }
            for draft in drafts
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _drafts_from_json(payload_json: str) -> tuple[QuestionDraft, ...]:
    from oms_hub.study_generation.domain import QuizImageRef

    payload = json.loads(payload_json)
    return tuple(
        QuestionDraft(
            item["question_id"],
            item["original_identifier"],
            item["stem"],
            tuple(item["choices"]),
            item["correct_index"],
            item["rationale"],
            QuizImageRef(**item["image_ref"]) if item["image_ref"] else None,
            tuple(QuestionSourceRef(**ref) for ref in item["source_refs"]),
            AnswerProvenance(item["answer_provenance"]) if item["answer_provenance"] else None,
            item["extraction_confidence"],
            tuple(
                DraftDiagnostic(
                    value["code"],
                    value["message"],
                    DiagnosticSeverity(value["severity"]),
                )
                for value in item["diagnostics"]
            ),
            item["verification_required"],
            item["verified_at"],
        )
        for item in payload
    )
