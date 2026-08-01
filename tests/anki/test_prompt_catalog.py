from pathlib import Path

import pytest

import oms_hub.anki.prompts as prompt_module
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService, PromptRole
from oms_hub.anki.prompts import AnkiPromptConfigurationError


def _write_prompt(path: Path, prompt_id: str, version: str, schema: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nid: {prompt_id}\nversion: "{version}"\nschema: {schema}\n---\n\nPrompt body.',
        encoding="utf-8",
    )


def _bundled_root() -> Path:
    return Path(prompt_module.__file__).with_name("prompt_assets")


def test_catalog_groups_only_valid_top_level_role_prompts(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write_prompt(root / "lcl-v2.md", "lcl-v2", "2.0", "lcl_v2")
    _write_prompt(root / "_shared" / "hidden.md", "hidden", "2.0", "lcl_v2")
    _write_prompt(root / "bad.md", "bad", "2.0", "unknown_v9")

    catalog = AnkiPromptCatalogService(
        lambda: root,
        bundled_directory=_bundled_root(),
    ).catalog()

    assert [choice.id for choice in catalog.choices[PromptRole.LCL]] == ["lcl-v2"]
    assert catalog.choices[PromptRole.COVERAGE] == ()
    assert any(issue.path.name == "bad.md" for issue in catalog.issues)
    assert not any(issue.path.name == "hidden.md" for issue in catalog.issues)


def test_job_snapshot_uses_selected_directory_and_bundled_internal_prompts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "prompts"
    _write_prompt(root / "lcl.md", "lcl", "2.0", "lcl_v2")
    _write_prompt(root / "coverage.md", "coverage", "2.0", "coverage_v2")
    _write_prompt(root / "gap.md", "gap", "2.0", "gap_cards_v2")

    snapshot = AnkiPromptCatalogService(
        lambda: root,
        bundled_directory=_bundled_root(),
    ).load_job_snapshot(lcl_id="lcl", coverage_id="coverage", gap_id="gap")

    assert snapshot.require("lcl").path.parent == root
    assert snapshot.require("card-relevance-audit").path.parent == _bundled_root()
    assert snapshot.require("paraphrase-expansion").path.parent == _bundled_root()


def test_job_snapshot_rejects_prompt_from_the_wrong_role(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write_prompt(root / "coverage.md", "coverage", "2.0", "coverage_v2")
    _write_prompt(root / "gap.md", "gap", "2.0", "gap_cards_v2")

    with pytest.raises(AnkiPromptConfigurationError, match="LCL"):
        AnkiPromptCatalogService(
            lambda: root,
            bundled_directory=_bundled_root(),
        ).load_job_snapshot(
            lcl_id="coverage",
            coverage_id="coverage",
            gap_id="gap",
        )
