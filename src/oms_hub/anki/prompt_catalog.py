from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from oms_hub.anki.prompts import (
    AnkiPrompt,
    AnkiPromptConfigurationError,
    AnkiPromptLibrary,
    AnkiPromptSnapshot,
)


class PromptRole(StrEnum):
    LCL = "lcl"
    COVERAGE = "coverage"
    GAP_CARDS = "gap_cards"


_SCHEMA_ROLES: dict[str, PromptRole] = {
    "lcl_v1": PromptRole.LCL,
    "lcl_v2": PromptRole.LCL,
    "coverage_v1": PromptRole.COVERAGE,
    "coverage_v2": PromptRole.COVERAGE,
    "gap_cards_v1": PromptRole.GAP_CARDS,
    "gap_cards_v2": PromptRole.GAP_CARDS,
}
_SYSTEM_PROMPT_IDS = ("card-relevance-audit", "paraphrase-expansion")
_CARD_CENTRIC_INTERNAL_PROMPT_IDS = (
    "card-centric-ledger-v1",
    "card-centric-ledger-v2",
    "card-centric-classifier",
    "card-centric-classifier-v1",
    "card-centric-fast-classifier",
    "card-centric-gap-v1",
    "card-centric-gap-v2",
)
_CARD_CENTRIC_V2_PROMPT_IDS = (
    "card-centric-ledger-v2",
    "card-centric-fast-classifier",
    "card-centric-classifier",
    "card-centric-gap-v2",
)


@dataclass(frozen=True, slots=True)
class PromptChoice:
    id: str
    version: str
    prompt_hash: str
    schema_name: str

    @property
    def label(self) -> str:
        return f"{self.id} — v{self.version} · {self.prompt_hash}"

    def payload(self) -> dict[str, str]:
        return {
            "id": self.id,
            "version": self.version,
            "prompt_hash": self.prompt_hash,
            "schema": self.schema_name,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class PromptCatalogIssue:
    path: Path
    message: str

    def payload(self) -> dict[str, str]:
        return {"path": str(self.path), "message": self.message}


@dataclass(frozen=True, slots=True)
class PromptCatalog:
    root: Path
    choices: Mapping[PromptRole, tuple[PromptChoice, ...]]
    issues: tuple[PromptCatalogIssue, ...]

    @property
    def choice_count(self) -> int:
        return sum(len(values) for values in self.choices.values())

    @property
    def ready(self) -> bool:
        return all(self.choices.get(role) for role in PromptRole)

    def payload(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "ready": self.ready,
            "choice_count": self.choice_count,
            "choices": {
                role.value: [choice.payload() for choice in self.choices[role]]
                for role in PromptRole
            },
            "issues": [issue.payload() for issue in self.issues],
        }


class AnkiPromptCatalogService:
    def __init__(
        self,
        directory_provider: Callable[[], Path | None] | None = None,
        *,
        fallback_directory: Path | None = None,
        bundled_directory: Path | None = None,
    ) -> None:
        default = Path(__file__).with_name("prompt_assets")
        self.directory_provider = directory_provider or (lambda: None)
        self.fallback_directory = fallback_directory or default
        self.bundled_directory = bundled_directory or default

    def active_directory(self) -> Path:
        configured = self.directory_provider()
        return configured if configured is not None else self.fallback_directory

    def catalog(self) -> PromptCatalog:
        root = self.active_directory()
        choices: dict[PromptRole, list[PromptChoice]] = {role: [] for role in PromptRole}
        issues: list[PromptCatalogIssue] = []
        if not root.is_dir():
            issues.append(PromptCatalogIssue(root, "Prompt directory is unavailable"))
            return PromptCatalog(
                root=root,
                choices={role: () for role in PromptRole},
                issues=tuple(issues),
            )
        library = AnkiPromptLibrary(root)
        seen: set[str] = set()
        for path in sorted(root.glob("*.md"), key=lambda item: item.name.casefold()):
            try:
                prompt = library.load(path.stem)
            except AnkiPromptConfigurationError as error:
                issues.append(PromptCatalogIssue(path, str(error)))
                continue
            if prompt.metadata.shared or prompt.metadata.id in (
                *_SYSTEM_PROMPT_IDS,
                *_CARD_CENTRIC_INTERNAL_PROMPT_IDS,
            ):
                continue
            prompt_id = prompt.metadata.id.casefold()
            if prompt_id in seen:
                issues.append(PromptCatalogIssue(path, f"Duplicate prompt ID {prompt.metadata.id}"))
                continue
            seen.add(prompt_id)
            schema = prompt.metadata.schema_name or ""
            role = _SCHEMA_ROLES.get(schema)
            if role is None:
                issues.append(
                    PromptCatalogIssue(path, f"Unsupported prompt schema {schema or '(missing)'}")
                )
                continue
            choices[role].append(_choice(prompt))
        return PromptCatalog(
            root=root,
            choices={
                role: tuple(sorted(values, key=lambda choice: choice.id.casefold()))
                for role, values in choices.items()
            },
            issues=tuple(issues),
        )

    def load_job_snapshot(
        self,
        *,
        lcl_id: str,
        coverage_id: str,
        gap_id: str,
    ) -> AnkiPromptSnapshot:
        library = AnkiPromptLibrary(self.active_directory())
        selected = (
            (PromptRole.LCL, library.load(lcl_id)),
            (PromptRole.COVERAGE, library.load(coverage_id)),
            (PromptRole.GAP_CARDS, library.load(gap_id)),
        )
        for expected, prompt in selected:
            actual = _SCHEMA_ROLES.get(prompt.metadata.schema_name or "")
            if actual is not expected:
                raise AnkiPromptConfigurationError(
                    f"Selected {expected.name} prompt has an unsupported role"
                )
        system = AnkiPromptLibrary(self.bundled_directory).load_many(_SYSTEM_PROMPT_IDS)
        prompts = (
            selected[0][1],
            selected[1][1],
            system.require("card-relevance-audit"),
            selected[2][1],
            system.require("paraphrase-expansion"),
        )
        ids = [prompt.metadata.id for prompt in prompts]
        if len(ids) != len(set(ids)):
            raise AnkiPromptConfigurationError("Anki job prompt IDs must be unique")
        return AnkiPromptSnapshot(prompts=prompts)

    def load_card_centric_v2_snapshot(self) -> AnkiPromptSnapshot:
        """Resolve all bundled v2 prompts into immutable exact-content values.

        This is the single catalog entry point for the S0 v2 prompt capture.
        It resolves includes before returning, so ``content`` and
        ``content_sha256`` are the bytes logically executed by the stages, not
        live asset names or unexpanded Markdown fragments.
        """
        return AnkiPromptLibrary(self.bundled_directory).load_many(
            _CARD_CENTRIC_V2_PROMPT_IDS
        )


def _choice(prompt: AnkiPrompt) -> PromptChoice:
    schema = prompt.metadata.schema_name
    if schema is None:
        raise AnkiPromptConfigurationError("Selectable prompt schema is missing")
    return PromptChoice(
        id=prompt.metadata.id,
        version=prompt.metadata.version,
        prompt_hash=prompt.prompt_hash,
        schema_name=schema,
    )
