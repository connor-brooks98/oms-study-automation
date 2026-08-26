import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from oms_hub.study_generation.domain import PromptKind, PromptSnapshot
from oms_hub.study_generation.repository import GenerationRepository


class PromptConfigurationError(ValueError):
    pass


_OUTLINE_OUTPUT_CONTRACT = """

STUDY HUB OUTLINE CONTRACT
Use only the selected lecture slides and cleaned transcript. Do not use prior
conversation context or information from other lectures. Prioritize learning
objectives, testable distinctions, mechanisms, diagnoses, and treatments over
repetition. Target 4,500-5,500 characters and never exceed 5,500 characters;
shorter is allowed when the selected lecture does not support that much content.
"""


def outline_prompt(prompt: PromptSnapshot) -> PromptSnapshot:
    return replace(
        prompt,
        content=f"{prompt.content.rstrip()}\n{_OUTLINE_OUTPUT_CONTRACT}",
    )


class PromptFileService:
    def __init__(self, repository: GenerationRepository):
        self.repository = repository

    def inspect(self, kind: PromptKind) -> PromptSnapshot:
        configured = self.repository.prompt_path(kind)
        if configured is None:
            raise PromptConfigurationError(
                f"{kind.value} prompt path is not configured"
            )
        path = Path(os.path.expandvars(configured)).expanduser()
        if not path.is_file():
            raise PromptConfigurationError(
                f"{kind.value} prompt file was not found"
            )
        try:
            payload = path.read_bytes()
            content = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise PromptConfigurationError(
                f"{kind.value} prompt file is not readable UTF-8"
            ) from error
        if not content.strip():
            raise PromptConfigurationError(
                f"{kind.value} prompt file is empty"
            )
        modified_at = datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=UTC,
        ).isoformat()
        sha256 = hashlib.sha256(payload).hexdigest()
        self.repository.record_prompt_validation(
            kind,
            sha256,
            modified_at,
        )
        return PromptSnapshot(path, content, sha256, modified_at)
