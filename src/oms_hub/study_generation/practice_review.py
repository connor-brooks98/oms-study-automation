"""Authoritative, restart-safe review state for imported practice questions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from oms_hub.models import StudioRunArtifactModel
from oms_hub.study_generation.domain import NativeQuiz, QuizChoice, QuizImageRef, QuizQuestion
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    QuestionDraft,
)
from oms_hub.study_generation.quiz_import_worker import _drafts_from_json, _drafts_json
from oms_hub.study_generation.studio_repository import StudioRepository

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


class PracticeReviewService:
    """One server-owned review model used by API, preview, and publication gates."""

    def __init__(self, repository: StudioRepository) -> None:
        self.repository = repository

    def store(self, run_id: str, drafts: tuple[QuestionDraft, ...]) -> None:
        self._save(run_id, tuple(ReviewQuestion(draft) for draft in drafts))

    def review(self, run_id: str) -> tuple[ReviewQuestion, ...]:
        stored = self.repository.run_artifact(run_id, _ARTIFACT_KEY)
        if stored is not None:
            return _questions_from_json(stored.payload_json)
        normalized = self.repository.run_artifact(run_id, "normalized")
        if normalized is None:
            raise KeyError(run_id)
        questions = tuple(ReviewQuestion(draft) for draft in _drafts_from_json(normalized.payload_json))  # noqa: E501
        self._save(run_id, questions)
        return questions

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
            "chosen_image",
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
        answer_changed = choices != draft.choices or correct_index != draft.correct_index
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
            _image_ref(values.get("chosen_image", current.chosen_image)),
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
