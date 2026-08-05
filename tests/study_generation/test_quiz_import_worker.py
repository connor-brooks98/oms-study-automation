import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.files.atomic import verified_atomic_write
from oms_hub.llm.domain import DiagnosticSource, LLMRequestError
from oms_hub.study_generation.practice_contracts import ExtractedQuestion, SegmentCitation
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    ImportSourceRole,
    ImportSourceSelection,
    QuestionSourceRef,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.practice_extraction import ExtractionResult
from oms_hub.study_generation.quiz_import_worker import QuizImportWorker, stage_signature
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
