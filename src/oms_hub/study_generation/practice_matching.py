"""Deterministic supplied-answer pairing for imported practice questions."""

import re
from collections import Counter, defaultdict

from oms_hub.study_generation.practice_contracts import ExtractedAnswer, ExtractedQuestion
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    QuestionDraft,
    QuestionSourceRef,
)

_NUMBERED_IDENTIFIER = re.compile(r"^(?:question|q)?\s*0*(\d+)\s*[.:)]?$", re.IGNORECASE)


def normalize_identifier(value: str | None) -> str | None:
    """Return a stable numeric label for ordinary question-number variants."""

    if value is None:
        return None
    match = _NUMBERED_IDENTIFIER.fullmatch(value.strip())
    if match is None:
        return None
    return str(int(match.group(1)))


def pair_supplied_answers(
    questions: tuple[ExtractedQuestion, ...],
    answers: tuple[ExtractedAnswer, ...],
    *,
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...] | None = None,
) -> tuple[QuestionDraft, ...]:
    """Pair only unambiguous supplied answers, retaining every ambiguity as review work."""

    if question_source_refs is not None and len(question_source_refs) != len(questions):
        raise ValueError("question_source_refs must align with questions")

    question_ids = tuple(
        normalize_identifier(question.original_identifier) for question in questions
    )
    answer_ids = tuple(normalize_identifier(answer.original_identifier) for answer in answers)
    question_id_counts = Counter(
        identifier for identifier in question_ids if identifier is not None
    )
    answer_id_counts = Counter(identifier for identifier in answer_ids if identifier is not None)
    answer_by_id: dict[str, list[ExtractedAnswer]] = defaultdict(list)
    for identifier, answer in zip(answer_ids, answers, strict=True):
        if identifier is not None:
            answer_by_id[identifier].append(answer)

    matched_answer_indexes: set[int] = set()
    matched_answers: dict[int, ExtractedAnswer] = {}
    diagnostics: list[list[DraftDiagnostic]] = [[] for _ in questions]

    for question_index, identifier in enumerate(question_ids):
        if identifier is None:
            continue
        if question_id_counts[identifier] > 1:
            diagnostics[question_index].append(
                _blocker("duplicate-question-identifier", "duplicate question identifier")
            )
            continue
        candidates = answer_by_id.get(identifier, [])
        if len(candidates) > 1:
            indexes = {candidate.correct_index for candidate in candidates}
            message = (
                "conflicting supplied answers"
                if len(indexes) > 1
                else "duplicate supplied answer identifier"
            )
            diagnostics[question_index].append(_blocker("duplicate-supplied-answer", message))
            continue
        if len(candidates) == 1:
            answer_index = answers.index(candidates[0])
            matched_answers[question_index] = candidates[0]
            matched_answer_indexes.add(answer_index)

    if _can_align_by_source_order(
        questions,
        answers,
        question_ids,
        answer_ids,
        question_id_counts,
        answer_id_counts,
    ):
        residual_questions = [
            question_index
            for question_index in range(len(questions))
            if question_index not in matched_answers
        ]
        residual_answers = [
            answer_index
            for answer_index in range(len(answers))
            if answer_index not in matched_answer_indexes
        ]
        for question_index, answer_index in zip(residual_questions, residual_answers, strict=True):
            if _ids_contradict(question_ids[question_index], answer_ids[answer_index]):
                continue
            matched_answers[question_index] = answers[answer_index]
            matched_answer_indexes.add(answer_index)

    for answer_index, answer in enumerate(answers):
        if answer_index not in matched_answer_indexes:
            _append_unmatched_answer_diagnostic(diagnostics, answer)

    drafts: list[QuestionDraft] = []
    for index, question in enumerate(questions):
        question_diagnostics = diagnostics[index]
        supplied = _supplied_answers(question, matched_answers.get(index))
        correct_index = _resolve_correct_index(question, supplied, question_diagnostics)
        if correct_index is None and not any(
            diagnostic.severity is DiagnosticSeverity.BLOCKER
            for diagnostic in question_diagnostics
        ):
            question_diagnostics.append(
                _blocker("missing-supplied-answer", "supplied answer is missing")
            )
            question_diagnostics.append(_blocker("unmatched-question", "question is unmatched"))
        drafts.append(
            QuestionDraft(
                question_id=_question_id(index, question, question_ids[index]),
                original_identifier=question.original_identifier,
                stem=question.stem,
                choices=question.choices,
                correct_index=correct_index,
                rationale=_rationale(supplied, question),
                image_ref=None,
                source_refs=(
                    question_source_refs[index] if question_source_refs is not None else ()
                ),
                answer_provenance=(
                    AnswerProvenance.PROVIDED_BY_SOURCE if correct_index is not None else None
                ),
                extraction_confidence=question.confidence,
                diagnostics=tuple(question_diagnostics),
                verification_required=correct_index is None,
                verified_at=None,
            )
        )
    return tuple(drafts)


def _can_align_by_source_order(
    questions: tuple[ExtractedQuestion, ...],
    answers: tuple[ExtractedAnswer, ...],
    question_ids: tuple[str | None, ...],
    answer_ids: tuple[str | None, ...],
    question_id_counts: Counter[str],
    answer_id_counts: Counter[str],
) -> bool:
    if len(questions) != len(answers):
        return False
    if any(count > 1 for count in question_id_counts.values()) or any(
        count > 1 for count in answer_id_counts.values()
    ):
        return False
    return True


def _ids_contradict(question_id: str | None, answer_id: str | None) -> bool:
    return question_id is not None and answer_id is not None and question_id != answer_id


def _supplied_answers(
    question: ExtractedQuestion, answer: ExtractedAnswer | None
) -> tuple[tuple[int, str | None], ...]:
    candidates: list[tuple[int, str | None]] = []
    if question.supplied_correct_index is not None:
        candidates.append((question.supplied_correct_index, question.rationale))
    if answer is not None:
        candidates.append((answer.correct_index, answer.rationale))
    return tuple(candidates)


def _resolve_correct_index(
    question: ExtractedQuestion,
    supplied: tuple[tuple[int, str | None], ...],
    diagnostics: list[DraftDiagnostic],
) -> int | None:
    indexes = {index for index, _ in supplied}
    if len(indexes) > 1:
        diagnostics.append(_blocker("conflicting-supplied-answers", "conflicting supplied answers"))
        return None
    if not indexes:
        return None
    index = indexes.pop()
    if index >= len(question.choices):
        diagnostics.append(
            _blocker(
                "supplied-answer-out-of-bounds",
                "supplied answer is outside the available choices",
            )
        )
        return None
    return index


def _rationale(
    supplied: tuple[tuple[int, str | None], ...], question: ExtractedQuestion
) -> str | None:
    for _, rationale in reversed(supplied):
        if rationale is not None:
            return rationale
    return question.rationale


def _append_unmatched_answer_diagnostic(
    diagnostics: list[list[DraftDiagnostic]], answer: ExtractedAnswer
) -> None:
    if not diagnostics:
        return
    identifier = answer.original_identifier or "without an identifier"
    diagnostics[0].append(
        _blocker("unmatched-supplied-answer", f"unmatched supplied answer: {identifier}")
    )


def _question_id(index: int, question: ExtractedQuestion, identifier: str | None) -> str:
    if identifier is not None:
        return f"question-{identifier}-{index + 1}"
    if question.source_segments:
        return f"question-{question.source_segments[0].segment_key}-{index + 1}"
    return f"question-{index + 1}"


def _blocker(code: str, message: str) -> DraftDiagnostic:
    return DraftDiagnostic(code, message, DiagnosticSeverity.BLOCKER)
