import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

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
from oms_hub.models import StudioImportRunSourceModel
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
from oms_hub.study_generation.studio_domain import StudioRunState, StudioSourceType
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

    def attach_studio_source(self, subject, exam_number, source_type, title, **kwargs):
        self.calls.append((subject, exam_number, source_type, title, kwargs))
        return "notebook-1", f"remote-{len(self.calls)}"


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
