import hashlib
import subprocess
from pathlib import Path

import pytest

from oms_hub.anki.prompts import (
    AnkiPromptConfigurationError,
    AnkiPromptLibrary,
    GitPromptSynchronizer,
)


def test_resolves_frontmatter_and_shared_markdown_in_declared_order(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "AnkiPipeline" / "prompts"
    shared = vault / "_shared"
    shared.mkdir(parents=True)
    (shared / "rules.md").write_text(
        "---\nid: rules\nversion: 2.0.0\nshared: true\n---\n\n## Rules\n\nShared rule.",
        encoding="utf-8",
    )
    (vault / "coverage-rubric.md").write_text(
        "---\n"
        "id: coverage-rubric\n"
        "version: 2.0.0\n"
        "model: claude-sonnet-4-6\n"
        "temperature: 0\n"
        "max_tokens: 8000\n"
        "response_format: json\n"
        "schema: coverage_v2\n"
        "includes:\n"
        "  - _shared/rules.md\n"
        "cache_prefix: true\n"
        "batch_size: 30\n"
        "---\n\n"
        "# Coverage Rubric\n\nJudge coverage.",
        encoding="utf-8",
    )
    expected = "## Rules\n\nShared rule.\n\n# Coverage Rubric\n\nJudge coverage."

    prompt = AnkiPromptLibrary(vault).load("coverage-rubric")

    assert prompt.content == expected
    assert prompt.prompt_hash == hashlib.sha256(expected.encode("utf-8")).hexdigest()[:12]
    assert prompt.metadata.id == "coverage-rubric"
    assert prompt.metadata.version == "2.0.0"
    assert prompt.metadata.model == "claude-sonnet-4-6"
    assert prompt.metadata.temperature == 0
    assert prompt.metadata.max_tokens == 8000
    assert prompt.metadata.schema_name == "coverage_v2"
    assert prompt.metadata.includes == ("_shared/rules.md",)
    assert prompt.metadata.cache_prefix is True
    assert prompt.metadata.batch_size == 30


def test_load_many_is_an_immutable_job_snapshot(tmp_path: Path) -> None:
    vault = tmp_path / "prompts"
    vault.mkdir()
    path = vault / "lecture-concept-ledger.md"
    path.write_text(
        "---\nid: lecture-concept-ledger\nversion: 2.0.0\n---\n\nFirst body.",
        encoding="utf-8",
    )
    library = AnkiPromptLibrary(vault)

    snapshot = library.load_many(("lecture-concept-ledger",))
    path.write_text(
        "---\nid: lecture-concept-ledger\nversion: 2.0.1\n---\n\nChanged body.",
        encoding="utf-8",
    )

    assert snapshot.require("lecture-concept-ledger").content == "First body."
    assert snapshot.require("lecture-concept-ledger").metadata.version == "2.0.0"


def test_missing_prompt_error_identifies_id_path_and_remediation(tmp_path: Path) -> None:
    with pytest.raises(AnkiPromptConfigurationError) as captured:
        AnkiPromptLibrary(tmp_path / "configured-prompts").load("missing-prompt")

    message = str(captured.value)
    assert "missing-prompt" in message
    assert str(tmp_path / "configured-prompts" / "missing-prompt.md") in message
    assert "Restore" in message
    assert "configure" in message


def test_include_cycle_is_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "prompts"
    vault.mkdir()
    (vault / "first.md").write_text(
        "---\nid: first\nversion: 2.0.0\nincludes:\n  - second.md\n---\n\nFirst.",
        encoding="utf-8",
    )
    (vault / "second.md").write_text(
        "---\nid: second\nversion: 2.0.0\nincludes:\n  - first.md\n---\n\nSecond.",
        encoding="utf-8",
    )

    with pytest.raises(AnkiPromptConfigurationError, match="cycle"):
        AnkiPromptLibrary(vault).load("first")


def test_include_depth_over_three_is_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "prompts"
    vault.mkdir()
    for index in range(1, 6):
        include = (
            f"includes:\n  - p{index + 1}.md\n" if index < 5 else ""
        )
        (vault / f"p{index}.md").write_text(
            f"---\nid: p{index}\nversion: 2.0.0\n{include}---\n\nPrompt {index}.",
            encoding="utf-8",
        )

    with pytest.raises(AnkiPromptConfigurationError, match="depth"):
        AnkiPromptLibrary(vault).load("p1")


def test_prompt_id_must_match_requested_filename(tmp_path: Path) -> None:
    vault = tmp_path / "prompts"
    vault.mkdir()
    (vault / "coverage-rubric.md").write_text(
        "---\nid: wrong-id\nversion: 2.0.0\n---\n\nRubric.",
        encoding="utf-8",
    )

    with pytest.raises(AnkiPromptConfigurationError, match="does not match"):
        AnkiPromptLibrary(vault).load("coverage-rubric")


def test_utf8_bom_does_not_hide_yaml_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "prompts"
    vault.mkdir()
    (vault / "coverage-rubric.md").write_bytes(
        b"\xef\xbb\xbf---\n"
        b"id: coverage-rubric\n"
        b"version: 2.0.0\n"
        b"---\n\nRubric."
    )

    prompt = AnkiPromptLibrary(vault).load("coverage-rubric")

    assert prompt.metadata.id == "coverage-rubric"


def test_gap_generation_prompt_is_quality_first_and_forbids_padding() -> None:
    prompt = (
        Path(__file__).parents[2]
        / "src"
        / "oms_hub"
        / "anki"
        / "prompt_assets"
        / "gap-card-generation.md"
    ).read_text(encoding="utf-8")

    assert "Counts are soft\ntargets, never quotas" in prompt
    assert "Do\nnot split merely to increase card count" in prompt
    assert "do not label a card eligible merely to reach a count" in prompt
    assert "forbidden_cloze_targets_by_fact" in prompt


def test_rendered_card_centric_v2_gap_prompt_is_complete_and_fact_scoped() -> None:
    assets = Path(__file__).parents[2] / "src" / "oms_hub" / "anki" / "prompt_assets"
    prompt = AnkiPromptLibrary(assets).load("card-centric-gap-v2")

    assert prompt.metadata.version == "2.0.4"
    assert prompt.prompt_hash == "c5a11baeb234"
    assert prompt.prompt_hash == hashlib.sha256(prompt.content.encode()).hexdigest()[:12]
    assert "Return every requested fact ID M1 through MN." in prompt.content
    assert "one unresolved row" in prompt.content
    assert "one unsplit generated row with `split: false` and no `split_index`" in prompt.content
    assert "sequential `split_index` values 1 through N" in prompt.content
    assert "Never return both generated and unresolved rows for the same fact." in prompt.content
    assert (
        "Use only the `forbidden_cloze_targets_by_fact` row matching the card's"
        in prompt.content
    )
    assert "Never flatten, union, or globalize targets belonging to unrelated" in prompt.content
    assert "When only legacy `forbidden_cloze_targets[]` is supplied" in prompt.content
    assert "smallest set of the best-supported, highest-yield," in prompt.content
    assert "Counts are soft targets, never quotas." in prompt.content
    assert "split one fact into unnecessary cards" in prompt.content
    assert "label a card eligible merely to reach a count" in prompt.content
    assert (
        "Summary-only support is allowed when it is the best available supplied"
        in prompt.content
    )
    assert "Cite the honest `SUM:` passage and label the card" in prompt.content
    assert "`summary_grounded` downstream" in prompt.content
    assert "never upgrade summary support to primary" in prompt.content
    assert "or inherit emphasis from it" in prompt.content
    assert "prefer and cite the underlying primary passage" in prompt.content
    assert "A card may not be kept on summary support alone." not in prompt.content
    assert (
        "A generated card may not cite a summary passage as its sole evidence."
        not in prompt.content
    )


def test_card_centric_v2_ledger_prompt_preserves_quality_first_sizing_policy() -> None:
    assets = Path(__file__).parents[2] / "src" / "oms_hub" / "anki" / "prompt_assets"
    prompt = AnkiPromptLibrary(assets).load("card-centric-ledger-v2")

    assert prompt.metadata.version == "2.1.2"
    assert prompt.metadata.model is None
    assert prompt.metadata.temperature is None
    assert prompt.content_sha256 == (
        "14fdea3f71f33c61e1a84546d25da29faf0101cb441d8610854f4708a617b9aa"
    )
    assert prompt.prompt_hash == "14fdea3f71f3"
    assert "smallest set of the best-supported, highest-yield," in prompt.content
    assert (
        "60 as a warning floor, 65 as the ordinary target,\nand 70 as a soft cap"
        in prompt.content
    )
    assert "never pad\ncoverage merely to reach a count" in prompt.content
    assert "unique, grounded, high-value\nfact solely because the ordinary target" in prompt.content


@pytest.mark.parametrize(
    "prompt_id",
    ("card-centric-gap-v1", "lecture-concept-ledger", "gap-card-generation"),
)
def test_existing_generation_prompts_retain_strict_summary_authority(
    prompt_id: str,
) -> None:
    assets = Path(__file__).parents[2] / "src" / "oms_hub" / "anki" / "prompt_assets"
    prompt = AnkiPromptLibrary(assets).load(prompt_id)

    assert "A card may not be kept on summary support alone." in prompt.content
    assert "A generated card may not cite a summary passage as its sole evidence." in prompt.content
    assert "Summary-only support is allowed" not in prompt.content
    assert "`summary_grounded` downstream" not in prompt.content


def test_git_sync_fast_forwards_nuc_prompt_checkout(tmp_path: Path) -> None:
    seed, remote, nuc = _prompt_repositories(tmp_path)
    prompt = seed / "coverage-rubric.md"
    prompt.write_text(
        "---\nid: coverage-rubric\nversion: 2.0.1\n---\n\nUpdated rubric.",
        encoding="utf-8",
    )
    _git(seed, "add", "coverage-rubric.md")
    _git(seed, "commit", "-m", "update prompt")
    _git(seed, "push", str(remote), "main")

    result = GitPromptSynchronizer(nuc).sync()

    assert result.stale is False
    assert AnkiPromptLibrary(nuc).load("coverage-rubric").content == (
        "Updated rubric."
    )


def test_git_sync_failure_keeps_last_known_good_checkout(tmp_path: Path) -> None:
    _, _, nuc = _prompt_repositories(tmp_path)
    _git(nuc, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = GitPromptSynchronizer(nuc).sync()

    assert result.stale is True
    assert result.detail
    assert AnkiPromptLibrary(nuc).load("coverage-rubric").content == (
        "Initial rubric."
    )


def _prompt_repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Prompt Test")
    (seed / "coverage-rubric.md").write_text(
        "---\nid: coverage-rubric\nversion: 2.0.0\n---\n\nInitial rubric.",
        encoding="utf-8",
    )
    _git(seed, "add", "coverage-rubric.md")
    _git(seed, "commit", "-m", "initial prompt")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    nuc = tmp_path / "nuc"
    subprocess.run(
        ["git", "clone", str(remote), str(nuc)],
        check=True,
        capture_output=True,
        text=True,
    )
    return seed, remote, nuc


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    )
