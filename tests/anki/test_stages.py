import asyncio
from types import SimpleNamespace

from oms_hub.anki.prompts import AnkiPromptLibrary, StaticPromptSynchronizer
from oms_hub.anki.stages import CurationServicesRunner


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
