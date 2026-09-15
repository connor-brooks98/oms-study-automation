"""Native checks use fresh synthetic files, never the managed account."""

import os
import shutil
import sys
from pathlib import Path

import pytest


def test_windows_runtime_and_model_are_explicitly_accepted(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from oms_hub.llm import codex_session as session

    monkeypatch.setattr(session, "sys", SimpleNamespace(platform="win32"))
    client = session.CodexSessionClient(
        tmp_path / "codex.exe", tmp_path / "home", tmp_path / "work"
    )
    assert (
        client.binary_sha256 == "d6eb90b7409dc22f407a9dfa44ec629a8a5a6bf3ca001493aef13dd85a75dbda"
    )
    client._require_generation_ready(session.SessionRequest("probe", "gpt-5.5", "", ""))
    with pytest.raises(session.SessionError):
        client._require_generation_ready(
            session.SessionRequest("probe", "unverified-model", "", "")
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Requires actual Windows token and filesystem")
def test_native_lpac_boundaries_and_cleanup(tmp_path):
    from oms_hub.llm.windows_lpac import LpacProcess

    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    (work / "read.txt").write_text("INSIDE_MARKER")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_MARKER")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cmd = str(runtime / "cmd.exe")
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", cmd)
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "WINDIR": os.environ["SystemRoot"],
        "LOCALAPPDATA": os.environ["LOCALAPPDATA"],
        "TEMP": str(home),
        "TMP": str(home),
    }
    instructions = (
        r"type read.txt & type ..\outside.txt & echo PRIVATE_WRITE>..\home\write.txt"
        r" & echo FORBIDDEN>write.txt & echo FORBIDDEN>..\bad.txt"
        r" & ..\runtime\cmd.exe /d /c echo CHILD_PROCESS_FORBIDDEN"
    )
    process = LpacProcess(
        [cmd, "/d", "/v:off", "/s", "/c", instructions],
        cwd=work,
        env=env,
        session_home=home,
        shutdown_timeout=2,
    )
    try:
        process.stdin.close()
        process.wait(timeout=15)
        stdout, stderr = process.stdout.read(), process.stderr.read()
        assert b"INSIDE_MARKER" in stdout
        assert b"OUTSIDE_MARKER" not in stdout and b"CHILD_PROCESS_FORBIDDEN" not in stdout
        assert (home / "write.txt").exists(), (stdout, stderr)
        assert (home / "write.txt").read_text().strip() == "PRIVATE_WRITE"
        assert not (work / "write.txt").exists() and not (tmp_path / "bad.txt").exists()
        assert outside.read_text() == "OUTSIDE_MARKER"
        assert stderr and process.token_verified
    finally:
        process.close()
    assert process.poll() is not None
    # Reuse the login home but retain a previous request, including an abandoned grant.
    from oms_hub.llm import windows_lpac as lpac

    next_work = tmp_path / "next"
    next_work.mkdir()
    lpac._grant(work, process._sid, "(OI)(CI)(RX)")
    second = lpac.LpacProcess(
        [cmd, "/d", "/v:off", "/s", "/c", r"type ..\work\read.txt & type ..\home\write.txt"],
        cwd=next_work,
        env=env,
        session_home=home,
        shutdown_timeout=2,
    )
    try:
        second.stdin.close()
        second.wait(timeout=15)
        output = second.stdout.read()
        assert b"INSIDE_MARKER" not in output and b"PRIVATE_WRITE" in output
        assert second._sid != process._sid
    finally:
        second.close()
    runtime_in_home = home / "cmd.exe"
    shutil.copy2(cmd, runtime_in_home)
    with pytest.raises(OSError, match="runtime outside writable home"):
        lpac.LpacProcess(
            [str(runtime_in_home)], cwd=work, env=env, session_home=home, shutdown_timeout=2
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows job ownership")
def test_parent_death_reaps_suspended_child(tmp_path):
    import json
    import subprocess
    import time

    from oms_hub.llm import windows_lpac as lpac

    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    runtime = tmp_path / "cmd.exe"
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", runtime)
    script = tmp_path / "parent.py"
    script.write_text("""
import os,json,time,ctypes as c
from pathlib import Path
from oms_hub.llm import windows_lpac as w
root=Path(__file__).parent
original=w.CreateProcess
def created(*args):
 result=original(*args)
 if result:
  pi=c.cast(args[-1],c.POINTER(w.ProcessInfo)).contents
  (root/'child.json').write_text(json.dumps({'pid':pi.pid}))
  while not (root/'release').exists():time.sleep(.01)
  os._exit(0)  # Simulate Hub death before the constructor can resume or clean up.
 return result
w.CreateProcess=created
w.LpacProcess([str(root/'cmd.exe'),'/d','/c','echo MUST_NOT_RUN'],cwd=root/'work',
 env={'SystemRoot':os.environ['SystemRoot'],'LOCALAPPDATA':os.environ['LOCALAPPDATA']},
 session_home=root/'home',shutdown_timeout=2)
""")
    parent = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    handle = None
    try:
        deadline = time.monotonic() + 20
        marker = tmp_path / "child.json"
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), parent.communicate(timeout=2)
        pid = json.loads(marker.read_text())["pid"]
        open_process = lpac._bind(lpac.k, "OpenProcess", lpac.P, lpac.D, lpac.w.BOOL, lpac.D)
        handle = open_process(0x100000, False, pid)
        assert handle and lpac.Wait(handle, 0) == 258
        (tmp_path / "release").touch()
        assert parent.wait(timeout=5) == 0
        assert lpac.Wait(handle, 5000) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if handle:
            lpac.CloseHandle(handle)
        for stream in (parent.stdout, parent.stderr):
            stream.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows error semantics")
@pytest.mark.parametrize("exits", [True, False])
def test_termination_access_denied_requires_confirmed_exit(monkeypatch, exits):
    import ctypes
    import subprocess

    from oms_hub.llm import windows_lpac as lpac

    process = object.__new__(lpac.LpacProcess)
    process._process = 1
    process.shutdown_timeout = 0.25
    monkeypatch.setattr(process, "poll", lambda: None)
    waits = []

    def terminate(handle, code):
        ctypes.set_last_error(5)
        return False

    def wait(timeout):
        waits.append(timeout)
        if not exits:
            raise subprocess.TimeoutExpired("native-test", timeout)
        return 1

    monkeypatch.setattr(lpac, "Terminate", terminate)
    monkeypatch.setattr(process, "wait", wait)
    if exits:
        process.terminate()
    else:
        with pytest.raises(PermissionError):
            process.terminate()
    assert waits == [0.25]


@pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("OMS_WINDOWS_CODEX_TEST_EXECUTABLE"),
    reason="Requires the pinned native Windows runtime",
)
def test_native_managed_home_inherits_private_acl_and_persists(tmp_path, monkeypatch):
    import sqlite3
    import subprocess

    from oms_hub.llm import codex_session as session
    from oms_hub.llm.windows_lpac import LpacProcess

    home, work = tmp_path / "home", tmp_path / "work"
    wires = []
    original = session._Stdio

    class Capture(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            wires.append(self)

    monkeypatch.setattr(session, "_Stdio", Capture)
    executable = Path(os.environ["OMS_WINDOWS_CODEX_TEST_EXECUTABLE"])
    for _ in range(2):
        client = session.CodexSessionClient(executable, home, work)
        try:
            status = client.status()
            assert status.state == "disconnected" and status.error_code == "auth_required", status
        finally:
            client.close()
        with sqlite3.connect(home / "state_5.sqlite") as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert wires[0].process._sid != wires[1].process._sid
    cmd = tmp_path / "cmd.exe"
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", cmd)
    process = LpacProcess(
        [str(cmd), "/d", "/v:off", "/s", "/c",
         r"echo PRIVATE>..\home\host-home\tmp\probe.txt & echo BAD>bad.txt & echo BAD>..\bad.txt"],
        cwd=work,
        env={key: os.environ[key] for key in ("SystemRoot", "WINDIR", "LOCALAPPDATA")},
        session_home=home, shutdown_timeout=2,
    )
    try:
        process.stdin.close()
        process.wait(timeout=15)
        assert (home / "host-home/tmp/probe.txt").read_text().strip() == "PRIVATE"
        assert not (work / "bad.txt").exists() and not (tmp_path / "bad.txt").exists()
    finally:
        process.close()
    paths = [home, home / "host-home", home / "host-home/tmp", home / "state_5.sqlite"]
    icacls = str(Path(os.environ["SystemRoot"]) / "System32/icacls.exe")
    for path in paths:
        acl = subprocess.run([icacls, str(path)], capture_output=True, check=True).stdout
        for child in [w.process for w in wires] + [process]:
            assert child._profile is None and child._process is None
            assert child._sid.encode() not in acl
