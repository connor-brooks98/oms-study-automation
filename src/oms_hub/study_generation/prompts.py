import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from oms_hub.study_generation.domain import PromptKind, PromptSnapshot
from oms_hub.study_generation.repository import GenerationRepository


class PromptConfigurationError(ValueError):
    pass


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
