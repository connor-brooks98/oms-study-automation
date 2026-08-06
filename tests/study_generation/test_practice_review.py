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
from oms_hub.models import StudioRunModel
from oms_hub.study_generation.domain import QuizImageRef
from oms_hub.study_generation.practice_contracts import (
    AssetCitation,
    ExtractedQuestion,
    SegmentCitation,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    QuestionDraft,
    QuestionSourceRef,
    QuizContentKind,
)
from oms_hub.study_generation.practice_extraction import ExtractionResult
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
        (
            replace(draft, image_ref=QuizImageRef("manual-image", "source", "page 1", "image")),
        ),
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


def test_import_candidate_selection_rejects_changed_source_file(tmp_path: Path) -> None:
    service, path = _image_review_service(tmp_path)
    candidate = service.candidates("run-1", "q1")[0]
    path.write_bytes(b"not the parsed image")

    with pytest.raises(ValueError, match="could not be verified"):
        service.select_image_candidate("run-1", "q1", candidate.candidate_id)
    assert service.question("run-1", "q1").chosen_image is None


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
        _extraction_json(ExtractionResult((extracted,), (), (draft.source_refs,), (), ())),
    )
    service.repository.save_run_artifact("run-1", "normalized", "d" * 64, _drafts_json((draft,)))

    reviewed = service.review("run-1")[0]

    assert reviewed.draft.image_ref is not None
    assert reviewed.draft.image_ref.key.startswith("img-")
    assert len(reviewed.draft.image_ref.key) <= 64
    assert service.blockers("run-1") == ("q1: required image is unresolved",)


def test_review_auto_selects_only_a_unique_exact_candidate(tmp_path: Path) -> None:
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
        _extraction_json(ExtractionResult((extracted,), (), (draft.source_refs,), (), ())),
    )
    service.repository.save_run_artifact("run-1", "normalized", "d" * 64, _drafts_json((draft,)))
    service.set_image_service(StudioQuizImageService(service.repository, tmp_path / "quiz-media"))

    reviewed = service.review("run-1")[0]

    assert reviewed.chosen_image is not None
    assert service.blockers("run-1") == ()


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
