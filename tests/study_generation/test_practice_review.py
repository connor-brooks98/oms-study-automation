import json
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from oms_hub.db import Database
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.files.atomic import sha256_file
from oms_hub.models import PublishedQuizModel, StudioRunModel
from oms_hub.study_generation.domain import QuizImageRef
from oms_hub.study_generation.practice_contracts import (
    AssetCitation,
    ExtractedAnswer,
    ExtractedMatchingPrompt,
    ExtractedMatchingQuestion,
    ExtractedQuestion,
    SegmentCitation,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    MatchingPromptDraft,
    MatchingQuestionDraft,
    QuestionDraft,
    QuestionSourceRef,
    QuizContentKind,
)
from oms_hub.study_generation.practice_extraction import ExtractionResult
from oms_hub.study_generation.practice_matching import matching_summary, pair_supplied_answers
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.quiz_import_worker import (
    _document_json,
    _drafts_json,
    _extraction_json,
)
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_repository import StudioRepository


def _service(tmp_path: Path) -> PracticeReviewService:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(
            StudioRunModel(
                id="run-1",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Imported practice",
                label_key="imported practice",
                prompt="",
                workflow_kind="direct_import",
                state="awaiting_review",
                stage="review",
            )
        )
    return PracticeReviewService(StudioRepository(database))


def _draft(question_id: str, *, generated: bool) -> QuestionDraft:
    return QuestionDraft(
        question_id,
        question_id,
        "What is correct?",
        ("A", "B"),
        0,
        "Because.",
        None,
        (QuestionSourceRef("source", "segment", "page 1"),),
        AnswerProvenance.GENERATED_BY_AI if generated else AnswerProvenance.PROVIDED_BY_SOURCE,
        0.8,
        (),
        generated,
        None,
    )


def _matching_draft(question_id: str = "matching-1") -> MatchingQuestionDraft:
    return MatchingQuestionDraft(
        question_id,
        "1",
        "Match each description with its term.",
        (MatchingPromptDraft("p1", "A", "Alpha", 1), MatchingPromptDraft("p2", "B", "Beta", 0)),
        ("Term one", "Term two"),
        "Source-marked matches: A -> Term two; B -> Term one.",
        None,
        (QuestionSourceRef("source-1", "question-1", "page 1"),),
        AnswerProvenance.PROVIDED_BY_SOURCE,
        0.99,
        (),
        False,
        None,
    )


def test_matching_edit_is_atomic_and_regenerates_a_prefixed_summary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_matching_draft(),))
    updated = service.update_question(
        "run-1",
        "matching-1",
        {
            "kind": "matching",
            "stem": "Updated group stem",
            "prompts": [
                {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 0},
                {"id": "p2", "label": "B", "text": "Beta", "correct_index": 1},
            ],
            "choices": ["Renamed one", "Renamed two"],
            "rationale": "Source-marked matches: stale",
        },
    )
    assert isinstance(updated.draft, MatchingQuestionDraft)
    assert updated.draft.rationale == "Source-marked matches: A -> Renamed one; B -> Renamed two."
    before = service.question("run-1", "matching-1")
    with pytest.raises(ValueError, match="prompt IDs"):
        service.update_question(
            "run-1",
            "matching-1",
            {
                "kind": "matching",
                "stem": "Rejected",
                "prompts": [{"id": "p9", "label": "A", "text": "Alpha", "correct_index": 0}],
                "choices": ["One", "Two"],
                "rationale": "Custom",
            },
        )
    assert service.question("run-1", "matching-1") == before


@pytest.mark.parametrize("change", ["prompt_label", "choice_text", "choice_order", "mapping"])
def test_matching_synthesized_rationale_regenerates_for_every_mapping_edit(
    tmp_path: Path, change: str
) -> None:
    service = _service(tmp_path)
    draft = _matching_draft()
    service.store("run-1", (draft,))
    prompts = [
        {"id": item.id, "label": item.label, "text": item.text, "correct_index": item.correct_index}
        for item in draft.prompts
    ]
    choices = list(draft.choices)
    if change == "prompt_label":
        prompts[0]["label"] = "Alpha"
    elif change == "choice_text":
        choices[0] = "Renamed term"
    elif change == "choice_order":
        choices.reverse()
    else:
        prompts[0]["correct_index"] = 0
    updated = service.update_question(
        "run-1",
        "matching-1",
        {
            "kind": "matching",
            "stem": draft.stem,
            "prompts": prompts,
            "choices": choices,
            "rationale": draft.rationale,
        },
    )
    assert updated.draft.rationale == matching_summary(updated.draft.prompts, updated.draft.choices)
    assert updated.draft.rationale != draft.rationale


def test_matching_custom_rationale_survives_mapping_edit_and_complete_edit_clears_only_owned_codes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    draft = replace(
        _matching_draft(),
        rationale="Reviewer-authored explanation.",
        diagnostics=(
            DraftDiagnostic(
                "missing-supplied-matching-answer", "missing", DiagnosticSeverity.BLOCKER
            ),
            DraftDiagnostic("source-warning", "uncertain text", DiagnosticSeverity.WARNING),
        ),
    )
    service.store("run-1", (draft,))
    updated = service.update_question(
        "run-1",
        "matching-1",
        {
            "kind": "matching",
            "stem": draft.stem,
            "prompts": [
                {
                    "id": item.id,
                    "label": item.label,
                    "text": item.text,
                    "correct_index": item.correct_index,
                }
                for item in draft.prompts
            ],
            "choices": list(draft.choices),
            "rationale": draft.rationale,
        },
    )
    assert updated.draft.rationale == "Reviewer-authored explanation."
    assert updated.draft.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert tuple(item.code for item in updated.draft.diagnostics) == ("source-warning",)


def test_matching_answer_refs_do_not_hide_group_image_candidates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    question_ref = QuestionSourceRef("source-1", "question-1", "page 1")
    answer_ref = QuestionSourceRef("source-1", "answer-1", "page 4")
    extracted = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match them",
        prompts=(
            ExtractedMatchingPrompt(original_identifier="A", text="Alpha"),
            ExtractedMatchingPrompt(original_identifier="B", text="Beta"),
        ),
        choices=("One", "Two"),
        source_segments=(SegmentCitation(source_id="source-1", segment_key="question-1"),),
        candidate_assets=(AssetCitation(source_id="source-1", asset_key="asset-1"),),
        confidence=1.0,
    )
    extraction = ExtractionResult((extracted,), (), ((question_ref,),), (), (), ())
    draft = replace(_matching_draft(), stem="Match them", source_refs=(question_ref, answer_ref))
    assert service._candidate_asset_keys("run-1", draft, extraction) == frozenset(
        {("source-1", "asset-1")}
    )


def test_matching_verification_is_rejected_without_server_error(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_matching_draft(),))
    with pytest.raises(
        ValueError, match="matching answers do not require generated-answer verification"
    ):
        service.verify_generated_answer("run-1", "matching-1")


def test_matching_diagnostic_acknowledgement_waits_for_complete_mapping(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (replace(_matching_draft(), prompts=(
        MatchingPromptDraft("p1", "A", "Alpha", None),
        MatchingPromptDraft("p2", "B", "Beta", 0),
    )),))
    service.repository.save_run_artifact(
        "run-1",
        "review:run-diagnostics",
        "a" * 64,
        json.dumps(
            [{"code": "unknown-matching-prompt-answer", "message": "unknown",
              "severity": "blocker", "overridable": True, "acknowledged": False}]
        ),
    )
    with pytest.raises(ValueError, match="matching answers are incomplete"):
        service.acknowledge_run_diagnostic("run-1", "unknown-matching-prompt-answer")
    service.update_question("run-1", "matching-1", {
        "kind": "matching", "stem": "Match each description with its term.",
        "prompts": [
            {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 1},
            {"id": "p2", "label": "B", "text": "Beta", "correct_index": 0},
        ],
        "choices": ["Term one", "Term two"],
        "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
    })
    service.acknowledge_run_diagnostic("run-1", "unknown-matching-prompt-answer")
    assert service.run_diagnostics("run-1")[0]["acknowledged"] is True


def _legacy_extraction_result() -> ExtractionResult:
    question_ref = QuestionSourceRef("source-1", "question-1", "page 1")
    answer_ref = QuestionSourceRef("source-1", "answer-1", "page 4")
    return ExtractionResult(
        questions=(
            ExtractedQuestion(
                original_identifier="1",
                stem="Which term is correct?",
                choices=("Term one", "Term two"),
                source_segments=(SegmentCitation(source_id="source-1", segment_key="question-1"),),
                confidence=0.9,
            ),
        ),
        answers=(
            ExtractedAnswer(
                original_identifier="1",
                correct_index=1,
                rationale="The source key selects Term two.",
                source_segments=(SegmentCitation(source_id="source-1", segment_key="answer-1"),),
            ),
        ),
        question_source_refs=((question_ref,),),
        answer_source_refs=((answer_ref,),),
        provider_metadata=(),
        diagnostics=(),
    )


def test_awaiting_review_can_read_a_legacy_extract_artifact_without_answer_refs(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))
    legacy = json.loads(_extraction_json(_legacy_extraction_result()))
    legacy.pop("answer_source_refs", None)
    service.repository.save_run_artifact("run-1", "extract", "a" * 64, json.dumps(legacy))

    assert service.candidates_by_question("run-1", service.review("run-1")) == {"q1": ()}


def _candidate_asset(tmp_path: Path) -> tuple[Path, ParsedDocument]:
    payload = BytesIO()
    Image.new("RGB", (2, 3), "red").save(payload, format="PNG")
    path = tmp_path / "candidate.png"
    path.write_bytes(payload.getvalue())
    asset = ParsedAsset(
        "asset-1",
        path,
        "image/png",
        sha256_file(path),
        DocumentLocator("page 1 image", page_number=1),
        2,
        3,
        "full-slide-render",
    )
    return path, ParsedDocument(
        "source",
        "a" * 64,
        "pdf",
        "test",
        "1",
        (
            ParsedSegment(
                "segment",
                SegmentKind.PARAGRAPH,
                "question source",
                DocumentLocator("page 1", page_number=1),
                (asset.key,),
            ),
        ),
        (asset,),
        (),
    )


def _image_review_service(tmp_path: Path) -> tuple[PracticeReviewService, Path]:
    service = _service(tmp_path)
    path, document = _candidate_asset(tmp_path)
    service.repository.save_run_artifact(
        "run-1",
        "parse:source",
        "b" * 64,
        _document_json(document),
    )
    service.set_image_service(StudioQuizImageService(service.repository, tmp_path / "quiz-media"))
    draft = _draft("q1", generated=False)
    service.store(
        "run-1",
        (replace(draft, image_ref=QuizImageRef("manual-image", "source", "page 1", "image")),),
    )
    return service, path


def test_generated_answer_blocks_until_same_question_is_verified(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = "run-1"
    service.store(run_id, (_draft("q1", generated=True), _draft("q2", generated=False)))

    assert service.blockers(run_id) == ("q1: AI-generated answer requires verification",)
    with pytest.raises(ValueError, match="requires verification"):
        service.to_native_quiz(run_id)
    service.verify_generated_answer(run_id, "q1")
    assert service.blockers(run_id) == ()


def test_overridable_run_diagnostic_is_stored_once_and_acknowledgement_persists(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False), _draft("q2", generated=False)))
    service.repository.save_run_artifact(
        "run-1",
        "review:run-diagnostics",
        "a" * 64,
        json.dumps(
            [
                {
                    "code": "incomplete-sequential-question-extraction",
                    "message": "Question count needs review",
                    "severity": "blocker",
                    "overridable": True,
                    "acknowledged": False,
                }
            ]
        ),
    )

    assert service.blockers("run-1") == ("Question count needs review",)
    assert len(service.run_diagnostics("run-1")) == 1
    with pytest.raises(ValueError, match="Question count needs review"):
        service.to_native_quiz("run-1")

    service.acknowledge_run_diagnostic("run-1", "incomplete-sequential-question-extraction")
    reloaded = PracticeReviewService(service.repository)

    assert reloaded.run_diagnostics("run-1")[0]["acknowledged"] is True
    assert reloaded.blockers("run-1") == ()
    assert len(reloaded.to_native_quiz("run-1").questions) == 2


def test_hard_run_diagnostic_cannot_be_acknowledged(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))
    service.repository.save_run_artifact(
        "run-1",
        "review:run-diagnostics",
        "a" * 64,
        json.dumps(
            [
                {
                    "code": "parser-blocker",
                    "message": "OCR is unavailable for slide 2",
                    "severity": "blocker",
                    "overridable": False,
                    "acknowledged": False,
                }
            ]
        ),
    )

    with pytest.raises(ValueError, match="cannot be acknowledged"):
        service.acknowledge_run_diagnostic("run-1", "parser-blocker")
    assert service.blockers("run-1") == ("OCR is unavailable for slide 2",)


def test_claimed_run_reserves_scope_against_reviewed_direct_import_publish(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))
    database = service.repository.database
    with database.session() as session:
        session.add(
            StudioRunModel(
                id="claimed-chat-run",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Imported practice",
                label_key="imported practice",
                prompt="Remote work",
                state="running",
                stage="chat",
            )
        )
    publisher = GenerationRepository(database, practice_review=service)

    with pytest.raises(
        ValueError,
        match="another active Studio run owns this publication scope",
    ):
        publisher.publish_reviewed_studio_quiz("run-1")

    with database.session() as session:
        reviewed = session.get(StudioRunModel, "run-1")
        assert reviewed is not None and reviewed.state == "awaiting_review"
        assert session.query(PublishedQuizModel).count() == 0


def test_missing_answer_is_not_mislabeled_as_ai_generated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = replace(
        _draft("q1", generated=False),
        correct_index=None,
        rationale=None,
        answer_provenance=None,
        verification_required=True,
    )
    service.store("run-1", (draft,))

    blockers = service.blockers("run-1")

    assert "q1: answer is missing" in blockers
    assert "q1: answer rationale is missing" in blockers
    assert "q1: AI-generated answer requires verification" not in blockers


@pytest.mark.parametrize(
    "content_kind",
    [QuizContentKind.EXAM_REVIEW, QuizContentKind.PRACTICE_QUESTIONS],
)
def test_manual_answer_save_clears_import_blockers_and_can_publish(
    tmp_path: Path,
    content_kind: QuizContentKind,
) -> None:
    service = _service(tmp_path)
    extracted = ExtractedQuestion(
        original_identifier="1",
        stem="What is correct?",
        choices=("A", "B"),
        supplied_correct_index=None,
        rationale=None,
        source_segments=(
            SegmentCitation(
                source_id="source",
                segment_key="question-1",
            ),
        ),
        candidate_assets=(),
        confidence=0.8,
    )
    draft = pair_supplied_answers((extracted,), ()).drafts[0]
    service.store("run-1", (draft,))
    with service.repository.database.session() as session:
        run = session.get(StudioRunModel, "run-1")
        assert run is not None
        run.content_kind = content_kind.value

    assert "question-1-1: answer is missing" in service.blockers("run-1")
    updated = service.update_question(
        "run-1",
        "question-1-1",
        {
            "choices": ["A", "B", "C"],
            "correct_index": 2,
            "rationale": "Choice C is correct.",
        },
    )

    assert updated.draft.correct_index == 2
    assert updated.draft.rationale == "Choice C is correct."
    assert updated.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert updated.verification_required is False
    assert updated.draft.diagnostics == ()
    assert service.question("run-1", "question-1-1") == updated
    assert service.blockers("run-1") == ()

    publisher = GenerationRepository(
        service.repository.database,
        practice_review=service,
    )
    published = publisher.publish_reviewed_studio_quiz("run-1")
    assert published.content_kind == content_kind.value


def test_resaving_legacy_manual_answer_clears_stale_conflict_blocker(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    legacy = replace(
        _draft("question-18-18", generated=False),
        original_identifier="18",
        correct_index=1,
        answer_provenance=AnswerProvenance.MANUALLY_CORRECTED,
        diagnostics=(
            DraftDiagnostic(
                "duplicate-supplied-answer",
                "conflicting supplied answers",
                DiagnosticSeverity.BLOCKER,
            ),
        ),
        verification_required=True,
        verified_at="2026-08-13T14:25:53+00:00",
    )
    service.store("run-1", (legacy,))

    assert service.blockers("run-1") == ("question-18-18: conflicting supplied answers",)
    updated = service.update_question(
        "run-1",
        "question-18-18",
        {
            "choices": list(legacy.choices),
            "correct_index": 1,
            "rationale": legacy.rationale,
        },
    )

    assert updated.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert updated.draft.diagnostics == ()
    assert updated.verification_required is False
    assert updated.verified_at is None
    assert service.blockers("run-1") == ()


def test_resolving_import_diagnostic_does_not_bypass_ai_verification(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    draft = replace(
        _draft("q1", generated=True),
        diagnostics=(
            DraftDiagnostic(
                "missing-supplied-answer",
                "supplied answer is missing",
                DiagnosticSeverity.BLOCKER,
            ),
        ),
    )
    service.store("run-1", (draft,))

    updated = service.update_question("run-1", "q1", {"correct_index": 1})

    assert updated.draft.diagnostics == ()
    assert updated.verification_required is True
    assert service.blockers("run-1") == ("q1: AI-generated answer requires verification",)


def test_structured_issues_keep_warnings_non_blocking_and_deduplicate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = replace(
        _draft("q1", generated=False),
        diagnostics=(
            DraftDiagnostic(
                "uncertain_source",
                "Source wording is uncertain",
                DiagnosticSeverity.WARNING,
            ),
            DraftDiagnostic(
                "uncertain_source",
                "Source wording is uncertain",
                DiagnosticSeverity.WARNING,
            ),
            DraftDiagnostic(
                "missing_context",
                "Source context is incomplete",
                DiagnosticSeverity.BLOCKER,
            ),
        ),
    )
    service.store("run-1", (draft,))

    issues = service.issues("run-1")

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("uncertain_source", DiagnosticSeverity.WARNING),
        ("missing_context", DiagnosticSeverity.BLOCKER),
    ]
    assert issues[0].question_id == "q1"
    assert issues[0].display_label == "q1"
    assert service.blockers("run-1") == ("q1: Source context is incomplete",)


def test_editing_answer_clears_verification_and_marks_manual(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = "run-1"
    service.store(run_id, (_draft("q1", generated=True),))
    service.verify_generated_answer(run_id, "q1")

    updated = service.update_question(run_id, "q1", {"correct_index": 1})

    assert updated.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert updated.verification_required is True
    assert updated.verified_at is None


def test_rationale_edit_reopens_generated_answer_verification(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    service.verify_generated_answer("run-1", "q1")

    updated = service.update_question("run-1", "q1", {"rationale": "Updated rationale."})

    assert updated.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert updated.verification_required is True
    assert updated.verified_at is None
    assert service.blockers("run-1") == ("q1: AI-generated answer requires verification",)
    service.verify_generated_answer("run-1", "q1")
    assert service.blockers("run-1") == ()


def test_non_answer_metadata_edit_preserves_existing_verification(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    verified = service.verify_generated_answer("run-1", "q1")

    updated = service.update_question("run-1", "q1", {"topic": "Neuro"})

    assert updated.answer_provenance is AnswerProvenance.GENERATED_BY_AI
    assert updated.verified_at == verified.verified_at


def test_partial_metadata_edit_preserves_whitespace_and_verification(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = replace(
        _draft("question-1", generated=True),
        choices=(" A ", " B "),
        rationale=" Because. ",
    )
    service.store("run-1", (draft,))
    verified = service.verify_generated_answer("run-1", "question-1")

    updated = service.update_question("run-1", "question-1", {"topic": "Neuro"})

    assert updated.draft.choices == (" A ", " B ")
    assert updated.draft.rationale == " Because. "
    assert updated.answer_provenance is AnswerProvenance.GENERATED_BY_AI
    assert updated.verified_at == verified.verified_at


def test_metadata_edit_allows_incomplete_draft_and_invalid_answer_edit_is_atomic(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    incomplete = replace(_draft("q1", generated=True), correct_index=None, rationale=None)
    service.store("run-1", (incomplete,))

    updated = service.update_question("run-1", "q1", {"area": "Neuro"})
    assert updated.area == "Neuro"
    assert updated.draft.correct_index is None
    assert updated.draft.rationale is None
    before = service.question("run-1", "q1")

    with pytest.raises(ValueError, match="choices"):
        service.update_question("run-1", "q1", {"choices": ["Only choice"]})
    assert service.question("run-1", "q1") == before


def test_choice_edit_rejects_an_invalid_existing_correct_index_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = replace(_draft("q1", generated=False), choices=("A", "B", "C"), correct_index=2)
    service.store("run-1", (draft,))

    with pytest.raises(ValueError, match="correct index"):
        service.update_question("run-1", "q1", {"choices": ["A", "B"]})
    assert service.question("run-1", "q1").draft == draft


def test_native_quiz_uses_public_question_ids_not_import_identifiers(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store(
        "run-1",
        (_draft("question-1", generated=False), _draft("question-2", generated=False)),
    )

    quiz = service.to_native_quiz("run-1")

    assert tuple(question.id for question in quiz.questions) == ("q1", "q2")
    assert tuple(question.choices[0].id for question in quiz.questions) == ("c1", "c1")


def test_rationale_edit_marks_supplied_answer_manual_without_new_verification(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))

    updated = service.update_question("run-1", "q1", {"rationale": "Clarified rationale."})

    assert updated.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert updated.verification_required is False
    assert updated.verified_at is None


def test_verifying_one_answer_does_not_verify_another_and_later_edit_reopens_it(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True), _draft("q2", generated=True)))
    service.verify_generated_answer("run-1", "q1")

    assert service.question("run-1", "q1").verified_at is not None
    assert service.question("run-1", "q2").verified_at is None
    service.update_question("run-1", "q1", {"choices": ["A", "C"]})
    assert service.question("run-1", "q1").verified_at is None


def test_direct_publication_uses_current_review_state_in_the_same_gate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    service.verify_generated_answer("run-1", "q1")
    assert service.blockers("run-1") == ()

    # A stale client may have observed the empty blocker list; the server sees
    # this later edit and must reject publication without creating a quiz row.
    service.update_question("run-1", "q1", {"correct_index": 1})
    publisher = GenerationRepository(service.repository.database, practice_review=service)
    with pytest.raises(ValueError, match="requires verification"):
        publisher.publish_reviewed_studio_quiz("run-1")
    assert publisher.published_quizzes(frozenset(QuizContentKind)) == ()
    assert service.repository.get_run("run-1").published_token is None


def test_rationale_edit_blocks_stale_publication_without_mutating_run(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    service.verify_generated_answer("run-1", "q1")
    assert service.blockers("run-1") == ()

    # The stale client observed no blockers before a reviewer corrected only
    # the rationale. Publication must re-read the artifact in its transaction.
    service.update_question("run-1", "q1", {"rationale": "Corrected rationale."})
    publisher = GenerationRepository(service.repository.database, practice_review=service)
    with pytest.raises(ValueError, match="requires verification"):
        publisher.publish_reviewed_studio_quiz("run-1")

    assert publisher.published_quizzes(frozenset(QuizContentKind)) == ()
    run = service.repository.get_run("run-1")
    assert run.published_token is None
    assert run.state.value == "awaiting_review"


def test_blocker_free_direct_review_publishes_without_private_review_fields(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    service.verify_generated_answer("run-1", "q1")
    publisher = GenerationRepository(service.repository.database, practice_review=service)

    published = publisher.publish_reviewed_studio_quiz("run-1")

    assert publisher.published_quiz(published.token) is not None
    with service.repository.database.session() as session:
        payload = session.get(StudioRunModel, "run-1")
        assert payload is not None
        assert payload.published_token == published.token


def test_import_candidates_hide_paths_and_selecting_one_publishes_media(tmp_path: Path) -> None:
    service, _ = _image_review_service(tmp_path)

    candidates = service.candidates("run-1", "q1")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.exact_match is True
    assert candidate.origin == "full-slide-render"
    assert "path" not in asdict(candidate)
    updated = service.select_image_candidate("run-1", "q1", candidate.candidate_id)
    assert updated.chosen_image is not None
    assert updated.chosen_image.key.startswith("img-")
    assert updated.chosen_image.source_title == "Imported question"

    publisher = GenerationRepository(service.repository.database, practice_review=service)
    published = publisher.publish_reviewed_studio_quiz("run-1")
    media = publisher.published_quiz_media(published.token)
    assert len(media) == 1
    assert media[0].image_key == updated.chosen_image.key
    assert media[0].path.is_file()


def test_candidate_batch_reuses_parse_and_extract_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _image_review_service(tmp_path)
    first = service.question("run-1", "q1").draft
    second = replace(first, question_id="q2", original_identifier="q2")
    service.store("run-1", (first, second))
    questions = service.review("run-1")
    calls: list[str] = []
    run_artifact = service.repository.run_artifact

    def recorded(run_id: str, key: str):
        calls.append(key)
        return run_artifact(run_id, key)

    monkeypatch.setattr(service.repository, "run_artifact", recorded)

    candidates = service.candidates_by_question("run-1", questions)

    assert set(candidates) == {"q1", "q2"}
    assert calls.count("parse:source") == 1
    assert calls.count("extract") == 1


def test_import_candidate_selection_rejects_changed_source_file(tmp_path: Path) -> None:
    service, path = _image_review_service(tmp_path)
    candidate = service.candidates("run-1", "q1")[0]
    path.write_bytes(b"not the parsed image")

    with pytest.raises(ValueError, match="could not be verified"):
        service.select_image_candidate("run-1", "q1", candidate.candidate_id)
    assert service.question("run-1", "q1").chosen_image is None


def test_image_requirement_can_be_waived_and_restored(tmp_path: Path) -> None:
    service, _ = _image_review_service(tmp_path)
    candidate = service.candidates("run-1", "q1")[0]
    service.select_image_candidate("run-1", "q1", candidate.candidate_id)

    waived = service.set_image_not_needed("run-1", "q1", True)

    assert waived.chosen_image is None
    assert waived.selected_candidate_id is None
    assert waived.image_not_needed is True
    assert service.blockers("run-1") == ()
    assert service.to_native_quiz("run-1").questions[0].image_ref is None

    restored = service.set_image_not_needed("run-1", "q1", False)

    assert restored.image_not_needed is False
    assert service.blockers("run-1") == ("q1: required image is unresolved",)


def test_extraction_candidate_citation_creates_a_stable_image_requirement(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _, document = _candidate_asset(tmp_path)
    draft = _draft("q1", generated=False)
    extracted = ExtractedQuestion(
        original_identifier="q1",
        stem=draft.stem,
        choices=draft.choices,
        supplied_correct_index=0,
        rationale=draft.rationale,
        source_segments=(SegmentCitation(source_id="source", segment_key="segment"),),
        candidate_assets=(AssetCitation(source_id="source", asset_key="asset-1"),),
        confidence=0.8,
    )
    service.repository.save_run_artifact(
        "run-1", "parse:source", "b" * 64, _document_json(document)
    )
    service.repository.save_run_artifact(
        "run-1",
        "extract",
        "c" * 64,
        _extraction_json(ExtractionResult((extracted,), (), (draft.source_refs,), (), (), ())),
    )
    service.repository.save_run_artifact("run-1", "normalized", "d" * 64, _drafts_json((draft,)))

    reviewed = service.review("run-1")[0]

    assert reviewed.draft.image_ref is not None
    assert reviewed.draft.image_ref.key.startswith("img-")
    assert len(reviewed.draft.image_ref.key) <= 64
    assert service.blockers("run-1") == ("q1: required image is unresolved",)


def test_review_does_not_auto_select_a_source_screenshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _, document = _candidate_asset(tmp_path)
    draft = _draft("q1", generated=False)
    extracted = ExtractedQuestion(
        original_identifier="q1",
        stem=draft.stem,
        choices=draft.choices,
        supplied_correct_index=0,
        rationale=draft.rationale,
        source_segments=(SegmentCitation(source_id="source", segment_key="segment"),),
        candidate_assets=(AssetCitation(source_id="source", asset_key="asset-1"),),
        confidence=0.8,
    )
    service.repository.save_run_artifact(
        "run-1", "parse:source", "b" * 64, _document_json(document)
    )
    service.repository.save_run_artifact(
        "run-1",
        "extract",
        "c" * 64,
        _extraction_json(ExtractionResult((extracted,), (), (draft.source_refs,), (), (), ())),
    )
    service.repository.save_run_artifact("run-1", "normalized", "d" * 64, _drafts_json((draft,)))
    service.set_image_service(StudioQuizImageService(service.repository, tmp_path / "quiz-media"))

    reviewed = service.review("run-1")[0]

    assert reviewed.chosen_image is None
    assert service.blockers("run-1") == ("q1: required image is unresolved",)


@pytest.mark.parametrize(
    "update",
    [
        {"choices": ["A"]},
        {"choices": ["A", "a"]},
        {"correct_index": 4},
        {"stem": " "},
    ],
)
def test_invalid_question_edits_are_rejected(tmp_path: Path, update: dict[str, object]) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))

    with pytest.raises(ValueError):
        service.update_question("run-1", "q1", update)
