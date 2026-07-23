import re
from datetime import datetime
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from oms_hub.models import LectureModel
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch

SCHEDULE_SAME_DAY = 0.35
SCHEDULE_WITHIN_TWO_HOURS = 0.20
SUBJECT_EVIDENCE = 0.20
LECTURE_NUMBER_EVIDENCE = 0.10
TOPIC_SIMILARITY_MAX = 0.10
LECTURER_EVIDENCE = 0.05
AUTO_MATCH_THRESHOLD = 0.90
REVIEW_MARGIN = 0.10


def _normalize(value: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", words)


def _tokens(value: str) -> set[str]:
    return {token for token in _normalize(value).split() if len(token) > 2}


def _scheduled(value: LectureModel) -> datetime | None:
    if not value.scheduled_start_utc:
        return None
    try:
        parsed = datetime.fromisoformat(value.scheduled_start_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class RecordingMatcher:
    def __init__(self, timezone: str = "America/New_York"):
        self.timezone = ZoneInfo(timezone)

    def match(
        self,
        session: PanoptoSession,
        lectures: list[LectureModel],
    ) -> RecordingMatch:
        normalized_name = _normalize(f"{session.name} {session.folder_name}")
        name_tokens = _tokens(normalized_name)
        scored: list[tuple[float, LectureModel, tuple[str, ...]]] = []
        for lecture in lectures:
            scheduled = _scheduled(lecture)
            if scheduled is None:
                continue
            evidence: list[str] = []
            score = 0.0
            session_local = session.created_utc.astimezone(self.timezone)
            lecture_local = scheduled.astimezone(self.timezone)
            same_day = session_local.date() == lecture_local.date()
            if same_day:
                score += SCHEDULE_SAME_DAY
                evidence.append("same local day")
            difference_hours = abs(
                (session.created_utc - scheduled).total_seconds()
            ) / 3600
            if difference_hours <= 2:
                score += SCHEDULE_WITHIN_TWO_HOURS
                evidence.append("within two hours")

            subject_tokens = _tokens(lecture.subject)
            subject_signal = bool(subject_tokens and subject_tokens <= name_tokens)
            if subject_signal:
                score += SUBJECT_EVIDENCE
                evidence.append("subject")

            number_pattern = rf"\b0*{lecture.lecture_number}(?:[a-z])?\b"
            number_signal = re.search(number_pattern, normalized_name) is not None
            if number_signal:
                score += LECTURE_NUMBER_EVIDENCE
                evidence.append("lecture number")

            topic_normalized = _normalize(lecture.topic)
            topic_similarity = SequenceMatcher(
                None,
                topic_normalized,
                normalized_name,
            ).ratio()
            topic_tokens = _tokens(lecture.topic)
            if topic_tokens:
                overlap = len(topic_tokens & name_tokens) / len(topic_tokens)
                topic_similarity = max(topic_similarity, overlap)
            if topic_similarity > 0:
                score += TOPIC_SIMILARITY_MAX * topic_similarity
                evidence.append(f"topic={topic_similarity:.2f}")

            lecturer_parts = _tokens(lecture.lecturer)
            lecturer_signal = bool(lecturer_parts & name_tokens)
            if lecturer_signal:
                score += LECTURER_EVIDENCE
                evidence.append("lecturer")

            has_title_signal = (
                subject_signal
                or number_signal
                or topic_similarity >= 0.35
                or lecturer_signal
            )
            if not same_day or not has_title_signal:
                continue
            scored.append((min(score, 1.0), lecture, tuple(evidence)))

        if not scored:
            return RecordingMatch(None, 0.0, ("no eligible candidate",), True)
        scored.sort(key=lambda item: (-item[0], item[1].id))
        best_score, best, best_evidence = scored[0]
        conflict = (
            len(scored) > 1
            and best_score - scored[1][0] < REVIEW_MARGIN
        )
        if conflict:
            best_evidence += ("competing candidates",)
        if best_score < AUTO_MATCH_THRESHOLD:
            best_evidence += ("low confidence",)
        needs_review = conflict or best_score < AUTO_MATCH_THRESHOLD
        return RecordingMatch(
            None if needs_review else best.id,
            best_score,
            best_evidence,
            needs_review,
        )
