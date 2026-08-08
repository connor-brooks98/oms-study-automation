from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

InputType = Literal["document", "query"]
FloatMatrix = NDArray[np.float32]
HalfMatrix = NDArray[np.float16]


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    note_id: int
    text: str
    content_hash: str


class SemanticManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: UUID
    model: str
    dimensions: int
    created_at: datetime
    note_ids: tuple[int, ...]
    content_hashes: tuple[str, ...]
    matrix_sha256: str


@dataclass(frozen=True, slots=True)
class SemanticSnapshot:
    manifest: SemanticManifest
    matrix: HalfMatrix

    def row_for(self, note_id: int) -> int:
        try:
            return self.manifest.note_ids.index(note_id)
        except ValueError as exc:
            raise KeyError(note_id) from exc


@dataclass(frozen=True, slots=True)
class SemanticHit:
    note_id: int
    score: float
    content_hash: str


@dataclass(frozen=True, slots=True)
class SemanticRefreshResult:
    manifest: SemanticManifest
    reused_count: int
    embedded_count: int
    deleted_count: int
    coverage: float


class SemanticGenerationMismatchError(ValueError):
    """The active semantic generation differs from a caller's pinned input.

    This is an input-integrity failure, distinct from embedding-provider
    unavailability.  I0 maps it to the established nonretryable
    ``PinnedInputChanged`` terminal outcome before a stage performs work.
    """


class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix: ...
