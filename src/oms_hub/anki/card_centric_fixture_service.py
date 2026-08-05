"""Provider adapter for an externally installed immutable Lecture07 fixture."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from oms_hub.anki.card_centric import CardCentricClassifier
from oms_hub.anki.card_centric_contracts import CardClassification, CardRecord
from oms_hub.anki.card_centric_fixture import (
    Lecture07Fixture,
    evaluate_lecture07_fixture,
    load_lecture07_fixture,
)
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredTextService


class FixtureClassifier(Protocol):
    async def classify_fixture(
        self, *, provider: str, model: str, fixture: Lecture07Fixture
    ) -> tuple[CardClassification, ...]: ...


@dataclass(slots=True)
class ProductionFixtureClassifier:
    structured: StructuredTextService

    async def classify_fixture(
        self, *, provider: str, model: str, fixture: Lecture07Fixture
    ) -> tuple[CardClassification, ...]:
        cards = tuple(
            CardRecord(
                note_id=int(item["note_id"]),
                content_sha256=str(item["content_sha256"]),
                text=str(item["text"]),
                extra=str(item.get("extra", "")),
                tags=tuple(item["tags"]),
                deck_names=tuple(item.get("deck_names", ("AnKing",))),
            )
            for item in fixture.cards
        )
        result = await CardCentricClassifier(self.structured).classify(
            cards,
            source_index=fixture.source_index,
            concept_ids=fixture.missed_concept_ids,
            provider=ProviderName(provider),
            model=model,
        )
        return result.results


@dataclass(frozen=True, slots=True)
class FixtureValidation:
    provider: str
    model: str
    passed: bool
    metrics: dict[str, object]


def fixture_for(path: Path | None) -> Lecture07Fixture:
    return load_lecture07_fixture(path)


async def validate_fixture(
    classifier: FixtureClassifier, *, provider: str, model: str, fixture: Lecture07Fixture
) -> FixtureValidation:
    observed = await classifier.classify_fixture(provider=provider, model=model, fixture=fixture)
    passed, metrics = evaluate_lecture07_fixture(fixture, observed)
    return FixtureValidation(provider, model, passed, metrics)
