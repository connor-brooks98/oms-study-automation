import re
import unicodedata
from dataclasses import dataclass

_MAX_COMPONENT_LENGTH = 80


@dataclass(frozen=True, slots=True)
class LectureIdentity:
    course: str
    exam_number: int
    lecture_number: int
    topic: str

    def __post_init__(self) -> None:
        if self.exam_number < 1:
            raise ValueError("exam_number must be positive")
        if self.lecture_number < 1:
            raise ValueError("lecture_number must be positive")


def canonical_component(value: str, *, separator: str) -> str:
    if separator not in {"", "_"}:
        raise ValueError("separator must be empty or an underscore")
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = decomposed.encode("ascii", "ignore").decode("ascii")
    marker = separator or "\0"
    normalized = re.sub(r"[^A-Za-z0-9]+", marker, ascii_value).strip(marker)
    if not separator:
        normalized = normalized.replace(marker, "")
    normalized = normalized[:_MAX_COMPONENT_LENGTH].rstrip(separator)
    if not normalized:
        raise ValueError("component must contain letters or digits")
    return normalized


def target_deck(value: LectureIdentity) -> str:
    course = canonical_component(value.course, separator="_")
    topic = canonical_component(value.topic, separator="_")
    return (
        f"OMS-II_Custom_Cards::{course}::Exam_{value.exam_number}"
        f"::Lec{value.lecture_number}_{topic}"
    )


def target_tag(value: LectureIdentity) -> str:
    course = canonical_component(value.course, separator="")
    topic = canonical_component(value.topic, separator="_")
    return (
        f"AnkiHub_Optional::LMU_OMS_II::{course}::Block{value.exam_number}"
        f"::Lec{value.lecture_number}_{topic}"
    )
