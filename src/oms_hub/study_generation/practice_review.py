"""Authoritative, restart-safe review state for imported practice questions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from oms_hub.files.atomic import sha256_file
from oms_hub.models import StudioRunArtifactModel
from oms_hub.study_generation.domain import NativeQuiz, QuizChoice, QuizImageRef, QuizQuestion
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    QuestionDraft,
)
from oms_hub.study_generation.quiz_import_worker import (
    _document_from_json,
    _drafts_from_json,
    _drafts_json,
    _extraction_from_json,
)
from oms_hub.study_generation.studio_repository import StudioRepository

if TYPE_CHECKING:
    from oms_hub.study_generation.quiz_images import StudioQuizImageService

_ARTIFACT_KEY = "review:questions"


@dataclass(frozen=True, slots=True)
class ReviewQuestion:
    draft: QuestionDraft
    topic: str | None = None
    area: str | None = None
    learning_objective: str | None = None
    chosen_image: QuizImageRef | None = None

    @property
    def answer_provenance(self) -> AnswerProvenance | None:
        return self.draft.answer_provenance

    @property
    def verification_required(self) -> bool:
        return self.draft.verification_required

    @property
    def verified_at(self) -> str | None:
        return self.draft.verified_at


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    candidate_id: str
    question_id: str
    source_id: str
    source_title: str
    asset_key: str
    locator: str
    origin: str
    media_type: str
    width: int | None
    height: int | None
    score: int
    exact_match: bool


@dataclass(frozen=True, slots=True)
class _ImageCandidateBinding:
    """Server-only resolution metadata; this type is never serialized."""

    candidate: ImageCandidate
    path: Path
    sha256: str


class PracticeReviewService:
    """One server-owned review model used by API, preview, and publication gates."""

    def __init__(
        self,
        repository: StudioRepository,
        image_service: StudioQuizImageService | None = None,
    ) -> None:
        self.repository = repository
        self.image_service = image_service

    def set_image_service(self, image_service: StudioQuizImageService) -> None:
        self.image_service = image_service

    def candidates(self, run_id: str, question_id: str) -> tuple[ImageCandidate, ...]:
        question = self.question(run_id, question_id)
        return tuple(item.candidate for item in self._candidate_bindings(run_id, question))

    def _candidate_bindings(
        self, run_id: str, question: ReviewQuestion
    ) -> tuple[_ImageCandidateBinding, ...]:
        """Resolve media solely from immutable ``parse:{source_id}`` artifacts."""
        references = question.draft.source_refs
        explicit = self._candidate_asset_keys(run_id, question.draft)
        bindings: dict[tuple[str, str], _ImageCandidateBinding] = {}
        for reference in references:
            artifact = self.repository.run_artifact(run_id, f"parse:{reference.source_id}")
            if artifact is None:
                continue
            document = _document_from_json(artifact.payload_json)
            source = self.repository.get(reference.source_id)
            source_title = source.title if source is not None else reference.source_id
            exact_locator = _locator_key(reference.locator)
            adjacent_asset_keys = _adjacent_asset_keys(document, reference.segment_key)
            for asset in document.assets:
                if asset.path is None or not asset.path.is_file():
                    continue
                asset_locator = _locator_key(asset.locator)
                exact = bool(exact_locator and asset_locator and exact_locator == asset_locator)
                explicit_citation = (reference.source_id, asset.key) in explicit
                adjacent = asset.key in adjacent_asset_keys
                if not (exact or explicit_citation or adjacent):
                    continue
                score = 3 if exact else 2 if explicit_citation else 1
                candidate_id = "candidate-" + hashlib.sha256(
                    f"{question.draft.question_id}:{reference.source_id}:{asset.key}".encode()
                ).hexdigest()[:32]
                candidate = ImageCandidate(
                    candidate_id,
                    question.draft.question_id,
                    reference.source_id,
                    source_title,
                    asset.key,
                    asset.locator.label,
                    _origin(asset.origin),
                    asset.media_type,
                    asset.width,
                    asset.height,
                    score,
                    exact,
                )
                binding = _ImageCandidateBinding(candidate, asset.path, asset.sha256)
                prior = bindings.get((reference.source_id, asset.key))
                if prior is None or binding.candidate.score > prior.candidate.score:
                    bindings[(reference.source_id, asset.key)] = binding
        return tuple(
            sorted(
                bindings.values(),
                key=lambda item: (
                    -item.candidate.score,
                    item.candidate.source_title.casefold(),
                    item.candidate.locator.casefold(),
                    item.candidate.asset_key,
                ),
            )
        )

    def select_image_candidate(
        self, run_id: str, question_id: str, candidate_id: str
    ) -> ReviewQuestion:
        if self.image_service is None:
            raise ValueError("imported image review is not configured")
        current = self.question(run_id, question_id)
        binding = next(
            (
                item
                for item in self._candidate_bindings(run_id, current)
                if item.candidate.candidate_id == candidate_id
            ),
            None,
        )
        if binding is None:
            raise ValueError("image candidate is not available for this question")
        candidate = binding.candidate
        image_key = (
            current.draft.image_ref.key
            if current.draft.image_ref is not None
            else _image_key(question_id)
        )
        if len(image_key) > 64:
            raise ValueError("image requirement key is invalid")
        self.image_service.copy_import_candidate(
            run_id,
            image_key,
            candidate.source_title,
            candidate.locator,
            _candidate_description(candidate),
            binding.path,
            binding.sha256,
            candidate.asset_key,
        )
        chosen = QuizImageRef(
            image_key,
            candidate.source_title,
            candidate.locator,
            _candidate_description(candidate),
        )
        updated = replace(
            current,
            chosen_image=chosen,
            draft=replace(current.draft, image_ref=chosen),
        )
        self._save(
            run_id,
            tuple(
                updated if item.draft.question_id == question_id else item
                for item in self.review(run_id)
            ),
        )
        return updated

    def selected_candidate_id(self, run_id: str, question_id: str) -> str | None:
        question = self.question(run_id, question_id)
        if question.chosen_image is None:
            return None
        for candidate in self.candidates(run_id, question_id):
            if (
                candidate.source_title == question.chosen_image.source_title
                and candidate.locator == question.chosen_image.locator
                and question.chosen_image.description == _candidate_description(candidate)
            ):
                return candidate.candidate_id
        return None

    def candidate_preview(
        self, run_id: str, question_id: str, candidate_id: str
    ) -> tuple[Path, str]:
        """Return a verified candidate file for a question-scoped preview only."""
        current = self.question(run_id, question_id)
        binding = next(
            (
                item
                for item in self._candidate_bindings(run_id, current)
                if item.candidate.candidate_id == candidate_id
            ),
            None,
        )
        if binding is None:
            raise KeyError(candidate_id)
        try:
            if not binding.path.is_file() or sha256_file(binding.path) != binding.sha256:
                raise KeyError(candidate_id)
        except OSError as error:
            raise KeyError(candidate_id) from error
        return binding.path, binding.candidate.media_type

    def store(self, run_id: str, drafts: tuple[QuestionDraft, ...]) -> None:
        self._save(run_id, tuple(ReviewQuestion(draft) for draft in drafts))

    def review(self, run_id: str) -> tuple[ReviewQuestion, ...]:
        stored = self.repository.run_artifact(run_id, _ARTIFACT_KEY)
        if stored is not None:
            return _questions_from_json(stored.payload_json)
        normalized = self.repository.run_artifact(run_id, "normalized")
        if normalized is None:
            raise KeyError(run_id)
        questions = self._initialize_image_requirements(
            run_id,
            _drafts_from_json(normalized.payload_json),
        )
        self._save(run_id, questions)
        self._auto_select_unique_exact_candidate(run_id, questions)
        stored = self.repository.run_artifact(run_id, _ARTIFACT_KEY)
        assert stored is not None
        return _questions_from_json(stored.payload_json)

    def _initialize_image_requirements(
        self, run_id: str, drafts: tuple[QuestionDraft, ...]
    ) -> tuple[ReviewQuestion, ...]:
        """Turn extraction-level image citations into stable review requirements."""
        initialized: list[ReviewQuestion] = []
        for draft in drafts:
            explicit = self._candidate_asset_keys(run_id, draft)
            if draft.image_ref is not None or not explicit:
                initialized.append(ReviewQuestion(draft))
                continue
            source_id, asset_key = sorted(explicit)[0]
            source = self.repository.get(source_id)
            source_ref = next(
                (item for item in draft.source_refs if item.source_id == source_id),
                None,
            )
            initialized.append(
                ReviewQuestion(
                    replace(
                        draft,
                        image_ref=QuizImageRef(
                            _image_key(draft.question_id),
                            source.title if source is not None else source_id,
                            source_ref.locator if source_ref is not None else asset_key,
                            "Imported candidate image",
                        ),
                    )
                )
            )
        return tuple(initialized)

    def _auto_select_unique_exact_candidate(
        self, run_id: str, questions: tuple[ReviewQuestion, ...]
    ) -> None:
        """A unique exact source/page match is the only safe automatic selection."""
        if self.image_service is None:
            return
        for question in questions:
            if question.draft.image_ref is None or question.chosen_image is not None:
                continue
            exact = tuple(
                item
                for item in self._candidate_bindings(run_id, question)
                if item.candidate.exact_match
            )
            if len(exact) == 1:
                self.select_image_candidate(
                    run_id,
                    question.draft.question_id,
                    exact[0].candidate.candidate_id,
                )

    def _candidate_asset_keys(
        self, run_id: str, draft: QuestionDraft
    ) -> frozenset[tuple[str, str]]:
        artifact = self.repository.run_artifact(run_id, "extract")
        if artifact is None:
            return frozenset()
        extraction = _extraction_from_json(artifact.payload_json)
        matches = [
            index
            for index, question in enumerate(extraction.questions)
            if extraction.question_source_refs[index] == draft.source_refs
            and (
                question.original_identifier == draft.original_identifier
                or question.stem == draft.stem
            )
        ]
        if len(matches) != 1:
            return frozenset()
        return frozenset(
            (citation.source_id, citation.asset_key)
            for citation in extraction.questions[matches[0]].candidate_assets
        )

    def question(self, run_id: str, question_id: str) -> ReviewQuestion:
        return self._find(self.review(run_id), question_id)

    def update_question(
        self, run_id: str, question_id: str, values: dict[str, object]
    ) -> ReviewQuestion:
        current = self.question(run_id, question_id)
        draft = current.draft
        allowed = {
            "stem",
            "choices",
            "correct_index",
            "rationale",
            "topic",
            "area",
            "learning_objective",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("question edit contains unsupported fields")
        stem = _required_text(values.get("stem", draft.stem), "stem")
        choices = _choices(values.get("choices", draft.choices))
        correct_index = values.get("correct_index", draft.correct_index)
        if not isinstance(correct_index, int) or isinstance(correct_index, bool):
            raise ValueError("correct index is invalid")
        if not 0 <= correct_index < len(choices):
            raise ValueError("correct index is outside the available choices")
        rationale = _required_text(values.get("rationale", draft.rationale), "rationale")
        answer_changed = (
            choices != draft.choices
            or correct_index != draft.correct_index
            or rationale != draft.rationale
        )
        requires_verification = (
            draft.verification_required
            or draft.answer_provenance is AnswerProvenance.GENERATED_BY_AI
        )
        updated_draft = replace(
            draft,
            stem=stem,
            choices=choices,
            correct_index=correct_index,
            rationale=rationale,
            answer_provenance=(AnswerProvenance.MANUALLY_CORRECTED if answer_changed else draft.answer_provenance),  # noqa: E501
            verification_required=(requires_verification if answer_changed else draft.verification_required),  # noqa: E501
            verified_at=(None if answer_changed else draft.verified_at),
        )
        updated = ReviewQuestion(
            updated_draft,
            _optional_text(values.get("topic", current.topic)),
            _optional_text(values.get("area", current.area)),
            _optional_text(values.get("learning_objective", current.learning_objective)),
            current.chosen_image,
        )
        questions = tuple(
            updated if item.draft.question_id == question_id else item for item in self.review(run_id)  # noqa: E501
        )
        self._save(run_id, questions)
        return updated

    def verify_generated_answer(self, run_id: str, question_id: str) -> ReviewQuestion:
        current = self.question(run_id, question_id)
        draft = current.draft
        if draft.correct_index is None or not draft.rationale or not draft.rationale.strip():
            raise ValueError("answer is incomplete")
        if draft.answer_provenance not in {
            AnswerProvenance.GENERATED_BY_AI,
            AnswerProvenance.MANUALLY_CORRECTED,
        }:
            raise ValueError("answer does not require generated-answer verification")
        updated = replace(
            current,
            draft=replace(draft, verification_required=True, verified_at=datetime.now(UTC).isoformat()),  # noqa: E501
        )
        self._save(
            run_id,
            tuple(updated if item.draft.question_id == question_id else item for item in self.review(run_id)),  # noqa: E501
        )
        return updated

    def blockers(self, run_id: str) -> tuple[str, ...]:
        return _blockers(self.review(run_id))

    def to_native_quiz_in_session(
        self, session: Session, run_id: str, *, title: str
    ) -> NativeQuiz:
        artifact = session.scalar(
            select(StudioRunArtifactModel).where(
                StudioRunArtifactModel.run_id == run_id,
                StudioRunArtifactModel.artifact_key == _ARTIFACT_KEY,
            )
        )
        if artifact is None:
            raise ValueError("imported question review is missing")
        return _native_quiz(_questions_from_json(artifact.payload_json), title)

    def to_native_quiz(self, run_id: str, *, title: str | None = None) -> NativeQuiz:
        return _native_quiz(self.review(run_id), title or "Imported practice questions")

    def _save(self, run_id: str, questions: tuple[ReviewQuestion, ...]) -> None:
        payload = _questions_json(questions)
        self.repository.save_run_artifact(
            run_id,
            _ARTIFACT_KEY,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            payload,
        )
        self.repository.save_question_reviews(run_id, tuple(item.draft for item in questions))

    @staticmethod
    def _find(questions: tuple[ReviewQuestion, ...], question_id: str) -> ReviewQuestion:
        for question in questions:
            if question.draft.question_id == question_id:
                return question
        raise KeyError(question_id)


def _blockers(questions: tuple[ReviewQuestion, ...]) -> tuple[str, ...]:
    blockers: list[str] = []
    for question in questions:
        draft = question.draft
        prefix = f"{draft.question_id}: "
        if draft.correct_index is None:
            blockers.append(prefix + "answer is missing")
        elif not 0 <= draft.correct_index < len(draft.choices):
            blockers.append(prefix + "correct answer is outside the available choices")
        if len(draft.choices) < 2 or len(draft.choices) > 8 or len(
            {choice.casefold() for choice in draft.choices}
        ) != len(draft.choices):
            blockers.append(prefix + "choices are invalid")
        for diagnostic in draft.diagnostics:
            if diagnostic.severity is DiagnosticSeverity.BLOCKER:
                blockers.append(prefix + diagnostic.message)
        if draft.verification_required and not draft.verified_at:
            blockers.append(prefix + "AI-generated answer requires verification")
        if draft.image_ref is not None and question.chosen_image is None:
            blockers.append(prefix + "required image is unresolved")
    return tuple(dict.fromkeys(blockers))


def _native_quiz(questions: tuple[ReviewQuestion, ...], title: str) -> NativeQuiz:
    blockers = _blockers(questions)
    if blockers:
        raise ValueError("; ".join(blockers))
    return NativeQuiz(
        title,
        tuple(
            QuizQuestion(
                item.draft.question_id,
                item.draft.stem,
                tuple(QuizChoice(f"c{number}", choice) for number, choice in enumerate(item.draft.choices, 1)),  # noqa: E501
                f"c{(item.draft.correct_index or 0) + 1}",
                item.draft.rationale or "",
                item.chosen_image,
                item.area,
                item.learning_objective,
                item.topic,
            )
            for item in questions
        ),
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("metadata must be text")
    return value.strip() or None


def _choices(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("choices must be a list")
    choices = tuple(_required_text(item, "choice") for item in value)
    if not 2 <= len(choices) <= 8 or len({item.casefold() for item in choices}) != len(choices):
        raise ValueError("choices must be two to eight distinct values")
    return choices


def _image_ref(value: object) -> QuizImageRef | None:
    if value is None or isinstance(value, QuizImageRef):
        return value
    if not isinstance(value, dict):
        raise ValueError("chosen image is invalid")
    try:
        return QuizImageRef(
            str(value["key"]),
            str(value["source_title"]),
            str(value["locator"]),
            str(value["description"]),
        )
    except KeyError as error:
        raise ValueError("chosen image is invalid") from error


def _locator_key(value: object) -> tuple[str, int] | None:
    """Normalize page/slide references without treating arbitrary labels as equal."""
    page_number = getattr(value, "page_number", None)
    slide_number = getattr(value, "slide_number", None)
    if isinstance(page_number, int):
        return ("page", page_number)
    if isinstance(slide_number, int):
        return ("slide", slide_number)
    if not isinstance(value, str):
        return None
    match = re.search(r"\b(page|p|slide|s)\s*(\d+)\b", value.casefold())
    if match is None:
        return None
    kind = "slide" if match.group(1) in {"slide", "s"} else "page"
    return (kind, int(match.group(2)))


def _adjacent_asset_keys(document: object, segment_key: str) -> frozenset[str]:
    segments = tuple(getattr(document, "segments", ()))
    segment = next((item for item in segments if item.key == segment_key), None)
    if segment is None:
        return frozenset()
    keys = set(segment.asset_keys)
    neighbor_keys = {segment.previous_key, segment.next_key}
    for neighbor in segments:
        if neighbor.key in neighbor_keys:
            keys.update(neighbor.asset_keys)
    return frozenset(keys)


def _origin(value: str | None) -> str:
    """Keep parser provenance labels intact while making omitted provenance explicit."""
    return value or "embedded"


def _image_key(question_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", question_id.casefold()).strip("-") or "question"
    digest = hashlib.sha256(question_id.encode("utf-8")).hexdigest()[:12]
    return f"import-{slug[:40]}-{digest}"[:64]


def _candidate_description(candidate: ImageCandidate) -> str:
    return f"Imported image from {candidate.source_title} ({candidate.asset_key})"


def _questions_json(questions: tuple[ReviewQuestion, ...]) -> str:
    return json.dumps(
        [
            {
                "draft": json.loads(_drafts_json((question.draft,)))[0],
                "topic": question.topic,
                "area": question.area,
                "learning_objective": question.learning_objective,
                "chosen_image": (
                    {
                        "key": question.chosen_image.key,
                        "source_title": question.chosen_image.source_title,
                        "locator": question.chosen_image.locator,
                        "description": question.chosen_image.description,
                    }
                    if question.chosen_image
                    else None
                ),
            }
            for question in questions
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


def _questions_from_json(payload: str) -> tuple[ReviewQuestion, ...]:
    return tuple(
        ReviewQuestion(
            _drafts_from_json(json.dumps([item["draft"]]))[0],
            item["topic"],
            item["area"],
            item["learning_objective"],
            _image_ref(item["chosen_image"]),
        )
        for item in json.loads(payload)
    )
