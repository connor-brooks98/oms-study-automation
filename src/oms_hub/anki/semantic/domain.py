from collections.abc import Sequence
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

InputType = Literal["document", "query"]
FloatMatrix = NDArray[np.float32]


class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix: ...
