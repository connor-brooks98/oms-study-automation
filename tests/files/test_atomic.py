import shutil

import pytest

from oms_hub.files.atomic import sha256_file, verified_atomic_copy


def test_verified_copy_creates_parent_and_preserves_checksum(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"complete")
    destination = tmp_path / "nested/final.bin"
    digest = verified_atomic_copy(source, destination)
    assert destination.read_bytes() == b"complete"
    assert digest == sha256_file(destination)
    assert not list(destination.parent.glob("*.partial-*"))


def test_failed_copy_leaves_existing_destination_unchanged(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "final.bin"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    def fail_copy(source_path, destination_path):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        verified_atomic_copy(source, destination)
    assert destination.read_bytes() == b"old"
