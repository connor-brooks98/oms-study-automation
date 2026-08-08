import hashlib
from pathlib import Path

import numpy as np
import pytest

from oms_hub.anki.semantic.domain import DocumentRecord
from oms_hub.anki.semantic.store import (
    SemanticSnapshotError,
    SemanticSnapshotStore,
)


def _record(note_id: int, text: str) -> DocumentRecord:
    return DocumentRecord(
        note_id=note_id,
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def test_store_round_trips_ordered_float16_snapshot(tmp_path: Path) -> None:
    store = SemanticSnapshotStore(tmp_path / "semantic")
    records = [_record(20, "second"), _record(10, "first")]
    vectors = np.asarray(
        [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
        dtype=np.float32,
    )

    manifest = store.replace(
        records,
        vectors,
        model="voyage-4-large",
    )
    snapshot = store.load(
        expected_model="voyage-4-large",
        expected_dimensions=3,
    )

    assert manifest == snapshot.manifest
    assert snapshot.manifest.note_ids == (10, 20)
    assert snapshot.manifest.content_hashes == (
        records[1].content_hash,
        records[0].content_hash,
    )
    assert snapshot.matrix.dtype == np.float16
    assert snapshot.matrix.flags.writeable is False
    np.testing.assert_array_equal(
        snapshot.matrix,
        np.asarray(
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float16,
        ),
    )
    assert snapshot.row_for(20) == 1


def test_store_rejects_duplicate_ids_and_wrong_vector_shape(
    tmp_path: Path,
) -> None:
    store = SemanticSnapshotStore(tmp_path / "semantic")
    records = [_record(10, "first"), _record(10, "duplicate")]

    with pytest.raises(SemanticSnapshotError, match="unique"):
        store.replace(
            records,
            np.ones((2, 3), dtype=np.float32),
            model="voyage-4-large",
        )
    with pytest.raises(SemanticSnapshotError, match="row count"):
        store.replace(
            [_record(10, "first")],
            np.ones((2, 3), dtype=np.float32),
            model="voyage-4-large",
        )


def test_store_rejects_corrupt_matrix_and_incompatible_load(
    tmp_path: Path,
) -> None:
    store = SemanticSnapshotStore(tmp_path / "semantic")
    manifest = store.replace(
        [_record(10, "first")],
        np.ones((1, 3), dtype=np.float32),
        model="voyage-4-large",
    )

    with pytest.raises(SemanticSnapshotError, match="model"):
        store.load(expected_model="voyage-other")
    with pytest.raises(SemanticSnapshotError, match="dimensions"):
        store.load(expected_dimensions=2)

    matrix_path = (
        store.root
        / "generations"
        / str(manifest.generation)
        / "vectors.npy"
    )
    matrix_path.write_bytes(matrix_path.read_bytes() + b"corruption")
    with pytest.raises(SemanticSnapshotError, match="checksum"):
        store.load()


def test_interrupted_publish_keeps_prior_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SemanticSnapshotStore(tmp_path / "semantic")
    first = store.replace(
        [_record(10, "first")],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
        model="voyage-4-large",
    )
    real_replace = __import__("os").replace

    def fail_current_pointer(source: str | Path, target: str | Path) -> None:
        if Path(target).name == "CURRENT":
            raise OSError("injected publish interruption")
        real_replace(source, target)

    monkeypatch.setattr("os.replace", fail_current_pointer)

    with pytest.raises(OSError, match="interruption"):
        store.replace(
            [_record(20, "second")],
            np.asarray([[0.0, 1.0]], dtype=np.float32),
            model="voyage-4-large",
        )

    loaded = store.load()
    assert loaded.manifest.generation == first.generation
    assert loaded.manifest.note_ids == (10,)


def test_open_reader_remains_usable_after_replacement(tmp_path: Path) -> None:
    store = SemanticSnapshotStore(tmp_path / "semantic")
    store.replace(
        [_record(10, "first")],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
        model="voyage-4-large",
    )
    old_reader = store.load()

    store.replace(
        [_record(20, "second")],
        np.asarray([[0.0, 1.0]], dtype=np.float32),
        model="voyage-4-large",
    )

    assert old_reader.manifest.note_ids == (10,)
    np.testing.assert_array_equal(
        old_reader.matrix,
        np.asarray([[1.0, 0.0]], dtype=np.float16),
    )
    assert store.load().manifest.note_ids == (20,)
