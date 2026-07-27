from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.study_generation.domain import PromptKind


def prepared_client(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    return TestClient(app), app


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
