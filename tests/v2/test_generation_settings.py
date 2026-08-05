from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.study_generation.domain import PromptKind
from oms_hub.study_generation.repository import GenerationRepository


def prepared_client(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    return TestClient(app), app


def test_saved_transcript_prompt_path_overrides_configured_fallback(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = Database(database_url)
    database.migrate()
    saved = tmp_path / "Moved Transcript Prompt.md"
    fallback = tmp_path / "Old Transcript Prompt.md"
    GenerationRepository(database).set_prompt_path(
        PromptKind.TRANSCRIPT,
        str(saved),
    )

    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=database_url,
            transcript_prompt_path=fallback,
        )
    )

    assert app.state.transcript_prompt.path == saved


def test_configured_transcript_prompt_path_remains_startup_fallback(tmp_path):
    fallback = tmp_path / "Configured Transcript Prompt.md"

    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            transcript_prompt_path=fallback,
        )
    )

    assert app.state.transcript_prompt.path == fallback


def test_prompt_path_can_be_saved_and_tested_without_returning_content(tmp_path):
    client, app = prepared_client(tmp_path)
    prompt = tmp_path / "Outline.md"
    prompt.write_text("private prompt contents", encoding="utf-8")

    saved = client.post(
        "/settings/generation/prompts/outline",
        json={"path": str(prompt)},
    )
    tested = client.post("/settings/generation/prompts/outline/test")

    assert saved.status_code == 200
    assert tested.status_code == 200
    assert tested.json()["state"] == "valid"
    assert tested.json()["sha256"]
    assert "private prompt contents" not in tested.text
    assert app.state.generation_repository.prompt_path(
        PromptKind.OUTLINE
    ) == str(prompt)


def test_settings_uses_existing_design_for_prompt_paths(tmp_path):
    client, _ = prepared_client(tmp_path)

    page = client.get("/settings")

    assert "Notebook prompts" in page.text
    assert page.text.count("data-prompt-path") == 2
    assert page.text.count("Select Path") == 2


class FakePromptPathPicker:
    def __init__(self, selected):
        self.selected = selected

    def select(self):
        return self.selected


def test_prompt_file_can_be_selected_without_saving_it(tmp_path):
    client, app = prepared_client(tmp_path)
    selected = tmp_path / "Obsidian" / "Outline Prompt.md"
    app.state.prompt_path_picker = FakePromptPathPicker(selected)

    response = client.post(
        "/settings/generation/prompts/outline/select",
        json={},
    )

    assert response.status_code == 200
    assert response.json() == {
        "kind": "outline",
        "path": str(selected),
        "selected": True,
    }
    assert app.state.generation_repository.prompt_path(PromptKind.OUTLINE) is None


def test_saved_prompt_path_changes_action_to_save_path(tmp_path):
    client, app = prepared_client(tmp_path)
    app.state.generation_repository.set_prompt_path(
        PromptKind.OUTLINE,
        str(tmp_path / "Outline.md"),
    )

    page = client.get("/settings")

    assert page.text.count("Select Path") == 1
    assert page.text.count("Save Path") == 1


class FakePromptDirectoryPicker:
    def __init__(self, selected):
        self.selected = selected

    def select_directory(self):
        return self.selected


def test_anki_prompt_directory_can_be_selected_saved_and_tested(tmp_path):
    client, app = prepared_client(tmp_path)
    directory = tmp_path / "Main Vault" / "Anki AI Prompts"
    directory.mkdir(parents=True)
    for filename, prompt_id, schema in (
        ("lcl.md", "lcl", "lcl_v2"),
        ("coverage.md", "coverage", "coverage_v2"),
        ("gap.md", "gap", "gap_cards_v2"),
    ):
        (directory / filename).write_text(
            f'---\nid: {prompt_id}\nversion: "2.0"\nschema: {schema}\n---\n\nPrompt.',
            encoding="utf-8",
        )
    app.state.prompt_directory_picker = FakePromptDirectoryPicker(directory)

    selected = client.post("/settings/anki/prompts/directory/select", json={})
    saved = client.post(
        "/settings/anki/prompts/directory",
        json={"path": str(directory)},
    )
    tested = client.post("/settings/anki/prompts/directory/test")

    assert selected.json()["path"] == str(directory)
    assert saved.status_code == 200
    assert tested.json()["state"] == "valid"
    assert tested.json()["choice_count"] == 3
    assert app.state.generation_repository.anki_prompt_directory() == str(directory)


def test_settings_renders_one_anki_prompt_directory_control(tmp_path):
    client, _ = prepared_client(tmp_path)
    page = client.get("/settings")
    assert page.text.count("data-anki-prompt-directory>") == 1
    assert "Select Folder" in page.text
