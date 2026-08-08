import asyncio
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.anki.source_index import (
    LectureSourceIndex,
    SourceScope,
)
from oms_hub.anki.sources import SourcePassage


class KeywordEmbedder:
    def __init__(self) -> None:
        self.calls: list[tuple[InputType, tuple[str, ...]]] = []
        self.fail = False

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        self.calls.append((input_type, tuple(texts)))
        if self.fail:
            raise RuntimeError("injected source embedding failure")
        rows = []
        for text in texts:
            lowered = text.casefold()
            rows.append(
                [
                    float("iron" in lowered or "ferritin" in lowered),
                    float("warfarin" in lowered or "anticoag" in lowered),
                    float("bacteria" in lowered or "staph" in lowered),
                ]
            )
        return np.asarray(rows, dtype=np.float32)


def _passage(
    revision_id: int,
    kind: SourceKind,
    locator: str,
    text: str,
    *,
    slide_number: int | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> SourcePassage:
    return SourcePassage.create(
        revision_id=revision_id,
        lecture_id=12,
        artifact_id=f"upload-{revision_id}",
        source_kind=kind,
        locator=locator,
        text=text,
        slide_number=slide_number,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def _passages() -> list[SourcePassage]:
    return [
        _passage(
            7,
            SourceKind.SLIDE,
            "slide:3",
            "Iron deficiency causes low ferritin.",
            slide_number=3,
        ),
        _passage(
            8,
            SourceKind.TRANSCRIPT,
            "transcript:1:12-25",
            "Ferritin reflects iron stores before microcytosis.",
            start_seconds=12,
            end_seconds=25,
        ),
        _passage(
            8,
            SourceKind.TRANSCRIPT,
            "transcript:2:25-39",
            "Warfarin is an oral anticoagulant.",
            start_seconds=25,
            end_seconds=39,
        ),
        SourcePassage.create(
            revision_id=7,
            lecture_id=12,
            artifact_id="upload-7",
            source_kind=SourceKind.VISION,
            locator="slide:4:image",
            text="",
            slide_number=4,
            extraction_status="vision_unavailable",
        ),
    ]


def test_refresh_and_hybrid_search_return_resolvable_citations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = KeywordEmbedder()
        index = LectureSourceIndex(
            tmp_path / "source-index",
            embedder,
            model="voyage-4-large",
            dimensions=3,
        )

        generation = await index.refresh(_passages())
        hits = await index.search(
            "low ferritin iron stores",
            SourceScope(),
            limit=3,
        )

        assert generation.passage_count == 4
        assert generation.indexed_count == 3
        assert [call[0] for call in embedder.calls] == [
            "document",
            "query",
        ]
        assert hits[0].passage.passage_id in {
            passage.passage_id for passage in _passages()
        }
        assert hits[0].passage.citation
        assert hits[0].score > 0
        assert index.get_passage(hits[0].passage.passage_id) == (
            hits[0].passage
        )

    asyncio.run(scenario())


def test_source_scope_is_applied_before_result_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        index = LectureSourceIndex(
            tmp_path / "source-index",
            KeywordEmbedder(),
            model="voyage-4-large",
            dimensions=3,
        )
        await index.refresh(_passages())

        hits = await index.search(
            'ferritin (iron) OR "unterminated',
            SourceScope(
                revision_ids=(8,),
                source_kinds=(SourceKind.TRANSCRIPT,),
            ),
            limit=1,
        )

        assert len(hits) == 1
        assert hits[0].passage.revision_id == 8
        assert hits[0].passage.source_kind is SourceKind.TRANSCRIPT

    asyncio.run(scenario())


def test_summary_metadata_round_trips_through_source_index(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        index = LectureSourceIndex(
            tmp_path / "source-index",
            KeywordEmbedder(),
            model="voyage-4-large",
            dimensions=3,
        )
        summary = SourcePassage.create(
            revision_id=9,
            lecture_id=12,
            artifact_id="outline:9",
            source_kind=SourceKind.SUMMARY,
            locator="summary:depth:1",
            text="DEEP: ferritin regulation [27, 28]",
            source_id="SUM:12:DEPTH:D1",
            summary_backrefs=("27", "28"),
            summary_section="depth",
        )

        await index.refresh([summary])
        restored = index.get_passage(summary.passage_id)

        assert restored == summary
        assert restored is not None
        assert restored.source_id == "SUM:12:DEPTH:D1"
        assert restored.summary_backrefs == ("27", "28")

    asyncio.run(scenario())


def test_failed_refresh_keeps_previous_generation_searchable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = KeywordEmbedder()
        index = LectureSourceIndex(
            tmp_path / "source-index",
            embedder,
            model="voyage-4-large",
            dimensions=3,
        )
        first = await index.refresh(_passages())
        embedder.fail = True

        try:
            await index.refresh(
                [
                    _passage(
                        9,
                        SourceKind.SLIDE,
                        "slide:1",
                        "Exploding replacement",
                        slide_number=1,
                    )
                ]
            )
        except RuntimeError as error:
            assert "injected" in str(error)
        else:
            raise AssertionError("failing source refresh succeeded")

        assert index.current_generation() == first.generation
        embedder.fail = False
        hits = await index.search("iron ferritin", SourceScope(), limit=3)
        assert hits
        assert all(hit.passage.revision_id in {7, 8} for hit in hits)

    asyncio.run(scenario())
