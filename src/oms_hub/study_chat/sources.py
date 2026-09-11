"""Current, authorized lecture sources and deterministic lexical retrieval."""

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, cast

from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from oms_hub.anki.sources import LectureSourceExtractor, SourcePassage
from oms_hub.files.atomic import sha256_file
from oms_hub.files.trusted_paths import is_indirection
from oms_hub.models import LectureModel, StudyRevisionModel
from oms_hub.study_chat.contracts import OwnerId, RevisionIds, SourceSnapshot

SessionFactory = Callable[[], AbstractContextManager[Session]]


def select_passages(
    question: str,
    passages: tuple[SourcePassage, ...],
    *,
    max_chars: int = 24000,
) -> tuple[SourcePassage, ...]:
    if not 0 <= max_chars <= 24000:
        raise ValueError("max_chars must be between 0 and 24000")
    tokens = set(re.findall(r"\w+", question.casefold()))
    # ponytail: lexical overlap misses paraphrases; add semantic retrieval only after evaluation.
    ranked = sorted(
        (
            (len(tokens & set(re.findall(r"\w+", p.text.casefold()))), p)
            for p in passages
            if p.extraction_status == "text"
        ),
        key=lambda item: (-item[0], item[1].passage_id),
    )
    selected: list[SourcePassage] = []
    seen: set[str] = set()
    remaining = max_chars
    for score, passage in ranked:
        if score and passage.passage_id not in seen and len(passage.text) <= remaining:
            selected.append(passage)
            seen.add(passage.passage_id)
            remaining -= len(passage.text)
    return tuple(selected)


class ChatSources:
    def __init__(
        self,
        session_factory: SessionFactory,
        extractor: LectureSourceExtractor,
        *,
        authorize_revision: Callable[[str, int], None],
    ) -> None:
        self.sessions = session_factory
        self.extractor = extractor
        self.authorize_revision = authorize_revision

    def snapshot(self, owner_id: str, revision_ids: tuple[int, ...]) -> tuple[SourceSnapshot, ...]:
        TypeAdapter(OwnerId).validate_python(owner_id)
        TypeAdapter(RevisionIds).validate_python(revision_ids)
        if len(set(revision_ids)) != len(revision_ids):
            raise ValueError("duplicate source revisions")
        for revision_id in revision_ids:
            self.authorize_revision(owner_id, revision_id)
        snapshots = []
        with self.sessions() as session:
            for revision_id in revision_ids:
                revision = session.get(StudyRevisionModel, revision_id)
                if revision is None or not revision.current or revision.state != "current":
                    raise ValueError("source revision is not current")
                lecture = session.get(LectureModel, revision.lecture_id)
                if lecture is None or revision.kind not in {"slides", "transcripts"}:
                    raise ValueError("source lecture or kind unavailable")
                self._check_file(revision.immutable_source_path, revision.source_sha256)
                if revision.derived_sha256 is not None:
                    self._check_file(revision.immutable_derived_path, revision.derived_sha256)
                elif revision.kind == "transcripts":
                    raise ValueError("source cleaned transcript unavailable")
                snapshots.append(
                    SourceSnapshot(
                        revision_id=revision.id,
                        lecture_id=lecture.id,
                        subject=lecture.subject,
                        exam_number=lecture.exam_number,
                        kind=cast(Literal["slides", "transcripts"], revision.kind),
                        source_sha256=revision.source_sha256,
                        derived_sha256=revision.derived_sha256,
                    )
                )
        return tuple(snapshots)

    def validate(self, owner_id: str, snapshots: tuple[SourceSnapshot, ...]) -> None:
        if self.snapshot(owner_id, tuple(s.revision_id for s in snapshots)) != snapshots:
            raise ValueError("source snapshot changed")

    def passages(
        self,
        owner_id: str,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> tuple[SourcePassage, ...]:
        self.validate(owner_id, snapshots)
        passages = tuple(self.extractor.extract(tuple(s.revision_id for s in snapshots)))
        allowed = {(s.revision_id, s.lecture_id) for s in snapshots}
        if any((p.revision_id, p.lecture_id) not in allowed for p in passages):
            raise ValueError("source extractor expanded scope")
        self.validate(owner_id, snapshots)
        return passages

    @staticmethod
    def _check_file(value: str | None, expected: str) -> None:
        if value is None:
            raise ValueError("source artifact unavailable")
        path = Path(value)
        try:
            if (
                not path.is_absolute()
                or not path.is_file()
                or any(is_indirection(p) for p in (path, *path.parents))
                or sha256_file(path) != expected
            ):
                raise ValueError("source artifact hash mismatch")
        except OSError as exc:
            raise ValueError("source artifact unavailable") from exc
