from pathlib import Path

import pytest

from oms_hub.files.atomic import verified_atomic_write


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
