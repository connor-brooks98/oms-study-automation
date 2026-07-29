import pytest

from oms_hub.db import Database
from oms_hub.study_generation.domain import PromptKind
from oms_hub.study_generation.prompts import (
    PromptConfigurationError,
    PromptFileService,
)
from oms_hub.study_generation.repository import GenerationRepository


def service_for(tmp_path, kind, path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = GenerationRepository(database)
    repository.set_prompt_path(kind, str(path))
    return PromptFileService(repository)


def test_prompt_snapshot_reads_latest_obsidian_content(tmp_path):
    path = tmp_path / "Quiz Prompt.md"
    path.write_text("Create 15 questions", encoding="utf-8")
    service = service_for(tmp_path, PromptKind.QUIZ, path)

    first = service.inspect(PromptKind.QUIZ)
    path.write_text("Create 20 questions", encoding="utf-8")
    second = service.inspect(PromptKind.QUIZ)

    assert first.content == "Create 15 questions"
    assert second.content == "Create 20 questions"
    assert first.sha256 != second.sha256
    assert second.path == path


@pytest.mark.parametrize("content", ["", "   "])
def test_empty_prompt_is_rejected(tmp_path, content):
    path = tmp_path / "Outline Prompt.md"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(PromptConfigurationError, match="empty"):
        service_for(tmp_path, PromptKind.OUTLINE, path).inspect(
            PromptKind.OUTLINE
        )


def test_missing_prompt_path_is_rejected_without_content(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    with pytest.raises(PromptConfigurationError, match="not configured"):
        PromptFileService(GenerationRepository(database)).inspect(
            PromptKind.OUTLINE
        )
