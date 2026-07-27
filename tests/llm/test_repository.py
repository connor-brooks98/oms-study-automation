from oms_hub.db import Database
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.repository import LLMSettingsRepository


def test_repository_seeds_three_providers_and_activates_openai(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    repository = LLMSettingsRepository(
        database,
        default_openai_model="gpt-5.2",
    )

    preferences = repository.list()
    assert [item.provider for item in preferences] == [
        ProviderName.OPENAI,
        ProviderName.GEMINI,
        ProviderName.ANTHROPIC,
    ]
    assert repository.active().provider is ProviderName.OPENAI
    assert repository.active().model == "gpt-5.2"


def test_repository_updates_models_and_keeps_exactly_one_active_provider(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.2")

    updated = repository.set_model(ProviderName.GEMINI, " gemini-3.6-flash ")
    active = repository.set_active(ProviderName.GEMINI)

    assert updated.model == "gemini-3.6-flash"
    assert active.provider is ProviderName.GEMINI
    assert sum(item.active for item in repository.list()) == 1


def test_repository_can_clear_stale_connection_status(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.2")
    repository.record_test(
        ProviderName.OPENAI,
        state="connected",
        tested_at="2026-07-26T12:00:00+00:00",
        provider_request_id="request",
    )

    cleared = repository.clear_test(ProviderName.OPENAI)

    assert cleared.last_test_state is None
    assert cleared.last_tested_at is None
    assert cleared.provider_request_id is None
