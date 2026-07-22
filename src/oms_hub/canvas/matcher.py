import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Protocol

from oms_hub.canvas.domain import CanvasAttachment, CatalogMatch


class LectureRecord(Protocol):
    id: int
    subject: str
    exam_number: int
    lecture_number: int
    topic: str


def _number(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _topic_score(value: CanvasAttachment, topic: str) -> float:
    normalized_topic = _normalized(topic)
    contexts = (
        _normalized(value.module_title),
        _normalized(re.sub(r"\s+lecture$", "", value.item_title, flags=re.IGNORECASE)),
        _normalized(re.sub(r"\s+lecture$", "", value.page_title, flags=re.IGNORECASE)),
    )
    scores = [
        SequenceMatcher(None, context, normalized_topic).ratio()
        for context in contexts
        if context
    ]
    return max(scores, default=0.0)


def match_attachment(
    value: CanvasAttachment,
    subject: str,
    lectures: Sequence[LectureRecord],
) -> CatalogMatch:
    candidates = [item for item in lectures if item.subject == subject]
    exam = _number(r"exam\s*(\d+)", value.module_title)
    number = _number(r"lecture\s*(\d+)", f"{value.item_title} {value.page_title}")
    if subject != "EPC" and exam is not None and number is not None:
        exact = [
            item
            for item in candidates
            if item.exam_number == exam and item.lecture_number == number
        ]
        if len(exact) == 1:
            return CatalogMatch(
                exact[0].id,
                subject,
                exam,
                0.99,
                "course, exam, and lecture number agree",
            )
        return CatalogMatch(
            None,
            subject,
            exam,
            0.0,
            "catalog number match is missing or conflicting",
        )
    ranked = sorted(
        ((_topic_score(value, item.topic), item) for item in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.62:
        return CatalogMatch(
            None,
            subject,
            None,
            ranked[0][0] if ranked else 0.0,
            "topic match is too weak",
        )
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.12:
        return CatalogMatch(None, subject, None, ranked[0][0], "topic match is not unique")
    best = ranked[0][1]
    return CatalogMatch(
        best.id,
        subject,
        best.exam_number,
        0.90,
        "unique strong topic match",
    )
