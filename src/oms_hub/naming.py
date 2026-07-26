import re
from dataclasses import dataclass

from oms_hub.domain import LectureKey

_SEPARATOR = re.compile(r'[:|"]+')
_JOINER = re.compile(r"[/\\]+")
_REMOVE = re.compile(r"[<>?*]+")
_SPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ArtifactNames:
    pptx: str
    pdf: str
    transcript: str
    summary: str


def sanitize_filename(value: str) -> str:
    cleaned = _SEPARATOR.sub(" - ", value)
    cleaned = _JOINER.sub("-", cleaned)
    cleaned = _REMOVE.sub("", cleaned)
    cleaned = _SPACE.sub(" ", cleaned).strip(" .-")
    return f"_{cleaned}" if _WINDOWS_RESERVED.match(cleaned) else cleaned


def _number(value: int) -> str:
    return f"{value:02d}" if value < 100 else f"{value:03d}"


def display_title(key: LectureKey) -> str:
    return f"Lecture {_number(key.lecture_number)}: {key.topic.strip()}"


def artifact_names(key: LectureKey) -> ArtifactNames:
    number = _number(key.lecture_number)
    topic = sanitize_filename(key.topic)
    stem = f"Lecture {number} - {topic}"
    return ArtifactNames(
        pptx=f"{stem}.pptx",
        pdf=f"{stem}.pdf",
        transcript=f"{stem} - Transcript.txt",
        summary=f"Lecture {number} - NotebookLM Summary.pdf",
    )
