import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Protocol

from oms_hub.ingestion.domain import MatchDecision, UploadEvidence

AUTO_MATCH_THRESHOLD = 0.85
REVIEW_MARGIN = 0.15

_ALIASES = {
    "Heme/Lymph": ("heme", "hematology", "lymph"),
    "Neuro": ("neuro", "neuroscience"),
    "MSK": ("msk", "musculoskeletal"),
    "Cardio": ("cardio", "cardiovascular"),
    "Renal": ("renal", "kidney"),
    "Resp": ("resp", "respiratory", "pulmonary"),
    "OPP": ("opp", "osteopathic"),
    "EPC": ("epc", "patient care"),
}


class LectureRecord(Protocol):
    id: int
    subject: str
    exam_number: int
    lecture_number: int
    topic: str
    lecturer: str


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize(value).split()
        if len(token) > 1
    }


def _number(pattern: str, value: str) -> int | None:
    match = re.search(pattern, value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _topic_score(topic: str, contexts: tuple[str, ...]) -> float:
    normalized_topic = _normalize(topic)
    topic_tokens = _tokens(topic)
    scores: list[float] = []
    for context in contexts:
        normalized_context = _normalize(context)
        if not normalized_context:
            continue
        sequence = SequenceMatcher(
            None,
            normalized_topic,
            normalized_context,
        ).ratio()
        overlap = (
            len(topic_tokens & _tokens(context)) / len(topic_tokens)
            if topic_tokens
            else 0.0
        )
        scores.append(max(sequence, overlap))
    return max(scores, default=0.0)


class UploadMatcher:
    def match(
        self,
        evidence: UploadEvidence,
        lectures: Sequence[LectureRecord],
    ) -> MatchDecision:
        combined = (
            f"{evidence.filename} {evidence.embedded_title} "
            f"{evidence.opening_text}"
        )
        normalized = _normalize(combined)
        exam = _number(r"\b(?:exam|block)\s*0*(\d+)\b", combined)
        number = _number(
            r"\b(?:lecture|lec)\s*0*(\d+)\b",
            combined,
        )
        contexts = (
            re.sub(r"\.[^.]+$", "", evidence.filename),
            evidence.embedded_title,
            evidence.opening_text,
        )
        scored: list[tuple[float, LectureRecord, tuple[str, ...]]] = []
        for lecture in lectures:
            score = 0.0
            candidate_reasons: list[str] = []
            aliases = _ALIASES.get(
                lecture.subject,
                (_normalize(lecture.subject),),
            )
            subject_signal = any(
                re.search(rf"\b{re.escape(alias)}\b", normalized)
                for alias in aliases
            )
            if subject_signal:
                score += 0.20
                candidate_reasons.append("subject signal")
            if exam is not None and exam == lecture.exam_number:
                score += 0.15
                candidate_reasons.append("exam number exact")
            if number is not None and number == lecture.lecture_number:
                score += 0.35
                candidate_reasons.append("lecture number exact")
            topic = _topic_score(lecture.topic, contexts)
            score += 0.30 * topic
            if topic:
                candidate_reasons.append(f"topic={topic:.2f}")
            lecturer_tokens = _tokens(lecture.lecturer)
            if lecturer_tokens and lecturer_tokens & _tokens(combined):
                score += 0.05
                candidate_reasons.append("lecturer signal")
            if topic >= 0.95 and (
                number == lecture.lecture_number or subject_signal
            ):
                score = max(score, 0.90)
                candidate_reasons.append("unique exact topic")
            scored.append(
                (
                    min(score, 1.0),
                    lecture,
                    tuple(candidate_reasons),
                )
            )

        if not scored:
            return MatchDecision(
                "quarantined",
                None,
                0.0,
                ("no catalog lectures",),
            )
        scored.sort(key=lambda item: (-item[0], item[1].id))
        best_score, best, best_reasons = scored[0]
        conflict = (
            len(scored) > 1
            and best_score - scored[1][0] < REVIEW_MARGIN
        )
        if conflict:
            best_reasons += ("competing candidates",)
        if best_score < AUTO_MATCH_THRESHOLD:
            best_reasons += ("low confidence",)
        if conflict or best_score < AUTO_MATCH_THRESHOLD:
            return MatchDecision(
                "quarantined",
                None,
                best_score,
                best_reasons,
            )
        return MatchDecision(
            "matched",
            best.id,
            best_score,
            best_reasons,
        )
