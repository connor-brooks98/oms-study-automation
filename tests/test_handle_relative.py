from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path

import pytest

import oms_hub.files.handle_relative as hardened


class FakeWindowsAPI:
    """In-memory Win32 facade: tests exercise the native call contract on macOS."""

    def __init__(self, directories: set[str], files: dict[str, bytes]) -> None:
        self.directories = directories
        self.files = files
        self.attributes: dict[str, int] = {}
        self.calls: list[tuple[str, object]] = []
        self.open_calls: list[tuple[str, int, int, int, int]] = []
        self.handles: dict[int, tuple[str, int]] = {}
        self.closed: list[int] = []
        self._next_handle = 100
        self.before_move: callable | None = None

    @staticmethod
    def _error(operation: str, code: int) -> OSError:
        return hardened._Win32CallError(operation, code)

    def open_file(self, path: str, access: int, share: int, creation: int, flags: int) -> int:
        self.open_calls.append((path, access, share, creation, flags))
        if creation == hardened._OPEN_EXISTING and path not in self.directories | self.files.keys():
            raise self._error("CreateFileW", hardened._ERROR_FILE_NOT_FOUND)
        if creation == hardened._CREATE_NEW:
            if path in self.directories | self.files.keys():
                raise self._error("CreateFileW", hardened._ERROR_FILE_EXISTS)
            self.files[path] = b""
        handle = self._next_handle
        self._next_handle += 1
        self.handles[handle] = (path, 0)
        return handle

    def attribute_tag(self, handle: int) -> int:
        path, _offset = self.handles[handle]
        return self.attributes.get(
            path,
            hardened._FILE_ATTRIBUTE_DIRECTORY if path in self.directories else 0,
        )

    def create_directory(self, path: str) -> None:
        self.calls.append(("CreateDirectoryW", path))
        if path in self.directories:
            raise self._error("CreateDirectoryW", hardened._ERROR_ALREADY_EXISTS)
        self.directories.add(path)

    def read_file(self, handle: int, size: int) -> bytes:
        path, offset = self.handles[handle]
        content = self.files[path]
        chunk = content[offset : offset + size]
        self.handles[handle] = (path, offset + len(chunk))
        return chunk

    def write_file(self, handle: int, data: bytes) -> None:
        path, _offset = self.handles[handle]
        self.files[path] += data

    def flush_file(self, handle: int) -> None:
        self.calls.append(("FlushFileBuffers", self.handles[handle][0]))

    def move_file(self, source: str, destination: str, flags: int) -> None:
        self.calls.append(("MoveFileExW", (source, destination, flags)))
        if self.before_move is not None:
            self.before_move()
        if destination in self.files and not flags & hardened._MOVEFILE_REPLACE_EXISTING:
            raise self._error("MoveFileExW", hardened._ERROR_ALREADY_EXISTS)
        self.files[destination] = self.files.pop(source)

    def delete_file(self, path: str) -> None:
        self.calls.append(("DeleteFileW", path))
        if path not in self.files:
            raise self._error("DeleteFileW", hardened._ERROR_FILE_NOT_FOUND)
        del self.files[path]

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)
        self.handles.pop(handle)


def _fixture_paths() -> tuple[Path, Path, Path, FakeWindowsAPI]:
    root = Path("/trusted/root")
    source = Path("/source/incoming.bin")
    destination = root / "nested" / "artifact.bin"
    api = FakeWindowsAPI(
        {"/", "/trusted", "/trusted/root", "/source"}, {str(source): b"pinned bytes"}
    )
    return root, source, destination, api


def test_posix_pinned_parent_copy_survives_parent_symlink_swap(tmp_path: Path) -> None:
    if hardened.sys.platform == "win32":
        pytest.skip("POSIX dir-fd behavior")
    root = tmp_path / "root"
    parent = root / "managed"
    parent.mkdir(parents=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"pinned bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved"
    swapped = False

    def hook(name: str) -> None:
        nonlocal swapped
        if name == "destination-parent-pinned" and not swapped:
            swapped = True
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)

    hardened.set_hardened_write_hook(hook)
    try:
        assert hardened.hardened_verified_copy(
            source,
            parent / "artifact.bin",
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=True,
        ) == hashlib.sha256(b"pinned bytes").hexdigest()
    finally:
        hardened.set_hardened_write_hook(None)
    assert (moved / "artifact.bin").read_bytes() == b"pinned bytes"
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(hardened.sys.platform == "win32", reason="POSIX dir-fd behavior")
def test_posix_pinned_source_parent_survives_substitution_before_source_open(
    tmp_path: Path,
) -> None:
    """The source leaf is opened through the already-pinned source parent fd."""
    destination_root = tmp_path / "destination-root"
    destination_root.mkdir()
    source_root = tmp_path / "input-root"
    source_parent = source_root / "incoming"
    source_parent.mkdir(parents=True)
    source = source_parent / "source.bin"
    source.write_bytes(b"validated source bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / source.name).write_bytes(b"attacker replacement bytes")
    moved = tmp_path / "incoming-pinned"
    source_pinned = False

    def hook(name: str) -> None:
        nonlocal source_pinned
        if name != "source-parent-pinned":
            return
        source_pinned = True
        # The source parent is now pinned, but the source leaf remains unopened.
        # Swapping its pathname here must not redirect the leaf open.
        source_parent.rename(moved)
        source_parent.symlink_to(outside, target_is_directory=True)

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    hardened.set_hardened_write_hook(hook)
    try:
        assert hardened.hardened_verified_copy(
            source,
            destination_root / "artifact.bin",
            destination_root,
            digest,
            replace=True,
            source_root=source_root,
        ) == digest
    finally:
        hardened.set_hardened_write_hook(None)

    assert source_pinned
    assert (destination_root / "artifact.bin").read_bytes() == b"validated source bytes"
    assert (moved / source.name).read_bytes() == b"validated source bytes"
    assert (outside / source.name).read_bytes() == b"attacker replacement bytes"


@pytest.mark.skipif(hardened.sys.platform == "win32", reason="POSIX dir-fd behavior")
@pytest.mark.parametrize("existing", [b"pinned bytes", b"different bytes"])
def test_posix_immutable_copy_is_idempotent_but_never_overwrites(
    tmp_path: Path, existing: bytes
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = tmp_path / "source.bin"
    destination = root / "artifact.bin"
    source.write_bytes(b"pinned bytes")
    destination.write_bytes(existing)
    digest = hashlib.sha256(b"pinned bytes").hexdigest()

    if existing == b"pinned bytes":
        assert hardened.hardened_verified_copy(
            source, destination, root, digest, replace=False
        ) == digest
    else:
        with pytest.raises(hardened.HardenedWriteError, match="different bytes"):
            hardened.hardened_verified_copy(source, destination, root, digest, replace=False)
    assert destination.read_bytes() == existing


@pytest.mark.skipif(hardened.sys.platform == "win32", reason="POSIX dir-fd behavior")
@pytest.mark.parametrize("raced", [b"pinned bytes", b"raced different bytes"])
def test_posix_immutable_publication_handles_destination_race_without_overwrite(
    tmp_path: Path, raced: bytes
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = tmp_path / "source.bin"
    destination = root / "artifact.bin"
    source.write_bytes(b"pinned bytes")
    digest = hashlib.sha256(b"pinned bytes").hexdigest()

    def race(name: str) -> None:
        if name == "before-pinned-replace":
            destination.write_bytes(raced)

    hardened.set_hardened_write_hook(race)
    try:
        if raced == b"pinned bytes":
            assert hardened.hardened_verified_copy(
                source, destination, root, digest, replace=False
            ) == digest
        else:
            with pytest.raises(hardened.HardenedWriteError, match="appeared"):
                hardened.hardened_verified_copy(source, destination, root, digest, replace=False)
    finally:
        hardened.set_hardened_write_hook(None)
    assert destination.read_bytes() == raced


@pytest.mark.skipif(hardened.sys.platform == "win32", reason="POSIX dir-fd behavior")
def test_posix_copy_completes_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = tmp_path / "source.bin"
    destination = root / "artifact.bin"
    source.write_bytes(b"partial writes must be completed")
    writes: list[int] = []
    original_write = hardened.os.write

    def partial_write(fd: int, data: bytes) -> int:
        writes.append(len(data))
        return original_write(fd, data[:3])

    monkeypatch.setattr(hardened.os, "write", partial_write)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert (
        hardened.hardened_verified_copy(source, destination, root, digest, replace=True)
        == digest
    )
    assert len(writes) > 1
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.skipif(hardened.sys.platform == "win32", reason="POSIX dir-fd behavior")
@pytest.mark.parametrize("operation", ["read", "write", "fsync", "link", "replace"])
def test_posix_native_copy_failures_normalize_to_hardened_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"pinned bytes")
    destination = root / "artifact.bin"
    target = {
        "read": "read", "write": "write", "fsync": "fsync", "link": "link", "replace": "replace"
    }[operation]
    original = getattr(hardened.os, target)

    def fail(*args, **kwargs):
        raise PermissionError("forced native failure")

    monkeypatch.setattr(hardened.os, target, fail)
    with pytest.raises(hardened.HardenedWriteError) as error:
        hardened.hardened_verified_copy(
            source,
            destination,
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=operation == "replace",
        )
    assert isinstance(error.value.__cause__, PermissionError)
    monkeypatch.setattr(hardened.os, target, original)


def test_windows_copy_pins_both_chains_and_uses_native_handles() -> None:
    root, source, destination, api = _fixture_paths()
    digest = hashlib.sha256(b"pinned bytes").hexdigest()

    assert hardened._copy_windows(
        source, destination, root, digest, replace=True, source_root=None, api=api
    ) == digest

    assert api.files[str(destination)] == b"pinned bytes"
    assert {share for _path, _access, share, _creation, _flags in api.open_calls} == {
        hardened._FILE_SHARE_READ | hardened._FILE_SHARE_WRITE
    }
    directory_calls = [
        call
        for call in api.open_calls
        if call[1] == hardened._FILE_LIST_DIRECTORY | hardened._FILE_READ_ATTRIBUTES
    ]
    assert [call[0] for call in directory_calls] == [
        "/",
        "/trusted",
        "/trusted/root",
        "/trusted/root/nested",
        "/trusted/root/nested",
        "/",
        "/source",
    ]
    assert ("CreateDirectoryW", "/trusted/root/nested") in api.calls
    assert any(call[1] == hardened._GENERIC_READ for call in api.open_calls)
    assert any(call[3] == hardened._CREATE_NEW for call in api.open_calls)
    assert any(
        call[0] == str(destination) and call[3] == hardened._OPEN_EXISTING
        for call in api.open_calls
    )
    move = next(value for name, value in api.calls if name == "MoveFileExW")
    assert isinstance(move, tuple)
    assert move[2] == hardened._MOVEFILE_REPLACE_EXISTING | hardened._MOVEFILE_WRITE_THROUGH
    assert not api.handles
    assert len(api.closed) == len(set(api.closed))


def test_windows_reparse_component_is_rejected_by_handle_and_closed() -> None:
    root, source, destination, api = _fixture_paths()
    api.attributes["/trusted/root"] = (
        hardened._FILE_ATTRIBUTE_DIRECTORY | hardened._FILE_ATTRIBUTE_REPARSE_POINT
    )

    with pytest.raises(hardened.HardenedWriteError, match="reparse point"):
        hardened._copy_windows(
            source,
            destination,
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=True,
            source_root=None,
            api=api,
        )

    assert not api.handles
    assert api.closed
    assert str(destination) not in api.files


def test_windows_immutable_copy_does_not_overwrite_destination_that_appears() -> None:
    root, source, destination, api = _fixture_paths()
    api.before_move = lambda: api.files.setdefault(str(destination), b"attacker bytes")

    with pytest.raises(hardened.HardenedWriteError, match="destination appeared"):
        hardened._copy_windows(
            source,
            destination,
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=False,
            source_root=None,
            api=api,
        )

    assert api.files[str(destination)] == b"attacker bytes"
    move = next(value for name, value in api.calls if name == "MoveFileExW")
    assert isinstance(move, tuple)
    assert move[2] == hardened._MOVEFILE_WRITE_THROUGH
    assert not api.handles


def test_windows_immutable_same_hash_existing_or_race_is_idempotent() -> None:
    root, source, destination, api = _fixture_paths()
    digest = hashlib.sha256(b"pinned bytes").hexdigest()
    api.files[str(destination)] = b"pinned bytes"
    assert hardened._copy_windows(
        source, destination, root, digest, replace=False, source_root=None, api=api
    ) == digest
    assert not [value for name, value in api.calls if name == "MoveFileExW"]

    root, source, destination, api = _fixture_paths()
    api.before_move = lambda: api.files.setdefault(str(destination), b"pinned bytes")
    assert hardened._copy_windows(
        source, destination, root, digest, replace=False, source_root=None, api=api
    ) == digest
    assert api.files[str(destination)] == b"pinned bytes"
    assert not api.handles


def test_windows_immutable_existing_different_hash_fails_without_move() -> None:
    root, source, destination, api = _fixture_paths()
    api.files[str(destination)] = b"different bytes"
    with pytest.raises(hardened.HardenedWriteError, match="different bytes"):
        hardened._copy_windows(
            source,
            destination,
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=False,
            source_root=None,
            api=api,
        )
    assert api.files[str(destination)] == b"different bytes"
    assert not [value for name, value in api.calls if name == "MoveFileExW"]


def test_native_windows_api_declares_complete_ctypes_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Function:
        argtypes: object | None = None
        restype: object | None = None

        def __call__(self, *_args: object) -> int:
            return 1

    class Kernel32:
        CreateFileW = Function()
        GetFileInformationByHandleEx = Function()
        CreateDirectoryW = Function()
        ReadFile = Function()
        WriteFile = Function()
        FlushFileBuffers = Function()
        MoveFileExW = Function()
        DeleteFileW = Function()
        CloseHandle = Function()

    kernel32 = Kernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    hardened._NativeWindowsAPI()

    assert kernel32.CreateFileW.argtypes == [
        hardened.wintypes.LPCWSTR,
        hardened.wintypes.DWORD,
        hardened.wintypes.DWORD,
        ctypes.POINTER(hardened._SecurityAttributes),
        hardened.wintypes.DWORD,
        hardened.wintypes.DWORD,
        hardened.wintypes.HANDLE,
    ]
    assert kernel32.GetFileInformationByHandleEx.argtypes == [
        hardened.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.POINTER(hardened._FileAttributeTagInfo),
        hardened.wintypes.DWORD,
    ]
    assert kernel32.CreateDirectoryW.argtypes == [
        hardened.wintypes.LPCWSTR,
        ctypes.POINTER(hardened._SecurityAttributes),
    ]
    read_write_args = [
        hardened.wintypes.HANDLE,
        hardened.wintypes.LPVOID,
        hardened.wintypes.DWORD,
        ctypes.POINTER(hardened.wintypes.DWORD),
        ctypes.POINTER(hardened._Overlapped),
    ]
    assert kernel32.ReadFile.argtypes == read_write_args
    assert kernel32.WriteFile.argtypes == read_write_args
    assert kernel32.FlushFileBuffers.argtypes == [hardened.wintypes.HANDLE]
    assert kernel32.MoveFileExW.argtypes == [
        hardened.wintypes.LPCWSTR,
        hardened.wintypes.LPCWSTR,
        hardened.wintypes.DWORD,
    ]
    assert kernel32.DeleteFileW.argtypes == [hardened.wintypes.LPCWSTR]
    assert kernel32.CloseHandle.argtypes == [hardened.wintypes.HANDLE]
    assert kernel32.CreateFileW.restype is hardened.wintypes.HANDLE
    for function in (
        kernel32.GetFileInformationByHandleEx,
        kernel32.CreateDirectoryW,
        kernel32.ReadFile,
        kernel32.WriteFile,
        kernel32.FlushFileBuffers,
        kernel32.MoveFileExW,
        kernel32.DeleteFileW,
        kernel32.CloseHandle,
    ):
        assert function.restype is hardened.wintypes.BOOL


def test_windows_parent_substitution_at_native_write_boundaries_stays_pinned() -> None:
    root, source, destination, api = _fixture_paths()
    parent = str(destination.parent)
    outside = "/outside/artifact.bin"
    boundaries: list[str] = []

    def substitute_after_pin(name: str) -> None:
        if name in {"before-pinned-temp-create", "before-pinned-replace"}:
            boundaries.append(name)
            # This models a junction substitution after the parent has been
            # opened.  The real handles deny delete sharing, so no component
            # can be replaced; the facade checks that the code never starts a
            # new pathname chain at either boundary.
            assert api.handles
            api.attributes[parent] = (
                hardened._FILE_ATTRIBUTE_DIRECTORY | hardened._FILE_ATTRIBUTE_REPARSE_POINT
            )

    hardened.set_hardened_write_hook(substitute_after_pin)
    try:
        hardened._copy_windows(
            source,
            destination,
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=True,
            source_root=None,
            api=api,
        )
    finally:
        hardened.set_hardened_write_hook(None)

    assert boundaries == ["before-pinned-temp-create", "before-pinned-replace"]
    assert api.files[str(destination)] == b"pinned bytes"
    assert outside not in api.files
    assert not api.handles


def test_windows_prepare_unlink_and_digest_stay_below_pinned_parent() -> None:
    root, _source, destination, api = _fixture_paths()
    api.directories.add("/trusted/root/new")
    target = root / "new" / "digest.bin"
    api.files[str(target)] = b"digest me"

    hardened._windows_prepare_directory(root / "new" / "created", api=api)
    assert "/trusted/root/new/created" in api.directories
    assert hardened._windows_sha256(target, root, api=api) == hashlib.sha256(
        b"digest me"
    ).hexdigest()
    hardened._windows_unlink(target, root, api=api)
    hardened._windows_unlink(target, root, api=api)

    assert str(target) not in api.files
    assert any(name == "DeleteFileW" and value == str(target) for name, value in api.calls)
    assert not api.handles


def test_windows_missing_digest_leaf_has_portable_missing_file_cause() -> None:
    root, _source, destination, api = _fixture_paths()
    api.directories.add(str(destination.parent))

    with pytest.raises(hardened.HardenedWriteError) as error:
        hardened._windows_sha256(destination, root, api=api)

    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_windows_missing_digest_parent_remains_fail_closed() -> None:
    root, _source, destination, api = _fixture_paths()

    with pytest.raises(hardened.HardenedWriteError) as error:
        hardened._windows_sha256(destination, root, api=api)

    assert not isinstance(error.value.__cause__, FileNotFoundError)


def test_windows_missing_coded_digest_metadata_failure_remains_fail_closed() -> None:
    root, _source, destination, api = _fixture_paths()
    api.directories.add(str(destination.parent))
    api.files[str(destination)] = b"existing"
    original_attribute_tag = api.attribute_tag

    def fail_leaf_metadata(handle: int) -> int:
        path, _offset = api.handles[handle]
        if path == str(destination):
            raise hardened._Win32CallError(
                "GetFileInformationByHandleEx", hardened._ERROR_FILE_NOT_FOUND
            )
        return original_attribute_tag(handle)

    api.attribute_tag = fail_leaf_metadata  # type: ignore[method-assign]

    with pytest.raises(hardened.HardenedWriteError) as error:
        hardened._windows_sha256(destination, root, api=api)

    assert not isinstance(error.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize(
    "failure",
    ["source-read", "temp-write", "flush", "final-read", "move", "delete"],
)
def test_windows_copy_normalizes_native_io_failures(failure: str) -> None:
    root, source, destination, api = _fixture_paths()
    if failure in {"source-read", "final-read"}:
        original_read = api.read_file

        def fail_read(handle: int, size: int) -> bytes:
            path, _offset = api.handles[handle]
            if (failure == "source-read" and path == str(source)) or (
                failure == "final-read" and path == str(destination)
            ):
                raise hardened._Win32CallError("ReadFile", 5)
            return original_read(handle, size)

        api.read_file = fail_read  # type: ignore[method-assign]
    elif failure == "temp-write":
        api.write_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("WriteFile", 5)
        )
    elif failure == "flush":
        api.flush_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("FlushFileBuffers", 5)
        )
    elif failure == "move":
        api.move_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("MoveFileExW", 5)
        )
    else:
        api.move_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("MoveFileExW", 5)
        )
        api.delete_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("DeleteFileW", 5)
        )
    with pytest.raises(hardened.HardenedWriteError) as error:
        hardened._copy_windows(
            source,
            destination,
            root,
            hashlib.sha256(b"pinned bytes").hexdigest(),
            replace=True,
            source_root=None,
            api=api,
        )
    assert isinstance(error.value.__cause__, hardened._Win32CallError)
    assert not api.handles
    assert len(api.closed) == len(set(api.closed))


@pytest.mark.parametrize("operation", ["open", "attribute", "delete"])
def test_windows_public_helpers_normalize_native_handle_failures(operation: str) -> None:
    root, _source, destination, api = _fixture_paths()
    if operation == "open":
        api.open_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("CreateFileW", 5)
        )

        def call() -> None:
            hardened._windows_prepare_directory(root, api=api)

    elif operation == "attribute":
        api.attribute_tag = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("GetFileInformationByHandleEx", 5)
        )

        def call() -> None:
            hardened._windows_prepare_directory(root, api=api)

    else:
        api.directories.add(str(destination.parent))
        api.files[str(destination)] = b"remove me"
        api.delete_file = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            hardened._Win32CallError("DeleteFileW", 5)
        )

        def call() -> None:
            hardened._windows_unlink(destination, root, api=api)
    with pytest.raises(hardened.HardenedWriteError) as error:
        call()
    assert isinstance(error.value.__cause__, hardened._Win32CallError)
