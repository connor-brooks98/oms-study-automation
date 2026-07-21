import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from oms_hub.outlook_parser import ParsedLectureEvent

_SUBJECT_ALIASES = {
    "heme": "heme lymph",
    "heme lymph": "heme lymph",
    "resp": "resp",
}


def _normalize(value: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", words)


def _subject(value: str) -> str:
    normalized = _normalize(value)
    return _SUBJECT_ALIASES.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class CatalogLecture:
    id: int
    subject: str
    exam_number: int
    lecture_number: int
    topic: str
    lecturer: str


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    lecture_id: int | None
    confidence: float
    needs_review: bool
    evidence: tuple[str, ...]


class LectureMatcher:
    def __init__(self, lectures: list[CatalogLecture]):
        self.lectures = lectures

    def match(self, event: ParsedLectureEvent) -> MatchCandidate:
        candidates = [
            item
            for item in self.lectures
            if item.lecture_number == event.lecture_number
            and _subject(item.subject) == _subject(event.subject)
        ]
        if not candidates:
            return MatchCandidate(
                None,
                0.0,
                True,
                ("no subject/number candidate",),
            )

        scored: list[tuple[float, CatalogLecture, tuple[str, ...]]] = []
        for item in candidates:
            title_score = SequenceMatcher(
                None,
                _normalize(item.topic),
                _normalize(event.topic),
            ).ratio()
            lecturer_score = SequenceMatcher(
                None,
                _normalize(item.lecturer),
                _normalize(event.lecturer),
            ).ratio()
            score = 0.60 + (0.30 * title_score) + (0.10 * lecturer_score)
            scored.append(
                (
                    score,
                    item,
                    (
                        "subject exact",
                        "number exact",
                        f"title={title_score:.2f}",
                        f"lecturer={lecturer_score:.2f}",
                    ),
                )
            )

        scored.sort(key=lambda value: value[0], reverse=True)
        best_score, best, evidence = scored[0]
        conflict = len(scored) > 1 and best_score - scored[1][0] < 0.10
        if conflict:
            evidence += ("competing candidates",)
        elif best_score < 0.85:
            evidence += ("low confidence",)
        needs_review = best_score < 0.85 or conflict
        return MatchCandidate(
            None if needs_review else best.id,
            best_score,
            needs_review,
            evidence,
        )
