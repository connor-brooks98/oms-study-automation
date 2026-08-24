import hashlib
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


def test_bundled_card_centric_v2_prompts_are_internal_not_catalog_errors() -> None:
    catalog = AnkiPromptCatalogService(bundled_directory=_bundled_root()).catalog()

    assert not any(
        issue.path.name
        in {
            "card-centric-ledger-v2.md",
            "card-centric-fast-classifier.md",
            "card-centric-gap-v2.md",
        }
        for issue in catalog.issues
    )


def test_v2_snapshot_resolves_all_executed_internal_prompt_content() -> None:
    snapshot = AnkiPromptCatalogService(
        bundled_directory=_bundled_root()
    ).load_card_centric_v2_snapshot()

    assert [prompt.metadata.id for prompt in snapshot.prompts] == [
        "card-centric-ledger-v2",
        "card-centric-fast-classifier",
        "card-centric-classifier",
        "card-centric-gap-v2",
    ]
    for prompt in snapshot.prompts:
        assert prompt.content_sha256 == hashlib.sha256(prompt.content.encode("utf-8")).hexdigest()
        assert prompt.prompt_hash == prompt.content_sha256[:12]


def test_v3_scope_prompt_is_internal_and_has_a_dedicated_snapshot() -> None:
    service = AnkiPromptCatalogService(bundled_directory=_bundled_root())

    assert not any(
        issue.path.name == "card-centric-scope-v3.md" for issue in service.catalog().issues
    )
    snapshot = service.load_card_centric_v3_scope_snapshot()
    prompt = snapshot.require("card-centric-scope-v3")
    assert [item.metadata.id for item in snapshot.prompts] == ["card-centric-scope-v3"]
    assert prompt.metadata.version == "3.3"
    assert prompt.metadata.response_format == "json"
    assert prompt.metadata.schema_name == "scope_v3"
    assert "Each fact must be one independently testable assertion" in prompt.content
    assert "complete sentence of at most 160 characters" in prompt.content
    assert "join separately testable claims" in prompt.content
    assert "Set `generation_allowed` true" in prompt.content
    assert prompt.content_sha256 == hashlib.sha256(prompt.content.encode("utf-8")).hexdigest()
