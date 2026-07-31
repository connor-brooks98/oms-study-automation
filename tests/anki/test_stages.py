import asyncio
from types import SimpleNamespace

import oms_hub.anki.stages as stages_module
from oms_hub.anki.domain import CurationStage, SourceKind
from oms_hub.anki.prompts import AnkiPromptLibrary, StaticPromptSynchronizer
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.anki.v2_contracts import LectureConceptLedgerV2, LectureConceptV2
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


class ReadyRuntime:
    async def ensure_running(self) -> SimpleNamespace:
        return SimpleNamespace(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Acceptance",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


def test_preflight_snapshots_all_prompts_for_the_job() -> None:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.runtime = ReadyRuntime()
    runner.prompts = AnkiPromptLibrary()
    runner.prompt_sync = StaticPromptSynchronizer()
    context = SimpleNamespace(
        job=SimpleNamespace(
            lcl_prompt_version="lecture-concept-ledger",
            judgment_rubric_version="coverage-rubric",
            gap_prompt_version="gap-card-generation",
        )
    )

    product = asyncio.run(runner._preflight(context))

    prompts = {
        item["id"]: item for item in product.payload["prompt_snapshot"]
    }
    assert set(prompts) == {
        "lecture-concept-ledger",
        "coverage-rubric",
        "card-relevance-audit",
        "gap-card-generation",
        "paraphrase-expansion",
    }
    assert all(len(item["prompt_hash"]) == 12 for item in prompts.values())
    assert all(item["content"] for item in prompts.values())
    assert product.payload["prompt_sync_stale"] is False


class V2StageStructuredService:
    def __init__(self, ledger: LectureConceptLedgerV2) -> None:
        self.ledger = ledger

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LectureConceptLedgerV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LectureConceptLedgerV2]:
        del instruction, input_text
        assert output_model is LectureConceptLedgerV2
        return StructuredJSONResult(
            value=self.ledger,
            raw_text=self.ledger.model_dump_json(),
            provider=provider,
            model=model,
            request_id="lcl-v2-request",
            input_tokens=40,
            output_tokens=20,
            cost_microusd=7,
        )


def test_lcl_stage_activates_schema_from_pinned_prompt_metadata() -> None:
    slide = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    transcript = SourcePassage.create(
        revision_id=8,
        lecture_id=12,
        artifact_id="transcript-8",
        source_kind=SourceKind.TRANSCRIPT,
        locator="transcript:1:12-24",
        text="Iron deficiency depletes iron stores.",
        start_seconds=12,
        end_seconds=24,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(slide.source_id, transcript.source_id),
            ),
        ),
        intentionally_uncited=(),
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = V2StageStructuredService(ledger)
    context = SimpleNamespace(
        job=SimpleNamespace(
            lcl_prompt_version="lecture-concept-ledger",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "lecture-concept-ledger",
                        "content": "# V2 ledger prompt",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "lcl_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [
                    stages_module._passage_payload(slide),
                    stages_module._passage_payload(transcript),
                ]
            },
        },
    )

    product = asyncio.run(runner._lcl(context))

    assert product.payload["ledger"] == ledger.model_dump(mode="json")
    assert product.payload["prompt_hash"] == "123456789abc"
    assert product.payload["schema_name"] == "lcl_v2"


def test_downstream_ledger_reader_adapts_v2_artifact() -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=True,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        }
    )

    runtime = stages_module._ledger(context)

    assert runtime.concepts[0].statement == (
        "Iron deficiency causes low ferritin."
    )
    assert runtime.concepts[0].source_refs[0].passage_id == passage.passage_id
    assert runtime.concepts[0].primary_entity == "iron deficiency"
