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
from oms_hub.models import StudioImportRunSourceModel, StudioSourceOperationModel
from oms_hub.study_generation.domain import QuizImageRef
from oms_hub.study_generation.notebook_errors import NotebookGatewayError
from oms_hub.study_generation.practice_contracts import ExtractedQuestion, SegmentCitation
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    ImportSourceRole,
    ImportSourceSelection,
    QuestionDraft,
    QuestionSourceRef,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.practice_extraction import (
    ExtractionError,
    ExtractionProviderMetadata,
    ExtractionResult,
)
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.quiz_import_worker import (
    QuizImportWorker,
    _document_from_json,
    _document_json,
    _drafts_from_json,
    _drafts_json,
    _extraction_from_json,
    _extraction_json,
    stage_signature,
)
from oms_hub.study_generation.studio_domain import (
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

    def list_studio_source_ids(self, notebook_id):
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


def test_supporting_binding_is_attached_once_and_reused_for_answering(tmp_path: Path) -> None:
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
                supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
            ),
        ),
    )
    parser = _Parser()
    answers = _ResolvedAnswers()
    notebook = _AttachingNotebook()
    worker = QuizImportWorker(
        repository, parser, _QuestionExtractor(), answers, notebook, tmp_path / "assets"
    )

    worker.run(repository.claim_next_run())

    assert repository.get_run(run.id).state is StudioRunState.AWAITING_REVIEW
    assert [call[3] for call in notebook.calls] == ["Reference"]
    assert answers.scopes[0].supporting_source_ids == ("remote-1",)
    bindings = repository.import_sources(run.id)
    assert bindings[0].remote_source_id is None
    assert bindings[1].remote_notebook_id == "notebook-1"
    assert bindings[1].remote_source_id == "remote-1"

    worker.run(repository.get_run(run.id))
    assert len(notebook.calls) == 1


def test_interrupted_direct_import_add_reconciles_without_duplicate_remote_source(
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
    notebook = _InterruptingNotebook()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _ResolvedAnswers(),
        notebook,
        tmp_path / "assets",
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process termination"):
        worker.run(repository.claim_next_run())

    assert notebook.remote_ids == {"remote-1"}
    assert repository.import_sources(run.id)[1].remote_source_id is None
    with repository.database.session() as session:
        operation = session.query(StudioSourceOperationModel).one()
        assert operation.state == "executing"
        assert operation.notebook_id == "notebook-1"
        assert json.loads(operation.baseline_remote_ids_json) == []
    assert repository.recover_interrupted_jobs() >= 2

    worker.run(repository.claim_next_run(datetime(2100, 1, 1, tzinfo=UTC)))

    assert repository.get_run(run.id).state is StudioRunState.AWAITING_REVIEW
    assert notebook.remote_ids == {"remote-1"}
    assert len(notebook.calls) == 1
    assert repository.import_sources(run.id)[1].remote_source_id == "remote-1"
    with repository.database.session() as session:
        operation = session.query(StudioSourceOperationModel).one()
        assert operation.state == "completed"
        assert operation.remote_source_id == "remote-1"


def test_ambiguous_direct_import_delta_stops_for_review_without_repeating_add(
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
    notebook = _AmbiguousInterruptingNotebook()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _ResolvedAnswers(),
        notebook,
        tmp_path / "assets",
    )

    with pytest.raises(KeyboardInterrupt):
        worker.run(repository.claim_next_run())
    repository.recover_interrupted_jobs()
    worker.run(repository.claim_next_run(datetime(2100, 1, 1, tzinfo=UTC)))

    assert repository.get_run(run.id).state is StudioRunState.FAILED
    assert repository.get(supporting.id).state is StudioSourceState.NEEDS_REVIEW
    assert len(notebook.calls) == 1
    with repository.database.session() as session:
        operation = session.query(StudioSourceOperationModel).one()
        assert operation.state == "needs_review"
        assert "ambiguous remote source delta" in operation.error
    with pytest.raises(ValueError, match="pending source mutation"):
        repository.queue_source_delete(supporting.id)
    assert repository.get(supporting.id).state is StudioSourceState.NEEDS_REVIEW


def test_direct_import_attachment_uses_durable_delete_saga(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    questions = _ready_source(repository, tmp_path, "Questions")
    supporting = _ready_source(repository, tmp_path, "Reference")
    repository.queue_import_run(
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
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _ResolvedAnswers(),
        _AttachingNotebook(),
        tmp_path / "assets",
    )
    worker.run(repository.claim_next_run())

    attached = repository.get(supporting.id)
    assert attached is not None
    assert attached.state is StudioSourceState.READY
    assert attached.remote_notebook_id == "notebook-1"
    assert attached.remote_source_id == "remote-1"

    deleting = repository.queue_source_delete(supporting.id)
    assert deleting.state is StudioSourceState.DELETING
    operation = repository.claim_next_source_operation()
    assert operation is not None
    assert operation[0].operation_kind == "delete"
    assert operation[0].remote_source_id == "remote-1"


def test_direct_import_delete_cannot_overtake_an_active_attachment(tmp_path: Path) -> None:
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
    operation = repository.ensure_import_source_attachment(run.id, supporting.id)
    assert operation is not None and operation.state == "queued"

    with pytest.raises(ValueError, match="pending source mutation"):
        repository.queue_source_delete(supporting.id)

    retained = repository.get(supporting.id)
    assert retained is not None
    assert retained.state is StudioSourceState.ATTACHING
    claimed = repository.claim_source_operation(operation.id)
    assert claimed is not None
    assert claimed[0].state == "queued"


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

    with pytest.raises(ValueError, match="no longer available"):
        worker._attach_supporting_source(run, stale_snapshot)

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
        "answer remains unresolved because no supporting reference was selected for NotebookLM"
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


def test_answer_cache_invalidates_for_fallback_assignment_and_binding_identity(
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
                supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
            ),
        ),
    )
    answers = _CachedAnswers()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        answers,
        _AttachingNotebook(),
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())
    assert len(answers.scopes) == 1

    answers.fallback.settings.model = "fallback-b"
    worker.run(repository.get_run(run.id))
    assert len(answers.scopes) == 2

    with repository.database.session() as session:
        binding = (
            session.query(StudioImportRunSourceModel)
            .filter_by(run_id=run.id, source_id=supporting.id)
            .one()
        )
        binding.remote_source_id = "remote-rebound"
    worker.run(repository.get_run(run.id))
    assert len(answers.scopes) == 3
    assert answers.scopes[-1].supporting_source_ids == ("remote-rebound",)


def test_attachment_arguments_are_exclusive_for_file_text_and_url(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    questions = _ready_source(repository, tmp_path, "Questions")
    attachments = []
    for source_type, title, final_url in (
        (StudioSourceType.FILE, "File", None),
        (StudioSourceType.TEXT, "Text", None),
        (StudioSourceType.URL, "URL", "https://example.test/reference"),
    ):
        source = repository.create_source(
            "Neuro", 1, source_type, title, purpose=StudioSourcePurpose.LOCAL_IMPORT
        )
        path = tmp_path / f"{source.id}.txt"
        digest = verified_atomic_write(b"Reference", path)
        attachments.append(
            repository.mark_import_ready(
                source.id, path, digest, media_type="text/plain", final_url=final_url
            )
        )
    repository.queue_import_run(
        "Neuro",
        1,
        "Imported practice",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            *tuple(
                ImportSourceSelection(
                    source.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
                )
                for source in attachments
            ),
        ),
    )
    notebook = _AttachingNotebook()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _ResolvedAnswers(),
        notebook,
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    assert [call[3] for call in notebook.calls] == ["File", "Text", "URL"]
    assert [call[4] for call in notebook.calls] == [
        {"path": attachments[0].payload_path},
        {"text": "Reference"},
        {"url": "https://example.test/reference"},
    ]


class _FailClosedAnswers:
    def __init__(self) -> None:
        self.fallback_calls = 0

    def resolve(self, draft, scope):
        raise NotebookGatewayError(
            "selected supporting source is stale or foreign",
            source=DiagnosticSource.VALIDATION,
            retryable=False,
        )


def test_foreign_reused_binding_fails_closed_before_any_fallback(tmp_path: Path) -> None:
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
                supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
            ),
        ),
    )
    repository.save_import_source_binding(
        run.id, supporting.id, "foreign-notebook", "foreign-source"
    )
    answers = _FailClosedAnswers()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        answers,
        _AttachingNotebook(),
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    assert repository.get_run(run.id).state is StudioRunState.FAILED
    assert answers.fallback_calls == 0


def test_partial_reused_binding_fails_closed_without_fallback(tmp_path: Path) -> None:
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
                supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
            ),
        ),
    )
    with repository.database.session() as session:
        binding = (
            session.query(StudioImportRunSourceModel)
            .filter_by(run_id=run.id, source_id=supporting.id)
            .one()
        )
        binding.remote_source_id = "orphaned-source"
    answers = _FailClosedAnswers()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        answers,
        _AttachingNotebook(),
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    assert repository.get_run(run.id).state is StudioRunState.FAILED
    assert answers.fallback_calls == 0


def test_url_attachment_rejects_missing_final_url(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    questions = _ready_source(repository, tmp_path, "Questions")
    source = repository.create_source(
        "Neuro", 1, StudioSourceType.URL, "URL", purpose=StudioSourcePurpose.LOCAL_IMPORT
    )
    path = tmp_path / "snapshot.txt"
    digest = verified_atomic_write(b"Reference", path)
    supporting = repository.mark_import_ready(
        source.id, path, digest, media_type="text/plain", final_url=None
    )
    repository.queue_import_run(
        "Neuro",
        1,
        "Imported practice",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(
                supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
            ),
        ),
    )
    notebook = _AttachingNotebook()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _QuestionExtractor(),
        _ResolvedAnswers(),
        notebook,
        tmp_path / "assets",
    )

    worker.run(repository.claim_next_run())

    assert repository.list_runs()[0].state is StudioRunState.FAILED
    assert notebook.calls == []


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
        ),
        (),
        ((QuestionSourceRef("source-1", "segment-1", "p1"),),),
        (),
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
    )

    assert _document_from_json(_document_json(document)) == document
    assert _extraction_from_json(_extraction_json(extracted)) == extracted
    assert _drafts_from_json(_drafts_json((draft,))) == (draft,)
