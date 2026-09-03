"""Deterministic supplied-answer pairing for imported practice questions."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import cast

from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedAnswerValue,
    ExtractedMatchingAnswer,
    ExtractedMatchingAnswerRow,
    ExtractedMatchingQuestion,
    ExtractedQuestion,
    ExtractedQuestionValue,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    MatchingPromptDraft,
    MatchingQuestionDraft,
    QuestionDraft,
    QuestionDraftValue,
    QuestionSourceRef,
)

_NUMBERED_IDENTIFIER = re.compile(r"^(?:question|q)?\s*0*(\d+)\s*[.:)]?$", re.IGNORECASE)


def normalize_identifier(value: str | None) -> str | None:
    """Return a stable label for numeric and nonnumeric source identifiers."""

    if value is None:
        return None
    normalized = " ".join(value.casefold().split()).rstrip(".:)")
    if not normalized:
        return None
    match = _NUMBERED_IDENTIFIER.fullmatch(normalized)
    return str(int(match.group(1))) if match is not None else normalized


@dataclass(frozen=True, slots=True)
class PairingResult:
    drafts: tuple[QuestionDraftValue, ...]
    diagnostics: tuple[DraftDiagnostic, ...]


def pair_supplied_answers(
    questions: tuple[ExtractedQuestionValue, ...],
    answers: tuple[ExtractedAnswerValue, ...],
    *,
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...] | None = None,
    answer_source_refs: tuple[tuple[QuestionSourceRef, ...], ...] | None = None,
) -> PairingResult:
    if question_source_refs is not None and len(question_source_refs) != len(questions):
        raise ValueError("question_source_refs must align with questions")
    if answer_source_refs is not None and len(answer_source_refs) != len(answers):
        raise ValueError("answer_source_refs must align with answers")
    question_refs = question_source_refs or tuple(() for _ in questions)
    answer_refs = answer_source_refs or tuple(() for _ in answers)
    mcq_positions = tuple(
        index for index, item in enumerate(questions) if isinstance(item, ExtractedQuestion)
    )
    matching_positions = tuple(
        index
        for index, item in enumerate(questions)
        if isinstance(item, ExtractedMatchingQuestion)
    )
    mcq_drafts = _pair_multiple_choice_answers(
        tuple(cast(ExtractedQuestion, questions[index]) for index in mcq_positions),
        tuple(item for item in answers if isinstance(item, ExtractedAnswer)),
        question_source_refs=tuple(question_refs[index] for index in mcq_positions),
    )
    matching = _pair_matching_answers(
        tuple(
            cast(ExtractedMatchingQuestion, questions[index])
            for index in matching_positions
        ),
        tuple(item for item in answers if isinstance(item, ExtractedMatchingAnswer)),
        tuple(question_refs[index] for index in matching_positions),
        tuple(
            refs
            for item, refs in zip(answers, answer_refs, strict=True)
            if isinstance(item, ExtractedMatchingAnswer)
        ),
    )
    by_position: dict[int, QuestionDraftValue] = dict(
        zip(mcq_positions, mcq_drafts, strict=True)
    )
    by_position.update(dict(zip(matching_positions, matching.drafts, strict=True)))
    return PairingResult(
        tuple(by_position[index] for index in range(len(questions))), matching.diagnostics
    )


def _pair_multiple_choice_answers(
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
        if not _residual_labels_contradict(
            residual_questions,
            residual_answers,
            question_ids,
            answer_ids,
        ):
            for question_index, answer_index in zip(
                residual_questions, residual_answers, strict=True
            ):
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
                rationale=_rationale(supplied, question, correct_index),
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


def _pair_matching_answers(
    questions: tuple[ExtractedMatchingQuestion, ...],
    answers: tuple[ExtractedMatchingAnswer, ...],
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...],
    answer_source_refs: tuple[tuple[QuestionSourceRef, ...], ...],
) -> PairingResult:
    answer_groups: dict[
        str, list[tuple[ExtractedMatchingAnswer, tuple[QuestionSourceRef, ...]]]
    ] = defaultdict(list)
    for answer, refs in zip(answers, answer_source_refs, strict=True):
        answer_groups[normalize_identifier(answer.original_identifier) or ""].append(
            (answer, refs)
        )

    question_ids = tuple(normalize_identifier(item.original_identifier) for item in questions)
    question_counts = Counter(item for item in question_ids if item is not None)
    used_groups: set[str] = set()
    run_diagnostics: list[DraftDiagnostic] = []
    drafts: list[MatchingQuestionDraft] = []
    for question_index, (question, group_id, source_refs) in enumerate(
        zip(questions, question_ids, question_source_refs, strict=True)
    ):
        owned: list[DraftDiagnostic] = []
        if group_id is not None and question_counts[group_id] > 1:
            run_diagnostics.append(
                _blocker(
                    "duplicate-matching-question-identifier",
                    "duplicate matching question identifier",
                )
            )
        grouped_answers = answer_groups.get(group_id or "", []) if group_id is not None else []
        if grouped_answers:
            used_groups.add(group_id or "")
        rows_with_refs: list[
            tuple[ExtractedMatchingAnswerRow, tuple[QuestionSourceRef, ...]]
        ] = []
        for answer, refs in grouped_answers:
            refs_by_key = {(ref.source_id, ref.segment_key): ref for ref in refs}
            for row in answer.matches:
                rows_with_refs.append(
                    (
                        row,
                        tuple(
                            refs_by_key[(citation.source_id, citation.segment_key)]
                            for citation in row.source_segments
                            if (citation.source_id, citation.segment_key) in refs_by_key
                        ),
                    )
                )
        rows = tuple(row for row, _ in rows_with_refs)
        prompt_counts = Counter(normalize_identifier(row.prompt_identifier) for row in rows)
        prompt_label_counts = Counter(
            normalize_identifier(prompt.original_identifier) for prompt in question.prompts
        )
        if any(count > 1 for count in prompt_label_counts.values()):
            owned.append(
                _blocker(
                    "duplicate-matching-prompt-identifier",
                    "duplicate matching prompt identifier",
                )
            )
        known_labels = set(prompt_label_counts)
        for row_label in set(prompt_counts) - known_labels:
            run_diagnostics.append(
                _blocker(
                    "unknown-matching-prompt-answer",
                    "matching answer references unknown prompt: "
                    f"{row_label or 'without an identifier'}",
                )
            )

        prompt_drafts: list[MatchingPromptDraft] = []
        accepted_answer_refs: list[QuestionSourceRef] = []
        accepted_rationales: list[str] = []
        for prompt_index, prompt in enumerate(question.prompts):
            label = normalize_identifier(prompt.original_identifier)
            indexes = {
                index
                for index in (
                    prompt.supplied_correct_index,
                    *(
                        row.correct_index
                        for row in rows
                        if normalize_identifier(row.prompt_identifier) == label
                    ),
                )
                if index is not None
            }
            if len(indexes) > 1 or prompt_counts[label] > 1 and len(indexes) != 1:
                owned.append(
                    _blocker(
                        "conflicting-supplied-matching-answer",
                        f"{prompt.original_identifier}: conflicting supplied matching answer",
                    )
                )
                correct_index = None
            elif not indexes:
                owned.append(
                    _blocker(
                        "missing-supplied-matching-answer",
                        f"{prompt.original_identifier}: supplied matching answer is missing",
                    )
                )
                correct_index = None
            else:
                correct_index = indexes.pop()
                if correct_index >= len(question.choices):
                    owned.append(
                        _blocker(
                            "supplied-matching-answer-out-of-bounds",
                            f"{prompt.original_identifier}: supplied matching answer is "
                            "outside the available choices",
                        )
                    )
                    correct_index = None
            if correct_index is not None:
                for row, refs in rows_with_refs:
                    if (
                        normalize_identifier(row.prompt_identifier) == label
                        and row.correct_index == correct_index
                    ):
                        accepted_answer_refs.extend(refs)
                        if row.rationale is not None and row.rationale.strip():
                            accepted_rationales.append(
                                f"{prompt.original_identifier}: {row.rationale.strip()}"
                            )
            prompt_drafts.append(
                MatchingPromptDraft(
                    f"p{prompt_index + 1}",
                    prompt.original_identifier,
                    prompt.text,
                    correct_index,
                )
            )
        group_source_refs = tuple(dict.fromkeys((*source_refs, *accepted_answer_refs)))
        prompt_values = tuple(prompt_drafts)
        complete = all(prompt.correct_index is not None for prompt in prompt_values)
        drafts.append(
            MatchingQuestionDraft(
                question_id=_question_id(question_index, question, group_id),
                original_identifier=question.original_identifier,
                stem=question.stem,
                prompts=prompt_values,
                choices=question.choices,
                rationale=(
                    "; ".join(accepted_rationales)
                    if accepted_rationales
                    else matching_summary(prompt_values, question.choices)
                ),
                image_ref=None,
                source_refs=group_source_refs,
                answer_provenance=(
                    AnswerProvenance.PROVIDED_BY_SOURCE if complete and not owned else None
                ),
                extraction_confidence=question.confidence,
                diagnostics=tuple(owned),
                verification_required=False,
                verified_at=None,
            )
        )
    for group_id in answer_groups:
        if group_id not in used_groups:
            run_diagnostics.append(
                _blocker(
                    "unmatched-matching-answer-group",
                    f"unmatched matching answer group: {group_id or 'without an identifier'}",
                )
            )
    return PairingResult(tuple(drafts), tuple(run_diagnostics))


def matching_summary(
    prompts: tuple[MatchingPromptDraft, ...], choices: tuple[str, ...]
) -> str | None:
    resolved = tuple(
        f"{prompt.label} -> {choices[prompt.correct_index]}"
        for prompt in prompts
        if prompt.correct_index is not None and 0 <= prompt.correct_index < len(choices)
    )
    return f"Source-marked matches: {'; '.join(resolved)}." if resolved else None


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


def _residual_labels_contradict(
    question_indexes: list[int],
    answer_indexes: list[int],
    question_ids: tuple[str | None, ...],
    answer_ids: tuple[str | None, ...],
) -> bool:
    question_labels = {question_ids[index] for index in question_indexes} - {None}
    answer_labels = {answer_ids[index] for index in answer_indexes} - {None}
    return bool(question_labels and answer_labels and question_labels != answer_labels)


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
    supplied: tuple[tuple[int, str | None], ...],
    question: ExtractedQuestion,
    correct_index: int | None,
) -> str | None:
    for _, rationale in reversed(supplied):
        if rationale is not None and rationale.strip():
            return rationale
    if question.rationale is not None and question.rationale.strip():
        return question.rationale
    if correct_index is not None and supplied:
        return f"Source-marked correct answer: {question.choices[correct_index]}"
    return None


def _append_unmatched_answer_diagnostic(
    diagnostics: list[list[DraftDiagnostic]], answer: ExtractedAnswer
) -> None:
    if not diagnostics:
        return
    identifier = answer.original_identifier or "without an identifier"
    diagnostics[0].append(
        _blocker("unmatched-supplied-answer", f"unmatched supplied answer: {identifier}")
    )


def _question_id(
    index: int,
    question: ExtractedQuestion | ExtractedMatchingQuestion,
    identifier: str | None,
) -> str:
    if identifier is not None:
        return f"question-{identifier}-{index + 1}"
    if question.source_segments:
        return f"question-{question.source_segments[0].segment_key}-{index + 1}"
    return f"question-{index + 1}"


def _blocker(code: str, message: str) -> DraftDiagnostic:
    return DraftDiagnostic(code, message, DiagnosticSeverity.BLOCKER)
