from oms_hub.db import Database
from oms_hub.llm.domain import LLMTask, ProviderName
from oms_hub.llm.repository import LLMSettingsRepository


def test_repository_seeds_four_providers_and_activates_openai(tmp_path):
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
        ProviderName.OPENROUTER,
    ]
    openai_preference = next(
        item for item in preferences if item.provider is ProviderName.OPENAI
    )
    assert openai_preference.active is True
    assert openai_preference.model == "gpt-5.2"
    assert sum(item.active for item in preferences) == 1


def test_repository_updates_models_without_changing_active_provider(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.2")

    updated = repository.set_model(ProviderName.GEMINI, " gemini-3.6-flash ")

    assert updated.model == "gemini-3.6-flash"
    preferences = repository.list()
    assert sum(item.active for item in preferences) == 1
    assert next(
        item for item in preferences if item.provider is ProviderName.OPENAI
    ).active is True


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


def test_assignment_returns_seeded_default_when_table_empty(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.create_schema()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.2")

    assignment = repository.assignment(LLMTask.ANKI_CURATION)

    assert assignment.task is LLMTask.ANKI_CURATION
    assert assignment.provider is ProviderName.OPENAI
    assert assignment.model == "gpt-5.2"


def test_set_assignment_round_trips(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.2")

    saved = repository.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.OPENROUTER,
        " openai/gpt-4o-mini ",
    )
    fetched = repository.assignment(LLMTask.ACCURACY_REVIEW)

    assert saved.provider is ProviderName.OPENROUTER
    assert saved.model == "openai/gpt-4o-mini"
    assert fetched == saved


def test_quiz_tasks_have_independent_provider_assignments(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.2")

    repository.set_assignment(
        LLMTask.QUIZ_EXTRACTION,
        ProviderName.OPENROUTER,
        "deepseek/model",
    )
    repository.set_assignment(
        LLMTask.QUIZ_ANSWER_GENERATION,
        ProviderName.OPENAI,
        "gpt-answer",
    )

    assert repository.assignment(LLMTask.QUIZ_EXTRACTION).model == "deepseek/model"
    assert repository.assignment(LLMTask.QUIZ_ANSWER_GENERATION).model == "gpt-answer"


def test_missing_quiz_assignment_uses_the_openai_default(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.create_schema()
    repository = LLMSettingsRepository(database, default_openai_model="gpt-quiz-default")

    assignment = repository.assignment(LLMTask.QUIZ_EXTRACTION)

    assert assignment.provider is ProviderName.OPENAI
    assert assignment.model == "gpt-quiz-default"
