"""Pinned-directory operations for security-sensitive imported artifacts.

The generic atomic helpers deliberately remain lightweight.  This module is
used where database provenance makes a pathname substitution meaningful.  POSIX
uses descriptor-relative operations.  Windows has no equivalent ``dir_fd``;
instead it holds each directory handle without delete sharing and performs every
operation through the resulting non-substitutable pathname chain.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast


class HardenedWriteError(OSError):
    """A path could not be used without following an untrusted indirection."""


WriteHook = Callable[[str], None]
_write_hook: ContextVar[WriteHook | None] = ContextVar("hardened_write_hook", default=None)


def set_hardened_write_hook(hook: WriteHook | None) -> None:
    """Set a context-local deterministic test hook; production leaves this unset."""
    _write_hook.set(hook)


def _checkpoint(name: str) -> None:
    if hook := _write_hook.get():
        hook(name)


def _no_follow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _no_follow()


def _relative(path: Path, root: Path) -> tuple[str, ...]:
    if not path.is_absolute() or not root.is_absolute() or not path.is_relative_to(root):
        raise HardenedWriteError("destination is not lexically within pinned root")
    parts = path.relative_to(root).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise HardenedWriteError("destination has an invalid relative component")
    return parts


@contextmanager
def _posix_directory(path: Path, *, create: bool) -> Iterator[int]:
    """Open an absolute directory one component at a time, never following links."""
    fd = os.open("/", _directory_flags())
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise HardenedWriteError(f"missing trusted directory: {path}") from None
                try:
                    os.mkdir(part, dir_fd=fd)
                    next_fd = os.open(part, _directory_flags(), dir_fd=fd)
                except OSError as error:
                    raise HardenedWriteError(
                        f"cannot pin trusted directory: {path}"
                    ) from error
            except OSError as error:
                raise HardenedWriteError(f"cannot pin trusted directory: {path}") from error
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _posix_parent(
    root: Path, destination: Path, *, create: bool, checkpoint: str = "destination-parent-pinned"
) -> Iterator[tuple[int, str]]:
    parts = _relative(destination, root)
    with _posix_directory(root, create=create) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, _directory_flags(), dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise HardenedWriteError("missing trusted destination parent") from None
                    try:
                        os.mkdir(part, dir_fd=fd)
                        next_fd = os.open(part, _directory_flags(), dir_fd=fd)
                    except OSError as error:
                        raise HardenedWriteError(
                            "cannot pin trusted destination parent"
                        ) from error
                except OSError as error:
                    raise HardenedWriteError("cannot pin trusted destination parent") from error
                os.close(fd)
                fd = next_fd
            _checkpoint(checkpoint)
            yield fd, parts[-1]
        finally:
            os.close(fd)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(fd), "rb", closefd=True) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte, including when a filesystem returns a short write."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise HardenedWriteError("pinned temporary write made no progress")
        view = view[written:]


def _posix_existing_matches(parent_fd: int, name: str, expected_sha256: str) -> bool:
    try:
        existing = os.open(name, os.O_RDONLY | _no_follow(), dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    try:
        return _hash_fd(existing) == expected_sha256
    finally:
        os.close(existing)


def _open_source(source: Path, root: Path | None = None) -> int:
    try:
        if sys.platform != "win32":
            # External absolute inputs are anchored at their filesystem root;
            # every component is still opened O_NOFOLLOW through dir-fds.
            root = root or Path(source.anchor)
            with _posix_parent(
                root, source, create=False, checkpoint="source-parent-pinned"
            ) as (parent_fd, name):
                return os.open(name, os.O_RDONLY | _no_follow(), dir_fd=parent_fd)
        return os.open(source, os.O_RDONLY | _no_follow())
    except OSError as error:
        raise HardenedWriteError(f"cannot pin copy source: {source}") from error


def _copy_posix(
    source: Path,
    destination: Path,
    root: Path,
    expected_sha256: str,
    *,
    replace: bool,
    source_root: Path | None,
) -> str:
    with _posix_parent(root, destination, create=True) as (parent_fd, name):
        source_fd = _open_source(source, source_root)
        temporary = f".oms-pinned-{uuid.uuid4().hex}.tmp"
        try:
            if not replace and _posix_existing_matches(parent_fd, name, expected_sha256):
                return expected_sha256
            if not replace:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise HardenedWriteError("immutable destination contains different bytes")
            _checkpoint("before-pinned-temp-create")
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow(),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    _write_all(temporary_fd, chunk)
                os.fsync(temporary_fd)
                if digest.hexdigest() != expected_sha256:
                    raise HardenedWriteError("pinned source checksum mismatch")
            finally:
                os.close(temporary_fd)
            _checkpoint("before-pinned-replace")
            if replace:
                os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if _posix_existing_matches(parent_fd, name, expected_sha256):
                        return expected_sha256
                    raise HardenedWriteError(
                        "immutable destination appeared with different bytes"
                    ) from None
                os.unlink(temporary, dir_fd=parent_fd)
            final_fd = os.open(name, os.O_RDONLY | _no_follow(), dir_fd=parent_fd)
            try:
                if _hash_fd(final_fd) != expected_sha256:
                    raise HardenedWriteError("pinned promoted checksum mismatch")
            finally:
                os.close(final_fd)
            os.fsync(parent_fd)
            return expected_sha256
        finally:
            os.close(source_fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


# Win32 constants are kept here so mocked tests exercise exactly the native
# contract without pretending that macOS implements Windows path semantics.
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_FILE_ATTRIBUTE_TAG_INFO = 9


class _WindowsAPI(Protocol):
    """Tiny injectable Win32 facade used by the hardened Windows path."""

    def open_file(self, path: str, access: int, share: int, creation: int, flags: int) -> int: ...

    def attribute_tag(self, handle: int) -> int: ...

    def create_directory(self, path: str) -> None: ...

    def read_file(self, handle: int, size: int) -> bytes: ...

    def write_file(self, handle: int, data: bytes) -> None: ...

    def flush_file(self, handle: int) -> None: ...

    def move_file(self, source: str, destination: str, flags: int) -> None: ...

    def delete_file(self, path: str) -> None: ...

    def close_handle(self, handle: int) -> None: ...


class _Win32CallError(OSError):
    def __init__(self, operation: str, code: int) -> None:
        super().__init__(code, f"{operation} failed with Win32 error {code}")
        self.winerror = code


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _NativeWindowsAPI:
    """Direct ctypes implementation, constructed only by a Windows caller."""

    def __init__(self) -> None:
        win_dll_name = "WinDLL"
        last_error_name = "get_last_error"
        win_dll = cast(Callable[..., Any], getattr(ctypes, win_dll_name))
        self._get_last_error = cast(Callable[[], int], getattr(ctypes, last_error_name))
        self._kernel32: Any = win_dll("kernel32", use_last_error=True)
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(_FileAttributeTagInfo),
            wintypes.DWORD,
        ]
        self._kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self._kernel32.CreateDirectoryW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(_SecurityAttributes),
        ]
        self._kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_Overlapped),
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_Overlapped),
        ]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self._kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self._kernel32.MoveFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.MoveFileExW.restype = wintypes.BOOL
        self._kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.DeleteFileW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _raise_last_error(self, operation: str) -> None:
        raise _Win32CallError(operation, self._get_last_error())

    def open_file(self, path: str, access: int, share: int, creation: int, flags: int) -> int:
        handle = self._kernel32.CreateFileW(path, access, share, None, creation, flags, None)
        if handle is None or int(handle) == self._invalid_handle:
            self._raise_last_error("CreateFileW")
        return int(handle)

    def attribute_tag(self, handle: int) -> int:
        info = _FileAttributeTagInfo()
        if not self._kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self._raise_last_error("GetFileInformationByHandleEx")
        return int(info.FileAttributes)

    def create_directory(self, path: str) -> None:
        if not self._kernel32.CreateDirectoryW(path, None):
            self._raise_last_error("CreateDirectoryW")

    def read_file(self, handle: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not self._kernel32.ReadFile(handle, buffer, size, ctypes.byref(read), None):
            self._raise_last_error("ReadFile")
        return buffer.raw[: int(read.value)]

    def write_file(self, handle: int, data: bytes) -> None:
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data)
        if not self._kernel32.WriteFile(
            handle, buffer, len(data), ctypes.byref(written), None
        ):
            self._raise_last_error("WriteFile")
        if written.value != len(data):
            raise HardenedWriteError("WriteFile performed a partial write")

    def flush_file(self, handle: int) -> None:
        if not self._kernel32.FlushFileBuffers(handle):
            self._raise_last_error("FlushFileBuffers")

    def move_file(self, source: str, destination: str, flags: int) -> None:
        if not self._kernel32.MoveFileExW(source, destination, flags):
            self._raise_last_error("MoveFileExW")

    def delete_file(self, path: str) -> None:
        if not self._kernel32.DeleteFileW(path):
            self._raise_last_error("DeleteFileW")

    def close_handle(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error("CloseHandle")


def _windows_error_code(error: OSError) -> int | None:
    code = getattr(error, "winerror", None)
    if isinstance(code, int):
        return code
    return error.errno


def _is_missing_windows_error(error: OSError) -> bool:
    return _windows_error_code(error) in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}


def _is_exists_windows_error(error: OSError) -> bool:
    return _windows_error_code(error) in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}


@dataclass
class _WindowsDirectoryChain:
    path: Path
    handles: list[int]


def _windows_path_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    if not path.is_absolute() or not path.anchor:
        raise HardenedWriteError("Windows path is not absolute")
    parts = path.parts
    if not parts or parts[0] != path.anchor:
        raise HardenedWriteError("Windows path has no stable drive or UNC anchor")
    return Path(path.anchor), parts[1:]


def _windows_open_checked_directory(api: _WindowsAPI, path: Path) -> int:
    handle = api.open_file(
        str(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        attributes = api.attribute_tag(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise HardenedWriteError("Windows directory contains a reparse point")
        if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise HardenedWriteError("Windows path component is not a directory")
        return handle
    except Exception:
        api.close_handle(handle)
        raise


def _windows_append_directory(
    chain: _WindowsDirectoryChain, part: str, *, create: bool, api: _WindowsAPI
) -> None:
    next_path = chain.path / part
    try:
        handle = _windows_open_checked_directory(api, next_path)
    except HardenedWriteError:
        raise
    except OSError as error:
        if not create or not _is_missing_windows_error(error):
            raise HardenedWriteError(f"cannot pin Windows directory: {next_path}") from error
        try:
            api.create_directory(str(next_path))
        except OSError as create_error:
            if not _is_exists_windows_error(create_error):
                raise HardenedWriteError(
                    f"cannot create trusted Windows directory: {next_path}"
                ) from create_error
        try:
            handle = _windows_open_checked_directory(api, next_path)
        except OSError as open_error:
            raise HardenedWriteError(f"cannot pin Windows directory: {next_path}") from open_error
    chain.path = next_path
    chain.handles.append(handle)


@contextmanager
def _windows_directory(
    path: Path, *, create: bool, api: _WindowsAPI
) -> Iterator[_WindowsDirectoryChain]:
    anchor, parts = _windows_path_parts(path)
    chain = _WindowsDirectoryChain(path=anchor, handles=[])
    try:
        chain.handles.append(_windows_open_checked_directory(api, anchor))
        for part in parts:
            _windows_append_directory(chain, part, create=create, api=api)
        yield chain
    finally:
        for handle in reversed(chain.handles):
            api.close_handle(handle)


@contextmanager
def _windows_parent(
    root: Path,
    destination: Path,
    *,
    create: bool,
    api: _WindowsAPI,
    checkpoint: str = "destination-parent-pinned",
) -> Iterator[tuple[_WindowsDirectoryChain, str]]:
    parts = _relative(destination, root)
    with _windows_directory(root, create=create, api=api) as chain:
        for part in parts[:-1]:
            _windows_append_directory(chain, part, create=create, api=api)
        _checkpoint(checkpoint)
        yield chain, parts[-1]


def _windows_open_checked_file(api: _WindowsAPI, path: Path, *, access: int, creation: int) -> int:
    handle = api.open_file(
        str(path),
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        creation,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_ATTRIBUTE_NORMAL,
    )
    try:
        attributes = api.attribute_tag(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise HardenedWriteError("Windows file is a reparse point")
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise HardenedWriteError("Windows file path is a directory")
        return handle
    except Exception:
        api.close_handle(handle)
        raise


@contextmanager
def _windows_source_file(
    source: Path, source_root: Path | None, *, api: _WindowsAPI
) -> Iterator[int]:
    if source_root is None:
        with _windows_directory(source.parent, create=False, api=api) as chain:
            with _windows_open_source_under(chain, source.name, api=api) as handle:
                yield handle
        return
    with _windows_parent(
        source_root, source, create=False, api=api, checkpoint="source-parent-pinned"
    ) as (chain, name):
        with _windows_open_source_under(chain, name, api=api) as handle:
            yield handle


@contextmanager
def _windows_open_source_under(
    chain: _WindowsDirectoryChain, name: str, *, api: _WindowsAPI
) -> Iterator[int]:
    handle = _windows_open_checked_file(
        api, chain.path / name, access=_GENERIC_READ, creation=_OPEN_EXISTING
    )
    try:
        yield handle
    finally:
        api.close_handle(handle)


def _windows_hash_handle(api: _WindowsAPI, handle: int) -> str:
    digest = hashlib.sha256()
    while chunk := api.read_file(handle, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _windows_existing_digest(
    parent: _WindowsDirectoryChain, name: str, *, api: _WindowsAPI
) -> str | None:
    try:
        handle = _windows_open_checked_file(
            api, parent.path / name, access=_GENERIC_READ, creation=_OPEN_EXISTING
        )
    except OSError as error:
        if _is_missing_windows_error(error):
            return None
        raise HardenedWriteError("cannot inspect pinned immutable destination") from error
    try:
        return _windows_hash_handle(api, handle)
    finally:
        api.close_handle(handle)


def _copy_windows_native(
    source: Path,
    destination: Path,
    root: Path,
    expected_sha256: str,
    *,
    replace: bool,
    source_root: Path | None,
    api: _WindowsAPI | None = None,
) -> str:
    """Copy using only native, pinned Windows handles; no pathname fallback."""
    native_api: _WindowsAPI = api if api is not None else _NativeWindowsAPI()
    with _windows_parent(root, destination, create=True, api=native_api) as (parent, name):
        with _windows_source_file(source, source_root, api=native_api) as source_handle:
            if not replace:
                try:
                    existing_digest = _windows_existing_digest(parent, name, api=native_api)
                    if existing_digest == expected_sha256:
                        return expected_sha256
                    if existing_digest is not None:
                        raise HardenedWriteError("immutable destination contains different bytes")
                except HardenedWriteError:
                    raise
                except OSError as error:
                    raise HardenedWriteError(
                        "cannot inspect pinned immutable destination"
                    ) from error
            temporary_path: Path | None = None
            temporary_handle: int | None = None
            try:
                _checkpoint("before-pinned-temp-create")
                for _attempt in range(16):
                    candidate = parent.path / f".oms-pinned-{uuid.uuid4().hex}.tmp"
                    try:
                        temporary_handle = _windows_open_checked_file(
                            native_api,
                            candidate,
                            access=_GENERIC_WRITE | _FILE_READ_ATTRIBUTES,
                            creation=_CREATE_NEW,
                        )
                    except OSError as error:
                        if _is_exists_windows_error(error):
                            continue
                        raise HardenedWriteError(
                            "cannot create pinned Windows temporary file"
                        ) from error
                    temporary_path = candidate
                    break
                if temporary_path is None or temporary_handle is None:
                    raise HardenedWriteError("could not allocate unique pinned temporary file")
                digest = hashlib.sha256()
                while chunk := native_api.read_file(source_handle, 1024 * 1024):
                    digest.update(chunk)
                    native_api.write_file(temporary_handle, chunk)
                native_api.flush_file(temporary_handle)
                if digest.hexdigest() != expected_sha256:
                    raise HardenedWriteError("pinned source checksum mismatch")
                native_api.close_handle(temporary_handle)
                temporary_handle = None
                _checkpoint("before-pinned-replace")
                flags = _MOVEFILE_WRITE_THROUGH
                if replace:
                    flags |= _MOVEFILE_REPLACE_EXISTING
                try:
                    native_api.move_file(str(temporary_path), str(parent.path / name), flags)
                except OSError as error:
                    if not replace and _is_exists_windows_error(error):
                        if (
                            _windows_existing_digest(parent, name, api=native_api)
                            == expected_sha256
                        ):
                            return expected_sha256
                        raise HardenedWriteError(
                            "immutable destination appeared during pinned copy"
                        ) from error
                    raise HardenedWriteError("pinned Windows promotion failed") from error
                temporary_path = None
                final_handle = _windows_open_checked_file(
                    native_api, parent.path / name, access=_GENERIC_READ, creation=_OPEN_EXISTING
                )
                try:
                    if _windows_hash_handle(native_api, final_handle) != expected_sha256:
                        raise HardenedWriteError("pinned promoted checksum mismatch")
                finally:
                    native_api.close_handle(final_handle)
                return expected_sha256
            finally:
                if temporary_handle is not None:
                    native_api.close_handle(temporary_handle)
                if temporary_path is not None:
                    try:
                        native_api.delete_file(str(temporary_path))
                    except OSError as error:
                        if not _is_missing_windows_error(error):
                            raise HardenedWriteError(
                                "could not remove pinned temporary file"
                            ) from error


def _copy_windows(
    source: Path,
    destination: Path,
    root: Path,
    expected_sha256: str,
    *,
    replace: bool,
    source_root: Path | None,
    api: _WindowsAPI | None = None,
) -> str:
    """Normalize every native copy failure to the public hardened contract."""
    try:
        return _copy_windows_native(
            source,
            destination,
            root,
            expected_sha256,
            replace=replace,
            source_root=source_root,
            api=api,
        )
    except HardenedWriteError:
        raise
    except OSError as error:
        raise HardenedWriteError("pinned Windows copy failed") from error


def hardened_verified_copy(
    source: Path,
    destination: Path,
    root: Path,
    expected_sha256: str,
    *,
    replace: bool,
    source_root: Path | None = None,
) -> str:
    """Copy through a pinned destination parent and verify the expected digest."""
    if sys.platform == "win32":
        return _copy_windows(
            source,
            destination,
            root,
            expected_sha256,
            replace=replace,
            source_root=source_root,
        )
    try:
        return _copy_posix(
            source,
            destination,
            root,
            expected_sha256,
            replace=replace,
            source_root=source_root,
        )
    except HardenedWriteError:
        raise
    except OSError as error:
        raise HardenedWriteError("pinned POSIX copy failed") from error


def hardened_prepare_directory(path: Path) -> None:
    """Create and pin a sensitive directory without ``Path.mkdir`` on Windows."""
    if sys.platform == "win32":
        _windows_prepare_directory(path, api=_NativeWindowsAPI())
        return
    try:
        with _posix_directory(path, create=True):
            return
    except HardenedWriteError:
        raise
    except OSError as error:
        raise HardenedWriteError("cannot prepare pinned POSIX directory") from error


def _windows_prepare_directory(path: Path, *, api: _WindowsAPI) -> None:
    try:
        with _windows_directory(path, create=True, api=api):
            return
    except HardenedWriteError:
        raise
    except OSError as error:
        raise HardenedWriteError("cannot prepare pinned Windows directory") from error


def hardened_unlink(destination: Path, root: Path) -> None:
    """Delete a file only while its complete parent chain remains pinned."""
    if sys.platform == "win32":
        _windows_unlink(destination, root, api=_NativeWindowsAPI())
        return
    try:
        with _posix_parent(root, destination, create=False) as (parent_fd, name):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    except HardenedWriteError:
        raise
    except OSError as error:
        raise HardenedWriteError("pinned POSIX cleanup failed") from error


def _windows_unlink(destination: Path, root: Path, *, api: _WindowsAPI) -> None:
    try:
        with _windows_parent(root, destination, create=False, api=api) as (parent, name):
            api.delete_file(str(parent.path / name))
    except HardenedWriteError:
        raise
    except OSError as error:
        if _is_missing_windows_error(error):
            return
        raise HardenedWriteError("pinned Windows cleanup failed") from error


def hardened_sha256(path: Path, root: Path) -> str:
    """Digest a file opened below a pinned root."""
    if sys.platform == "win32":
        return _windows_sha256(path, root, api=_NativeWindowsAPI())
    fd = _open_source(path, root)
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _windows_sha256(path: Path, root: Path, *, api: _WindowsAPI) -> str:
    try:
        with _windows_parent(root, path, create=False, api=api) as (parent, name):
            handle = _windows_open_checked_file(
                api, parent.path / name, access=_GENERIC_READ, creation=_OPEN_EXISTING
            )
            try:
                return _windows_hash_handle(api, handle)
            finally:
                api.close_handle(handle)
    except HardenedWriteError:
        raise
    except OSError as error:
        raise HardenedWriteError(f"cannot pin digest source: {path}") from error


def hardened_promote_with_rollback[T](
    pairs: list[tuple[Path, Path, Path, str]],
    revision_id: int,
    commit: Callable[[], T],
    assert_owned: Callable[[], None],
) -> T:
    """Pinned backup/promotion/rollback for imported-derived mutable PDFs."""
    backups: list[tuple[Path, Path | None, Path]] = []
    try:
        for _source, destination, root, _digest in pairs:
            assert_owned()
            backup = destination.with_name(f".{destination.name}.oms-backup-{revision_id}")
            try:
                digest = hardened_sha256(destination, root)
            except HardenedWriteError as error:
                if not isinstance(error.__cause__, FileNotFoundError):
                    raise
                backups.append((destination, None, root))
            else:
                hardened_verified_copy(
                    destination,
                    backup,
                    root,
                    digest,
                    replace=True,
                    source_root=root,
                )
                # A claim can lapse while a native copy is in progress.  Check
                # before advancing to the next backup/promotion boundary so a
                # successor never loses the complete recovery set.
                assert_owned()
                backups.append((destination, backup, root))
        for source, destination, root, digest in pairs:
            assert_owned()
            hardened_verified_copy(source, destination, root, digest, replace=True)
            assert_owned()
        assert_owned()
        result = commit()
    except Exception:
        for destination, saved, root in backups:
            assert_owned()
            if saved is None:
                hardened_unlink(destination, root)
            else:
                hardened_verified_copy(
                    saved,
                    destination,
                    root,
                    hardened_sha256(saved, root),
                    replace=True,
                    source_root=root,
                )
        raise
    else:
        for _destination, saved, root in backups:
            assert_owned()
            if saved is not None:
                hardened_unlink(saved, root)
        return result
