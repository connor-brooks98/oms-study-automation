from pathlib import Path

import pytest

import oms_hub.files.atomic as atomic_module
from oms_hub.files.atomic import (
    atomic_copy_temporary_path,
    verified_atomic_copy,
    verified_atomic_write,
)


def _windows_limited_destination(tmp_path: Path) -> Path:
    parent = tmp_path / "media"
    parent.mkdir()
    target_length = 250
    filename_length = target_length - len(str(parent)) - 1 - len(".pdf")
    destination = parent / ("a" * filename_length + ".pdf")
    assert len(str(destination)) == target_length
    assert len(destination.name) < 255
    return destination


def test_atomic_copy_uses_short_same_parent_temporary_for_windows_path_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    destination = _windows_limited_destination(tmp_path)
    temporary = atomic_copy_temporary_path(destination)
    assert temporary.parent == destination.parent
    assert len(temporary.name) < 64
    assert len(str(temporary)) < 260

    copied_targets: list[Path] = []
    original_copy2 = atomic_module.shutil.copy2

    def windows_limited_copy2(source: Path, target: Path, *args: object, **kwargs: object):
        assert len(str(target)) < 260
        copied_targets.append(target)
        return original_copy2(source, target, *args, **kwargs)

    monkeypatch.setattr(atomic_module.shutil, "copy2", windows_limited_copy2)

    assert verified_atomic_copy(source, destination) == atomic_module.sha256_file(source)
    assert destination.read_bytes() == b"source"
    assert copied_targets[0].parent == destination.parent
    assert len(copied_targets[0].name) < 64
    assert not copied_targets[0].exists()


def test_atomic_copy_skips_existing_temporary_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination.pdf"
    collision = tmp_path / ".oms-copy-collision.tmp"
    fresh = tmp_path / ".oms-copy-fresh.tmp"
    source.write_bytes(b"source")
    collision.write_bytes(b"another writer")
    candidates = iter((collision, fresh))
    monkeypatch.setattr(
        atomic_module,
        "atomic_copy_temporary_path",
        lambda _destination: next(candidates),
    )

    verified_atomic_copy(source, destination)

    assert collision.read_bytes() == b"another writer"
    assert destination.read_bytes() == b"source"
    assert not fresh.exists()


@pytest.mark.parametrize("copy_succeeds", (True, False))
def test_atomic_copy_cleans_its_reserved_temporary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copy_succeeds: bool
) -> None:
    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination.pdf"
    temporary = tmp_path / ".oms-copy-reserved.tmp"
    source.write_bytes(b"source")
    monkeypatch.setattr(atomic_module, "atomic_copy_temporary_path", lambda _destination: temporary)
    if not copy_succeeds:
        monkeypatch.setattr(
            atomic_module.shutil,
            "copy2",
            lambda _source, _destination: (_ for _ in ()).throw(OSError("copy failed")),
        )
        with pytest.raises(OSError, match="copy failed"):
            verified_atomic_copy(source, destination)
    else:
        verified_atomic_copy(source, destination)

    assert not temporary.exists()


def test_atomic_write_keeps_temporary_path_below_windows_max_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "media"
    parent.mkdir()
    target_length = 218
    filename_length = target_length - len(str(parent)) - 1 - len(".png")
    destination = parent / ("a" * filename_length + ".png")
    assert len(str(destination)) == target_length

    original_open = Path.open

    def windows_limited_open(path: Path, *args: object, **kwargs: object):
        if len(str(path)) >= 260:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", windows_limited_open)

    digest = verified_atomic_write(b"image payload", destination)

    assert digest == "e621ba2e9edf5bc3699d11b224651352d5e729647842f2f7fd9f4a67c3e2c02e"
    assert destination.read_bytes() == b"image payload"
