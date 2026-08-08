import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_PROMPT_ID = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
_MAX_INCLUDE_DEPTH = 3


class AnkiPromptConfigurationError(ValueError):
    """A versioned Anki prompt file cannot be used safely."""


@dataclass(frozen=True, slots=True)
class PromptSyncResult:
    stale: bool
    detail: str | None


class PromptSynchronizer(Protocol):
    def sync(self) -> PromptSyncResult: ...


class StaticPromptSynchronizer:
    def sync(self) -> PromptSyncResult:
        return PromptSyncResult(stale=False, detail=None)


class GitPromptSynchronizer:
    def __init__(self, checkout: Path, *, timeout_seconds: int = 30) -> None:
        if timeout_seconds < 1:
            raise ValueError("prompt sync timeout must be positive")
        self.checkout = checkout
        self.timeout_seconds = timeout_seconds

    def sync(self) -> PromptSyncResult:
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.checkout), "pull", "--ff-only"],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return PromptSyncResult(
                stale=True,
                detail=f"Git prompt sync failed: {type(error).__name__}",
            )
        if completed.returncode != 0:
            return PromptSyncResult(
                stale=True,
                detail=(
                    "Git prompt sync failed with exit code "
                    f"{completed.returncode}"
                ),
            )
        return PromptSyncResult(stale=False, detail=None)


class PromptMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    version: str = Field(min_length=1, max_length=100)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    response_format: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    includes: tuple[str, ...] = ()
    cache_prefix: bool = False
    batch_size: int | None = Field(default=None, ge=1)
    shared: bool = False


@dataclass(frozen=True, slots=True)
class AnkiPrompt:
    metadata: PromptMetadata
    path: Path
    content: str
    prompt_hash: str
    source_paths: tuple[Path, ...]

    @property
    def version(self) -> str:
        return self.metadata.version


@dataclass(frozen=True, slots=True)
class AnkiPromptSnapshot:
    prompts: tuple[AnkiPrompt, ...]

    def require(self, prompt_id: str) -> AnkiPrompt:
        for prompt in self.prompts:
            if prompt.metadata.id == prompt_id:
                return prompt
        raise AnkiPromptConfigurationError(
            f"Anki prompt {prompt_id} is absent from the job snapshot"
        )


class AnkiPromptLibrary:
    def __init__(self, vault_directory: Path | None = None) -> None:
        self.root = (
            vault_directory
            if vault_directory is not None
            else Path(__file__).with_name("prompt_assets")
        )

    def load(self, prompt_id: str) -> AnkiPrompt:
        normalized = prompt_id.strip().casefold()
        if not _PROMPT_ID.fullmatch(normalized):
            raise AnkiPromptConfigurationError(
                "Anki prompt ID is invalid; use a filename stem containing "
                "only lowercase letters, numbers, and hyphens"
            )
        relative = PurePosixPath(f"{normalized}.md")
        parts, metadata, paths = self._resolve(
            relative,
            depth=0,
            stack=(),
            emitted=set(),
        )
        if metadata.id != normalized:
            raise AnkiPromptConfigurationError(
                f"Anki prompt ID {metadata.id} does not match {normalized}.md"
            )
        content = "\n\n".join(part for part in parts if part).strip()
        if not content:
            raise AnkiPromptConfigurationError(
                f"Anki prompt {normalized} is empty"
            )
        return AnkiPrompt(
            metadata=metadata,
            path=self.root / relative,
            content=content,
            prompt_hash=hashlib.sha256(content.encode("utf-8")).hexdigest()[:12],
            source_paths=tuple(paths),
        )

    def load_many(self, prompt_ids: tuple[str, ...]) -> AnkiPromptSnapshot:
        prompts = tuple(self.load(prompt_id) for prompt_id in prompt_ids)
        ids = [prompt.metadata.id for prompt in prompts]
        if len(ids) != len(set(ids)):
            raise AnkiPromptConfigurationError(
                "Anki job prompt IDs must be unique"
            )
        return AnkiPromptSnapshot(prompts=prompts)

    def _resolve(
        self,
        relative: PurePosixPath,
        *,
        depth: int,
        stack: tuple[PurePosixPath, ...],
        emitted: set[PurePosixPath],
    ) -> tuple[list[str], PromptMetadata, list[Path]]:
        _validate_relative_path(relative)
        if relative in stack:
            chain = " -> ".join(str(item) for item in (*stack, relative))
            raise AnkiPromptConfigurationError(
                f"Anki prompt include cycle detected: {chain}"
            )
        if depth > _MAX_INCLUDE_DEPTH:
            raise AnkiPromptConfigurationError(
                "Anki prompt include depth exceeds three"
            )
        metadata, body, path = _read_markdown(self.root, relative)
        if relative in emitted:
            return [], metadata, []
        emitted.add(relative)
        parts: list[str] = []
        paths: list[Path] = []
        next_stack = (*stack, relative)
        for include in metadata.includes:
            include_relative = PurePosixPath(include)
            include_parts, _, include_paths = self._resolve(
                include_relative,
                depth=depth + 1,
                stack=next_stack,
                emitted=emitted,
            )
            parts.extend(include_parts)
            paths.extend(include_paths)
        parts.append(body)
        paths.append(path)
        return parts, metadata, paths


def _read_markdown(
    root: Path,
    relative: PurePosixPath,
) -> tuple[PromptMetadata, str, Path]:
    path = root.joinpath(*relative.parts)
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise AnkiPromptConfigurationError(
            f"Anki prompt {relative} was not found or is not readable UTF-8"
        ) from error
    frontmatter, body = _split_frontmatter(content, relative)
    try:
        metadata = PromptMetadata.model_validate(_parse_frontmatter(frontmatter))
    except (ValidationError, ValueError) as error:
        raise AnkiPromptConfigurationError(
            f"Anki prompt {relative} has invalid frontmatter: {error}"
        ) from error
    if not body.strip():
        raise AnkiPromptConfigurationError(
            f"Anki prompt {relative} has an empty body"
        )
    return metadata, body.strip(), path


def _split_frontmatter(
    content: str,
    relative: PurePosixPath,
) -> tuple[str, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AnkiPromptConfigurationError(
            f"Anki prompt {relative} is missing YAML frontmatter; "
            "the file must begin with ---"
        )
    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as error:
        raise AnkiPromptConfigurationError(
            f"Anki prompt {relative} has unclosed YAML frontmatter"
        ) from error
    return "\n".join(lines[1:closing]), "\n".join(lines[closing + 1 :])


def _parse_frontmatter(frontmatter: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    list_key: str | None = None
    for line in frontmatter.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("-"):
            if list_key is None:
                raise ValueError("list item has no field")
            values[list_key].append(_scalar(stripped[1:].strip()))
            continue
        if line[:1].isspace() or ":" not in line:
            raise ValueError(f"unsupported frontmatter line: {line}")
        key, raw = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError("frontmatter field name is empty")
        if not raw.strip():
            values[key] = []
            list_key = key
        else:
            values[key] = _scalar(raw.strip())
            list_key = None
    return values


def _scalar(value: str) -> str | bool | int | float:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    return value


def _validate_relative_path(relative: PurePosixPath) -> None:
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.suffix.casefold() != ".md"
    ):
        raise AnkiPromptConfigurationError(
            f"Anki prompt include path is unsafe: {relative}"
        )
