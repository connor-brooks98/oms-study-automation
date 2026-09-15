"""Windows-only LPAC transport. Port of the accepted 2026-09-14 native probe.

No shell, elevation, fallback sandbox, or inherited credentials. The parent verifies
both tokens before resuming the child. Only the managed home is writable; a
kill-on-close job owns the process even if the Hub exits unexpectedly.
"""

import ctypes as c
import msvcrt
import os
import subprocess
import sys
import uuid
from contextlib import ExitStack
from ctypes import wintypes as w
from pathlib import Path
from typing import Any, BinaryIO

assert sys.platform == "win32"

# Provider networking replaces the diagnostic's private-LAN capability.
CAPABILITIES = (
    "S-1-15-3-1",  # internetClient
    # lpacIdentityServices: Schannel HTTPS, with certificate verification intact.
    "S-1-15-3-1024-1788129303-2183208577-3999474272-3147359985-1757322193-3815756386-151582180-1888101193",
    "S-1-15-3-1024-2405443489-874036122-4286035555-1823921565-1746547431-2453885448-3625952902-991631256",
    "S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-432734479-3232135806-4053264122-3456934681",
)
P, D, S = c.c_void_p, w.DWORD, c.c_size_t


class SidEntry(c.Structure):
    _fields_ = [("sid", P), ("attributes", D)]


class Groups(c.Structure):
    _fields_ = [("count", D), ("first", SidEntry)]


class Caps(c.Structure):
    _fields_ = [("sid", P), ("capabilities", P), ("count", D), ("reserved", D)]


class Startup(c.Structure):
    _fields_ = [
        ("cb", D),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        *[(name, D) for name in ("x", "y", "xsize", "ysize", "xchars", "ychars", "fill", "flags")],
        ("show", w.WORD),
        ("reserved_bytes", w.WORD),
        ("reserved_ptr", P),
        ("stdin", P),
        ("stdout", P),
        ("stderr", P),
    ]


class StartupEx(c.Structure):
    _fields_ = [("startup", Startup), ("attributes", P)]


class ProcessInfo(c.Structure):
    _fields_ = [("process", P), ("thread", P), ("pid", D), ("tid", D)]


class JobLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_int64),
        ("job_time", c.c_int64),
        ("flags", D),
        ("minimum", S),
        ("maximum", S),
        ("active", D),
        ("affinity", S),
        ("priority", D),
        ("scheduling", D),
    ]


class JobInfo(c.Structure):
    _fields_ = [
        ("basic", JobLimits),
        ("io", c.c_uint64 * 6),
        ("process_memory", S),
        ("job_memory", S),
        ("peak_process", S),
        ("peak_job", S),
    ]


class Mapping(c.Structure):
    _fields_ = [("read", D), ("write", D), ("execute", D), ("all", D)]


k = c.WinDLL("kernel32", use_last_error=True)
a = c.WinDLL("advapi32", use_last_error=True)
u = c.WinDLL("userenv", use_last_error=True)


def _bind(dll: Any, name: str, result: Any, *args: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


CloseHandle = _bind(k, "CloseHandle", w.BOOL, P)
LocalFree = _bind(k, "LocalFree", P, P)
FreeSid = _bind(a, "FreeSid", P, P)
GetCurrentProcess = _bind(k, "GetCurrentProcess", P)
OpenProcessToken = _bind(a, "OpenProcessToken", w.BOOL, P, D, c.POINTER(P))
GetTokenInformation = _bind(a, "GetTokenInformation", w.BOOL, P, c.c_int, P, D, c.POINTER(D))
DuplicateTokenEx = _bind(a, "DuplicateTokenEx", w.BOOL, P, D, P, c.c_int, c.c_int, c.POINTER(P))
AccessCheck = _bind(
    a,
    "AccessCheck",
    w.BOOL,
    P,
    P,
    D,
    c.POINTER(Mapping),
    P,
    c.POINTER(D),
    c.POINTER(D),
    c.POINTER(w.BOOL),
)
ConvertSD = _bind(
    a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, w.LPCWSTR, D, c.POINTER(P), P
)
ConvertSid = _bind(a, "ConvertStringSidToSidW", w.BOOL, w.LPCWSTR, c.POINTER(P))
SidString = _bind(a, "ConvertSidToStringSidW", w.BOOL, P, c.POINTER(P))
CreateProfile = _bind(
    u, "CreateAppContainerProfile", c.c_long, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, P, D, c.POINTER(P)
)
DeleteProfile = _bind(u, "DeleteAppContainerProfile", c.c_long, w.LPCWSTR)
InitializeAttributes = _bind(k, "InitializeProcThreadAttributeList", w.BOOL, P, D, D, c.POINTER(S))
UpdateAttribute = _bind(k, "UpdateProcThreadAttribute", w.BOOL, P, D, S, P, S, P, P)
DeleteAttributes = _bind(k, "DeleteProcThreadAttributeList", None, P)
CreateProcess = _bind(
    k,
    "CreateProcessW",
    w.BOOL,
    w.LPCWSTR,
    w.LPWSTR,
    P,
    P,
    w.BOOL,
    D,
    P,
    w.LPCWSTR,
    c.POINTER(StartupEx),
    c.POINTER(ProcessInfo),
)
CreatePipe = _bind(k, "CreatePipe", w.BOOL, c.POINTER(P), c.POINTER(P), P, D)
SetHandleInformation = _bind(k, "SetHandleInformation", w.BOOL, P, D, D)
ResumeThread = _bind(k, "ResumeThread", D, P)
Wait = _bind(k, "WaitForSingleObject", D, P, D)
Terminate = _bind(k, "TerminateProcess", w.BOOL, P, w.UINT)
ExitCode = _bind(k, "GetExitCodeProcess", w.BOOL, P, c.POINTER(D))
CreateJob = _bind(k, "CreateJobObjectW", P, P, w.LPCWSTR)
SetJob = _bind(k, "SetInformationJobObject", w.BOOL, P, c.c_int, P, D)
CreateFile = _bind(k, "CreateFileW", P, w.LPCWSTR, D, D, P, D, D, P)
FinalPath = _bind(k, "GetFinalPathNameByHandleW", D, P, w.LPWSTR, D, D)


def _check(ok: object) -> None:
    if not ok:
        raise c.WinError(c.get_last_error())


def _require(ok: object, message: str) -> None:
    if not ok:
        raise OSError("LPAC verification failed: " + message)


def _sid_string(sid: Any) -> str:
    text = P()
    _check(SidString(sid, c.byref(text)))
    try:
        return c.wstring_at(text)
    finally:
        LocalFree(text)


def _token_int(token: Any, kind: int) -> int:
    value, needed = D(), D()
    _check(GetTokenInformation(token, kind, c.byref(value), 4, c.byref(needed)))
    _require(needed.value == 4, "token DWORD size")
    return value.value


def _token_data(token: Any, kind: int) -> c.Array[c.c_char]:
    needed = D()
    ok = GetTokenInformation(token, kind, None, 0, c.byref(needed))
    _require(
        not ok and c.get_last_error() in (24, 122) and 4 <= needed.value <= 65536,
        "token buffer size",
    )
    data = c.create_string_buffer(needed.value)
    _check(GetTokenInformation(token, kind, data, len(data), c.byref(needed)))
    _require(needed.value <= len(data), "token buffer length")
    return data


def _access_mask(token: Any) -> int:
    with ExitStack() as stack:
        duplicate, descriptor = P(), P()
        _check(DuplicateTokenEx(token, 8, None, 1, 2, c.byref(duplicate)))
        stack.callback(CloseHandle, duplicate)
        _check(
            ConvertSD(
                "O:SYG:SYD:(A;;0x3;;;WD)(A;;0x1;;;S-1-15-2-1)(A;;0x2;;;S-1-15-2-2)",
                1,
                c.byref(descriptor),
                None,
            )
        )
        stack.callback(LocalFree, descriptor)
        privileges, length, granted, status = c.create_string_buffer(1024), D(1024), D(), w.BOOL()
        _check(
            AccessCheck(
                descriptor,
                duplicate,
                0x02000000,
                c.byref(Mapping()),
                privileges,
                c.byref(length),
                c.byref(granted),
                c.byref(status),
            )
        )
        _require(status.value and length.value <= 1024, "functional AccessCheck")
        return granted.value


def canonical_path(path: Path) -> str:
    """Match the accepted private runtime's NT-volume canonicalization."""
    handle = CreateFile(str(path), 0x80, 7, None, 3, 0x02000000, None)
    _check(handle != P(-1).value)
    try:
        value = c.create_unicode_buffer(32768)
        size = FinalPath(handle, value, len(value), 2)
        _require(0 < size < len(value) and value.value.startswith("\\Device\\"), "canonical path")
        return "\\\\?\\GLOBALROOT" + value.value
    finally:
        CloseHandle(handle)


def _grant(path: Path, sid: str, rights: str) -> None:
    # Only the named profile is granted access; never ALL APPLICATION PACKAGES.
    icacls = str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe")
    result = subprocess.run(
        [icacls, str(path), "/grant:r", f"*{sid}:{rights}"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    _require(result.returncode == 0, "private path ACL")


class LpacProcess:
    """The small Popen subset consumed by the shared bounded stdio transport."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        session_home: Path,
        shutdown_timeout: float,
    ) -> None:
        self.args, self.shutdown_timeout = command, shutdown_timeout
        self.stdin: BinaryIO | None = None
        self.stdout: BinaryIO | None = None
        self.stderr: BinaryIO | None = None
        self._process: int | None = None
        self._job: int | None = None
        self.returncode: int | None = None
        self.token_verified = False
        self.pid = None
        self._profile: str | None = None
        self._sid: str | None = None
        self._granted: list[Path] = []
        for path in (Path(command[0]), cwd, session_home):
            _require(
                path.is_absolute() and path.resolve(strict=True) == path and not path.is_junction(),
                "dedicated canonical paths",
            )
        _require(
            cwd != session_home
            and not cwd.is_relative_to(session_home)
            and not session_home.is_relative_to(cwd),
            "separate roots",
        )
        _require(not Path(command[0]).is_relative_to(session_home), "runtime outside writable home")
        try:
            self._launch(command, cwd, env, session_home)
        except BaseException:
            self.close()
            raise

    def _launch(self, command: list[str], cwd: Path, env: dict[str, str], home: Path) -> None:
        with ExitStack() as stack:
            caller = P()
            _check(OpenProcessToken(GetCurrentProcess(), 10, c.byref(caller)))
            stack.callback(CloseHandle, caller)
            _require(
                _token_int(caller, 20) == 0
                and _token_int(caller, 29) == 0
                and _access_mask(caller) == 3,
                "ordinary non-elevated caller",
            )
            session = _token_int(caller, 12)
            # A fresh SID cannot read grants left by an earlier crashed request.
            profile = "OmsStudyHub." + uuid.uuid4().hex
            sid = P()
            hr = CreateProfile(profile, profile, "OMS managed Codex session", None, 0, c.byref(sid))
            _require(hr == 0 and sid.value, "profile creation")
            self._profile = profile
            stack.callback(FreeSid, sid)
            sid_text = _sid_string(sid)
            self._sid = sid_text
            for path, rights in (
                (home, "(OI)(CI)(M)"),
                (cwd, "(OI)(CI)(RX)"),
                (Path(command[0]), "(RX)"),
            ):
                self._granted.append(path)
                _grant(path, sid_text, rights)
            entries = (SidEntry * len(CAPABILITIES))()
            for i, capability_sid in enumerate(CAPABILITIES):
                cap = P()
                _check(ConvertSid(capability_sid, c.byref(cap)))
                stack.callback(LocalFree, cap)
                entries[i] = SidEntry(cap, 4)
            caps = Caps(sid, c.cast(entries, P), len(entries), 0)
            handles = (P * 3)()
            for i, mode in enumerate(("wb", "rb", "rb")):
                read, write = P(), P()
                _check(CreatePipe(c.byref(read), c.byref(write), None, 0))
                child, parent = (read, write) if i == 0 else (write, read)
                stack.callback(CloseHandle, child)
                try:
                    assert parent.value is not None
                    fd = msvcrt.open_osfhandle(
                        parent.value, os.O_BINARY | (os.O_WRONLY if i == 0 else os.O_RDONLY)
                    )
                except BaseException:
                    CloseHandle(parent)
                    raise
                stream = os.fdopen(fd, mode)
                setattr(self, ("stdin", "stdout", "stderr")[i], stream)
                _check(SetHandleInformation(child, 1, 1))
                handles[i] = child
            size = S()
            ok = InitializeAttributes(None, 5, 0, c.byref(size))
            _require(
                not ok and c.get_last_error() == 122 and 0 < size.value <= 65536,
                "attribute allocation",
            )
            attributes = c.create_string_buffer(size.value)
            _check(InitializeAttributes(attributes, 5, 0, c.byref(size)))
            stack.callback(DeleteAttributes, attributes)
            self._job = CreateJob(None, None)
            _check(self._job)
            limits = JobInfo()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(SetJob(self._job, 9, c.byref(limits), c.sizeof(limits)))
            jobs = (P * 1)(self._job)
            policy = D(1)
            for key, value in (
                (0x20009, caps),
                (0x2000F, policy),
                (0x2000E, policy),
                (0x20002, handles),
                (0x2000D, jobs),  # Own the child atomically, including parent death before resume.
            ):
                _check(
                    UpdateAttribute(attributes, 0, key, c.byref(value), c.sizeof(value), None, None)
                )
            startup = StartupEx()
            startup.startup.cb = c.sizeof(startup)
            startup.startup.flags = 0x100
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles
            startup.attributes = c.cast(attributes, P)
            pi = ProcessInfo()
            environment = c.create_unicode_buffer(
                "\0".join(
                    f"{key}={value}"
                    for key, value in sorted(env.items(), key=lambda x: x[0].upper())
                )
                + "\0\0"
            )
            # Suspended + detached + Unicode environment + extended startup. No console allocation.
            _check(
                CreateProcess(
                    command[0],
                    c.create_unicode_buffer(subprocess.list2cmdline(command)),
                    None,
                    None,
                    True,
                    0x8040C,
                    environment,
                    str(cwd),
                    c.byref(startup),
                    c.byref(pi),
                )
            )
            self._process, self.pid = pi.process, pi.pid
            stack.callback(CloseHandle, pi.thread)
            child = P()
            _check(OpenProcessToken(self._process, 10, c.byref(child)))
            stack.callback(CloseHandle, child)
            _require(
                _token_int(child, 29) == 1
                and _token_int(child, 20) == 0
                and _token_int(child, 12) == session,
                "child token/session",
            )
            data = _token_data(child, 31)
            _require(_sid_string(P.from_buffer(data).value) == sid_text, "child profile")
            data = _token_data(child, 30)
            count = D.from_buffer(data).value
            offset = Groups.first.offset
            _require(
                count == len(CAPABILITIES) and offset + count * c.sizeof(SidEntry) <= len(data),
                "capability count",
            )
            actual = []
            for i in range(count):
                entry = SidEntry.from_buffer(data, offset + i * c.sizeof(SidEntry))
                start, end = c.addressof(data), c.addressof(data) + len(data)
                _require(start <= entry.sid <= end - 8, "capability SID pointer")
                sid_size = 8 + 4 * c.c_ubyte.from_address(entry.sid + 1).value
                _require(
                    entry.sid + sid_size <= end and entry.attributes == 4,
                    "capability SID bounds/attributes",
                )
                actual.append(_sid_string(entry.sid))
            _require(
                sorted(actual) == sorted(CAPABILITIES) and _access_mask(child) == 2,
                "LPAC capability/access check",
            )
            self.token_verified = True
            _require(ResumeThread(pi.thread) == 1, "resume count")

    def poll(self) -> int | None:
        if self.returncode is not None or not self._process:
            return self.returncode
        result = Wait(self._process, 0)
        if result == 258:
            return None
        _require(result == 0, "process wait")
        code = D()
        _check(ExitCode(self._process, c.byref(code)))
        self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        code = self.poll()
        if code is not None:
            return code
        result = Wait(
            self._process,
            0xFFFFFFFF if timeout is None else max(0, min(int(timeout * 1000), 0xFFFFFFFE)),
        )
        if result == 258:
            assert timeout is not None
            raise subprocess.TimeoutExpired(self.args, timeout)
        _require(result == 0, "process wait")
        code = self.poll()
        assert code is not None
        return code

    def terminate(self) -> None:
        if self._process and self.poll() is None:
            if not Terminate(self._process, 1):
                error = c.get_last_error()
                if error == 5:
                    # An exiting Windows process can reject termination before it signals.
                    try:
                        self.wait(timeout=self.shutdown_timeout)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                if self.poll() is None:
                    raise c.WinError(error)

    kill = terminate

    def close(self) -> None:
        try:
            if self._process:
                self.terminate()
                self.wait(timeout=self.shutdown_timeout)
        finally:
            if self._job:
                CloseHandle(self._job)
                self._job = None
            if self._process:
                # Job close is a second termination boundary; keep the handle if reaping fails.
                self.wait(timeout=self.shutdown_timeout)
                CloseHandle(self._process)
                self._process = None
            for stream in (self.stdin, self.stdout, self.stderr):
                if stream is not None:
                    stream.close()

            if self._profile:
                assert self._sid is not None
                # Revoke while the process is gone, then remove only our profile.
                icacls = str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe")
                for path in self._granted:
                    result = subprocess.run(
                        [icacls, str(path), "/remove:g", "*" + self._sid],
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    _require(result.returncode == 0, "revoke private path ACL")
                _require(DeleteProfile(self._profile) == 0, "delete owned profile")
                self._profile = None
                self._granted.clear()
