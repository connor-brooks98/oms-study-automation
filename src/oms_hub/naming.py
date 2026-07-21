import re
from dataclasses import dataclass

from oms_hub.domain import LectureKey

_SEPARATOR = re.compile(r'[:|"]+')
_JOINER = re.compile(r"[/\\]+")
_REMOVE = re.compile(r"[<>?*]+")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ArtifactNames:
    pptx: str
    pdf: str
    transcript: str
    summary: str


def _safe_topic(topic: str) -> str:
    cleaned = _SEPARATOR.sub(" - ", topic)
    cleaned = _JOINER.sub("-", cleaned)
    cleaned = _REMOVE.sub("", cleaned)
    cleaned = _SPACE.sub(" ", cleaned).strip(" .-")
    return cleaned


def _number(value: int) -> str:
    return f"{value:02d}" if value < 100 else f"{value:03d}"


def display_title(key: LectureKey) -> str:
    return f"Lecture {_number(key.lecture_number)}: {key.topic.strip()}"


def artifact_names(key: LectureKey) -> ArtifactNames:
    number = _number(key.lecture_number)
    topic = _safe_topic(key.topic)
    stem = f"Lecture {number} - {topic}"
    return ArtifactNames(
        pptx=f"{stem}.pptx",
        pdf=f"{stem}.pdf",
        transcript=f"{stem} - Transcript.txt",
        summary=f"Lecture {number} - NotebookLM Summary.pdf",
    )
