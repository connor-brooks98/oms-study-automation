from pathlib import Path

import numpy as np
import pytest

from oms_hub.anki.embeddings import AtomicVectorStore, normalize_vectors


def test_normalize_vectors_rejects_zero_rows() -> None:
    with pytest.raises(ValueError, match="zero-length"):
        normalize_vectors(np.asarray([[0.0, 0.0]], dtype=np.float32))


def test_vector_matrix_and_note_order_are_replaced_together(tmp_path: Path) -> None:
    store = AtomicVectorStore(tmp_path)
    store.replace(
        [10, 20],
        np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    store.replace(
        [20, 30, 40],
        np.asarray(
            [[0.0, 1.0], [0.6, 0.8], [1.0, 0.0]],
            dtype=np.float32,
        ),
    )

    note_ids, vectors = store.load()

    assert note_ids == [20, 30, 40]
    np.testing.assert_array_equal(
        vectors,
        np.asarray(
            [[0.0, 1.0], [0.6, 0.8], [1.0, 0.0]],
            dtype=np.float32,
        ),
    )
    assert not list(tmp_path.glob("*.tmp"))
    assert not (tmp_path / ".vectors.lock").exists()
