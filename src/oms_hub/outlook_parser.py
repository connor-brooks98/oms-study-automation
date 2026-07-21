import re
from dataclasses import dataclass

_TITLE = re.compile(
    r"^\s*(?P<number>\d+)(?P<campus>[A-Z]{1,3})\.\s*"
    r"(?P<subject>[^:]+):\s*(?P<topic>[^|]+)\|\s*"
    r"(?P<lecturer>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedLectureEvent:
    lecture_number: int
    campus: str
    subject: str
    topic: str
    lecturer: str


def parse_lecture_title(title: str) -> ParsedLectureEvent:
    match = _TITLE.match(title)
    if match is None:
        raise ValueError(f"unrecognized lecture title: {title}")
    return ParsedLectureEvent(
        lecture_number=int(match.group("number")),
        campus=match.group("campus").upper(),
        subject=match.group("subject").strip(),
        topic=match.group("topic").strip(),
        lecturer=match.group("lecturer").strip(),
    )
