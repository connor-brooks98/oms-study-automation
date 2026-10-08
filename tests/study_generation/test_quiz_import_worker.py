import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.db import Database
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.files.atomic import verified_atomic_write
from oms_hub.llm.domain import DiagnosticSource, LLMRequestError, ProviderName
from oms_hub.models import StudioSourceOperationModel
from oms_hub.study_generation.domain import QuizImageRef
from oms_hub.study_generation.notebook_errors import NotebookGatewayError
from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedMatchingAnswer,
    ExtractedMatchingAnswerRow,
    ExtractedMatchingPrompt,
    ExtractedMatchingQuestion,
    ExtractedQuestion,
    SegmentCitation,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    ImportSourceRole,
    ImportSourceSelection,
    MatchingPromptDraft,
    MatchingQuestionDraft,
    QuestionDraft,
    QuestionSourceRef,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.practice_extraction import (
    ExtractionError,
    ExtractionProviderMetadata,
    ExtractionResult,
    SourceDocument,
)
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.quiz_import_worker import (
    QuizImportWorker,
    _artifact_hash,
    _document_from_json,
    _document_json,
    _drafts_from_json,
    _drafts_json,
    _extraction_from_json,
    _extraction_json,
    stage_signature,
)
from oms_hub.study_generation.studio_domain import (
    StudioRun,
    StudioRunState,
    StudioSourceState,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository

_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> None:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _repository(tmp_path: Path) -> StudioRepository:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    _OPEN_DATABASES.append(database)
    database.migrate()
    return StudioRepository(database)


def _ready_source(repository: StudioRepository, tmp_path: Path, title: str):
    source = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, title, purpose=StudioSourcePurpose.LOCAL_IMPORT
    )
    path = tmp_path / f"{source.id}.txt"
    digest = verified_atomic_write(b"Question 1\nA. Yes\nB. No", path)
    return repository.mark_import_ready(source.id, path, digest, media_type="text/plain")


def _queued_import(repository: StudioRepository, tmp_path: Path):
    source = _ready_source(repository, tmp_path, "Questions")
    return repository.queue_import_run(
        "Neuro",
        1,
        "Imported practice",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),),
    )


class _Parser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, snapshot, asset_root: Path) -> ParsedDocument:
        self.calls += 1
        return ParsedDocument(
            snapshot.id,
            snapshot.sha256,
            "text",
            "test",
            "1",
            (
                ParsedSegment(
                    "block-1", SegmentKind.PARAGRAPH, "Question", DocumentLocator("block 1")
                ),
            ),
            (),
            (),
        )


class _BlockingParser(_Parser):
    def parse(self, snapshot, asset_root: Path) -> ParsedDocument:
        parsed = super().parse(snapshot, asset_root)
        return replace(
            parsed,
            warnings=("BLOCKER: OCR is required but unavailable for slide 2",),
        )


class _FailOnceExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, documents):
        self.calls += 1
        raise LLMRequestError("temporary provider outage", source=DiagnosticSource.SERVICE)


class _UnusedAnswers:
    def resolve(self, draft, scope):
        raise AssertionError("no answer resolution is expected")


class _QuestionExtractor:
    def extract(self, documents) -> ExtractionResult:
        source = documents[0].document
        return ExtractionResult(
            (
                ExtractedQuestion(
                    original_identifier="1",
                    stem="Which answer is correct?",
                    choices=("Yes", "No"),
                    source_segments=(
                        SegmentCitation(source_id=source.source_id, segment_key="block-1"),
                    ),
                    confidence=0.9,
                ),
            ),
            (),
            ((QuestionSourceRef(source.source_id, "block-1", "block 1"),),),
            (),
            (),
            (),
        )


class _StaticExtractor:
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result

    def extract(self, documents: tuple[SourceDocument, ...]) -> ExtractionResult:
        del documents
        return self.result


class _RecordingAnswers:
    def __init__(self) -> None:
        self.calls: list[tuple[QuestionDraft, object]] = []

    def resolve(self, draft: QuestionDraft, scope: object) -> QuestionDraft:
        self.calls.append((draft, scope))
        return replace(
            draft,
            correct_index=0,
            rationale="The source supports the first choice.",
            answer_provenance=AnswerProvenance.NOTEBOOKLM,
            verification_required=False,
        )


def _matching_worker_fixture(
    tmp_path: Path,
    *,
    complete: bool,
    content_kind: QuizContentKind = QuizContentKind.PRACTICE_QUESTIONS,
) -> tuple[QuizImportWorker, StudioRepository, StudioRun, _RecordingAnswers]:
    repository = _repository(tmp_path)
    source = _ready_source(repository, tmp_path, "Matching questions")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Imported matching practice",
        "Neuro",
        1,
        content_kind,
        (ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),),
    )
    citation = SegmentCitation(source_id=source.id, segment_key="block-1")
    source_ref = QuestionSourceRef(source.id, "block-1", "block 1")
    matching = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match each description with its term.",
        prompts=(
            ExtractedMatchingPrompt(
                original_identifier="A",
                text="Alpha description",
                supplied_correct_index=1,
            ),
            ExtractedMatchingPrompt(
                original_identifier="B",
                text="Beta description",
                supplied_correct_index=0 if complete else None,
            ),
        ),
        choices=("Term one", "Term two"),
        rationale=None,
        source_segments=(citation,),
        candidate_assets=(),
        confidence=0.99,
    )
    extraction = ExtractionResult(
        questions=(matching,),
        answers=(),
        question_source_refs=((source_ref,),),
        answer_source_refs=(),
        provider_metadata=(),
        diagnostics=(),
    )
    answers = _RecordingAnswers()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _StaticExtractor(extraction),
        answers,
        object(),
        tmp_path / "assets",
    )
    return worker, repository, run, answers


def _mixed_worker_fixture(
    tmp_path: Path,
    *,
    content_kind: QuizContentKind = QuizContentKind.PRACTICE_QUESTIONS,
) -> tuple[
    QuizImportWorker,
    StudioRepository,
    StudioRun,
    _RecordingAnswers,
    "_AttachingNotebook",
]:
    repository = _repository(tmp_path)
    source = _ready_source(repository, tmp_path, "Mixed questions")
    supporting = _ready_source(repository, tmp_path, "Mixed supporting reference")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Imported mixed practice",
        "Neuro",
        1,
        content_kind,
        (
            ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(
                supporting.id,
                ImportSourceRole.SUPPORTING_REFERENCE,
                attach_to_notebook=True,
            ),
        ),
    )
    citation = SegmentCitation(source_id=source.id, segment_key="block-1")
    source_ref = QuestionSourceRef(source.id, "block-1", "block 1")
    matching = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match each description with its term.",
        prompts=(
            ExtractedMatchingPrompt(
                original_identifier="A",
                text="Alpha description",
                supplied_correct_index=1,
            ),
            ExtractedMatchingPrompt(
                original_identifier="B",
                text="Beta description",
                supplied_correct_index=0,
            ),
        ),
        choices=("Term one", "Term two"),
        rationale=None,
        source_segments=(citation,),
        candidate_assets=(),
        confidence=0.99,
    )
    mcq = ExtractedQuestion(
        original_identifier="2",
        stem="Which option is correct?",
        choices=("Yes", "No"),
        supplied_correct_index=None,
        rationale=None,
        source_segments=(citation,),
        candidate_assets=(),
        confidence=0.9,
    )
    extraction = ExtractionResult(
        questions=(matching, mcq),
        answers=(),
        question_source_refs=((source_ref,), (source_ref,)),
        answer_source_refs=(),
        provider_metadata=(),
        diagnostics=(),
    )
    answers = _RecordingAnswers()
    notebook = _AttachingNotebook()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _StaticExtractor(extraction),
        answers,
        notebook,
        tmp_path / "assets",
    )
    return worker, repository, run, answers, notebook


class _ResolvedAnswers:
    def __init__(self) -> None:
        self.scopes = []

    def resolve(self, draft, scope):
        self.scopes.append(scope)
        return replace(
            draft,
            correct_index=0,
            rationale="The supporting reference says yes.",
            answer_provenance=AnswerProvenance.NOTEBOOKLM,
            verification_required=False,
        )


class _AttachingNotebook:
    def __init__(self) -> None:
        self.calls = []
        self.remote_ids: set[str] = set()
        self.list_calls = []
        self.list_error = None

    def prepare_studio_source_add(self, subject, exam_number):
        return "notebook-1", frozenset(self.remote_ids)

    def add_studio_source_to_notebook(
        self, notebook_id, source_type, title, **kwargs
    ):
        subject, exam_number = "Neuro", 1
        self.calls.append((subject, exam_number, source_type, title, kwargs))
        remote_id = f"remote-{len(self.calls)}"
        self.remote_ids.add(remote_id)
        return remote_id

    def list_studio_source_ids(self, notebook_id, *, baseline_ids=None):
        self.list_calls.append((notebook_id, baseline_ids))
        if self.list_error is not None:
            raise self.list_error
        return frozenset(self.remote_ids)


class _InterruptingNotebook(_AttachingNotebook):
    def __init__(self) -> None:
        super().__init__()
        self.interrupt_once = True

    def add_studio_source_to_notebook(
        self, notebook_id, source_type, title, **kwargs
    ):
        remote_id = super().add_studio_source_to_notebook(
            notebook_id, source_type, title, **kwargs
        )
        if self.interrupt_once:
            self.interrupt_once = False
            raise KeyboardInterrupt("simulated process termination after remote add")
        return remote_id


class _AmbiguousInterruptingNotebook(_InterruptingNotebook):
    def add_studio_source_to_notebook(
        self, notebook_id, source_type, title, **kwargs
    ):
        _AttachingNotebook.add_studio_source_to_notebook(
            self,
            notebook_id, source_type, title, **kwargs
        )
        self.remote_ids.add("unexpected-competing-source")
        raise KeyboardInterrupt("simulated process termination after competing adds")


def test_retry_reuses_completed_parse_artifacts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    parser = _Parser()
    worker = QuizImportWorker(
        repository, parser, _FailOnceExtractor(), _UnusedAnswers(), object(), tmp_path / "assets"
    )
    run = _queued_import(repository, tmp_path)

    worker.run(repository.claim_next_run())
    assert repository.get_run(run.id).state is StudioRunState.RETRYING

    worker.run(repository.claim_next_run(datetime(2100, 1, 1, tzinfo=UTC)))
    assert parser.calls == 1  # one source, never reparsed on extraction retry


def test_parser_blocker_is_one_hard_run_diagnostic_not_repeated_per_question(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    run = _queued_import(repository, tmp_path)
    worker = QuizImportWorker(
        repository,
        _BlockingParser(),
        _QuestionExtractor(),
        _UnusedAnswers(),
        object(),
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    service = PracticeReviewService(repository)
    diagnostics = service.run_diagnostics(run.id)
    assert diagnostics == (
        {
            "acknowledged": False,
            "code": "parser-blocker",
            "message": "OCR is required but unavailable for slide 2",
            "overridable": False,
            "severity": "blocker",
        },
    )
    pair = repository.run_artifact(run.id, "pair")
    assert pair is not None
    drafts = _drafts_from_json(pair.payload_json)
    assert len(drafts) == 1
    assert all(item.code != "parser-blocker" for item in drafts[0].diagnostics)
    with pytest.raises(ValueError, match="cannot be acknowledged"):
        service.acknowledge_run_diagnostic(run.id, "parser-blocker")


def test_extraction_ambiguity_is_stored_once_as_overridable_run_diagnostic(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    run = _queued_import(repository, tmp_path)

    class DiagnosticExtractor(_QuestionExtractor):
        def extract(self, documents) -> ExtractionResult:
            result = super().extract(documents)
            return replace(
                result,
                diagnostics=(
                    DraftDiagnostic(
                        "incomplete-sequential-question-extraction",
                        "Question count needs review",
                        DiagnosticSeverity.BLOCKER,
                    ),
                ),
            )

    worker = QuizImportWorker(
        repository,
        _Parser(),
        DiagnosticExtractor(),
        _UnusedAnswers(),
        object(),
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    service = PracticeReviewService(repository)
    assert len(service.run_diagnostics(run.id)) == 1
    assert service.run_diagnostics(run.id)[0]["overridable"] is True
    assert service.blockers(run.id).count("Question count needs review") == 1
    service.acknowledge_run_diagnostic(
        run.id, "incomplete-sequential-question-extraction"
    )
    assert "Question count needs review" not in service.blockers(run.id)


def test_unmatched_mcq_answer_is_only_an_overridable_run_diagnostic(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    run = _queued_import(repository, tmp_path)

    class UnmatchedAnswerExtractor(_QuestionExtractor):
        def extract(self, documents) -> ExtractionResult:
            result = super().extract(documents)
            return replace(
                result,
                answers=(
                    ExtractedAnswer(
                        original_identifier="12",
                        correct_index=0,
                        rationale=None,
                        source_segments=(
                            SegmentCitation(
                                source_id=documents[0].document.source_id,
                                segment_key="block-1",
                            ),
                        ),
                    ),
                ),
                answer_source_refs=((QuestionSourceRef(
                    documents[0].document.source_id, "block-1", "block 1"
                ),),),
            )

    worker = QuizImportWorker(
        repository,
        _Parser(),
        UnmatchedAnswerExtractor(),
        _ResolvedAnswers(),
        object(),
        tmp_path / "assets",
    )
    worker.run(repository.claim_next_run())

    service = PracticeReviewService(repository)
    assert service.run_diagnostics(run.id) == ({
        "acknowledged": False,
        "code": "unmatched-supplied-answer",
        "message": "unmatched supplied answer: 12",
        "overridable": True,
        "severity": "blocker",
    },)
    assert all(issue.code != "unmatched-supplied-answer" for issue in service.issues(run.id))
    service.acknowledge_run_diagnostic(run.id, "unmatched-supplied-answer")
    assert "unmatched supplied answer: 12" not in service.blockers(run.id)


def test_v4_pair_cache_is_rebuilt_with_run_scoped_unmatched_answers(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    run = _queued_import(repository, tmp_path)
    source = repository.get(repository.import_sources(run.id)[0].source_id)
    assert source is not None
    extraction = ExtractionResult(
        (ExtractedQuestion(
            original_identifier="1", stem="Which answer is correct?", choices=("Yes", "No"),
            source_segments=(SegmentCitation(source_id=source.id, segment_key="block-1"),),
            confidence=0.9,
        ),),
        (ExtractedAnswer(
            original_identifier="12", correct_index=0, rationale=None,
            source_segments=(SegmentCitation(source_id=source.id, segment_key="block-1"),),
        ),),
        ((QuestionSourceRef(source.id, "block-1", "block 1"),),),
        ((QuestionSourceRef(source.id, "block-1", "block 1"),),),
        (),
        (),
    )
    repository.save_run_artifact(
        run.id, "extract", "extract-signature", _extraction_json(extraction)
    )
    old_signature = stage_signature(
        "pair",
        source_hashes=(source.snapshot_sha256 or "",),
        parser_versions=(),
        provider_model="deterministic",
        prompt_version="supplied-answer-pairing-v4",
        artifact_hashes=(_artifact_hash(repository, run.id, "extract"),),
        roles=(ImportSourceRole.QUESTIONS.value,),
    )
    repository.save_run_artifact(run.id, "pair", old_signature, "[]")
    worker = QuizImportWorker(
        repository, _Parser(), _StaticExtractor(extraction), _ResolvedAnswers(), object(),
        tmp_path / "assets",
    )

    drafts = worker._pair(run, extraction, (source,), (ImportSourceRole.QUESTIONS,))

    assert all(
        diagnostic.code != "unmatched-supplied-answer"
        for draft in drafts
        for diagnostic in draft.diagnostics
    )
    assert PracticeReviewService(repository).run_diagnostics(run.id)[0]["code"] == (
        "unmatched-supplied-answer"
    )


def test_extraction_signature_changes_with_model_or_source() -> None:
    first = stage_signature(
        "extract",
        source_hashes=("a" * 64,),
        parser_versions=("anydoc:0.1.3",),
        provider_model="openrouter:model-a",
        prompt_version="practice-extraction-v1",
    )
    changed_model = stage_signature(
        "extract",
        source_hashes=("a" * 64,),
        parser_versions=("anydoc:0.1.3",),
        provider_model="openrouter:model-b",
        prompt_version="practice-extraction-v1",
    )
    changed_source = stage_signature(
        "extract",
        source_hashes=("b" * 64,),
        parser_versions=("anydoc:0.1.3",),
        provider_model="openrouter:model-a",
        prompt_version="practice-extraction-v1",
    )
    assert len({first, changed_model, changed_source}) == 3
    assert (
        first
        == hashlib.sha256(
            b'{"parser_versions":["anydoc:0.1.3"],"prompt_version":"practice-extraction-v1","provider_model":"openrouter:model-a","source_hashes":["'
            + (b"a" * 64)
            + b'"],"stage":"extract"}'
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("stage", "old_version", "new_version", "downstream"),
    [
        (
            "extract",
            "practice-extraction-v3",
            "practice-extraction-v4",
            ("pair", "answered", "normalized"),
        ),
        (
            "pair",
            "supplied-answer-pairing-v4",
            "supplied-answer-pairing-v5",
            ("answered", "normalized"),
        ),
        (
            "answer",
            "practice-answer-resolution-v1",
            "practice-answer-resolution-v2",
            ("normalized",),
        ),
        (
            "normalize",
            "question-draft-review-v1",
            "question-draft-review-v2",
            ("normalized",),
        ),
    ],
)
def test_stage_version_change_invalidates_cached_downstream_artifacts(
    tmp_path: Path,
    stage: str,
    old_version: str,
    new_version: str,
    downstream: tuple[str, ...],
) -> None:
    repository = _repository(tmp_path)
    run = _queued_import(repository, tmp_path)
    old_signature = stage_signature(
        stage,
        source_hashes=("a" * 64,),
        parser_versions=(),
        provider_model="test",
        prompt_version=old_version,
    )
    new_signature = stage_signature(
        stage,
        source_hashes=("a" * 64,),
        parser_versions=(),
        provider_model="test",
        prompt_version=new_version,
    )
    for key in downstream:
        repository.save_run_artifact(run.id, key, old_signature, "[]")

    assert old_signature != new_signature
    repository.invalidate_import_artifacts_after(run.id, downstream)
    assert all(repository.run_artifact(run.id, key) is None for key in downstream)


def test_parse_cache_is_rebuilt_after_parser_version_change(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    run = _queued_import(repository, tmp_path)
    binding = repository.import_sources(run.id)[0]
    source = repository.get(binding.source_id)
    assert source is not None
    delegate = _Parser()
    old_document = ParsedDocument(
        source.id,
        source.snapshot_sha256 or "",
        "text",
        "test",
        "1",
        (
            ParsedSegment(
                "block-1",
                SegmentKind.PARAGRAPH,
                "stale cached question",
                DocumentLocator("block 1"),
            ),
        ),
        (),
        (),
    )
    old_signature = stage_signature(
        "parse",
        source_hashes=(source.snapshot_sha256 or "",),
        parser_versions=("anydoc:0.1.3", "pdf:1"),
        provider_model="local",
        prompt_version="canonical-document-v1",
        roles=(ImportSourceRole.QUESTIONS.value,),
    )
    repository.save_run_artifact(
        run.id,
        f"parse:{source.id}",
        old_signature,
        _document_json(old_document),
    )

    class Router:
        primary = SimpleNamespace(name="anydoc", version="0.1.4")
        fallbacks = (SimpleNamespace(name="pdf", version="2"),)

        def parse(self, snapshot, asset_root):
            return delegate.parse(snapshot, asset_root)

    worker = QuizImportWorker(
        repository,
        Router(),
        _QuestionExtractor(),
        _UnusedAnswers(),
        object(),
        tmp_path / "assets",
    )
    sources, roles = worker._sources(run)

    documents = worker._parse(run, sources, roles)

    assert delegate.calls == 1
    assert documents[0].parser_version == "1"
    artifact = repository.run_artifact(run.id, f"parse:{source.id}")
    assert artifact is not None
    assert artifact.signature_sha256 != old_signature












def test_stale_ready_snapshot_cannot_resurrect_a_deleted_import_source(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    questions = _ready_source(repository, tmp_path, "Questions")
    supporting = _ready_source(repository, tmp_path, "Reference")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Imported practice",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(
                supporting.id,
                ImportSourceRole.SUPPORTING_REFERENCE,
                attach_to_notebook=True,
            ),
        ),
    )
    stale_snapshot = repository.get(supporting.id)
    assert stale_snapshot is not None and stale_snapshot.state is StudioSourceState.READY
    assert repository.queue_source_delete(supporting.id).state is StudioSourceState.DELETED
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _ResolvedAnswers(),
        _AttachingNotebook(),
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())
    assert repository.get_run(run.id).state is StudioRunState.FAILED

    retained = repository.get(supporting.id)
    assert retained is not None and retained.state is StudioSourceState.DELETED
    with repository.database.session() as session:
        assert session.query(StudioSourceOperationModel).count() == 0


def test_blocked_pairing_finishes_awaiting_review_without_notebook_attachment(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    parser = _Parser()
    notebook = _AttachingNotebook()
    worker = QuizImportWorker(
        repository,
        parser,
        _BlockingExtractor(),
        _UnusedAnswers(),
        notebook,
        tmp_path / "assets",
    )
    run = _queued_import(repository, tmp_path)

    worker.run(repository.claim_next_run())

    assert repository.get_run(run.id).state is StudioRunState.AWAITING_REVIEW
    assert notebook.calls == []


def test_missing_answer_without_selected_notebook_source_finishes_awaiting_review(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    questions = _ready_source(repository, tmp_path, "Questions URL")
    answer_key = _ready_source(repository, tmp_path, "Answer key URL")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Imported practice",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(answer_key.id, ImportSourceRole.ANSWER_KEY),
        ),
    )
    notebook = _AttachingNotebook()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _UnusedAnswers(),
        notebook,
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    stored = repository.get_run(run.id)
    assert stored.state is StudioRunState.AWAITING_REVIEW
    assert stored.error is None
    assert repository.list_run_attempts(run.id) == ()
    assert notebook.calls == []
    normalized = repository.run_artifact(run.id, "normalized")
    assert normalized is not None
    drafts = _drafts_from_json(normalized.payload_json)
    expected = (
        "No answer was supplied. Enter and verify it during private review."
    )
    assert expected in drafts[0].blocking_diagnostics


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (DiagnosticSource.SERVICE, StudioRunState.RETRYING),
        (DiagnosticSource.AUTHENTICATION, StudioRunState.FAILED),
        (DiagnosticSource.MODEL, StudioRunState.FAILED),
    ],
)
def test_provider_failure_classification_is_durable(
    tmp_path: Path, source: DiagnosticSource, expected: StudioRunState
) -> None:
    repository = _repository(tmp_path)
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _FailingExtractor(source),
        _UnusedAnswers(),
        object(),
        tmp_path / "assets",
    )
    run = _queued_import(repository, tmp_path)

    worker.run(repository.claim_next_run())

    assert repository.get_run(run.id).state is expected


class _FailingExtractor:
    def __init__(self, source: DiagnosticSource) -> None:
        self.source = source

    def extract(self, documents):
        raise LLMRequestError("provider failed", source=self.source)


class _BlockingExtractor(_QuestionExtractor):
    def extract(self, documents) -> ExtractionResult:
        result = super().extract(documents)
        return replace(
            result,
            diagnostics=(
                DraftDiagnostic(
                    "conflicting-question", "Question data conflicts", DiagnosticSeverity.BLOCKER
                ),
            ),
        )


class _ContractExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, documents) -> ExtractionResult:
        self.calls += 1
        raise ExtractionError(
            "schema remained invalid",
            raw_responses=("{not-json}", "{still-not-json}"),
            provider_metadata=(
                ExtractionProviderMetadata(
                    ProviderName.OPENROUTER,
                    "model-a",
                    "request-1",
                    10,
                    20,
                    30,
                    40,
                    50,
                ),
                ExtractionProviderMetadata(
                    ProviderName.OPENROUTER,
                    "model-a",
                    "request-2",
                    11,
                    21,
                    31,
                    41,
                    51,
                ),
            ),
        )


def test_extraction_contract_failure_is_terminal_and_retains_full_provider_evidence(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    extractor = _ContractExtractor()
    worker = QuizImportWorker(
        repository, _Parser(), extractor, _UnusedAnswers(), object(), tmp_path / "assets"
    )
    run = _queued_import(repository, tmp_path)

    worker.run(repository.claim_next_run())

    assert extractor.calls == 1
    assert repository.get_run(run.id).state is StudioRunState.FAILED
    attempt = repository.list_run_attempts(run.id)[0]
    assert attempt.raw_response == "{not-json}\n{still-not-json}"
    artifact = repository.run_artifact(run.id, "failure:extract")
    assert artifact is not None
    assert (artifact.provider, artifact.model, artifact.request_id) == (
        "openrouter",
        "model-a",
        "request-2",
    )
    assert json.loads(artifact.payload_json) == {
        "provider_metadata": [
            {
                "cache_creation_input_tokens": 40,
                "cache_read_input_tokens": 50,
                "cost_microusd": 30,
                "input_tokens": 10,
                "model": "model-a",
                "output_tokens": 20,
                "provider": "openrouter",
                "request_id": "request-1",
            },
            {
                "cache_creation_input_tokens": 41,
                "cache_read_input_tokens": 51,
                "cost_microusd": 31,
                "input_tokens": 11,
                "model": "model-a",
                "output_tokens": 21,
                "provider": "openrouter",
                "request_id": "request-2",
            },
        ],
        "raw_responses": ["{not-json}", "{still-not-json}"],
    }


class _Assignment:
    def __init__(self, model: str) -> None:
        self.provider = ProviderName.OPENROUTER
        self.model = model


class _AnswerSettings:
    def __init__(self, model: str) -> None:
        self.model = model

    def assignment(self, task):
        assert task.value == "quiz_answer_generation"
        return _Assignment(self.model)


class _Fallback:
    def __init__(self, model: str) -> None:
        self.settings = _AnswerSettings(model)


class _CachedAnswers(_ResolvedAnswers):
    def __init__(self, model: str = "fallback-a") -> None:
        super().__init__()
        self.fallback = _Fallback(model)






class _FailClosedAnswers:
    def __init__(self) -> None:
        self.fallback_calls = 0

    def resolve(self, draft, scope):
        raise NotebookGatewayError(
            "selected supporting source is stale or foreign",
            source=DiagnosticSource.VALIDATION,
            retryable=False,
        )








def test_artifact_serializers_round_trip_full_provenance(tmp_path: Path) -> None:
    asset_path = tmp_path / "diagram.png"
    asset_digest = verified_atomic_write(b"asset", asset_path)
    document = ParsedDocument(
        "source-1",
        "a" * 64,
        "text",
        "parser",
        "2",
        (ParsedSegment("segment-1", SegmentKind.PARAGRAPH, "Question", DocumentLocator("p1")),),
        (
            ParsedAsset(
                "asset-1",
                asset_path,
                "image/png",
                asset_digest,
                DocumentLocator("p1"),
            ),
        ),
        ("warning",),
    )
    extracted = ExtractionResult(
        (
            ExtractedQuestion(
                original_identifier="1",
                stem="Question?",
                choices=("A", "B"),
                source_segments=(SegmentCitation(source_id="source-1", segment_key="segment-1"),),
                confidence=0.8,
            ),
            ExtractedMatchingQuestion(
                kind="matching",
                original_identifier="2",
                stem="Match these.",
                prompts=(
                    ExtractedMatchingPrompt(
                        original_identifier="A", text="First", supplied_correct_index=1
                    ),
                    ExtractedMatchingPrompt(
                        original_identifier="B", text="Second", supplied_correct_index=0
                    ),
                ),
                choices=("One", "Two"),
                rationale=None,
                source_segments=(
                    SegmentCitation(source_id="source-1", segment_key="matching-question"),
                ),
                confidence=0.9,
            ),
        ),
        (
            ExtractedMatchingAnswer(
                kind="matching",
                original_identifier="2",
                matches=(
                    ExtractedMatchingAnswerRow(
                        prompt_identifier="A",
                        correct_index=1,
                        source_segments=(
                            SegmentCitation(source_id="source-1", segment_key="matching-answer"),
                        ),
                    ),
                ),
            ),
        ),
        (
            (QuestionSourceRef("source-1", "segment-1", "p1"),),
            (QuestionSourceRef("source-1", "matching-question", "p2"),),
        ),
        ((QuestionSourceRef("source-1", "matching-answer", "p3"),),),
        (ExtractionProviderMetadata(ProviderName.GEMINI, "extract", "req", 1, 2, 3, 4, 5),),
        (DraftDiagnostic("warning", "Review", DiagnosticSeverity.WARNING),),
    )
    draft = QuestionDraft(
        "q1",
        "1",
        "Question?",
        ("A", "B"),
        0,
        "Because.",
        QuizImageRef("asset-1", "Source", "p1", "diagram"),
        (QuestionSourceRef("source-1", "segment-1", "p1"),),
        AnswerProvenance.PROVIDED_BY_SOURCE,
        0.8,
        (DraftDiagnostic("warning", "Review", DiagnosticSeverity.WARNING),),
        False,
        None,
        answer_evidence=("Model evidence",),
        answer_uncertainty_note="Check the source.",
    )
    matching_draft = MatchingQuestionDraft(
        "q2",
        "2",
        "Match these.",
        (
            MatchingPromptDraft("p1", "A", "First", 1),
            MatchingPromptDraft("p2", "B", "Second", 0),
        ),
        ("One", "Two"),
        "Source-marked matches: A -> Two; B -> One.",
        None,
        (QuestionSourceRef("source-1", "matching-question", "p2"),),
        AnswerProvenance.PROVIDED_BY_SOURCE,
        0.9,
        (),
        False,
        None,
    )

    assert _document_from_json(_document_json(document)) == document
    assert _extraction_from_json(_extraction_json(extracted)) == extracted
    assert _drafts_from_json(_drafts_json((draft, matching_draft))) == (
        draft,
        matching_draft,
    )
    legacy_payload = json.loads(_drafts_json((draft,)))
    del legacy_payload[0]["answer_evidence"]
    del legacy_payload[0]["answer_uncertainty_note"]
    assert _drafts_from_json(json.dumps(legacy_payload)) == (
        replace(draft, answer_evidence=(), answer_uncertainty_note=None),
    )




@pytest.mark.parametrize(
    ("answer_source_refs", "message"),
    [
        ({}, "answer_source_refs must be a list when present"),
        ([], "answer_source_refs must align with answers"),
    ],
)
def test_extraction_artifact_rejects_present_invalid_answer_source_refs(
    answer_source_refs: object, message: str
) -> None:
    result = ExtractionResult(
        (
            ExtractedQuestion(
                original_identifier="1",
                stem="Question?",
                choices=("A", "B"),
                source_segments=(SegmentCitation(source_id="source-1", segment_key="question-1"),),
                confidence=0.8,
            ),
        ),
        (
            ExtractedAnswer(
                original_identifier="1",
                correct_index=0,
                rationale=None,
                source_segments=(
                    SegmentCitation(source_id="source-1", segment_key="answer-1"),
                ),
            ),
        ),
        ((QuestionSourceRef("source-1", "question-1", "p1"),),),
        ((QuestionSourceRef("source-1", "answer-1", "p4"),),),
        (),
        (),
    )
    payload = json.loads(_extraction_json(result))
    payload["answer_source_refs"] = answer_source_refs

    with pytest.raises(ValueError, match=message):
        _extraction_from_json(json.dumps(payload))


def test_legacy_extraction_artifact_without_answer_source_refs_is_readable() -> None:
    matching = ExtractedMatchingAnswer(
        kind="matching",
        original_identifier="1",
        matches=(
            ExtractedMatchingAnswerRow(
                prompt_identifier="A",
                correct_index=0,
                source_segments=(SegmentCitation(source_id="source-1", segment_key="answer-1"),),
            ),
        ),
    )
    extracted = ExtractionResult(
        questions=(
            ExtractedMatchingQuestion(
                kind="matching",
                original_identifier="1",
                stem="Match terms.",
                prompts=(
                    ExtractedMatchingPrompt(
                        original_identifier="A", text="First", supplied_correct_index=0
                    ),
                    ExtractedMatchingPrompt(
                        original_identifier="B", text="Second", supplied_correct_index=1
                    ),
                ),
                choices=("One", "Two"),
                rationale=None,
                source_segments=(
                    SegmentCitation(source_id="source-1", segment_key="question-1"),
                ),
                confidence=0.9,
            ),
        ),
        answers=(matching,),
        question_source_refs=((QuestionSourceRef("source-1", "question-1", "p1"),),),
        answer_source_refs=((QuestionSourceRef("source-1", "answer-1", "p2"),),),
        provider_metadata=(),
        diagnostics=(),
    )
    payload = json.loads(_extraction_json(extracted))
    del payload["answer_source_refs"]

    restored = _extraction_from_json(json.dumps(payload))

    assert restored.questions == extracted.questions
    assert restored.answers == extracted.answers
    assert restored.answer_source_refs == ((),)


def test_matching_pairing_diagnostics_are_saved_as_overridable_run_diagnostics(
    tmp_path: Path,
) -> None:
    worker, repository, run, _answers = _matching_worker_fixture(tmp_path, complete=True)
    source_id = repository.import_sources(run.id)[0].source_id
    unmatched = ExtractedMatchingAnswer(
        kind="matching",
        original_identifier="unmatched",
        matches=(
            ExtractedMatchingAnswerRow(
                prompt_identifier="A",
                correct_index=0,
                source_segments=(SegmentCitation(source_id=source_id, segment_key="block-1"),),
            ),
        ),
    )
    extraction = worker.extractor.result
    worker.extractor = _StaticExtractor(
        replace(
            extraction,
            answers=(unmatched,),
            answer_source_refs=((QuestionSourceRef(source_id, "block-1", "block 1"),),),
        )
    )

    worker.run(repository.claim_next_run())

    assert PracticeReviewService(repository).run_diagnostics(run.id) == (
        {
            "acknowledged": False,
            "code": "unmatched-matching-answer-group",
            "message": "unmatched matching answer group: unmatched",
            "overridable": True,
            "severity": "blocker",
        },
    )


def test_incomplete_matching_group_stops_before_any_answer_provider_call(tmp_path: Path) -> None:
    worker, repository, run, answers = _matching_worker_fixture(tmp_path, complete=False)

    worker.run(repository.claim_next_run())

    assert answers.calls == []
    assert repository.get_run(run.id).state is StudioRunState.AWAITING_REVIEW
    assert "missing-supplied-matching-answer" in repository.run_artifact(
        run.id, "normalized"
    ).payload_json


def test_mixed_import_keeps_unanswered_mcq_private_and_preserves_supplied_matches(
    tmp_path: Path,
) -> None:
    worker, repository, run, answers, _notebook = _mixed_worker_fixture(tmp_path)

    worker.run(repository.claim_next_run())

    assert answers.calls == []
    drafts = _drafts_from_json(repository.run_artifact(run.id, "normalized").payload_json)
    matching = next(item for item in drafts if isinstance(item, MatchingQuestionDraft))
    assert tuple(prompt.correct_index for prompt in matching.prompts) == (1, 0)


def test_non_practice_mixed_import_rejects_matching_before_resolution(tmp_path: Path) -> None:
    worker, repository, run, answers, notebook = _mixed_worker_fixture(
        tmp_path,
        content_kind=QuizContentKind.EXAM_REVIEW,
    )

    worker.run(repository.claim_next_run())

    rejected = repository.get_run(run.id)
    assert answers.calls == []
    assert notebook.calls == []
    assert rejected.state is StudioRunState.FAILED
    assert rejected.error == "matching questions require practice-question content"


def test_non_practice_direct_import_rejects_matching_before_review(tmp_path: Path) -> None:
    worker, repository, run, answers = _matching_worker_fixture(
        tmp_path,
        complete=True,
        content_kind=QuizContentKind.EXAM_REVIEW,
    )

    worker.run(repository.claim_next_run())

    rejected = repository.get_run(run.id)
    assert answers.calls == []
    assert rejected.state is StudioRunState.FAILED
    assert rejected.error == "matching questions require practice-question content"


def test_retired_notebook_reference_selection_keeps_missing_answer_private(tmp_path):
    repository = _repository(tmp_path)
    question = _ready_source(repository, tmp_path, 'Questions')
    reference = _ready_source(repository, tmp_path, 'Reference')
    run = repository.queue_import_run('Neuro', 1, 'Retired reference', 'Neuro', 1,
        QuizContentKind.PRACTICE_QUESTIONS, (
            ImportSourceSelection(question.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(reference.id, ImportSourceRole.SUPPORTING_REFERENCE, True),
        ))
    worker = QuizImportWorker(repository, _Parser(), _QuestionExtractor(), _UnusedAnswers(),
                              object(), tmp_path / 'assets')
    worker.run(repository.claim_next_run())
    stored = repository.get_run(run.id)
    assert stored.state is StudioRunState.AWAITING_REVIEW
    assert stored.published_token is None
    review = PracticeReviewService(repository)
    assert review.review(run.id)[0].draft.correct_index is None
    assert review.blockers(run.id)
    assert repository.run_artifact(run.id, 'answered') is None
    question_id = review.review(run.id)[0].draft.question_id
    review.update_question(run.id, question_id, {"correct_index": 0,
        "rationale": "Verified against the supplied source."})
    assert "answer-review-required" not in {
        item.code for item in review.review(run.id)[0].draft.diagnostics
    }
    with repository.database.session() as session:
        assert session.query(StudioSourceOperationModel).count() == 0
